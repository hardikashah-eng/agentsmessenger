"""The work marketplace: rate cards, sealed bids, value-based awards, reputation, pay on approval, ranks."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentsmessenger import Bus, Client, MessengerError, Store
from agentsmessenger.agent import Agent, Result, Verdict
from agentsmessenger.market import MarketConfig
from agentsmessenger.models import utcnow
from agentsmessenger.router import RouterConfig
from agentsmessenger.server import create_app

OBS = "obs_test"
H = {"Authorization": f"Bearer {OBS}"}


@pytest.fixture
def env():
    def make(router=None, **market):
        bus = Bus(Store(), router=RouterConfig(**(router or {})), market=MarketConfig(**{"explore_rate": 0, **market}))
        http = TestClient(create_app(bus, observer_token=OBS, tick_seconds=0))

        def agent(name, caps=(), vendor="test", owner="", rates=None, cost=None):
            c = Client(http=http, token=OBS)
            c.register(name, list(caps), vendor=vendor, owner=owner, rates=rates or {}, cost_hint_usd=cost)
            c.me()  # seen now: counts as online
            return c
        return bus, http, agent
    return make


def status(fn, *a, **kw):
    with pytest.raises(MessengerError) as e:
        fn(*a, **kw)
    return e.value.status


def routed(client, kind=None):
    return [d for d in client.inbox() if d.get("route") and (kind is None or d["route"]["kind"] == kind)]


def rep(bus, client, kind="code", role="worker"):
    return bus.store.reputation(client.me()["id"], kind, role)


def chance(r):
    return r["alpha"] / (r["alpha"] + r["beta"])


# -------------------------------------------------------------- awarding

def test_instant_award_uses_rate_cards_and_value(env):
    bus, _, agent = env()
    user = agent("user")
    newbie = agent("newbie", ["code.task"], rates={"code.task": 0.01})
    pro = agent("pro", ["code.task"], rates={"code": 0.03})  # rate card by type of work
    bus.store.update_reputation(pro.me()["id"], "code", "worker", good=18, tasks=18, approved=18)

    # Balanced: the cheap newcomer is better value ($0.01 / 0.5 < $0.03 / 0.95).
    t = bus.get_thread(user.request("code.task", {"x": 1})["thread_id"])
    assert t.assigned_to == newbie.me()["id"] and t.routing["price_usd"] == 0.01
    # Quality: the proven agent wins despite costing 3x.
    t = bus.get_thread(user.request("code.task", {"x": 2}, prefer="quality")["thread_id"])
    assert t.assigned_to == pro.me()["id"] and t.routing["price_usd"] == 0.03
    assert "★" in user.messages(t.id)[1]["envelope"]["text"]


def test_sealed_auction_awards_best_value_not_lowest_price(env):
    bus, _, agent = env()
    user = agent("user")
    cheap_bad = agent("cheap-bad", ["code.task"])
    solid = agent("solid", ["code.task"])
    middle = agent("middle", ["code.task"])
    outsider = agent("writer", ["text.write"])
    bus.store.update_reputation(cheap_bad.me()["id"], "code", "worker", good=1, bad=9, tasks=10)
    bus.store.update_reputation(solid.me()["id"], "code", "worker", good=9, tasks=9)

    tid = user.request("code.task", {"task": "parse dates"}, bidding="auction")["thread_id"]
    t = bus.get_thread(tid)
    assert t.market["status"] == "open" and len(t.market["invited"]) == 3 and t.assigned_to is None
    call = routed(solid, "bid_call")[0]
    assert call["envelope"]["payload"]["task"]["payload"] == {"task": "parse dates"}
    assert status(solid.claim, tid) == 409                    # bid, don't claim
    assert status(outsider.bid, tid, 0.001) == 403            # not invited

    cheap_bad.bid(tid, 0.01, eta_s=60, plan="quick fix")
    assert list(cheap_bad.thread(tid)["market"]["bids"]) == [cheap_bad.me()["id"]]  # sealed
    middle.bid(tid, 0.015)
    assert bus.get_thread(tid).market["status"] == "open"
    solid.bid(tid, 0.02, eta_s=90)                            # last invited bid closes it early

    t = bus.get_thread(tid)
    # values: cheap-bad 0.01/0.17=0.06, middle 0.015/0.5=0.03, solid 0.02/0.91=0.022
    assert t.market["status"] == "awarded" and t.assigned_to == solid.me()["id"]
    assert t.routing["price_usd"] == 0.02 and t.routing["eta_s"] == 90
    assert routed(solid, "work")
    assert any(d["envelope"]["payload"].get("event") == "not_awarded" for d in cheap_bad.inbox())
    seen = {c["name"]: c["price_usd"] for c in cheap_bad.thread(tid)["routing"]["candidates"]}
    assert seen["solid"] == 0.02 and seen["middle"] is None    # losers' prices stay sealed
    assert status(middle.bid, tid, 0.001) == 409              # closed
    assert status(cheap_bad.claim, tid) == 403                # assigned to solid


def test_bid_rules_and_window(env):
    bus, _, agent = env(auction_window_s=10)
    user = agent("user")
    a, b = agent("a", ["code.task"], rates={"code.task": 0.02}), agent("b", ["code.task"], rates={"code.task": 0.04})
    tid = user.request("code.task", {"x": 1}, bidding="auction", budget_usd=0.05)["thread_id"]
    assert status(a.bid, tid, 0.10) == 422                    # over budget
    b.bid(tid, 0.04)
    assert bus.tick(utcnow() + timedelta(seconds=5)) == []
    assert tid in bus.tick(utcnow() + timedelta(seconds=11))   # window over: award with the bids in
    assert bus.get_thread(tid).assigned_to == b.me()["id"]

    # Nobody bids: fall back to the invited agents' rate cards.
    tid2 = user.request("code.task", {"x": 2}, bidding="auction")["thread_id"]
    bus.tick(utcnow() + timedelta(seconds=11))
    t2 = bus.get_thread(tid2)
    assert t2.assigned_to == a.me()["id"] and t2.market["award"]["source"].startswith("rate cards")


def test_newcomers_get_a_chance(env):
    bus, _, agent = env(explore_rate=1.0)
    user = agent("user")
    vet = agent("vet", ["code.task"], rates={"code.task": 0.01})
    agent("rookie", ["code.task"], rates={"code.task": 0.05})
    bus.store.update_reputation(vet.me()["id"], "code", "worker", good=10, tasks=10)
    t = bus.get_thread(user.request("code.task", {"x": 1})["thread_id"])
    assert t.routing["candidates"][0]["name"] == "rookie" and t.routing["candidates"][0]["explored"]


# ------------------------------------------------------------ reputation

def _deliver(bus, user, worker, cap="code.task", payload=None):
    tid = user.request(cap, payload or {"x": 1})["thread_id"]
    assert bus.get_thread(tid).assigned_to == worker.me()["id"]
    worker.claim(tid)
    req = routed(worker, "work")[-1]["envelope"]
    worker.reply(req, "result", {"code": "x = 1"})
    return tid


def test_reviews_move_ratings_and_humans_count_more(env):
    bus, http, agent = env()
    user = agent("user")
    w1 = agent("w1", ["code.task"], owner="alice")
    rev = agent("rev", ["code.review"], owner="bob")
    _deliver(bus, user, w1)
    rev.review(routed(rev, "verification")[-1]["envelope"], True, "good")
    after_agent = chance(rep(bus, w1))
    assert after_agent > 0.5 and rep(bus, w1)["approved"] == 1

    # The same approval from a human counts 3x as much.
    http.post(f"/v1/observe/agents/{rev.me()['id']}/pause", headers=H)
    bus.store.set_agent_paused(w1.me()["id"], True)
    w2 = agent("w2", ["code.task"], owner="carol")
    tid = _deliver(bus, user, w2)
    assert bus.get_thread(tid).verification["verifier"] == "human"
    http.post(f"/v1/observe/threads/{tid}/verdict", json={"verdict": "approve"}, headers=H)
    assert chance(rep(bus, w2)) > after_agent

    # Failing after winning hurts more than one rejection.
    w3 = agent("w3", ["code.task"], owner="dave")
    bus.store.set_agent_paused(w2.me()["id"], True)
    tid3 = user.request("code.task", {"x": 3})["thread_id"]
    w3.claim(tid3)
    assert w3.set_state(tid3, "failed", "crashed")["state"] == "failed"
    assert chance(rep(bus, w3)) == pytest.approx(1 / 4) and rep(bus, w3)["failed"] == 1


def test_reviewers_are_rated_against_humans(env):
    bus, http, agent = env(router={"max_review_rounds": 2})
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice")
    picky = agent("picky", ["code.review"], owner="bob")
    tid = _deliver(bus, user, coder)
    picky.review(routed(picky, "verification")[-1]["envelope"], False, "nope")
    coder.reply(routed(coder, "work")[-1]["envelope"], "result", {"code": "x = 2"})
    picky.review(routed(picky, "verification")[-1]["envelope"], False, "still nope")
    assert bus.get_thread(tid).state.value == "waiting_human"
    http.post(f"/v1/observe/threads/{tid}/verdict", json={"verdict": "approve", "notes": "it's fine"}, headers=H)
    assert bus.get_thread(tid).state.value == "done"
    assert chance(rep(bus, picky, role="reviewer")) < 0.5      # the human disagreed with it


def test_owners_agents_dont_review_each_other(env):
    bus, _, agent = env()
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice", vendor="openai")
    agent("alices-reviewer", ["code.review"], owner="alice", vendor="anthropic")
    bobs = agent("bobs-reviewer", ["code.review"], owner="bob", vendor="openai")
    tid = _deliver(bus, user, coder)
    assert bus.get_thread(tid).verification["verifier"] == bobs.me()["id"]


# --------------------------------------------------------- money and ranks

def test_pay_on_approval_only(env):
    bus, _, agent = env()
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice", rates={"code.task": 0.02})
    rev = agent("rev", ["code.review"], owner="bob")
    tid = _deliver(bus, user, coder)
    assert coder.ledger()["balance_usd"] == 0                 # nothing until it passes
    rev.review(routed(rev, "verification")[-1]["envelope"], True)
    assert coder.ledger()["balance_usd"] == 0.02 and user.ledger()["balance_usd"] == -0.02
    assert bus.get_thread(tid).routing["paid"] == 0.02 and rep(bus, coder)["earned_usd"] == 0.02

    tid2 = user.request("code.task", {"x": 2})["thread_id"]
    coder.claim(tid2)
    coder.set_state(tid2, "failed", "gave up")
    assert coder.ledger()["balance_usd"] == 0.02              # no pay for failed work


def test_leaderboard_ranks_and_badges(env):
    bus, http, agent = env()
    a, b, w = agent("ace", ["code.task"]), agent("bee", ["code.task"]), agent("wordsmith", ["text.write"])
    bus.store.update_reputation(a.me()["id"], "code", "worker", good=9, tasks=9, approved=9)
    bus.store.update_reputation(b.me()["id"], "code", "worker", good=3, bad=2, tasks=5, approved=3, rejected=2)
    bus.store.update_reputation(w.me()["id"], "writing", "worker", good=4, tasks=4, approved=4)
    board = http.get("/v1/leaderboard", headers=H).json()
    assert [r["name"] for r in board["boards"]["code"]] == ["ace", "bee"]
    assert board["badges"][a.me()["id"]] == ["#1 coding"] and board["badges"][b.me()["id"]] == ["#2 coding"]
    stats = {s["name"]: s for s in http.get("/v1/agents/stats", headers=H).json()}
    assert stats["wordsmith"]["badges"] == ["#1 writing"] and stats["ace"]["ratings"]["code"]["rank"] == 1


# --------------------------------------------------- framework, end to end

def test_framework_agents_bid_work_and_get_paid(env, tmp_path, monkeypatch):
    monkeypatch.setattr("agentsmessenger.agent.CREDENTIALS", tmp_path / "agents.json")
    bus, http, agent = env()
    user = agent("user")

    def framework_agent(name, caps, owner, rates=None):
        c = Client(http=http, token=OBS)
        c.register(name, caps, owner=owner, rates=rates or {})
        return Agent(c)

    fast = framework_agent("fast-coder", ["code.task"], "alice", {"code.task": 0.02})
    slow = framework_agent("slow-coder", ["code.task"], "carol", {"code.task": 0.03})
    reviewer = framework_agent("reviewer", ["code.review"], "bob")
    for a in (fast, slow):
        a.handles("code.task")(lambda _p, task: Result({"code": "def f(): return 1"}))
    reviewer.reviews("code.review")(lambda p, _t: Verdict("return" in p["result"]["payload"]["code"], "ok"))

    stop = threading.Event()
    for a in (fast, slow, reviewer):
        threading.Thread(target=lambda a=a: a.client.listen(a._dispatch, stop=stop.is_set, mode="poll", wait=0.2),  # pylint: disable=protected-access
                         daemon=True).start()
    tid = user.ask("Write a python function f", bidding="auction")["thread_id"]
    deadline = time.time() + 15
    while time.time() < deadline and bus.get_thread(tid).state.value != "done":
        time.sleep(0.1)
    stop.set()
    t = bus.get_thread(tid)
    assert t.state.value == "done", user.context(tid)["summary"]
    assert len(t.market["bids"]) == 2 and t.market["award"]["agent"] == fast.client.me()["id"]
    assert fast.client.ledger()["balance_usd"] == 0.02 and slow.client.ledger()["balance_usd"] == 0
    assert "Paid $0.020 on approval" in user.context(tid)["summary"]


def test_concurrent_bids_are_not_lost(env):
    bus, _, agent = env(max_invites=8)
    user = agent("user")
    bidders = [agent(f"coder-{i}", ["code.task"]) for i in range(8)]
    tid = user.request("code.task", {"x": 1}, bidding="auction")["thread_id"]
    records = {c.me()["id"]: bus.store.get_agent(c.me()["id"]) for c in bidders}
    start = threading.Barrier(len(records))

    def go(rec, price):
        start.wait()
        bus.submit_bid(rec, tid, price)
    threads = [threading.Thread(target=go, args=(r, 0.01 + i / 1000)) for i, r in enumerate(records.values())]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t = bus.get_thread(tid)
    assert len(t.market["bids"]) == 8 and t.market["status"] == "awarded"


def test_right_first_time_beats_approved_after_revision(env):
    bus, _, agent = env()
    user = agent("user")
    first = agent("first-time", ["code.task"], owner="alice")
    rev = agent("rev", ["code.review"], owner="bob")
    _deliver(bus, user, first)
    rev.review(routed(rev, "verification")[-1]["envelope"], True)

    bus.store.set_agent_paused(first.me()["id"], True)
    redo = agent("needs-redo", ["code.task"], owner="carol")
    _deliver(bus, user, redo)
    rev.review(routed(rev, "verification")[-1]["envelope"], False, "bug")
    redo.reply(routed(redo, "work")[-1]["envelope"], "result", {"code": "x = 2"})
    rev.review(routed(rev, "verification")[-1]["envelope"], True)
    assert chance(rep(bus, redo)) < 0.5 < chance(rep(bus, first))


def test_open_requests_ignore_router_copies(env):
    bus, _, agent = env()
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice")
    rev = agent("rev", ["code.review"], owner="bob")
    tid = _deliver(bus, user, coder)
    rev.review(routed(rev, "verification")[-1]["envelope"], True)
    ctx = user.context(tid)
    assert ctx["thread"]["state"] == "done" and ctx["open_requests"] == [] and "Open requests: 0" in ctx["summary"]


def test_readers_never_see_half_routed_work(env):
    """A routed request and its routing note become visible together (regression: an agent
    polling between the two got work without a route and ignored it)."""
    bus, _, agent = env()
    user = agent("user")
    coder = agent("coder", ["code.task"])
    rec = bus.store.get_agent(coder.me()["id"])
    seen, readers = [], []
    original = bus.store.append

    def spy(*a, **kw):
        stored = original(*a, **kw)
        th = threading.Thread(target=lambda: seen.extend(bus.inbox(rec)))  # reads while the send is mid-way
        th.start()
        readers.append(th)
        return stored
    bus.store.append = spy
    try:
        user.ask("Fix the bug in parse_date() and add unit tests")
    finally:
        bus.store.append = original
    for th in readers:
        th.join(5)
    work = [d for d in seen if d.envelope.intent.value == "request" and d.envelope.sender_kind == "agent"]
    assert work and all(d.route and d.route["kind"] == "work" for d in work)
