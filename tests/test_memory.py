"""Memory scopes (private / shared / public), sharing by reference, deltas and token budgets."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

import pytest
from fastapi.testclient import TestClient

from agentsmessenger import Bus, Client, MessengerError, Store
from agentsmessenger.models import Envelope
from agentsmessenger.server import create_app
from agentsmessenger.signing import canonical_bytes, generate_keypair, verify_stored

OBS = "obs_test"


@pytest.fixture
def env():
    bus = Bus(Store())
    http = TestClient(create_app(bus, observer_token=OBS))

    def agent(name, caps=(), key=None):
        c = Client(http=http, token=OBS)
        priv, pub = key or (None, None)
        c.register(name, list(caps), public_key=pub)
        c.signing_key = priv
        return c
    return bus, http, agent


def status(fn, *a, **kw):
    with pytest.raises(MessengerError) as e:
        fn(*a, **kw)
    return e.value.status


def ids(items):
    return {i["id"] for i in items}


def test_private_shared_public_visibility(env):
    _, _, agent = env
    a, b, c = agent("alpha"), agent("beta"), agent("gamma")
    priv = a.remember("notes", {"todo": 1})
    shared = a.remember("plan", ["step 1", "step 2"], visibility="shared", shared_with=["beta"])
    pub = a.remember("style", "Write short sentences.", visibility="public")

    assert ids(a.memories()) == {priv["id"], shared["id"], pub["id"]}
    assert ids(b.memories()) == {shared["id"], pub["id"]}
    assert ids(c.memories()) == {pub["id"]}

    assert status(b.recall, priv["id"]) == 403
    assert status(c.recall, shared["id"]) == 403
    got = b.recall(shared["id"])
    assert got["value"] == ["step 1", "step 2"] and got["untrusted"] is True
    assert got["shared_with"] == []  # grant lists are only shown to the owner
    assert a.recall(shared["id"])["shared_with"]

    # Lists are stubs by default: no values, but a summary and a size.
    stub = next(i for i in b.memories() if i["id"] == shared["id"])
    assert stub["value"] is None and stub["summary"] == "list of 2 items" and stub["tokens"] > 0


def test_only_owner_writes_and_observer_sees_all(env):
    _, http, agent = env
    a, b = agent("alpha"), agent("beta")
    m = a.remember("k", 1, visibility="public")
    assert status(b.share, m["id"], visibility="private") == 403
    assert status(b.forget, m["id"]) == 403
    v2 = a.remember("k", 2, visibility="public")
    assert v2["id"] == m["id"] and v2["version"] == 2 and v2["value"] == 2

    priv = a.remember("secret", "x")
    obs = Client(http=http, token=OBS)
    assert priv["id"] in ids(obs.memories())
    assert obs.recall(priv["id"])["value"] == "x"
    obs.forget(priv["id"])
    assert status(a.recall, priv["id"]) == 404


def test_share_into_thread_by_reference(env):
    _, _, agent = env
    a, b, c = agent("alpha"), agent("beta", ["research"]), agent("gamma")
    req = a.request("research", {"topic": "batteries"})
    tid = req["thread_id"]
    b.claim(tid)

    big = {"findings": [{"title": f"finding {i}", "detail": "x" * 400} for i in range(10)]}
    m = a.remember("brief", big, visibility="shared", summary="10 findings on batteries")

    # Private memory can't be referenced.
    p = a.remember("mine", "private notes")
    assert status(a.send, "inform", thread_id=tid, memory_refs=[p["id"]]) == 422

    # Referencing a shared item shares it with the thread and delivers a stub, not the value.
    a.send("inform", thread_id=tid, text="see brief", memory_refs=[m["id"]])
    d = [x for x in b.inbox() if x["envelope"]["memory_refs"]][-1]
    assert d["memory"][0]["summary"] == "10 findings on batteries" and "value" not in d["memory"][0]
    assert d["memory"][0]["tokens"] > 1000
    # Only the part that's needed.
    assert b.recall(m["id"], path="findings.3.title")["value"] == "finding 3"
    assert status(b.recall, m["id"], path="findings.99") == 404
    # A non-participant still can't read it.
    assert status(c.recall, m["id"]) == 403

    style = a.remember("style", "Be brief.", visibility="public")
    a.send("inform", thread_id=tid, memory_refs=[style["id"]])
    ctx = b.context(tid)
    assert [s["id"] for s in ctx["memory"]] == [m["id"], style["id"]] and "Shared memory: 2" in ctx["summary"]

    # Revoking the thread grant takes it away again.
    a.share(m["id"], remove_threads=[tid])
    assert status(b.recall, m["id"]) == 403


def test_cannot_share_into_foreign_thread(env):
    _, _, agent = env
    a, b, _c = agent("alpha"), agent("beta"), agent("gamma")
    t = b.send("request", to_agent="gamma", payload={})["thread_id"]
    assert status(a.remember, "k", 1, visibility="shared", threads=[t]) == 403
    assert status(a.remember, "k", 1, visibility="private", shared_with=["beta"]) == 422


def test_since_returns_only_changes_with_tombstones(env):
    _, _, agent = env
    a, b = agent("alpha"), agent("beta")
    x = a.remember("x", 1, visibility="public")
    y = a.remember("y", 2, visibility="public")
    cursor = max(i["rev"] for i in b.memories())
    assert b.memories(since=cursor) == []
    a.remember("x", 10, visibility="public")
    a.forget(y["id"])
    changes = b.memories(since=cursor)
    assert [(c["id"], c["deleted"]) for c in changes] == [(x["id"], False), (y["id"], True)]


def test_include_values_respects_token_budget(env):
    _, _, agent = env
    a = agent("alpha")
    a.remember("small", "tiny")
    a.remember("large", "y" * 4000)
    items = {i["key"]: i for i in a.memories(include_values=True, max_tokens=100)}
    assert items["small"]["value"] == "tiny" and items["large"]["value"] is None


def test_memory_size_limit(env):
    _, _, agent = env
    a = agent("alpha")
    assert status(a.remember, "huge", "z" * 70_000) == 413


def test_context_pack_fits_token_budget(env):
    _, _, agent = env
    a, b = agent("alpha"), agent("beta")
    tid = a.send("request", to_agent="beta", payload={"q": "start"})["thread_id"]
    for i in range(12):
        b.send("inform", thread_id=tid, payload={"chunk": "data " * 300, "i": i})

    full = a.context(tid, recent=12)
    small = a.context(tid, recent=12, max_tokens=1500)
    assert full["tokens_estimate"] > 5000 and full["truncated"] is False
    assert small["tokens_estimate"] <= 1500 and small["truncated"] is True
    assert small["omitted"].get("clipped_payloads", 0) > 0
    assert small["open_requests"] and "Thread" in small["summary"]  # essentials stay
    clipped = [d for d in small["recent"] if d["clipped"]]
    assert all(d["envelope"]["payload"]["_clipped"] for d in clipped)


def test_signatures_unchanged_without_memory_refs(env):
    """v0.2 clients don't send memory_refs; their signatures must still verify."""
    _, http, agent = env
    key = generate_keypair()
    s = agent("signer", key=key)
    agent("beta")
    m = s.send("inform", to_agent="beta", payload={"x": 1})
    assert m["verified"] and b'"memory_refs"' not in canonical_bytes({"intent": "inform", "to": {"agent": "beta"}})

    ref = s.remember("doc", {"a": 1}, visibility="public")
    m2 = s.send("inform", thread_id=m["thread_id"], memory_refs=[ref["id"]])
    assert m2["verified"] is True
    server_pub = http.get("/v1/keys").json()["server"]["public_key"]
    assert verify_stored(Envelope(**m2), server_pub) and verify_stored(Envelope(**m), server_pub)
