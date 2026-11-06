"""Routing, coordination and verification. On by default.

Routing
    A request sent to a capability, or to nobody, is assigned to one agent.
    The router classifies the request (code, research, writing, ...), scores
    every agent that can do it on reliability (share of its past results that
    passed review), cost, current load and whether it is online, assigns the
    best, and posts its reasoning in the thread. If the agent doesn't claim
    the work in time, or fails, the next candidate gets it. When nobody is
    left, the human is asked.

Verification
    Every result is checked by a different agent before the thread is done:
    a `code.review` agent for coding work, a `verify` agent for anything
    else, preferring a different vendor than the worker. Approval closes the
    thread; a rejection sends it back to the worker with the feedback (up to
    `max_review_rounds`), then to the human. If no reviewer agent exists, the
    human reviews. Agents cannot skip review; only the human observer can
    override it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .guards import Forbidden, GuardError, NotFound
from .models import AgentRecord, Envelope, EnvelopeIn, Intent, Thread, ThreadState, TERMINAL, estimate_tokens, utcnow

if TYPE_CHECKING:
    from .market import MarketConfig
    from .store import Store

HUMAN = "human"
SYSTEM = "system"


@dataclass
class RouterConfig:
    enabled: bool = True                # assign capability/unaddressed requests to one agent
    verify: bool = True                 # every result is checked by another agent before `done`
    strategy: str = "balanced"          # balanced | cheapest | quality
    claim_timeout_s: float = 90.0       # reassign if the agent hasn't claimed by then
    verify_timeout_s: float = 300.0     # try another reviewer if no verdict by then
    max_attempts: int = 3               # agents tried per request before asking the human
    max_review_rounds: int = 2          # rejections before the human decides
    online_window_s: float = 300.0      # seen this recently counts as online
    human_fallback: bool = True         # with no reviewer agent, the human reviews (False: skip review)


# ------------------------------------------------------------- classifying

CATEGORIES: dict[str, dict[str, Any]] = {
    "code": {
        "prefixes": ("code", "dev", "git", "programming"),
        "words": r"\b(code|coding|bug|bugs|function|refactor|implement|unit tests?|pull request|repo|repository|compile|"
                 r"python|javascript|typescript|java|golang|rust|sql|script|deploy|stack ?trace|exception|api endpoint|"
                 r"regex|dockerfile|frontend|backend)\b|```|def \w+\(|class \w+[:(]|\b[a-z]+_\w*\([\w, ]*\)",
    },
    "research": {"prefixes": ("research", "web", "search"),
                 "words": r"\b(research|find out|look up|investigate|sources?|compare|market|competitors?|latest)\b"},
    "summarize": {"prefixes": ("text.summarize", "summarize", "summary"),
                  "words": r"\b(summari[sz]e|summary|tl;?dr|key points|digest)\b"},
    "writing": {"prefixes": ("text.write", "writing", "write", "text.edit", "copy"),
                "words": r"\b(write|draft|rewrite|email|blog|essay|copy|proofread|edit the|tone)\b"},
    "data": {"prefixes": ("data", "analytics", "sql", "spreadsheet"),
             "words": r"\b(analy[sz]e|dataset|csv|spreadsheet|chart|metrics|statistics|forecast)\b"},
    "payment": {"prefixes": ("payment", "pay", "billing"), "words": r"\b(pay|payment|invoice|refund|transfer)\b"},
}
REVIEW_CAPS = {"code": ("code.review", "verify"), "default": ("verify", "review")}


def _is_review_cap(cap: str) -> bool:
    return cap in ("verify", "review") or cap.endswith(".review") or cap.startswith("verify.")


def classify(capability: str | None, payload: dict[str, Any], text: str | None, subject: str | None) -> str:
    """Request type from the capability name, or else from the words in it."""
    if capability:
        for kind, spec in CATEGORIES.items():
            if capability.startswith(spec["prefixes"]):
                return kind
    blob = " ".join(filter(None, [subject, text, json.dumps(payload, default=str)[:4000]])).lower()
    for kind in ("code", "summarize", "research", "writing", "data", "payment"):
        if re.search(CATEGORIES[kind]["words"], blob):
            return kind
    return capability.split(".")[0] if capability else "general"


def _matching_cap(agent: AgentRecord, kind: str) -> str | None:
    """The agent's capability that fits a request type (review capabilities don't do the work)."""
    prefixes = CATEGORIES.get(kind, {}).get("prefixes", (kind,))
    for cap in agent.capabilities:
        if cap.startswith(prefixes) and not _is_review_cap(cap):
            return cap
    if kind == "general":
        return next((c for c in agent.capabilities if c.startswith("general")), None)
    return None


# ----------------------------------------------------------------- scoring

WEIGHTS = {
    "balanced": {"reliability": 0.40, "cost": 0.25, "load": 0.15, "online": 0.15, "exact": 0.05},
    "cheapest": {"reliability": 0.20, "cost": 0.55, "load": 0.10, "online": 0.10, "exact": 0.05},
    "quality": {"reliability": 0.60, "cost": 0.05, "load": 0.15, "online": 0.15, "exact": 0.05},
}


def reliability(stats: dict[str, float]) -> float:
    """Share of an agent's work that held up, with a neutral prior for newcomers."""
    good = stats["approved"]
    bad = stats["rejected"] + stats["failed"] + stats["timeouts"]
    return (good + 1) / (good + bad + 2)


class RouterMixin:  # pylint: disable=too-few-public-methods
    store: "Store"
    router: RouterConfig
    market: "MarketConfig"

    # Provided by Bus.
    def _thread(self, thread_id: str) -> Thread:
        raise NotImplementedError(thread_id)

    def _system(self, thread_id: str, payload: dict[str, Any], text: str, *, intent: Intent = Intent.inform,
                to: str | None = None, artifacts: list[str] | None = None, memory_refs: list[str] | None = None) -> Envelope:
        raise NotImplementedError((thread_id, payload, text, intent, to, artifacts, memory_refs))

    # -------------------------------------------------------------- scoring
    def is_online(self, a: AgentRecord, now: datetime | None = None) -> bool:
        """Seen within the online window (agents are seen whenever they call the API)."""
        return bool(a.last_seen and (now or utcnow()) - a.last_seen <= timedelta(seconds=self.router.online_window_s))

    def score_candidates(self, pairs: list[tuple[AgentRecord, str, bool]], strategy: str | None = None,
                         budget_left: float | None = None, prefer_not_vendor: str | None = None,
                         prefer_not: set[str] | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        """Reviewer selection. pairs: (agent, capability it would use, exact match?). Best first, with reasons.
        With `kind`, an agent's reviewer rating for that type of work replaces its overall track record."""
        w = WEIGHTS.get(strategy or self.router.strategy, WEIGHTS["balanced"])
        now = utcnow()
        costs = [a.cost_hint_usd for a, _, _ in pairs if a.cost_hint_usd is not None]
        top = max(costs, default=0.0)
        out = []
        for a, cap, exact in pairs:
            if budget_left is not None and a.cost_hint_usd is not None and a.cost_hint_usd > budget_left + 1e-9:
                continue  # can't afford it
            st = self.store.stats(a.id)
            rep = self.store.reputation(a.id, kind, "reviewer") if kind else None
            parts = {
                "reliability": rep["alpha"] / (rep["alpha"] + rep["beta"]) if rep else reliability(st),
                "cost": 0.5 if a.cost_hint_usd is None else (1.0 if top <= 0 else 1 - a.cost_hint_usd / top * 0.9),
                "load": 1 / (1 + self.store.load(a.id)),
                "online": 1.0 if self.is_online(a, now) else 0.3,
                "exact": 1.0 if exact else 0.0,
            }
            score = sum(w[k] * v for k, v in parts.items())
            if prefer_not_vendor and a.vendor and a.vendor != prefer_not_vendor:
                score += 0.15  # independent reviewers are worth more
            if prefer_not and a.id in prefer_not:
                score -= 0.2
            out.append({"agent": a.id, "name": a.name, "capability": cap, "score": round(score, 3),
                        "cost_hint_usd": a.cost_hint_usd, "vendor": a.vendor,
                        "track_record": f"{int(st['approved'])} approved, {int(st['rejected'])} rejected, "
                                        f"{int(st['failed'] + st['timeouts'])} failed",
                        "online": parts["online"] == 1.0, "load": self.store.load(a.id)})
        out.sort(key=lambda c: (-c["score"], c["name"]))
        return out

    def _worker_pairs(self, capability: str | None, kind: str, exclude: set[str]) -> list[tuple[AgentRecord, str, bool]]:
        pairs = []
        for a in self.store.list_agents():
            if a.paused or a.id in exclude:
                continue
            if capability and capability in a.capabilities:
                pairs.append((a, capability, True))
            elif not capability:
                cap = _matching_cap(a, kind)
                if cap:
                    pairs.append((a, cap, False))
        return pairs

    # -------------------------------------------------------------- routing
    def plan_route(self, sender_id: str, env: EnvelopeIn) -> dict[str, Any] | None:
        """For a new request to a capability or to nobody: who should do it, and why."""
        if not self.router.enabled:
            if not env.to.capability:
                raise GuardError("no_recipient", "routing is off; address an agent or a capability", 422)
            return None
        kind = classify(env.to.capability, env.payload, env.text, env.subject)
        mode = env.bidding or self.market.mode
        ranked = self.rank_workers(self._offers(env.to.capability, kind, {sender_id}), kind,
                                   env.prefer or self.router.strategy, env.budget_usd, explore=mode == "instant")
        if not ranked:
            if env.to.capability:
                raise NotFound(f"no available agent offers capability {env.to.capability!r} within budget")
            raise NotFound(f"no agent can handle this {kind} request; add one with `amsg add`, or address a capability")
        return {"kind": kind, "strategy": env.prefer or self.router.strategy, "mode": mode,
                "candidates": ranked if mode == "auction" else ranked[:5]}

    def _offers(self, capability: str | None, kind: str, exclude: set[str]) -> list[dict[str, Any]]:
        """Each eligible agent with its standing price (rate card or cost hint)."""
        return [{"agent": a, "capability": cap, "exact": exact, "price_usd": self.rate_for(a, cap, kind),
                 "eta_s": a.eta_hint_s, "tokens_est": a.tokens_hint}
                for a, cap, exact in self._worker_pairs(capability, kind, exclude)]

    def apply_route(self, t: Thread, request: Envelope, plan: dict[str, Any]) -> None:
        best = plan["candidates"][0]
        t.kind = plan["kind"]
        t.capability = t.capability or best["capability"]
        t.assigned_to = best["agent"]
        t.routing = {"request_seq": request.seq, "strategy": plan["strategy"], "candidates": plan["candidates"],
                     "requested_capability": request.to.capability, "price_usd": best["price_usd"],
                     "tried": [best["agent"]], **self.estimate_fields(best),
                     "messages": {str(request.seq): {"for": best["agent"], "kind": "work", "capability": best["capability"]}}}
        self.store.save_thread(t)
        self.store.add_participant(t, best["agent"])
        self.store.bump_stats(best["agent"], assigned=1)
        self._system(t.id, {"event": "routed", "assigned": best["name"], "kind": t.kind, "candidates": plan["candidates"]},
                     self._explain(best, plan["candidates"][1:], t.kind))

    @staticmethod
    def _explain(best: dict[str, Any], others: list[dict[str, Any]], kind: str | None) -> str:
        cost = f"${best['price_usd']:.3f}/request" if best["price_usd"] is not None else "price unknown"
        why = f"Router: {kind or 'request'} work assigned to {best['name']} ({best['capability']}; {cost}; " \
              f"{best['rating']}★ from {best['tasks']} {kind or ''} tasks; value ${best['value_usd']:.4f} per approved " \
              f"result; {'online' if best['online'] else 'not seen recently'}" + \
              ("; a newcomer getting a chance to build a record" if best.get("explored") else "") + ")."
        if others:
            why += " Next in line: " + ", ".join(f"{o['name']} (${o['value_usd']:.4f})" for o in others[:3]) + "."
        return why

    def reroute(self, t: Thread, reason: str, to_agent: AgentRecord | None = None,
                on_failure: bool = False) -> Thread | None:
        """Give the request to the next candidate (or a specific agent). With nobody left: after a
        failure, return None so the thread fails; otherwise (nobody responded) ask the human."""
        tried = set(t.routing.get("tried", []))
        if to_agent is None:
            if len(tried) >= self.router.max_attempts:
                ranked = []
            elif t.market.get("status") == "awarded":  # next-best bid
                ranked = [c for c in t.routing.get("candidates", []) if c["agent"] not in tried]
            else:
                ranked = self.rank_workers(
                    self._offers(t.routing.get("requested_capability"), t.kind or "general", tried | {t.creator}),
                    t.kind or "general", t.routing.get("strategy"), explore=False)
            if not ranked:
                if on_failure:
                    return None
                why = f"tried {len(tried)} agents" if len(tried) >= self.router.max_attempts else "no other agent can take it"
                return self._escalate(t, f"{reason}; {why}. Assign it to an agent or close it")
            nxt = ranked[0]
        else:
            cap = t.capability if t.capability in to_agent.capabilities else \
                (_matching_cap(to_agent, t.kind or "general") or t.capability)
            nxt = {"agent": to_agent.id, "name": to_agent.name, "capability": cap,
                   "price_usd": self.rate_for(to_agent, cap, t.kind or "general"),
                   "eta_s": to_agent.eta_hint_s, "tokens_est": to_agent.tokens_hint}
        req = self._original_request(t)
        t.owner, t.state, t.assigned_to = None, ThreadState.open, nxt["agent"]
        t.routing["tried"] = sorted(tried | {nxt["agent"]})
        t.routing["price_usd"] = nxt.get("price_usd")
        t.routing.update(self.estimate_fields(nxt))
        self.store.save_thread(t)
        self.store.add_participant(t, nxt["agent"])
        self.store.bump_stats(nxt["agent"], assigned=1)
        self._system(t.id, {"event": "rerouted", "assigned": nxt["name"], "reason": reason},
                     f"Router: {reason}. Reassigned to {nxt['name']}.")
        self._send_work(t, req, nxt["agent"], nxt["capability"])
        return self._thread(t.id)

    def _send_work(self, t: Thread, req: Envelope, agent_id: str, capability: str | None,
                   revision: dict[str, Any] | None = None) -> Envelope:
        """A system copy of the request, addressed to one agent (signatures stay on the original)."""
        copy = self._system(t.id, req.payload, req.text or "", intent=Intent.request, to=agent_id,
                            artifacts=req.artifacts, memory_refs=req.memory_refs)
        t = self._thread(t.id)
        meta: dict[str, Any] = {"for": agent_id, "kind": "work", "capability": capability, "original": req.seq}
        if revision:
            meta["revision"] = revision
        t.routing.setdefault("messages", {})[str(copy.seq)] = meta
        self.store.save_thread(t)
        return copy

    def _original_request(self, t: Thread) -> Envelope:
        seq = t.routing.get("request_seq")
        env = self.store.get_message(seq) if seq else None
        if env is None:
            env = next((m for m in self.store.thread_messages(t.id, 0, 50) if m.intent == Intent.request), None)
        if env is None:
            raise GuardError("no_request", "thread has no request", 409)
        return env

    def _escalate(self, t: Thread, reason: str) -> Thread:
        t = self._thread(t.id)
        t.state, t.owner, t.assigned_to = ThreadState.waiting_human, None, None
        self.store.save_thread(t)
        self._system(t.id, {"event": "escalated", "reason": reason}, f"Needs a human: {reason}.", intent=Intent.ask_human)
        return self._thread(t.id)

    def route_for(self, reader_id: str, env: Envelope) -> dict[str, Any] | None:
        """Routing metadata for a request, shown only to the agent it was routed to."""
        if env.intent != Intent.request:
            return None
        t = self.store.get_thread(env.thread_id)
        if not t:
            return None
        meta = (t.routing.get("messages") or {}).get(str(env.seq))
        if meta and meta.get("for") == reader_id:
            return {k: v for k, v in meta.items() if k != "for"}
        return None

    # --------------------------------------------------------- verification
    def maybe_verify(self, sender: AgentRecord, t: Thread, result: Envelope) -> None:
        """Called when an agent posts a result: start (another) review."""
        v = t.verification
        if not self.router.verify or t.state in TERMINAL or v.get("status") in ("approved", "overridden"):
            return
        if v.get("status") == "pending" and sender.id == v.get("verifier"):
            return
        if t.owner and sender.id != t.owner:
            return  # only the worker's results are reviewed
        if not t.owner:
            t.owner = sender.id
        v["tried"] = []  # a new result starts a new round; the last reviewer is the best one to check the fix
        self._start_review(t, sender, result)

    def _start_review(self, t: Thread, worker: AgentRecord, result: Envelope, exclude: set[str] | None = None) -> None:
        v = t.verification
        kind = t.kind or classify(t.capability, result.payload, result.text, t.subject)
        caps = REVIEW_CAPS["code"] if kind == "code" else REVIEW_CAPS["default"]
        tried = set(v.get("tried", [])) | (exclude or set())
        pairs = []
        for a in self.store.list_agents():
            if a.paused or a.id == worker.id or a.id in tried:
                continue
            if worker.owner and a.owner == worker.owner:
                continue  # an owner's agents don't review each other's work
            cap = next((c for c in caps if c in a.capabilities), None)
            if cap:
                pairs.append((a, cap, cap == caps[0]))
        budget_left = None if t.budget_usd is None else t.budget_usd - t.spent_usd
        ranked = self.score_candidates(pairs, "quality", budget_left=budget_left,
                                       prefer_not_vendor=worker.vendor or None, prefer_not={t.creator}, kind=kind)
        v.update({"status": "pending", "worker": worker.id, "result_seq": result.seq, "kind": kind,
                  "round": v.get("round", 0), "requested_at": utcnow().isoformat(),
                  "history": v.get("history", [])})
        if not ranked:
            if not self.router.human_fallback:
                v.update({"status": "skipped", "verifier": None})
                t.state = ThreadState.done
                self.store.save_thread(t)
                self._system(t.id, {"event": "verification_skipped"}, "No reviewer available; closed without review.")
                return
            v.update({"verifier": HUMAN})
            t.state = ThreadState.verifying
            self.store.save_thread(t)
            self._system(t.id, {"event": "verification", "verifier": HUMAN, "result_seq": result.seq},
                         f"Verification: no reviewer agent is available, so a human needs to check result #{result.seq}. "
                         "Approve or reject it in the viewer (or reply 'approve' / 'reject: why').")
            return
        best = ranked[0]
        v.update({"verifier": best["agent"], "verifier_capability": best["capability"],
                  "tried": sorted(tried | {best["agent"]})})
        t.state = ThreadState.verifying
        self.store.save_thread(t)
        self.store.add_participant(t, best["agent"])
        self._system(t.id, {"event": "verification", "verifier": best["name"], "result_seq": result.seq},
                     f"Verification: {best['name']} ({best['capability']}, vendor {best['vendor'] or 'unknown'}) "
                     f"will check result #{result.seq} before this thread is done.")
        self._send_review_request(t, result, best)

    def _send_review_request(self, t: Thread, result: Envelope, reviewer: dict[str, Any]) -> None:
        original = self._original_request(t)

        def clip(payload: dict[str, Any]) -> dict[str, Any]:
            return payload if estimate_tokens(payload) <= 1500 else {"_clipped": True, "keys": list(payload)[:20],
                                                                     "tokens": estimate_tokens(payload)}
        payload = {
            "kind": "verification",
            "category": t.verification.get("kind"),
            "round": t.verification.get("round", 0) + 1,
            "task": {"subject": t.subject, "payload": clip(original.payload), "text": (original.text or "")[:4000],
                     "seq": original.seq},
            "result": {"payload": clip(result.payload), "text": (result.text or "")[:4000], "seq": result.seq,
                       "artifacts": result.artifacts, "memory_refs": result.memory_refs},
            "instructions": "Check the result against the task. Reply with intent 'result' and payload "
                            "{'verdict': 'approve' | 'reject', 'notes': '...', 'issues': [...]}. "
                            "Reject anything wrong, incomplete, unsafe or unverifiable.",
        }
        copy = self._system(t.id, payload, f"Please verify result #{result.seq}: {t.subject}",
                            intent=Intent.request, to=reviewer["agent"])
        t = self._thread(t.id)
        t.verification["request_seq"] = copy.seq
        t.routing.setdefault("messages", {})[str(copy.seq)] = {"for": reviewer["agent"], "kind": "verification",
                                                               "capability": reviewer["capability"]}
        self.store.save_thread(t)

    @staticmethod
    def parse_verdict(env: Envelope) -> str | None:
        if env.intent == Intent.reject:
            return "reject"
        raw = str(env.payload.get("verdict") or env.payload.get("decision") or "").lower() or (env.text or "").lower()[:40]
        if raw.startswith(("approve", "pass", "accept", "lgtm", "yes", "ok")):
            return "approve"
        if raw.startswith(("reject", "fail", "deny", "no", "changes")):
            return "reject"
        return None

    def apply_verdict(self, t: Thread, verifier_id: str, verdict: str | None, notes: str, issues: list[Any] | None = None) -> Thread:
        v = t.verification
        worker = v.get("worker")
        v["history"] = [*v.get("history", []), {"round": v.get("round", 0) + 1, "verifier": verifier_id, "verdict": verdict,
                                                "notes": notes[:2000], "at": utcnow().isoformat()}]
        names = {a.id: a.name for a in self.store.list_agents()}
        who = names.get(verifier_id, verifier_id)
        if verifier_id != HUMAN:
            self.store.bump_stats(verifier_id, reviews=1)
        if verdict == "approve":
            v["status"] = "approved"
            t.state = ThreadState.done
            if worker:
                self.store.bump_stats(worker, approved=1)
            self.store.save_thread(t)
            self.record_review(t, worker, verifier_id, "approve")
            self.settle(self._thread(t.id), worker)
            t = self._thread(t.id)
            self._system(t.id, {"event": "verified", "verdict": "approve", "by": who, "notes": notes},
                         f"Verified by {who}: approved." + (f" {notes}" if notes else ""))
            return self._thread(t.id)
        if verdict is None:
            v["status"] = "inconclusive"
            self.store.save_thread(t)
            return self._escalate(t, f"{who} gave no clear verdict on the result")
        # rejected
        v["round"] = v.get("round", 0) + 1
        v["status"] = "rejected"
        if worker:
            self.store.bump_stats(worker, rejected=1)
        self.store.save_thread(t)
        self.record_review(t, worker, verifier_id, "reject")
        if v["round"] >= self.router.max_review_rounds or not worker or verifier_id == HUMAN and not t.owner:
            return self._escalate(t, f"{who} rejected the result {v['round']} time(s): {notes or 'no notes'}")
        t.state = ThreadState.working
        self.store.save_thread(t)
        self._system(t.id, {"event": "revision_requested", "by": who, "notes": notes, "issues": issues or []},
                     f"{who} rejected the result: {notes or 'no notes'}. Sent back to {names.get(worker, worker)} to revise.")
        self._send_work(self._thread(t.id), self._original_request(t), worker, t.capability,
                        revision={"round": v["round"], "feedback": notes, "issues": issues or [], "by": who})
        return self._thread(t.id)

    def on_verifier_message(self, t: Thread, sender: AgentRecord, env: Envelope) -> None:
        v = t.verification
        if v.get("status") != "pending" or v.get("verifier") != sender.id:
            return
        if env.intent not in (Intent.result, Intent.reject, Intent.error):
            return
        if env.intent == Intent.error:
            self.store.bump_stats(sender.id, failed=1)
            worker = self.store.get_agent(v["worker"])
            self._start_review(t, worker, self.store.get_message(v["result_seq"]), exclude={sender.id})  # type: ignore[arg-type]
            return
        self.apply_verdict(t, sender.id, self.parse_verdict(env), str(env.payload.get("notes") or env.text or ""),
                           env.payload.get("issues"))

    def human_verdict(self, thread_id: str, verdict: str, notes: str = "") -> Thread:
        t = self._thread(thread_id)
        if t.state in TERMINAL:
            raise GuardError("thread_closed", f"thread is {t.state.value}")
        if verdict not in ("approve", "reject"):
            raise GuardError("bad_verdict", "verdict must be approve or reject", 422)
        if verdict == "reject" and not t.verification.get("worker"):
            raise GuardError("nothing_to_review", "there is no result to reject yet", 409)
        if t.verification.get("status") != "pending" or t.verification.get("verifier") != HUMAN:
            t.verification["overridden_by_human"] = True
        if not t.verification.get("worker"):
            t.verification["status"] = "overridden"
            t.state = ThreadState.done
            self.store.save_thread(t)
            self._system(t.id, {"event": "verified", "verdict": "approve", "by": HUMAN}, "Closed by the human observer.")
            return self._thread(t.id)
        return self.apply_verdict(t, HUMAN, verdict, notes)

    # ---------------------------------------------------------------- ticks
    def tick(self, now: datetime | None = None) -> list[str]:
        """Reassign stalled work and reviews. The server runs this every few seconds."""
        if not self.router.enabled and not self.router.verify:
            return []
        now = now or utcnow()
        acted = self.tick_market(now)
        for t in self.store.threads_in_states(["open", "verifying"]):
            if t.market.get("status") == "open":
                continue
            if t.state == ThreadState.open and t.assigned_to and not t.owner and t.routing.get("assigned_at"):
                if now - datetime.fromisoformat(t.routing["assigned_at"]) > timedelta(seconds=self.router.claim_timeout_s):
                    self.store.bump_stats(t.assigned_to, timeouts=1)
                    self.record_failure(t, t.assigned_to, weight=1.0)
                    name = getattr(self.store.get_agent(t.assigned_to), "name", t.assigned_to)
                    self.reroute(t, f"{name} did not pick it up within {int(self.router.claim_timeout_s)}s")
                    acted.append(t.id)
            elif t.state == ThreadState.verifying and t.verification.get("status") == "pending" \
                    and t.verification.get("verifier") not in (None, HUMAN):
                asked = datetime.fromisoformat(t.verification["requested_at"])
                if now - asked > timedelta(seconds=self.router.verify_timeout_s):
                    self.store.bump_stats(t.verification["verifier"], timeouts=1)
                    worker = self.store.get_agent(t.verification["worker"])
                    result = self.store.get_message(t.verification["result_seq"])
                    self._system(t.id, {"event": "verifier_timeout"}, "Reviewer did not answer in time; trying another.")
                    self._start_review(self._thread(t.id), worker, result,  # type: ignore[arg-type]
                                       exclude={t.verification["verifier"]})
                    acted.append(t.id)
        return acted

    def assign(self, thread_id: str, agent_ref: str, by_human: bool = True) -> Thread:
        """Observer override: give this thread to a specific agent."""
        t = self._thread(thread_id)
        if t.state in TERMINAL:
            raise GuardError("thread_closed", f"thread is {t.state.value}")
        if not by_human:
            raise Forbidden("only the observer can reassign")
        if not t.routing.get("request_seq"):
            first = next((m for m in self.store.thread_messages(t.id, 0, 50) if m.intent == Intent.request), None)
            if not first:
                raise GuardError("no_request", "thread has no request to assign", 409)
            t.routing = {"request_seq": first.seq, "tried": [], "messages": {}, "strategy": self.router.strategy}
        agent = self.store.get_agent(agent_ref)
        if not agent:
            raise NotFound(f"no agent {agent_ref!r}")
        return self.reroute(t, "the human observer reassigned it", to_agent=agent)  # type: ignore[return-value]


__all__ = ["RouterConfig", "RouterMixin", "classify", "reliability"]
