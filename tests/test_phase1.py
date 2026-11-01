"""Phase 1: live updates (SSE), signed envelopes, A2A bridge, agent framework."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import agentsmessenger.agent as agent_mod
from agentsmessenger import Bus, Client, MessengerError, Store
from agentsmessenger.a2a import A2AClient, A2AProxy
from agentsmessenger.agent import Agent, Result
from agentsmessenger.models import Envelope
from agentsmessenger.server import create_app
from agentsmessenger.signing import generate_keypair, verify_stored

OBS = "obs_test"
H = {"Authorization": f"Bearer {OBS}"}


@pytest.fixture
def app_client():
    bus = Bus(Store())
    http = TestClient(create_app(bus, observer_token=OBS))

    def agent(name, caps=(), key=None):
        c = Client(http=http, token=OBS)
        priv, pub = key or (None, None)
        c.register(name, list(caps), public_key=pub)
        c.signing_key = priv
        return c
    return bus, http, agent


# ------------------------------------------------------------------ SSE

def test_agent_stream_delivers(app_client):
    _, _, agent = app_client
    a, b = agent("alpha"), agent("beta")
    m = a.send("request", to_agent="beta", payload={"n": 1})
    got = list(b.events(after=0, timeout=5, max_events=1))
    assert got[0]["envelope"]["seq"] == m["seq"] and got[0]["untrusted"] is True


def test_observer_stream_and_auth(app_client):
    _, http, agent = app_client
    a, _b = agent("alpha"), agent("beta")
    a.send("request", to_agent="beta", payload={})
    with http.stream("GET", f"/v1/observe/stream?token={OBS}&max_events=1&timeout=5") as r:
        body = "".join(r.iter_text())
    assert "event: envelope" in body and "id: 1" in body
    assert http.get("/v1/observe/stream?token=wrong").status_code == 401


# ----------------------------------------------------------- signatures

def test_signed_envelopes(app_client):
    bus, http, agent = app_client
    key = generate_keypair()
    signer = agent("signer", key=key)
    agent("beta")

    m = signer.send("inform", to_agent="beta", payload={"x": 1}, text="hello")
    assert m["verified"] is True and m["server_sig"]

    unsigned = Client(http=http, token=signer.token)
    with pytest.raises(MessengerError) as e:
        unsigned.send("inform", to_agent="beta", payload={})
    assert e.value.status == 401

    wrong = Client(http=http, token=signer.token, signing_key=generate_keypair()[0])
    with pytest.raises(MessengerError) as e:
        wrong.send("inform", to_agent="beta", payload={})
    assert e.value.status == 401

    # Anyone can verify stored envelopes offline with the server's public key.
    server_pub = http.get("/v1/keys").json()["server"]["public_key"]
    env = Envelope(**m)
    assert verify_stored(env, server_pub)
    env.payload = {"x": 2}
    assert not verify_stored(env, server_pub)

    # A token holder can't swap the agent's key.
    with pytest.raises(MessengerError) as e:
        signer.update_card("signer", [], public_key=generate_keypair()[1])
    assert e.value.status == 403


def test_require_signatures():
    http = TestClient(create_app(Bus(Store(), require_signatures=True), observer_token=OBS))
    assert http.post("/v1/agents", json={"name": "nokey"}, headers=H).status_code == 422
    pub = generate_keypair()[1]
    assert http.post("/v1/agents", json={"name": "haskey", "public_key": pub}, headers=H).status_code == 201


# --------------------------------------------------------------- A2A in

def _rpc(http, agent, token, method, params):
    r = http.post(f"/a2a/{agent}", headers={"Authorization": f"Bearer {token}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return r


def test_a2a_inbound(app_client):
    _, http, agent = app_client
    worker = agent("worker", ["math.add"])
    caller = agent("a2a-caller")

    card = http.get("/a2a/worker/.well-known/agent-card.json").json()
    assert card["url"].endswith("/a2a/worker") and card["skills"][0]["id"] == "math.add"

    msg = {"kind": "message", "role": "user", "messageId": "m1",
           "parts": [{"kind": "text", "text": "add these"}, {"kind": "data", "data": {"a": 2, "b": 3}}]}
    task = _rpc(http, "worker", caller.token, "message/send", {"message": msg}).json()["result"]
    assert task["kind"] == "task" and task["status"]["state"] == "submitted"

    req = worker.inbox()[0]["envelope"]
    assert req["payload"] == {"a": 2, "b": 3} and req["text"] == "add these"
    worker.claim(req["thread_id"])
    worker.set_state(req["thread_id"], "working")
    worker.reply(req, "result", {"sum": 5}, text="5")
    worker.set_state(req["thread_id"], "done")

    done = _rpc(http, "worker", caller.token, "tasks/get", {"id": task["id"]}).json()["result"]
    assert done["status"]["state"] == "completed"
    assert {"kind": "data", "data": {"sum": 5}} in done["artifacts"][0]["parts"]

    # Same messageId is idempotent.
    again = _rpc(http, "worker", caller.token, "message/send", {"message": msg}).json()["result"]
    assert again["id"] == task["id"]

    msg2 = {**msg, "messageId": "m2"}
    t2 = _rpc(http, "worker", caller.token, "message/send", {"message": msg2}).json()["result"]
    canceled = _rpc(http, "worker", caller.token, "tasks/cancel", {"id": t2["id"]}).json()["result"]
    assert canceled["status"]["state"] == "canceled"

    assert _rpc(http, "worker", caller.token, "tasks/nope", {}).json()["error"]["code"] == -32601
    assert _rpc(http, "worker", "bad", "tasks/get", {"id": "x"}).status_code == 401


def test_a2a_rejects_signing_agents(app_client):
    _, http, agent = app_client
    agent("worker")
    signer = agent("signer", key=generate_keypair())
    msg = {"kind": "message", "role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "hi"}]}
    assert "error" in _rpc(http, "worker", signer.token, "message/send", {"message": msg}).json()


# ------------------------------------------------- end to end, live server

@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "CREDENTIALS", tmp_path / "agents.json")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Bus(Store(str(tmp_path / "db.sqlite"))), observer_token=OBS)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    th.join(5)


def _wait(client, tid, states=("done", "failed"), timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.thread(tid)
        if t["state"] in states:
            return t
        time.sleep(0.1)
    raise AssertionError(f"thread stuck in {client.thread(tid)['state']}")


def test_end_to_end_agents_a2a_and_human(live):
    # A signed worker built with the Agent framework, served over SSE.
    worker = Agent.connect("adder", ["math.add"], url=live, observer_token=OBS)

    @worker.handles("math.add")
    def add(payload, task):
        task.progress("adding")
        return Result({"sum": payload["a"] + payload["b"]}, cost_usd=0.001)

    worker.handles()(add)  # also handle direct requests (e.g. arriving over A2A)

    @worker.handles("payment.pay")
    def pay(payload, task):
        if not task.confirm(f"Pay {payload['amount']}?", amount=payload["amount"]):
            raise RuntimeError("declined")
        return {"paid": payload["amount"]}

    threading.Thread(target=worker.run, daemon=True).start()

    # 1. Capability request from a signed planner.
    planner = Agent.connect("planner", [], url=live, observer_token=OBS).client
    req = planner.request("math.add", {"a": 2, "b": 3})
    assert req["verified"] is True
    _wait(planner, req["thread_id"])
    assert planner.context(req["thread_id"])["latest_results"][0]["payload"] == {"sum": 5}

    # 2. A remote A2A agent proxied onto the bus (here the remote is the adder's own A2A endpoint).
    caller = Client(live, OBS)
    caller.register("a2a-caller", [])
    proxy = Client(live, OBS)
    proxy.register("remote-adder", ["remote.add"], vendor="a2a", protocols=["a2a"])
    remote = A2AClient(f"{live}/a2a/adder", caller.token)
    assert remote.card()["name"] == "adder"
    stop = threading.Event()
    threading.Thread(target=A2AProxy(proxy, remote, poll_seconds=0.2).run, args=(stop.is_set,), daemon=True).start()

    r2 = planner.request("remote.add", {"a": 10, "b": 5})
    t2 = _wait(planner, r2["thread_id"])
    assert t2["state"] == "done", planner.context(r2["thread_id"])["summary"]
    res = planner.context(r2["thread_id"])["latest_results"][0]["payload"]
    assert res["sum"] == 15 and res["_a2a"]["a2a_state"] == "completed"

    # 3. Human-in-the-loop: the agent waits for the observer.
    r3 = planner.request("payment.pay", {"amount": 84})
    _wait(planner, r3["thread_id"], states=("waiting_human",))
    httpx.post(f"{live}/v1/observe/threads/{r3['thread_id']}/messages", json={"text": "yes"}, headers=H)
    _wait(planner, r3["thread_id"])
    assert planner.context(r3["thread_id"])["latest_results"][0]["payload"] == {"paid": 84}

    stop.set()
    worker.stop()
