"""A work marketplace: rate cards, sealed-bid auctions, reputation and pay on approval.

Who gets the work
    Every eligible agent has a price for a request: its sealed bid (auction
    mode) or its rate card / cost hint (instant mode, the default). The
    winner has the lowest *value*:

        value = price / P(pass review) ** k  x  (credible ETA / typical ETA) ** t

    (k, t: cheapest 0.5, 0.1; balanced 1, 0.3; quality 3, 0.2; fastest 1, 1), so
    a cheap agent whose work is often rejected doesn't win on price alone, and a
    faster one wins ties. The credible ETA is the promised ETA times how much the
    agent usually overruns, so neither over-promising nor padding pays. Load and
    being offline make an agent slightly less attractive.

Reputation
    Each agent has a rating per type of work (code, writing, ...) and role
    (worker or reviewer): a Beta(alpha, beta) estimate of the chance its work
    passes, where older evidence fades toward the neutral prior. Reviews move
    it: an agent reviewer's verdict counts by that reviewer's own rating, a
    human's counts 3x, and an approval that needed revisions counts half as
    much per rejected round (right first time is what quality means). Failing after winning, finishing late and costing more
    than the bid count against the worker. Reviewers are rated on whether
    humans agree with them.

Pay on approval
    The agreed price moves from the requester to the worker in a simulated
    credits ledger only when the result passes review.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .guards import Forbidden, GuardError
from .models import AgentRecord, Envelope, Intent, Thread, ThreadState, estimate_tokens, utcnow

if TYPE_CHECKING:
    from .store import Store

HUMAN = "human"
KIND_LABELS = {"code": "coding", "writing": "writing", "research": "research", "summarize": "summarizing",
               "data": "data", "payment": "payments", "general": "general"}
K = {"cheapest": 0.5, "balanced": 1.0, "quality": 3.0, "fastest": 1.0}  # weight on P(pass)
T = {"cheapest": 0.1, "balanced": 0.3, "quality": 0.2, "fastest": 1.0}  # weight on (credible) ETA
PADDED = 1 / 3  # finishing in under a third of the ETA means the estimate was padded


@dataclass
class MarketConfig:
    mode: str = "instant"            # default for requests that don't say: instant | auction
    auction_window_s: float = 15.0   # how long bids are accepted
    max_invites: int = 8             # agents invited to bid on one request
    decay: float = 0.97              # how much older evidence keeps per new review
    human_weight: float = 3.0        # a human review counts this many times an average agent review
    probation_tasks: int = 5         # agents with fewer finished tasks of a type are "new" at it
    explore_rate: float = 0.1        # share of work given to a new agent when an established one would win
    default_eta_s: float = 120.0


def label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "general", kind or "general")


def chance(rep: dict[str, Any]) -> float:
    """Estimated chance that this agent's work of this type passes review."""
    return rep["alpha"] / (rep["alpha"] + rep["beta"])


def stars(p: float) -> float:
    return round(1 + 4 * p, 1)


def _dur(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m{seconds % 60:02d}s"


class MarketMixin:  # pylint: disable=too-few-public-methods
    store: "Store"
    market: MarketConfig
    _rng: random.Random

    # Provided by Bus / RouterMixin.
    def _thread(self, thread_id: str) -> Thread:
        raise NotImplementedError(thread_id)

    def _system(self, thread_id: str, payload: dict[str, Any], text: str, *, intent: Intent = Intent.inform,
                to: str | None = None, artifacts: list[str] | None = None, memory_refs: list[str] | None = None) -> Envelope:
        raise NotImplementedError((thread_id, payload, text, intent, to, artifacts, memory_refs))

    # ----------------------------------------------------------- pricing
    @staticmethod
    def rate_for(a: AgentRecord, capability: str | None, kind: str) -> float | None:
        """An agent's standing price: rate card by capability, then by type of work, then its cost hint."""
        for key in (capability, kind, label(kind)):
            if key and key in a.rates:
                return a.rates[key]
        return a.cost_hint_usd

    def rank_workers(self, offers: list[dict[str, Any]], kind: str, strategy: str | None,
                     budget: float | None = None, explore: bool = True) -> list[dict[str, Any]]:
        """offers: {"agent": AgentRecord, "capability", "price_usd", "eta_s"?}. Best (lowest value) first."""
        k = K.get(strategy or "balanced", 1.0)
        t_w = T.get(strategy or "balanced", 0.3)
        now = utcnow()
        known = [o["price_usd"] for o in offers if o.get("price_usd") is not None]
        fallback = statistics.median(known) if known else 1.0
        slips = {o["agent"].id: self.eta_slip(o["agent"].id) for o in offers}
        credible = {o["agent"].id: o["eta_s"] * slips[o["agent"].id] for o in offers if o.get("eta_s")}
        typical = statistics.median(credible.values()) if credible else None
        out = []
        for o in offers:
            a: AgentRecord = o["agent"]
            price = o.get("price_usd")
            if budget is not None and price is not None and price > budget + 1e-9:
                continue
            rep = self.store.reputation(a.id, kind, "worker")
            p = chance(rep)
            load = self.store.load(a.id)
            online = self.is_online(a, now)
            value = (price if price is not None else fallback) / (p ** k)
            value *= (1 + 0.15 * load) * (1.0 if online else 1.25) * (0.95 if o.get("exact") else 1.0)
            eta_c = credible.get(a.id)
            if typical:  # no ETA while others give one counts as slower than typical
                value *= ((eta_c if eta_c else 1.25 * typical) / typical) ** t_w
            out.append({"agent": a.id, "name": a.name, "vendor": a.vendor, "capability": o["capability"],
                        "price_usd": price, "eta_s": o.get("eta_s"), "tokens_est": o.get("tokens_est"),
                        "eta_slip": round(slips[a.id], 2), "credible_eta_s": round(eta_c, 1) if eta_c else None,
                        "chance": round(p, 3), "rating": stars(p), "traits": self.traits(a.id),
                        "tasks": rep["tasks"], "new": rep["tasks"] < self.market.probation_tasks,
                        "value_usd": round(value, 6), "online": online, "load": load})
        out.sort(key=lambda c: (c["value_usd"], -c["chance"], c["name"]))
        if explore and len(out) > 1 and not out[0]["new"] and self.market.explore_rate > 0 \
                and self._rng.random() < self.market.explore_rate:
            newcomers = [c for c in out[1:] if c["new"]]
            if newcomers:  # give a newcomer a fair chance to build a record
                pick = newcomers[0]
                pick["explored"] = True
                out.remove(pick)
                out.insert(0, pick)
        return out

    # ----------------------------------------------------------- auctions
    def open_auction(self, t: Thread, request: Envelope, plan: dict[str, Any]) -> None:
        invited = plan["candidates"][: self.market.max_invites]
        now = utcnow()
        closes = now + timedelta(seconds=self.market.auction_window_s)
        t.kind = plan["kind"]
        t.market = {"mode": "auction", "status": "open", "opened_at": now.isoformat(), "closes_at": closes.isoformat(),
                    "invited": [c["agent"] for c in invited], "caps": {c["agent"]: c["capability"] for c in invited},
                    "bids": {}}
        t.routing = {"request_seq": request.seq, "strategy": plan["strategy"], "requested_capability": request.to.capability,
                     "tried": [], "messages": {}}
        self.store.save_thread(t)
        self._system(t.id, {"event": "bidding_open", "invited": len(invited), "closes_at": closes.isoformat()},
                     f"Bidding open for this {label(t.kind)} work: {len(invited)} agents invited; "
                     f"closes in {int(self.market.auction_window_s)}s.")
        task = {"subject": t.subject, "text": (request.text or "")[:4000],
                "payload": request.payload if estimate_tokens(request.payload) <= 1500 else
                {"_clipped": True, "keys": list(request.payload)[:20]}}
        for c in invited:
            call = self._system(t.id, {"kind": "bid_call", "task": task, "type": t.kind, "budget_usd": t.budget_usd,
                                       "deadline": t.deadline.isoformat() if t.deadline else None,
                                       "closes_at": closes.isoformat(),
                                       "instructions": "Bid with POST /v1/threads/{id}/bids {price_usd, eta_s, plan, "
                                                       "confidence}. Bids are sealed. The winner has the lowest price "
                                                       "per expected approved result; pay is on approval only."},
                                f"Call for bids: {t.subject}", intent=Intent.request, to=c["agent"])
            t = self._thread(t.id)
            t.routing["messages"][str(call.seq)] = {"for": c["agent"], "kind": "bid_call", "capability": c["capability"]}
            self.store.save_thread(t)

    def submit_bid(self, agent: AgentRecord, thread_id: str, price_usd: float, eta_s: float | None = None,
                   plan: str = "", confidence: float | None = None, tokens_est: int | None = None,
                   eta_at: datetime | None = None) -> dict[str, Any]:
        """eta_at is a wall-clock time ("done by 14:30 UTC"); eta_s a duration. Give either."""
        t = self._thread(thread_id)
        now = utcnow()
        if eta_at is not None:
            if eta_at.tzinfo is None:
                raise GuardError("bad_bid", "eta_at needs a timezone, e.g. 2026-11-06T14:30:00Z", 422)
            eta_s = (eta_at - now).total_seconds()
            if eta_s <= 0:
                raise GuardError("bad_bid", "eta_at is in the past", 422)
        if tokens_est is not None and tokens_est < 0:
            raise GuardError("bad_bid", "tokens_est must be 0 or more", 422)
        m = t.market
        if m.get("status") != "open":
            raise GuardError("bidding_closed", "this request is not open for bids", 409)
        if agent.id not in m.get("invited", []):
            raise Forbidden("you were not invited to bid on this request")
        if agent.paused:
            raise GuardError("agent_paused", "this agent has been paused", 423)
        if price_usd < 0:
            raise GuardError("bad_bid", "price_usd must be 0 or more", 422)
        if t.budget_usd is not None and price_usd > t.budget_usd - t.spent_usd + 1e-9:
            raise GuardError("over_budget", f"the budget for this request is ${t.budget_usd:.2f}", 422)
        if t.deadline and eta_s and utcnow() + timedelta(seconds=eta_s) > t.deadline:
            raise GuardError("too_slow", "your ETA is past the request's deadline", 422)
        m["bids"][agent.id] = {"price_usd": round(price_usd, 6), "eta_s": eta_s, "tokens_est": tokens_est,
                               "eta_at": (now + timedelta(seconds=eta_s)).isoformat() if eta_s else None,
                               "plan": plan[:1000], "confidence": confidence, "at": now.isoformat()}
        self.store.save_thread(t)
        if set(m["bids"]) >= set(m["invited"]):
            self.close_auction(self._thread(t.id), "every invited agent bid")
        return {"accepted": True, "closes_at": m["closes_at"], "bids_so_far": len(m["bids"])}

    def close_auction(self, t: Thread, why: str) -> Thread:
        m = t.market
        if m.get("status") != "open":
            return t
        agents = {a.id: a for a in self.store.list_agents()}
        offers = []
        for aid in m["invited"]:
            a = agents.get(aid)
            if not a or a.paused:
                continue
            bid = m["bids"].get(aid)
            if bid:
                offers.append({"agent": a, "capability": m["caps"].get(aid), "price_usd": bid["price_usd"],
                               "eta_s": bid.get("eta_s"), "tokens_est": bid.get("tokens_est")})
        source = "bids"
        if not offers:  # nobody bid: fall back to the invited agents' rate cards
            source = "rate cards (no bids arrived)"
            offers = [{"agent": agents[aid], "capability": m["caps"].get(aid),
                       "price_usd": self.rate_for(agents[aid], m["caps"].get(aid), t.kind or "general"),
                       "eta_s": agents[aid].eta_hint_s, "tokens_est": agents[aid].tokens_hint}
                      for aid in m["invited"] if aid in agents and not agents[aid].paused]
        ranked = self.rank_workers(offers, t.kind or "general", t.routing.get("strategy"),
                                   None if t.budget_usd is None else t.budget_usd - t.spent_usd)
        m["status"] = "awarded" if ranked else "no_offers"
        m["closed_at"] = utcnow().isoformat()
        self.store.save_thread(t)
        if not ranked:
            t.state = ThreadState.waiting_human
            self.store.save_thread(t)
            self._system(t.id, {"event": "escalated", "reason": "no usable bids"},
                         "Needs a human: no agent made a usable offer. Assign it to an agent or close it.",
                         intent=Intent.ask_human)
            return self._thread(t.id)
        best = ranked[0]
        m["award"] = {"agent": best["agent"], "price_usd": best["price_usd"], "eta_s": best["eta_s"],
                      "tokens_est": best.get("tokens_est"), "source": source}
        self.store.save_thread(t)
        self._award(self._thread(t.id), best, ranked, f"Bidding closed ({why}); {len(m['bids'])} bid(s), from {source}")
        for c in ranked[1:]:
            self._system(t.id, {"event": "not_awarded", "winner": best["name"]},
                         f"Not awarded: {best['name']} offered better value.", to=c["agent"])
        return self._thread(t.id)

    def _award(self, t: Thread, best: dict[str, Any], ranked: list[dict[str, Any]], why: str) -> None:
        t.assigned_to, t.owner, t.state = best["agent"], None, ThreadState.open
        t.capability = t.capability or best["capability"]
        t.routing.update({"candidates": ranked[:5], "tried": sorted(set(t.routing.get("tried", [])) | {best["agent"]}),
                          "price_usd": best["price_usd"], **self.estimate_fields(best)})
        self.store.save_thread(t)
        self.store.add_participant(t, best["agent"])
        self.store.bump_stats(best["agent"], assigned=1)
        price = f"${best['price_usd']:.3f}" if best["price_usd"] is not None else "no price"
        eta = f", due {t.routing['due_at'][11:16]} UTC ({int(best['eta_s'])}s)" if best.get("eta_s") else ""
        if best.get("tokens_est"):
            eta += f", ~{best['tokens_est']:,} tokens"
        self._system(t.id, {"event": "awarded", "assigned": best["name"], "price_usd": best["price_usd"],
                            "rating": best["rating"], "bids": len(t.market.get("bids", {}))},
                     f"{why}. Awarded to {best['name']} at {price}{eta}: {best['rating']}★ for {label(t.kind)} "
                     f"({best['tasks']} tasks). Value = price ÷ chance of passing review.")
        self._send_work(self._thread(t.id), self._original_request(t), best["agent"], best["capability"])

    def tick_market(self, now: datetime) -> list[str]:
        acted = []
        for t in self.store.threads_in_states(["open"]):
            if t.market.get("status") == "open" and now >= datetime.fromisoformat(t.market["closes_at"]):
                self.close_auction(t, "the bidding window ended")
                acted.append(t.id)
        return acted

    def redact(self, t: Thread, reader_id: str | None) -> Thread:
        """Bids are sealed: agents see only their own bid and the winning price."""
        if reader_id is None or not t.market:
            return t
        out = t.model_copy(deep=True)
        out.market["bids"] = {k: v for k, v in t.market.get("bids", {}).items() if k == reader_id}
        winner = t.market.get("award", {}).get("agent")
        for c in out.routing.get("candidates", []):
            if c["agent"] not in (winner, reader_id):
                c["price_usd"] = None
                c["value_usd"] = None
        return out

    # --------------------------------------------------------- reputation
    def reviewer_weight(self, reviewer_id: str, kind: str) -> float:
        if reviewer_id == HUMAN:
            return self.market.human_weight
        return 0.5 + chance(self.store.reputation(reviewer_id, kind, "reviewer"))  # 0.5 .. 1.5

    def record_review(self, t: Thread, worker: str | None, reviewer: str, verdict: str) -> None:
        if not worker:
            return
        kind = t.kind or "general"
        w = self.reviewer_weight(reviewer, kind)
        d = self.market.decay
        if verdict == "approve":
            # Right first time counts fully; each rejection round before approval halves the credit.
            rounds = int(t.verification.get("round", 0))
            self.store.update_reputation(worker, kind, "worker", good=w * 0.5 ** rounds, decay=d, approved=1, tasks=1)
            self.check_delivery(t, worker, kind)
        else:
            self.store.update_reputation(worker, kind, "worker", bad=w, decay=d, rejected=1)
        # Reviewers: a human verdict tells us whether the last agent reviewer got it right.
        if reviewer == HUMAN:
            last = next((h for h in reversed(t.verification.get("history", [])[:-1])
                         if h["verifier"] != HUMAN and h.get("verdict")), None)
            if last:
                agreed = last["verdict"] == verdict
                self.store.update_reputation(last["verifier"], kind, "reviewer", good=1.0 if agreed else 0.0,
                                             bad=0.0 if agreed else 2.0, decay=d)
        else:
            self.store.update_reputation(reviewer, kind, "reviewer", good=0.25, decay=1.0, tasks=1)

    @staticmethod
    def estimate_fields(offer: dict[str, Any]) -> dict[str, Any]:
        """What the winner promised, with the wall-clock time it's due."""
        now = utcnow()
        eta = offer.get("eta_s")
        return {"assigned_at": now.isoformat(), "eta_s": eta, "tokens_est": offer.get("tokens_est"),
                "due_at": (now + timedelta(seconds=eta)).isoformat() if eta else None}

    def eta_slip(self, agent_id: str) -> float:
        """How much the agent's jobs usually overrun their ETA (median actual / promised), from 3+ jobs.
        1.0 means it keeps its word; 2.0 means it takes twice as long as it says."""
        ratios = [d["took_s"] / d["eta_s"] for d in self.store.deliveries(agent_id, limit=20)
                  if d["eta_s"] and d["took_s"] is not None]
        if len(ratios) < 3:
            return 1.0
        return min(3.0, max(0.5, statistics.median(ratios)))

    def check_delivery(self, t: Thread, worker: str, kind: str) -> dict[str, Any]:
        """Compare the finished job with the estimate: wall-clock time, tokens and cost.
        Posts the comparison, keeps it as a delivery record, and counts misses against the worker."""
        r = t.routing
        msgs = [m for m in self.store.thread_messages(t.id, 0, 10_000) if m.sender == worker]
        result = self.store.get_message(t.verification["result_seq"]) if t.verification.get("result_seq") else None
        start = datetime.fromisoformat(r["assigned_at"]) if r.get("assigned_at") else t.created_at
        took = ((result.created_at if result else utcnow()) - start).total_seconds()
        used = sum(m.tokens_used or 0 for m in msgs) if any(m.tokens_used is not None for m in msgs) else None
        spent = round(sum(m.cost_usd for m in msgs), 6)
        eta, est, price = r.get("eta_s"), r.get("tokens_est"), r.get("price_usd")
        on_time = None if not eta else took <= 1.1 * eta
        row = {"thread_id": t.id, "agent_id": worker, "kind": kind, "at": utcnow().isoformat(), "price_usd": price,
               "cost_usd": spent, "eta_s": eta, "took_s": round(took, 1), "tokens_est": est, "tokens_used": used,
               "on_time": None if on_time is None else int(on_time)}
        self.store.record_delivery(row)
        parts, misses = [], 0.0
        if eta:
            if not on_time:
                verdict = "late"
                misses += 0.5
            elif took < PADDED * eta:
                verdict = f"early: the ETA was {eta / max(took, 1):.0f}x too long"
                misses += 0.1  # padded estimates make planning harder too
            else:
                verdict = "on time"
            parts.append(f"took {_dur(took)} vs ETA {_dur(eta)} ({verdict})")
        if est and used is not None:
            parts.append(f"{used:,} tokens vs {est:,} estimated ({(used - est) / est:+.0%})")
            if used > 1.25 * est:
                misses += 0.25
            elif used < PADDED * est:
                misses += 0.1
        elif est:
            parts.append(f"tokens not reported (estimated {est:,})")
        if price is not None:
            parts.append(f"cost ${spent:.3f} vs price ${price:.3f}")
            if spent > 1.2 * price + 1e-9:
                misses += 0.5
        if misses:
            self.store.update_reputation(worker, kind, "worker", bad=misses)
        r["actuals"] = row
        self.store.save_thread(t)
        if parts:
            self._system(t.id, {"event": "estimate_check", **row}, "Checked against the estimate: " + "; ".join(parts) + ".")
        return row

    def traits(self, agent_id: str) -> list[str]:
        """Behaviour badges from the agent's recent deliveries (at least 3 of the relevant kind)."""
        recent = self.store.deliveries(agent_id, limit=20)
        out = []
        timed = [d for d in recent if d["on_time"] is not None and d["eta_s"]]
        tok = [d["tokens_used"] / d["tokens_est"] for d in recent if d["tokens_est"] and d["tokens_used"] is not None]
        eta_ratios = [d["took_s"] / d["eta_s"] for d in timed]
        padded = (len(eta_ratios) >= 3 and sum(r < PADDED for r in eta_ratios) / len(eta_ratios) >= 0.5) or \
                 (len(tok) >= 3 and sum(x < PADDED for x in tok) / len(tok) >= 0.5)
        if padded:
            out.append("#pads-estimates")  # "on time" only because the estimates are far too generous
        if len(timed) >= 3:
            rate = sum(d["on_time"] for d in timed) / len(timed)
            out += ["#late"] if rate <= 0.66 else ["#on-time"] if rate >= 0.8 and not padded else []
        if len(tok) >= 3:
            if sum(abs(x - 1) <= 0.25 for x in tok) / len(tok) >= 0.8:
                out.append("#accurate-estimates")
            elif sum(x > 1.25 for x in tok) / len(tok) >= 0.34:
                out.append("#token-heavy")
        costed = [d for d in recent if d["price_usd"] and d["cost_usd"] is not None]
        if len(costed) >= 3 and sum(d["cost_usd"] > 1.2 * d["price_usd"] for d in costed) / len(costed) >= 0.34:
            out.append("#over-budget")
        out += self._price_trait(agent_id, recent)
        return out

    def _price_trait(self, agent_id: str, recent: list[dict[str, Any]]) -> list[str]:
        """#expensive / #budget: the agent's typical price against others doing the same type of work."""
        mine: dict[str, list[float]] = defaultdict(list)
        for d in recent:
            if d["price_usd"] is not None:
                mine[d["kind"]].append(d["price_usd"])
        for kind, prices in mine.items():
            others = [d["price_usd"] for d in self.store.deliveries(kind=kind, limit=200)
                      if d["agent_id"] != agent_id and d["price_usd"] is not None]
            if len(others) < 2 or not statistics.median(others):
                continue
            ratio = statistics.median(prices) / statistics.median(others)
            if ratio >= 1.5:
                return ["#expensive"]
            if ratio <= 0.67:
                return ["#budget"]
        return []

    def record_failure(self, t: Thread, worker: str, weight: float = 2.0) -> None:
        self.store.update_reputation(worker, t.kind or "general", "worker", bad=weight, decay=self.market.decay, failed=1)

    # ------------------------------------------------------------- money
    def settle(self, t: Thread, worker: str | None) -> None:
        """Pay the agreed price on approval (simulated credits)."""
        price = t.routing.get("price_usd")
        if not worker or price is None or t.routing.get("paid"):
            return
        self.store.post_ledger([(t.creator, -price, f"paid for {t.subject[:80]}"),
                                (worker, price, f"earned for {t.subject[:80]}")], t.id)
        self.store.update_reputation(worker, t.kind or "general", "worker", earned_usd=price)
        t.routing["paid"] = price
        self.store.save_thread(t)

    # ----------------------------------------------------------- rankings
    def leaderboard(self, role: str = "worker") -> dict[str, list[dict[str, Any]]]:
        """Per type of work: agents ranked by rating (only agents with finished tasks of that type)."""
        names = {a.id: a for a in self.store.list_agents()}
        boards: dict[str, list[dict[str, Any]]] = {}
        for r in self.store.all_reputation():
            if r["role"] != role or r["agent_id"] not in names:
                continue
            if role == "worker" and r["tasks"] < 1:
                continue
            p = chance(r)
            boards.setdefault(r["kind"], []).append({
                "agent": r["agent_id"], "name": names[r["agent_id"]].name, "rating": stars(p), "chance": round(p, 3),
                "tasks": r["tasks"], "approved": r["approved"], "rejected": r["rejected"], "failed": r["failed"],
                "earned_usd": round(r["earned_usd"], 6), "new": r["tasks"] < self.market.probation_tasks})
        for rows in boards.values():
            rows.sort(key=lambda x: (-x["chance"], -x["tasks"], x["name"]))
            for i, row in enumerate(rows, 1):
                row["rank"] = i
        return boards

    def badges(self) -> dict[str, list[str]]:
        """'#1 coding', '#2 writing', ... for each agent, best rank first."""
        out: dict[str, list[tuple[int, str]]] = {}
        for kind, rows in self.leaderboard().items():
            for row in rows:
                out.setdefault(row["agent"], []).append((row["rank"], f"#{row['rank']} {label(kind)}"))
        for kind, rows in self.leaderboard("reviewer").items():
            for row in rows:
                if row["tasks"]:
                    out.setdefault(row["agent"], []).append((row["rank"], f"#{row['rank']} reviewing {label(kind)}"))
        ranked = {a: [b for _, b in sorted(v)] for a, v in out.items()}
        for a in self.store.list_agents():
            traits = self.traits(a.id)
            if traits:
                ranked[a.id] = ranked.get(a.id, []) + traits
        return ranked


__all__ = ["MarketConfig", "MarketMixin", "chance", "label", "stars"]
