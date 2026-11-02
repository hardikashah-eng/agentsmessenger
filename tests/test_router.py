"""Default routing (who gets the work) and verification (another agent checks it)."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentsmessenger import Bus, Client, MessengerError, Store
from agentsmessenger.agent import Agent, Result, Verdict
from agentsmessenger.models import utcnow
from agentsmessenger.router import RouterConfig, classify
from agentsmessenger.server import create_app

OBS = "obs_test"


@pytest.fixture
def env():
    def make(**router):
        bus = Bus(Store(), router=RouterConfig(**router))
        http = TestClient(create_app(bus, observer_token=OBS, tick_seconds=0))

        def agent(name, caps=(), vendor="test", cost=None):
            c = Client(http=http, token=OBS)
            c.register(name, list(caps), vendor=vendor, cost_hint_usd=cost)
            return c
        return bus, http, agent, Client(http=http, token=OBS)
    return make


def status(fn, *a, **kw):
    with pytest.raises(MessengerError) as e:
        fn(*a, **kw)
    return e.value.status


def events(client, tid):
    return [d["envelope"]["payload"].get("event") for d in client.messages(tid) if d["envelope"]["sender"] == "system"]


def routed(client):
    """Deliveries the router addressed to this agent."""
    return [d for d in client.inbox() if d.get("route")]


# ----------------------------------------------------------------- routing

def test_classify():
    assert classify(None, {}, "Fix the bug in parse_date() and add unit tests", None) == "code"
    assert classify(None, {}, "Summarize this report", None) == "summarize"
    assert classify("code.task", {}, None, None) == "code"
    assert classify(None, {"question": "what is the capital of peru"}, None, None) == "general"


def test_unaddressed_request_is_routed_by_type(env):
    bus, _, agent, _ = env()
    user = agent("user")
    agent("writer", ["text.write"])
    coder = agent("coder", ["code.task"])
    r = user.ask("Please refactor the payment module and fix the failing unit tests")
    t = bus.get_thread(r["thread_id"])
    assert t.kind == "code" and t.assigned_to == coder.me()["id"] and t.capability == "code.task"
    got = routed(coder)
    assert got and got[0]["route"] == {"kind": "work", "capability": "code.task"}
    assert "routed" in events(user, t.id)


def test_cost_and_track_record_drive_the_choice(env):
    bus, _, agent, _ = env()
    user = agent("user")
    cheap = agent("cheap-coder", ["code.task"], cost=0.01)
    pricey = agent("pricey-coder", ["code.task"], cost=0.50)
    assert bus.get_thread(user.request("code.task", {"x": 1})["thread_id"]).assigned_to == cheap.me()["id"]

    # A strong track record wins when the requester prefers quality.
    bus.store.bump_stats(pricey.me()["id"], approved=20)
    bus.store.bump_stats(cheap.me()["id"], approved=2, rejected=8)
    t = bus.get_thread(user.request("code.task", {"x": 2}, prefer="quality")["thread_id"])
    assert t.assigned_to == pricey.me()["id"]
    assert t.routing["candidates"][0]["track_record"].startswith("20 approved")
    # A budget too small for an agent rules it out.
    t2 = bus.get_thread(user.request("code.task", {"x": 3}, prefer="quality", budget_usd=0.05)["thread_id"])
    assert t2.assigned_to == cheap.me()["id"]


def test_only_the_assigned_agent_sees_and_claims(env):
    bus, _, agent, _ = env()
    user = agent("user")
    a, b = agent("a-coder", ["code.task"]), agent("b-coder", ["code.task"])
    tid = user.request("code.task", {"x": 1})["thread_id"]
    assigned = bus.get_thread(tid).assigned_to
    winner, loser = (a, b) if assigned == a.me()["id"] else (b, a)
    assert loser.inbox() == []
    assert status(loser.claim, tid) == 403
    assert winner.claim(tid)["owner"] == assigned


def test_reassigns_when_agent_does_not_respond(env):
    bus, _, agent, _ = env(claim_timeout_s=30)
    user = agent("user")
    a, b = agent("a-coder", ["code.task"]), agent("b-coder", ["code.task"])
    tid = user.request("code.task", {"x": 1})["thread_id"]
    first = bus.get_thread(tid).assigned_to
    assert bus.tick(utcnow() + timedelta(seconds=10)) == []
    assert bus.tick(utcnow() + timedelta(seconds=31)) == [tid]
    t = bus.get_thread(tid)
    second = b if first == a.me()["id"] else a
    assert t.assigned_to == second.me()["id"] and t.state.value == "open"
    copy = routed(second)[-1]
    assert copy["envelope"]["payload"] == {"x": 1} and copy["route"]["original"] == 1
    assert bus.store.stats(first)["timeouts"] == 1
    assert "rerouted" in events(user, tid)


def test_reroutes_on_failure_then_fails(env):
    bus, _, agent, _ = env()
    user = agent("user")
    a, b = agent("a-coder", ["code.task"]), agent("b-coder", ["code.task"])
    tid = user.request("code.task", {"x": 1})["thread_id"]
    by_id = {a.me()["id"]: a, b.me()["id"]: b}
    w1 = by_id[bus.get_thread(tid).assigned_to]
    w1.claim(tid)
    assert w1.set_state(tid, "failed", "crashed")["state"] == "open"  # rerouted, not failed
    w2 = by_id[bus.get_thread(tid).assigned_to]
    assert w2 is not w1
    w2.claim(tid)
    # Nobody else can do it: the thread fails honestly instead of waiting on a human.
    assert w2.set_state(tid, "failed", "also crashed")["state"] == "failed"
    assert bus.store.stats(w1.me()["id"])["failed"] == 1 and "rerouted" in events(user, tid)


def test_unclaimed_work_goes_to_human_when_nobody_is_left(env):
    bus, _, agent, _ = env(claim_timeout_s=10)
    user = agent("user")
    agent("only-coder", ["code.task"])
    tid = user.request("code.task", {"x": 1})["thread_id"]
    bus.tick(utcnow() + timedelta(seconds=11))
    t = bus.get_thread(tid)
    assert t.state.value == "waiting_human" and "escalated" in events(user, tid)


def test_no_capable_agent_and_routing_off(env):
    _, _, agent, _ = env()
    user = agent("user")
    assert status(user.ask, "Refactor this python code") == 404
    _, _, agent2, _ = env(enabled=False)
    assert status(agent2("u2").ask, "Refactor this python code") == 422


def test_route_preview(env):
    _, _, agent, _ = env()
    user = agent("user")
    agent("coder", ["code.task"], cost=0.02)
    p = user.route_preview("fix the bug in my python script")
    assert p["kind"] == "code" and p["candidates"][0]["name"] == "coder" and p["verification"] is True


# ------------------------------------------------------------ verification

def _work(bus, user, worker, payload=None, cap="code.task"):
    tid = user.request(cap, payload or {"task": "add(a, b)"})["thread_id"]
    assert bus.get_thread(tid).assigned_to == worker.me()["id"]
    worker.claim(tid)
    worker.set_state(tid, "working")
    req = next(d["envelope"] for d in worker.messages(tid) if d["envelope"]["intent"] == "request")
    return tid, req


def test_code_result_is_reviewed_by_independent_code_reviewer(env):
    bus, _, agent, _ = env()
    user = agent("user")
    coder = agent("coder", ["code.task"], vendor="openai")
    agent("same-vendor-reviewer", ["code.review"], vendor="openai")
    other = agent("other-reviewer", ["code.review"], vendor="anthropic")
    agent("generalist", ["verify"], vendor="google")
    tid, req = _work(bus, user, coder)

    coder.reply(req, "result", {"code": "def add(a, b): return a + b"})
    t = bus.get_thread(tid)
    assert t.state.value == "verifying" and t.verification["verifier"] == other.me()["id"]
    # The worker can't close its own work.
    assert coder.set_state(tid, "done")["state"] == "verifying"

    review = routed(other)[-1]
    assert review["route"]["kind"] == "verification" and review["route"]["capability"] == "code.review"
    assert review["envelope"]["payload"]["result"]["payload"] == {"code": "def add(a, b): return a + b"}
    other.review(review["envelope"], True, "correct and minimal")
    t = bus.get_thread(tid)
    assert t.state.value == "done" and t.verification["status"] == "approved"
    assert bus.store.stats(coder.me()["id"])["approved"] == 1 and bus.store.stats(other.me()["id"])["reviews"] == 1


def test_non_code_work_uses_verify_agents(env):
    bus, _, agent, _ = env()
    user = agent("user")
    writer = agent("writer", ["text.write"])
    agent("code-only-reviewer", ["code.review"])
    checker = agent("checker", ["verify"])
    tid, req = _work(bus, user, writer, {"brief": "write a tagline"}, cap="text.write")
    writer.reply(req, "result", {"draft": "Talk less, ship more."})
    assert bus.get_thread(tid).verification["verifier"] == checker.me()["id"]


def test_rejection_sends_it_back_then_escalates(env):
    bus, _, agent, _ = env(max_review_rounds=2)
    user = agent("user")
    coder = agent("coder", ["code.task"], vendor="openai")
    rev = agent("reviewer", ["code.review"], vendor="anthropic")
    tid, req = _work(bus, user, coder)

    coder.reply(req, "result", {"code": "def add(a, b): return a - b"})
    rev.review(routed(rev)[-1]["envelope"], False, "subtracts instead of adding", ["wrong operator"])
    t = bus.get_thread(tid)
    assert t.state.value == "working" and t.verification["status"] == "rejected"
    redo = routed(coder)[-1]
    assert redo["route"]["revision"]["feedback"] == "subtracts instead of adding"

    coder.reply(redo["envelope"], "result", {"code": "def add(a, b): return a - b  # still wrong"})
    assert bus.get_thread(tid).state.value == "verifying"
    rev.review(routed(rev)[-1]["envelope"], False, "still wrong")
    t = bus.get_thread(tid)
    assert t.state.value == "waiting_human" and len(t.verification["history"]) == 2
    assert bus.store.stats(coder.me()["id"])["rejected"] == 2


def test_human_reviews_when_no_reviewer_agent(env):
    bus, http, agent, _ = env()
    user = agent("user")
    coder = agent("coder", ["code.task"])
    tid, req = _work(bus, user, coder)
    coder.reply(req, "result", {"code": "print('hi')"})
    t = bus.get_thread(tid)
    assert t.state.value == "verifying" and t.verification["verifier"] == "human"
    # The human answers in plain words.
    http.post(f"/v1/observe/threads/{tid}/messages", json={"text": "approve: looks fine"},
              headers={"Authorization": f"Bearer {OBS}"})
    t = bus.get_thread(tid)
    assert t.state.value == "done" and t.verification["history"][-1]["notes"] == "looks fine"


def test_observer_verdict_and_assign_endpoints(env):
    bus, http, agent, _ = env()
    h = {"Authorization": f"Bearer {OBS}"}
    user = agent("user")
    a, b = agent("a-coder", ["code.task"]), agent("b-coder", ["code.task"])
    tid = user.request("code.task", {"x": 1})["thread_id"]
    target = b if bus.get_thread(tid).assigned_to == a.me()["id"] else a
    r = http.post(f"/v1/observe/threads/{tid}/assign", json={"agent": target.me()["name"]}, headers=h)
    assert r.status_code == 200 and r.json()["assigned_to"] == target.me()["id"]
    target.claim(tid)
    req = routed(target)[-1]["envelope"]
    target.reply(req, "result", {"ok": True})
    r = http.post(f"/v1/observe/threads/{tid}/verdict", json={"verdict": "reject", "notes": "needs tests"}, headers=h)
    assert r.json()["state"] == "working"
    assert routed(target)[-1]["route"]["revision"]["feedback"] == "needs tests"


def test_done_needs_a_result_and_reviewer_timeout(env):
    bus, _, agent, _ = env(verify_timeout_s=60)
    user = agent("user")
    coder = agent("coder", ["code.task"], vendor="x")
    r1, r2 = agent("rev-1", ["code.review"], vendor="y"), agent("rev-2", ["code.review"], vendor="z")
    tid, req = _work(bus, user, coder)
    assert status(coder.set_state, tid, "done") == 409  # nothing to verify yet
    coder.reply(req, "result", {"code": "x = 1"})
    first = bus.get_thread(tid).verification["verifier"]
    assert bus.tick(utcnow() + timedelta(seconds=61)) == [tid]
    second = bus.get_thread(tid).verification["verifier"]
    assert second != first and second in (r1.me()["id"], r2.me()["id"])
    assert bus.store.stats(first)["timeouts"] == 1


def test_verification_can_be_turned_off(env):
    bus, _, agent, _ = env(verify=False)
    user = agent("user")
    coder = agent("coder", ["code.task"])
    tid, req = _work(bus, user, coder)
    coder.reply(req, "result", {"code": "x = 1"})
    assert coder.set_state(tid, "done")["state"] == "done"


# ------------------------------------------------- framework, end to end

def test_framework_worker_and_reviewer_end_to_end(env, tmp_path, monkeypatch):
    monkeypatch.setattr("agentsmessenger.agent.CREDENTIALS", tmp_path / "agents.json")
    bus, http, agent, _ = env()
    user = agent("user")

    def framework_agent(name, caps, vendor):
        c = Client(http=http, token=OBS)
        c.register(name, caps, vendor=vendor)
        return Agent(c)

    coder = framework_agent("coder", ["code.task"], "openai")
    reviewer = framework_agent("reviewer", ["code.review"], "anthropic")
    attempts = []

    @coder.handles("code.task")
    def write(_payload, task):
        attempts.append(task.revision)
        # First try is wrong; the revision uses the reviewer's feedback.
        return Result({"code": "def add(a, b): return a + b" if task.revision else "def add(a, b): return a - b"})

    @reviewer.reviews("code.review")
    def check(payload, _task):
        ok = "a + b" in payload["result"]["payload"]["code"]
        return Verdict(ok, "correct" if ok else "uses - instead of +")

    stop = threading.Event()
    for a in (coder, reviewer):
        threading.Thread(target=lambda a=a: a.client.listen(a._dispatch, stop=stop.is_set, mode="poll", wait=0.2),  # pylint: disable=protected-access
                         daemon=True).start()
    tid = user.ask("Write a python function add(a, b)")["thread_id"]
    deadline = time.time() + 15
    while time.time() < deadline and bus.get_thread(tid).state.value != "done":
        time.sleep(0.1)
    stop.set()
    t = bus.get_thread(tid)
    assert t.state.value == "done", user.context(tid)["summary"]
    assert attempts[0] is None and attempts[1]["feedback"] == "uses - instead of +"
    assert [h["verdict"] for h in t.verification["history"]] == ["reject", "approve"]
    ctx = user.context(tid)
    assert ctx["latest_results"][-1]["payload"] == {"code": "def add(a, b): return a + b"}  # the work, not the verdict
    assert "Verification: approved after 2 review(s)" in ctx["summary"]
