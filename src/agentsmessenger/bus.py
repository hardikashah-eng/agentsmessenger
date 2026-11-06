"""The message bus: routing, threads, claims, guards and context packs.

Transport-agnostic: the HTTP server, the MCP adapter and tests all call this.
"""

from __future__ import annotations

import functools
import random
from typing import Any, Callable, TypeVar

from .guards import GuardConfig, GuardError, Guards, Forbidden, NotFound, UNTRUSTED_NOTICE, injection_flags
from .market import MarketConfig, MarketMixin, label
from .memory import MemoryMixin
from .models import (
    Address, AgentCard, AgentRecord, ContextPack, Delivery, Envelope, EnvelopeIn, Intent,
    Thread, ThreadState, TRANSITIONS, TERMINAL, estimate_tokens,
)
from .router import HUMAN, SYSTEM, RouterConfig, RouterMixin
from .signing import ServerSigner, valid_public_key, verify
from .store import Store

__all__ = ["Bus", "Forbidden", "NotFound", "HUMAN", "SYSTEM", "RouterConfig", "MarketConfig"]


F = TypeVar("F", bound=Callable[..., Any])


def _serialized(fn: F) -> F:
    """State changes read a thread, modify it and save it, often over several writes. Run them
    one at a time, holding the store's lock throughout, so concurrent requests (two bids, two
    claims) can't overwrite each other and readers never see a change half-applied."""
    @functools.wraps(fn)
    def wrapper(self: "Bus", *a: Any, **kw: Any) -> Any:
        with self.state_lock:
            return fn(self, *a, **kw)
    return wrapper  # type: ignore[return-value]


class Bus(MemoryMixin, RouterMixin, MarketMixin):
    def __init__(self, store: Store | None = None, guard_config: GuardConfig | None = None,
                 signer: ServerSigner | None = None, require_signatures: bool = False,
                 router: RouterConfig | None = None, market: MarketConfig | None = None,
                 seed: int | None = None) -> None:
        self.store = store or Store()
        self.router = router or RouterConfig()
        self.market = market or MarketConfig()
        self._rng = random.Random(seed)
        self.state_lock = self.store.lock
        self.guards = Guards(guard_config)
        self.signer = signer or ServerSigner()
        self.require_signatures = require_signatures

    # ------------------------------------------------------------ directory
    def register(self, card: AgentCard) -> tuple[AgentRecord, str]:
        if self.store.get_agent(card.name):
            raise GuardError("name_taken", f"an agent named {card.name!r} already exists")
        self._check_card(card)
        return self.store.register_agent(card)

    def update_card(self, agent: AgentRecord, card: AgentCard) -> AgentRecord:
        if card.name != agent.name and self.store.get_agent(card.name):
            raise GuardError("name_taken", f"an agent named {card.name!r} already exists")
        if agent.public_key and card.public_key != agent.public_key:
            # Rotating or removing a key must be done by the observer (re-register), not by a token holder.
            raise Forbidden("public_key cannot be changed with an agent token")
        self._check_card(card)
        return self.store.update_card(agent.id, card)

    def _check_card(self, card: AgentCard) -> None:
        if card.public_key and not valid_public_key(card.public_key):
            raise GuardError("bad_key", "public_key must be a base64url raw Ed25519 key", 422)
        if self.require_signatures and not card.public_key:
            raise GuardError("key_required", "this messenger requires every agent to register a public_key", 422)

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
    @_serialized
    def send(self, sender: AgentRecord | None, env: EnvelopeIn, transport: str = "native") -> Envelope:
        """Send an envelope. sender=None means the human observer."""
        sender_id = sender.id if sender else HUMAN
        kind = "agent" if sender else "human"

        verified = False
        if sender is not None:
            if sender.public_key:
                if transport != "native":
                    raise Forbidden("agents with a signing key must send through the native API")
                if not env.signature:
                    raise GuardError("signature_required", "this agent registered a public key; sign every envelope", 401)
                if not verify(env, env.signature, sender.public_key):
                    raise GuardError("bad_signature", "envelope signature does not verify", 401)
                verified = True
            elif self.require_signatures:
                raise GuardError("signature_required", "this messenger requires signed envelopes", 401)

        if env.idempotency_key:
            prior = self.store.find_idempotent(sender_id, env.idempotency_key)
            if prior:
                return prior

        to_agent: AgentRecord | None = None
        if env.to.agent:
            # Keep the envelope exactly as signed; the resolved id is stored alongside for routing.
            to_agent = self.resolve_agent(env.to.agent)
        if env.to.capability and not self.store.list_agents(env.to.capability):
            raise NotFound(f"no agent offers capability {env.to.capability!r}")

        thread: Thread | None = None
        if env.thread_id:
            thread = self._thread(env.thread_id)
            if sender and sender.id not in thread.participants and not self._can_join(sender, thread):
                raise Forbidden("not a participant of this thread")

        if env.memory_refs:
            self.check_memory_refs(sender.id if sender else None, env.memory_refs)

        if sender is not None:
            self.guards.check_send(sender_id, sender.paused, env, thread)
        elif thread is not None and thread.state in TERMINAL:
            raise GuardError("thread_closed", f"thread is {thread.state.value}")

        plan = None
        if thread is None and env.intent == Intent.request and not env.to.agent:
            plan = self.plan_route(sender_id, env)

        if thread is None:
            subject = env.subject or (env.text or env.intent.value)[:120]
            budget = env.budget_usd if env.budget_usd is not None else self.guards.config.default_thread_budget_usd
            thread = self.store.create_thread(
                subject=subject, creator=sender_id, capability=env.to.capability,
                budget_usd=budget, deadline=env.deadline,
            )

        stored = self.store.append(env, thread_id=thread.id, sender=sender_id, sender_kind=kind,
                                   verified=verified, signer=self.signer,
                                   to_agent_id=to_agent.id if to_agent else
                                   plan["candidates"][0]["agent"] if plan and plan["mode"] == "instant" else None)

        thread = self._thread(thread.id)
        thread.message_count += 1
        thread.spent_usd = round(thread.spent_usd + env.cost_usd, 6)
        self.store.save_thread(thread)
        if sender:
            self.store.add_participant(thread, sender.id)
        if to_agent:
            self.store.add_participant(thread, to_agent.id)
        if stored.memory_refs:
            self.grant_refs_to_thread(sender.id if sender else None, stored)
        if plan and plan["mode"] == "auction":
            self.open_auction(self._thread(thread.id), stored, plan)
        elif plan:
            self.apply_route(self._thread(thread.id), stored, plan)
        if sender and env.cost_usd:
            self.store.bump_stats(sender.id, cost_usd=env.cost_usd)
        thread = self._thread(thread.id)

        if env.intent == Intent.ask_human:
            thread.state = ThreadState.waiting_human
        elif sender is None and thread.state == ThreadState.waiting_human:
            thread.state = ThreadState.working if thread.owner else ThreadState.open
        self.store.save_thread(thread)
        self._after_send(sender, self._thread(thread.id), stored)

        thread = self._thread(thread.id)
        reason = self.guards.detect_loop(self.store.last_messages(thread.id, self.guards.config.loop_window))
        if reason and thread.state not in TERMINAL:
            self._pause_thread(thread, reason)
        return stored

    def _after_send(self, sender: AgentRecord | None, t: Thread, stored: Envelope) -> None:
        """Verification hooks: verdicts from the reviewer, new results from the worker, human answers."""
        v = t.verification
        if sender is not None:
            if v.get("status") == "pending" and v.get("verifier") == sender.id:
                self.on_verifier_message(t, sender, stored)
            elif stored.intent == Intent.result:
                self.maybe_verify(sender, t, stored)
        elif t.state == ThreadState.verifying and v.get("verifier") == HUMAN and v.get("status") == "pending":
            verdict = self.parse_verdict(stored)
            if verdict:
                notes = (stored.text or "").split(":", 1)[1].strip() if ":" in (stored.text or "") else ""
                self.apply_verdict(t, HUMAN, verdict, notes)

    def _can_join(self, agent: AgentRecord, t: Thread) -> bool:
        # Agents offering the routed capability may join an unclaimed thread, unless the router assigned it.
        if t.assigned_to and t.assigned_to != agent.id:
            return False
        return bool(t.capability and t.capability in agent.capabilities and t.owner is None)

    # -------------------------------------------------------------- threads
    def _thread(self, thread_id: str) -> Thread:
        t = self.store.get_thread(thread_id)
        if not t:
            raise NotFound(f"no thread {thread_id!r}")
        return t

    def get_thread(self, thread_id: str) -> Thread:
        return self._thread(thread_id)

    @_serialized
    def claim(self, agent: AgentRecord, thread_id: str) -> Thread:
        t = self._thread(thread_id)
        if agent.paused:
            raise GuardError("agent_paused", "this agent has been paused", 423)
        if t.owner == agent.id:
            return t
        if t.state != ThreadState.open or t.owner:
            raise GuardError("already_claimed", f"thread is {t.state.value}" + (f", owned by {t.owner}" if t.owner else ""))
        if t.market.get("status") == "open":
            raise GuardError("bidding_open", "this request is open for bids; bid instead of claiming", 409)
        if t.assigned_to and t.assigned_to != agent.id:
            owner = self.store.get_agent(t.assigned_to)
            raise Forbidden(f"the router assigned this thread to {owner.name if owner else t.assigned_to}")
        if t.capability and t.capability not in agent.capabilities and agent.id not in t.participants:
            raise Forbidden(f"agent does not offer {t.capability!r}")
        t.owner = agent.id
        t.state = ThreadState.claimed
        self.store.save_thread(t)
        self.store.add_participant(t, agent.id)
        self._system(t.id, {"event": "claimed", "owner": agent.id}, f"{agent.name} claimed this thread")
        return self._thread(t.id)

    @_serialized
    def set_state(self, agent: AgentRecord | None, thread_id: str, state: ThreadState, reason: str | None = None) -> Thread:
        t = self._thread(thread_id)
        if agent is not None and agent.id not in (t.owner, t.creator):
            raise Forbidden("only the thread's owner or creator can change its state")
        if agent is not None and state == ThreadState.paused:
            raise Forbidden("only the human observer can pause a thread")
        if state == t.state:
            return t
        if state == ThreadState.verifying and agent is not None:
            raise Forbidden("verification starts when you post a result")
        if state == ThreadState.done:
            if agent is not None and self.router.verify and \
                    t.verification.get("status") not in ("approved", "overridden", "skipped"):
                return self._done_needs_review(agent, t)
            if agent is None and t.verification.get("status") == "pending":
                t.verification["status"] = "overridden"
            if agent is not None and agent.id == t.owner and not self.router.verify:
                self.store.bump_stats(agent.id, approved=1)
                self.store.update_reputation(agent.id, t.kind or "general", "worker", tasks=1)
                if t.routing.get("assigned_at"):
                    self.check_delivery(t, agent.id, t.kind or "general")
                    t = self._thread(thread_id)
                self.settle(t, agent.id)
                t = self._thread(thread_id)
        if state == ThreadState.failed and agent is not None and agent.id == t.owner and t.routing.get("request_seq"):
            self.store.bump_stats(agent.id, failed=1)
            self.record_failure(t, agent.id)
            if self.router.enabled and t.state not in TERMINAL:
                # Next capable agent; with none left the thread fails as usual.
                moved = self.reroute(t, f"{agent.name} failed" + (f": {reason}" if reason else ""), on_failure=True)
                if moved is not None:
                    return moved
                t = self._thread(thread_id)
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

    def _done_needs_review(self, agent: AgentRecord, t: Thread) -> Thread:
        """An agent can't close its own work: 'done' means 'verified'."""
        if t.state == ThreadState.verifying:
            return t
        mine = [m for m in self.store.thread_messages(t.id, 0, 10_000) if m.intent == Intent.result and m.sender == agent.id]
        if not mine:
            raise GuardError("result_required", "post a result first; it is verified before the thread is done", 409)
        self.maybe_verify(agent, t, mine[-1])
        return self._thread(t.id)

    @_serialized
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

    def _system(self, thread_id: str, payload: dict[str, Any], text: str, *, intent: Intent = Intent.inform,
                to: str | None = None, artifacts: list[str] | None = None, memory_refs: list[str] | None = None) -> Envelope:
        env = EnvelopeIn(thread_id=thread_id, intent=intent, payload=payload, text=text, to=Address(agent=to),
                         artifacts=artifacts or [], memory_refs=memory_refs or [])
        stored = self.store.append(env, thread_id=thread_id, sender=SYSTEM, sender_kind="system", signer=self.signer,
                                   to_agent_id=to)
        t = self._thread(thread_id)
        t.message_count += 1
        self.store.save_thread(t)
        return stored

    # ------------------------------------------------------------- reading
    def deliver(self, reader_id: str, env: Envelope) -> Delivery:
        untrusted = env.sender_kind == "agent" and env.sender != reader_id
        notice = None
        if untrusted:
            flags = injection_flags(env)
            notice = UNTRUSTED_NOTICE + (f" Flagged phrases: {', '.join(flags)}." if flags else "")
        rid = None if reader_id == HUMAN else reader_id
        memory = self.memory_stubs_for(rid, env.memory_refs) if env.memory_refs else []
        route = self.route_for(reader_id, env) if env.intent == Intent.request else None
        return Delivery(envelope=env, untrusted=untrusted, notice=notice, memory=memory, route=route)

    def inbox(self, agent: AgentRecord, after: int = 0, limit: int = 100) -> list[Delivery]:
        return [self.deliver(agent.id, e) for e in self.store.inbox(agent.id, after, limit)]

    def read_thread(self, reader: AgentRecord | None, thread_id: str, after: int = 0) -> list[Delivery]:
        t = self._thread(thread_id)
        if reader is not None and reader.id not in t.participants and not self._can_join(reader, t):
            raise Forbidden("not a participant of this thread")
        rid = reader.id if reader else HUMAN
        return [self.deliver(rid, e) for e in self.store.thread_messages(thread_id, after)]

    def context_pack(self, reader: AgentRecord | None, thread_id: str, recent: int = 10,
                     max_tokens: int | None = None) -> ContextPack:
        t = self._thread(thread_id)
        if reader is not None and reader.id not in t.participants and not self._can_join(reader, t):
            raise Forbidden("not a participant of this thread")
        msgs = self.store.thread_messages(thread_id, 0, 10_000)
        answered = {m.in_reply_to for m in msgs if m.intent in (Intent.result, Intent.reject, Intent.error) and m.in_reply_to}
        # A reply to the router's copy of a request answers the original too.
        copies = {int(k): v.get("original") for k, v in (t.routing.get("messages") or {}).items() if v.get("original")}
        answered |= {copies[s] for s in list(answered) if s in copies}
        # The router's own requests (calls for bids, work copies, review requests) aren't open work.
        open_requests = [m for m in msgs if m.intent == Intent.request and m.sender != SYSTEM and m.seq not in answered]
        # Reviewers' verdicts live in t.verification; results here are the work itself.
        review_reqs = {int(k) for k, m in (t.routing.get("messages") or {}).items() if m.get("kind") == "verification"}
        work = [m for m in msgs if m.intent == Intent.result and m.in_reply_to not in review_reqs]
        results = work[-3:]
        names = {a.id: a.name for a in self.store.list_agents()}
        names.update({HUMAN: HUMAN, SYSTEM: SYSTEM})
        budget = f"${t.spent_usd:.3f} of ${t.budget_usd:.2f}" if t.budget_usd is not None else f"${t.spent_usd:.2f} (no budget)"
        lines = [
            f"Thread '{t.subject}' is {t.state.value}"
            + (f", owned by {names.get(t.owner, t.owner)}" if t.owner else ", unclaimed")
            + f". {t.message_count} messages; spent {budget}.",
            "Participants: " + ", ".join(names.get(p, p) for p in t.participants) + ".",
            f"Open requests: {len(open_requests)}. Results so far: {len(work)}.",
        ]
        if t.pause_reason:
            lines.append(f"Paused because: {t.pause_reason}.")
        m = t.market
        if m.get("status") == "open":
            lines.append(f"Bidding open until {m['closes_at']} ({len(m.get('bids', {}))} of {len(m['invited'])} bids in).")
        elif m.get("award"):
            price = m["award"].get("price_usd")
            lines.append(f"Awarded to {names.get(m['award']['agent'], m['award']['agent'])}"
                         + (f" at ${price:.3f}" if price is not None else "") + ".")
        if t.assigned_to and not t.owner:
            lines.append(f"Router assigned it to {names.get(t.assigned_to, t.assigned_to)} (#{label(t.kind)}).")
        if t.routing.get("paid"):
            lines.append(f"Paid ${t.routing['paid']:.3f} on approval.")
        v = t.verification
        if v.get("status") == "pending":
            lines.append(f"Waiting for verification by {names.get(v.get('verifier'), v.get('verifier'))}.")
        elif v.get("status") in ("approved", "rejected", "overridden"):
            lines.append(f"Verification: {v['status']} after {len(v.get('history', []))} review(s).")
        if t.blackboard:
            lines.append("Blackboard keys: " + ", ".join(sorted(t.blackboard)) + ".")
        rid = reader.id if reader else HUMAN
        # Memory shared into the thread, plus anything referenced in it that this reader may open.
        mid = reader.id if reader else None
        memory = [self._view(mid, m, full=False).stub()
                  for m in self.store.visible_memory(mid, thread_id=thread_id, limit=50)]
        seen = {m.id for m in memory}
        refs = [r for e in reversed(msgs) for r in e.memory_refs if r not in seen]
        memory += [s for s in self.memory_stubs_for(mid, list(dict.fromkeys(refs))[:50]) if s.accessible]
        if memory:
            lines.append(f"Shared memory: {len(memory)} items (stubs below; open with GET /v1/memory/{{id}}).")
        pack = ContextPack(
            thread=t, summary=" ".join(lines), open_requests=open_requests, latest_results=results,
            recent=[self.deliver(rid, m) for m in msgs[-recent:]] if recent > 0 else [],
            memory=memory, notice=UNTRUSTED_NOTICE, max_tokens=max_tokens,
        )
        pack.thread = self.redact(t, reader.id if reader else None)
        return fit_to_budget(pack, max_tokens) if max_tokens else _measured(pack)

    # Mixin operations that change state, serialized the same way.
    submit_bid = _serialized(MarketMixin.submit_bid)
    tick = _serialized(RouterMixin.tick)
    assign = _serialized(RouterMixin.assign)
    human_verdict = _serialized(RouterMixin.human_verdict)
    remember = _serialized(MemoryMixin.remember)
    share_memory = _serialized(MemoryMixin.share_memory)
    forget = _serialized(MemoryMixin.forget)

    # --------------------------------------------------------------- human
    @_serialized
    def human_pause_agent(self, agent_ref: str, paused: bool) -> AgentRecord:
        a = self.resolve_agent(agent_ref)
        self.store.set_agent_paused(a.id, paused)
        return self.resolve_agent(a.id)

    @_serialized
    def human_pause_thread(self, thread_id: str, paused: bool, reason: str | None = None) -> Thread:
        t = self._thread(thread_id)
        if paused:
            return self.set_state(None, thread_id, ThreadState.paused, reason or "paused by observer")
        target = ThreadState.working if t.owner else ThreadState.open
        return self.set_state(None, thread_id, target)


# ---------------------------------------------------------- token budgets

CLIP_MIN_TOKENS = 50  # payloads at least this large may be replaced by stubs


def _measured(pack: ContextPack) -> ContextPack:
    pack.tokens_estimate = estimate_tokens(pack.model_dump_json())
    return pack


def _clip(env: Envelope) -> Envelope:
    keys = list(env.payload)[:10]
    return env.model_copy(update={
        "payload": {"_clipped": True, "keys": keys, "tokens": estimate_tokens(env.payload)},
        "text": (env.text[:200] + "...") if env.text and len(env.text) > 200 else env.text,
    })


def fit_to_budget(pack: ContextPack, max_tokens: int) -> ContextPack:
    """Shrink a context pack until it fits, cheapest losses first:
    1. replace large payloads in recent messages with stubs (keys + size),
    2. drop the oldest recent messages,
    3. clip large payloads in open requests and latest results,
    4. drop memory stubs beyond the newest five.
    The thread summary and the open requests themselves are always kept."""
    omitted: dict[str, int] = {}

    def size() -> int:
        return _measured(pack).tokens_estimate

    if size() <= max_tokens:
        return pack
    for d in sorted(pack.recent, key=lambda d: -estimate_tokens(d.envelope.payload)):
        if size() <= max_tokens:
            break
        if not d.clipped and estimate_tokens(d.envelope.payload) > CLIP_MIN_TOKENS:
            d.envelope, d.clipped = _clip(d.envelope), True
            omitted["clipped_payloads"] = omitted.get("clipped_payloads", 0) + 1
    while pack.recent and size() > max_tokens:
        pack.recent.pop(0)
        omitted["recent_messages"] = omitted.get("recent_messages", 0) + 1
    for group in (pack.latest_results, pack.open_requests):
        for i, e in enumerate(group):
            if size() <= max_tokens:
                break
            if estimate_tokens(e.payload) > CLIP_MIN_TOKENS and not e.payload.get("_clipped"):
                group[i] = _clip(e)
                omitted["clipped_payloads"] = omitted.get("clipped_payloads", 0) + 1
    while len(pack.memory) > 5 and size() > max_tokens:
        pack.memory.pop()
        omitted["memory_stubs"] = omitted.get("memory_stubs", 0) + 1
    pack.truncated = bool(omitted)
    pack.omitted = omitted
    return _measured(pack)
