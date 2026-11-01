"""The message bus: routing, threads, claims, guards and context packs.

Transport-agnostic: the HTTP server, the MCP adapter and tests all call this.
"""

from __future__ import annotations

from typing import Any

from .guards import GuardConfig, GuardError, Guards, UNTRUSTED_NOTICE, injection_flags
from .models import (
    AgentCard, AgentRecord, ContextPack, Delivery, Envelope, EnvelopeIn, Intent,
    Thread, ThreadState, TRANSITIONS, TERMINAL,
)
from .store import Store

HUMAN = "human"
SYSTEM = "system"


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Bus:
    def __init__(self, store: Store | None = None, guard_config: GuardConfig | None = None) -> None:
        self.store = store or Store()
        self.guards = Guards(guard_config)

    # ------------------------------------------------------------ directory
    def register(self, card: AgentCard) -> tuple[AgentRecord, str]:
        if self.store.get_agent(card.name):
            raise GuardError("name_taken", f"an agent named {card.name!r} already exists")
        return self.store.register_agent(card)

    def authenticate(self, token: str) -> AgentRecord:
        a = self.store.agent_by_token(token)
        if not a:
            raise Forbidden("invalid agent token")
        self.store.touch(a.id)
        return a

    def find_agents(self, capability: str | None = None) -> list[AgentRecord]:
        return self.store.list_agents(capability)

    def resolve_agent(self, ref: str) -> AgentRecord:
        a = self.store.get_agent(ref)
        if not a:
            raise NotFound(f"no agent {ref!r}")
        return a

    # ------------------------------------------------------------- sending
    def send(self, sender: AgentRecord | None, env: EnvelopeIn) -> Envelope:
        """Send an envelope. sender=None means the human observer."""
        sender_id = sender.id if sender else HUMAN
        kind = "agent" if sender else "human"

        if env.idempotency_key:
            prior = self.store.find_idempotent(sender_id, env.idempotency_key)
            if prior:
                return prior

        to_agent: AgentRecord | None = None
        if env.to.agent:
            to_agent = self.resolve_agent(env.to.agent)
            env = env.model_copy(update={"to": env.to.model_copy(update={"agent": to_agent.id})})
        if env.to.capability and not self.store.list_agents(env.to.capability):
            raise NotFound(f"no agent offers capability {env.to.capability!r}")

        thread: Thread | None = None
        if env.thread_id:
            thread = self._thread(env.thread_id)
            if sender and sender.id not in thread.participants and not self._can_join(sender, thread):
                raise Forbidden("not a participant of this thread")

        if sender is not None:
            self.guards.check_send(sender_id, sender.paused, env, thread)
        elif thread is not None and thread.state in TERMINAL:
            raise GuardError("thread_closed", f"thread is {thread.state.value}")

        if thread is None:
            subject = env.subject or (env.text or env.intent.value)[:120]
            budget = env.budget_usd if env.budget_usd is not None else self.guards.config.default_thread_budget_usd
            thread = self.store.create_thread(
                subject=subject, creator=sender_id, capability=env.to.capability,
                budget_usd=budget, deadline=env.deadline,
            )

        stored = self.store.append(env, thread_id=thread.id, sender=sender_id, sender_kind=kind)

        thread = self._thread(thread.id)
        thread.message_count += 1
        thread.spent_usd = round(thread.spent_usd + env.cost_usd, 6)
        self.store.save_thread(thread)
        if sender:
            self.store.add_participant(thread, sender.id)
        if to_agent:
            self.store.add_participant(thread, to_agent.id)
        thread = self._thread(thread.id)

        if env.intent == Intent.ask_human:
            thread.state = ThreadState.waiting_human
        elif sender is None and thread.state == ThreadState.waiting_human:
            thread.state = ThreadState.working if thread.owner else ThreadState.open
        self.store.save_thread(thread)

        reason = self.guards.detect_loop(self.store.last_messages(thread.id, self.guards.config.loop_window))
        if reason and thread.state not in TERMINAL:
            self._pause_thread(thread, reason)
        return stored

    def _can_join(self, agent: AgentRecord, t: Thread) -> bool:
        # Agents offering the routed capability may join an unclaimed thread.
        return bool(t.capability and t.capability in agent.capabilities and t.owner is None)

    # -------------------------------------------------------------- threads
    def _thread(self, thread_id: str) -> Thread:
        t = self.store.get_thread(thread_id)
        if not t:
            raise NotFound(f"no thread {thread_id!r}")
        return t

    def get_thread(self, thread_id: str) -> Thread:
        return self._thread(thread_id)

    def claim(self, agent: AgentRecord, thread_id: str) -> Thread:
        t = self._thread(thread_id)
        if agent.paused:
            raise GuardError("agent_paused", "this agent has been paused", 423)
        if t.owner == agent.id:
            return t
        if t.state != ThreadState.open or t.owner:
            raise GuardError("already_claimed", f"thread is {t.state.value}" + (f", owned by {t.owner}" if t.owner else ""))
        if t.capability and t.capability not in agent.capabilities and agent.id not in t.participants:
            raise Forbidden(f"agent does not offer {t.capability!r}")
        t.owner = agent.id
        t.state = ThreadState.claimed
        self.store.save_thread(t)
        self.store.add_participant(t, agent.id)
        self._system(t.id, {"event": "claimed", "owner": agent.id}, f"{agent.name} claimed this thread")
        return self._thread(t.id)

    def set_state(self, agent: AgentRecord | None, thread_id: str, state: ThreadState, reason: str | None = None) -> Thread:
        t = self._thread(thread_id)
        if agent is not None and agent.id not in (t.owner, t.creator):
            raise Forbidden("only the thread's owner or creator can change its state")
        if agent is not None and state == ThreadState.paused:
            raise Forbidden("only the human observer can pause a thread")
        if state not in TRANSITIONS[t.state] and not (agent is None and t.state not in TERMINAL):
            raise GuardError("bad_transition", f"cannot go from {t.state.value} to {state.value}")
        t.state = state
        t.pause_reason = reason if state == ThreadState.paused else None
        if state == ThreadState.open:
            t.owner = None
        self.store.save_thread(t)
        who = agent.name if agent else HUMAN
        self._system(t.id, {"event": "state", "state": state.value, "by": who, "reason": reason},
                     f"{who} set state to {state.value}" + (f": {reason}" if reason else ""))
        return self._thread(t.id)

    def update_blackboard(self, agent: AgentRecord, thread_id: str, patch: dict[str, Any]) -> Thread:
        t = self._thread(thread_id)
        if agent.id not in t.participants:
            raise Forbidden("not a participant of this thread")
        for k, v in patch.items():
            if v is None:
                t.blackboard.pop(k, None)
            else:
                t.blackboard[k] = v
        return self.store.save_thread(t)

    def _pause_thread(self, t: Thread, reason: str) -> None:
        t.state = ThreadState.paused
        t.pause_reason = reason
        self.store.save_thread(t)
        self._system(t.id, {"event": "paused", "reason": reason}, f"Thread paused: {reason}")

    def _system(self, thread_id: str, payload: dict[str, Any], text: str) -> Envelope:
        env = EnvelopeIn(thread_id=thread_id, intent=Intent.inform, payload=payload, text=text)
        return self.store.append(env, thread_id=thread_id, sender=SYSTEM, sender_kind="system")

    # ------------------------------------------------------------- reading
    def deliver(self, reader_id: str, env: Envelope) -> Delivery:
        untrusted = env.sender_kind == "agent" and env.sender != reader_id
        notice = None
        if untrusted:
            flags = injection_flags(env)
            notice = UNTRUSTED_NOTICE + (f" Flagged phrases: {', '.join(flags)}." if flags else "")
        return Delivery(envelope=env, untrusted=untrusted, notice=notice)

    def inbox(self, agent: AgentRecord, after: int = 0, limit: int = 100) -> list[Delivery]:
        return [self.deliver(agent.id, e) for e in self.store.inbox(agent.id, after, limit)]

    def read_thread(self, reader: AgentRecord | None, thread_id: str, after: int = 0) -> list[Delivery]:
        t = self._thread(thread_id)
        if reader is not None and reader.id not in t.participants and not self._can_join(reader, t):
            raise Forbidden("not a participant of this thread")
        rid = reader.id if reader else HUMAN
        return [self.deliver(rid, e) for e in self.store.thread_messages(thread_id, after)]

    def context_pack(self, reader: AgentRecord | None, thread_id: str, recent: int = 10) -> ContextPack:
        t = self._thread(thread_id)
        if reader is not None and reader.id not in t.participants and not self._can_join(reader, t):
            raise Forbidden("not a participant of this thread")
        msgs = self.store.thread_messages(thread_id, 0, 10_000)
        answered = {m.in_reply_to for m in msgs if m.intent in (Intent.result, Intent.reject, Intent.error) and m.in_reply_to}
        open_requests = [m for m in msgs if m.intent == Intent.request and m.seq not in answered]
        results = [m for m in msgs if m.intent == Intent.result][-3:]
        names = {a.id: a.name for a in self.store.list_agents()}
        names.update({HUMAN: HUMAN, SYSTEM: SYSTEM})
        budget = f"${t.spent_usd:.3f} of ${t.budget_usd:.2f}" if t.budget_usd is not None else f"${t.spent_usd:.2f} (no budget)"
        lines = [
            f"Thread '{t.subject}' is {t.state.value}"
            + (f", owned by {names.get(t.owner, t.owner)}" if t.owner else ", unclaimed")
            + f". {t.message_count} messages; spent {budget}.",
            "Participants: " + ", ".join(names.get(p, p) for p in t.participants) + ".",
            f"Open requests: {len(open_requests)}. Results so far: {len([m for m in msgs if m.intent == Intent.result])}.",
        ]
        if t.pause_reason:
            lines.append(f"Paused because: {t.pause_reason}.")
        if t.blackboard:
            lines.append("Blackboard keys: " + ", ".join(sorted(t.blackboard)) + ".")
        rid = reader.id if reader else HUMAN
        return ContextPack(
            thread=t, summary=" ".join(lines), open_requests=open_requests, latest_results=results,
            recent=[self.deliver(rid, m) for m in msgs[-recent:]], notice=UNTRUSTED_NOTICE,
        )

    # --------------------------------------------------------------- human
    def human_pause_agent(self, agent_ref: str, paused: bool) -> AgentRecord:
        a = self.resolve_agent(agent_ref)
        self.store.set_agent_paused(a.id, paused)
        return self.resolve_agent(a.id)

    def human_pause_thread(self, thread_id: str, paused: bool, reason: str | None = None) -> Thread:
        t = self._thread(thread_id)
        if paused:
            return self.set_state(None, thread_id, ThreadState.paused, reason or "paused by observer")
        target = ThreadState.working if t.owner else ThreadState.open
        return self.set_state(None, thread_id, target)
