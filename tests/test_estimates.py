"""Bids carry a wall-clock ETA and a token estimate; delivered work is checked against them; behaviour badges."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentsmessenger import Bus, Client, MessengerError, Store
from agentsmessenger.agent import Agent, Result
from agentsmessenger.market import MarketConfig
from agentsmessenger.models import utcnow
from agentsmessenger.server import create_app

OBS = "obs_test"
H = {"Authorization": f"Bearer {OBS}"}


@pytest.fixture
def env():
    bus = Bus(Store(), market=MarketConfig(explore_rate=0))
    http = TestClient(create_app(bus, observer_token=OBS, tick_seconds=0))

    def agent(name, caps=(), owner="", **card):
        c = Client(http=http, token=OBS)
        c.register(name, list(caps), owner=owner, **card)
        c.me()
        return c
    return bus, http, agent


def status(fn, *a, **kw):
    with pytest.raises(MessengerError) as e:
        fn(*a, **kw)
    return e.value.status


def routed(client, kind):
    return [d for d in client.inbox() if d.get("route") and d["route"]["kind"] == kind]


def system_texts(client, tid, event):
    return [d["envelope"]["text"] for d in client.messages(tid)
            if d["envelope"]["sender"] == "system" and d["envelope"]["payload"].get("event") == event]


def _auction_job(bus, user, coder, reviewer, *, eta_s=120, tokens_est=2000, price=0.02, took_s=None,
                 tokens_used=1800, cost=0.018):
    tid = user.request("code.task", {"task": "add tests"}, bidding="auction")["thread_id"]
    coder.bid(tid, price, eta_s=eta_s, tokens_est=tokens_est)  # only invited bidder: closes at once
    coder.claim(tid)
    if took_s is not None:  # pretend the work started `took_s` seconds ago
        t = bus.get_thread(tid)
        t.routing["assigned_at"] = (utcnow() - timedelta(seconds=took_s)).isoformat()
        bus.store.save_thread(t)
    work = routed(coder, "work")[-1]["envelope"]
    coder.send("result", thread_id=tid, in_reply_to=work["seq"], payload={"code": "ok"}, cost_usd=cost,
               tokens_used=tokens_used)
    reviewer.review(routed(reviewer, "verification")[-1]["envelope"], True)
    return tid


def test_bid_with_wall_clock_eta_and_tokens(env):
    bus, _, agent = env
    user = agent("user")
    coder = agent("coder", ["code.task"])
    agent("other", ["code.task"])
    tid = user.request("code.task", {"x": 1}, bidding="auction")["thread_id"]
    done_by = (utcnow() + timedelta(minutes=10)).isoformat()
    assert status(coder.bid, tid, 0.02, eta_at=done_by.replace("+00:00", "")) == 422   # no timezone
    assert status(coder.bid, tid, 0.02, eta_at=(utcnow() - timedelta(minutes=1)).isoformat()) == 422  # past
    coder.bid(tid, 0.02, eta_at=done_by, tokens_est=3000)
    bid = bus.get_thread(tid).market["bids"][coder.me()["id"]]
    assert 590 < bid["eta_s"] <= 600 and bid["tokens_est"] == 3000 and bid["eta_at"][:16] == done_by[:16]

    bus.tick(utcnow() + timedelta(seconds=20))  # window ends; coder is the only bidder
    t = bus.get_thread(tid)
    assert t.routing["tokens_est"] == 3000 and t.routing["due_at"] and t.routing["eta_s"] == bid["eta_s"]
    award = system_texts(user, tid, "awarded")[0]
    assert "due " in award and "3,000 tokens" in award


def test_instant_award_uses_card_estimates(env):
    bus, _, agent = env
    user = agent("user")
    agent("coder", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=60, tokens_hint=1500)
    t = bus.get_thread(user.request("code.task", {"x": 1})["thread_id"])
    assert t.routing["eta_s"] == 60 and t.routing["tokens_est"] == 1500 and t.routing["due_at"]


def test_on_time_delivery_is_checked_against_estimate(env):
    bus, _, agent = env
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice")
    rev = agent("rev", ["code.review"], owner="bob")
    tid = _auction_job(bus, user, coder, rev, took_s=100)  # 100s against a 120s ETA
    t = bus.get_thread(tid)
    a = t.routing["actuals"]
    assert a["on_time"] == 1 and a["tokens_used"] == 1800 and a["tokens_est"] == 2000 and a["cost_usd"] == 0.018
    check = system_texts(user, tid, "estimate_check")[0]
    assert "on time" in check and "1,800 tokens vs 2,000 estimated (-10%)" in check and "$0.018 vs price $0.020" in check
    assert bus.store.deliveries(coder.me()["id"])[0]["thread_id"] == tid


def test_late_and_token_heavy_delivery_costs_reputation(env):
    bus, _, agent = env
    user = agent("user")
    punctual = agent("punctual", ["code.task"], owner="alice")
    slow = agent("slow", ["code.task"], owner="carol")
    rev = agent("rev", ["code.review"], owner="bob")
    bus.store.set_agent_paused(slow.me()["id"], True)
    _auction_job(bus, user, punctual, rev, took_s=100)
    bus.store.set_agent_paused(slow.me()["id"], False)
    bus.store.set_agent_paused(punctual.me()["id"], True)
    tid = _auction_job(bus, user, slow, rev, took_s=1000, tokens_used=5000)
    check = system_texts(user, tid, "estimate_check")[0]
    assert "(late)" in check and "+150%" in check

    def chance(c):
        r = bus.store.reputation(c.me()["id"], "code", "worker")
        return r["alpha"] / (r["alpha"] + r["beta"])
    assert chance(slow) < chance(punctual)


def test_behaviour_badges(env):
    bus, http, agent = env
    pricey, cheap, mid = agent("pricey", ["code.task"]), agent("cheap", ["code.task"]), agent("mid", ["code.task"])

    def deliver(c, n, price, on_time, est, used, cost=None):
        for i in range(n):
            bus.store.record_delivery({"thread_id": f"{c.me()['name']}-{i}", "agent_id": c.me()["id"], "kind": "code",
                                       "at": utcnow().isoformat(), "price_usd": price, "cost_usd": cost or price,
                                       "eta_s": 100, "took_s": 90 if on_time else 200, "tokens_est": est,
                                       "tokens_used": used, "on_time": int(on_time)})
    deliver(pricey, 3, 0.05, True, 2000, 2100)
    deliver(cheap, 3, 0.01, False, 2000, 4000, cost=0.02)
    deliver(mid, 3, 0.025, True, 2000, 2000)  # close to the typical price: no price badge
    assert bus.traits(pricey.me()["id"]) == ["#on-time", "#accurate-estimates", "#expensive"]
    assert bus.traits(cheap.me()["id"]) == ["#late", "#token-heavy", "#over-budget", "#budget"]
    stats = {s["name"]: s for s in http.get("/v1/agents/stats", headers=H).json()}
    assert "#late" in stats["cheap"]["badges"] and stats["mid"]["traits"] == ["#on-time", "#accurate-estimates"]
    assert len(stats["cheap"]["recent_deliveries"]) == 3
    assert "#expensive" in http.get("/v1/leaderboard", headers=H).json()["badges"][pricey.me()["id"]]


def test_framework_bids_estimates_and_reports_tokens(env, tmp_path, monkeypatch):
    monkeypatch.setattr("agentsmessenger.agent.CREDENTIALS", tmp_path / "agents.json")
    bus, http, _agent = env
    c = Client(http=http, token=OBS)
    c.register("bot", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=90, tokens_hint=2500)
    bot = Agent(c)
    bot.handles("code.task")(lambda _p, _t: Result({"code": "ok"}, cost_usd=0.01, tokens_used=2400))
    user = Client(http=http, token=OBS)
    user.register("user", [])
    tid = user.request("code.task", {"x": 1}, bidding="auction")["thread_id"]
    call = routed(c, "bid_call")[0]
    bot._bid(call["envelope"], call["route"])  # pylint: disable=protected-access
    t = bus.get_thread(tid)
    assert t.market["bids"][c.me()["id"]]["tokens_est"] == 2500 and t.routing["eta_s"] == 90
    work = routed(c, "work")[0]
    bot._run(bot.handlers["code.task"], work["envelope"], [], work["route"])  # pylint: disable=protected-access
    result = [d["envelope"] for d in user.messages(tid) if d["envelope"]["intent"] == "result"][-1]
    assert result["tokens_used"] == 2400 and result["verified"] is False


def _history(bus, c, n, eta_s, took_s, kind="code", tokens=(None, None)):
    for i in range(n):
        bus.store.record_delivery({"thread_id": f"h-{c.me()['name']}-{i}", "agent_id": c.me()["id"], "kind": kind,
                                   "at": utcnow().isoformat(), "price_usd": 0.02, "cost_usd": 0.02, "eta_s": eta_s,
                                   "took_s": took_s, "tokens_est": tokens[0], "tokens_used": tokens[1],
                                   "on_time": int(took_s <= 1.1 * eta_s)})


def test_shorter_eta_wins_and_padding_does_not_pay(env):
    bus, _, agent = env
    user = agent("user")
    quick = agent("quick", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=60)
    agent("padder", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=600)
    t = bus.get_thread(user.request("code.task", {"x": 1})["thread_id"])
    assert t.assigned_to == quick.me()["id"]  # same price and rating: the faster promise wins


def test_eta_is_corrected_by_track_record(env):
    bus, _, agent = env
    user = agent("user")
    overpromiser = agent("overpromiser", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=30)
    honest = agent("honest", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=60)
    _history(bus, overpromiser, 4, eta_s=30, took_s=120)   # usually takes 4x its ETA (capped at 3x)
    _history(bus, honest, 4, eta_s=60, took_s=60)
    t = bus.get_thread(user.request("code.task", {"x": 1})["thread_id"])
    assert t.assigned_to == honest.me()["id"]
    over = next(c for c in t.routing["candidates"] if c["name"] == "overpromiser")
    assert over["eta_slip"] == 3.0 and over["credible_eta_s"] == 90.0


def test_missing_eta_counts_as_slower_and_fastest_preference(env):
    bus, _, agent = env
    user = agent("user")
    agent("no-eta", ["code.task"], rates={"code.task": 0.02})
    typed = agent("with-eta", ["code.task"], rates={"code.task": 0.02}, eta_hint_s=120)
    assert bus.get_thread(user.request("code.task", {"x": 1})["thread_id"]).assigned_to == typed.me()["id"]

    # 12x faster but 3x the price: balanced won't pay that much for speed; fastest will.
    fast = agent("fast-pricey", ["code.task"], rates={"code.task": 0.06}, eta_hint_s=10)
    assert bus.get_thread(user.request("code.task", {"x": 2})["thread_id"]).assigned_to != fast.me()["id"]
    assert bus.get_thread(user.request("code.task", {"x": 3}, prefer="fastest")["thread_id"]).assigned_to == fast.me()["id"]


def test_padded_estimates_are_flagged_both_ways(env):
    bus, _, agent = env
    user = agent("user")
    coder = agent("coder", ["code.task"], owner="alice")
    rev = agent("rev", ["code.review"], owner="bob")
    tid = _auction_job(bus, user, coder, rev, eta_s=600, took_s=20)
    assert "early: the ETA was 30x too long" in system_texts(user, tid, "estimate_check")[0]

    slowpoke, hoarder = agent("slowpoke", ["code.task"]), agent("hoarder", ["code.task"])
    _history(bus, slowpoke, 3, eta_s=600, took_s=30)                  # always "on time", by a mile
    _history(bus, hoarder, 3, eta_s=100, took_s=90, tokens=(9000, 1000))  # tokens far over-estimated
    assert bus.traits(slowpoke.me()["id"]) == ["#pads-estimates"]     # not "#on-time"
    assert "#pads-estimates" in bus.traits(hoarder.me()["id"]) and "#on-time" not in bus.traits(hoarder.me()["id"])
