"""Adding agents in plain language: cards, setup plans, the viewer endpoint, `amsg add`, generated bots."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

import argparse
import json
import runpy
import socket
import sys
import threading
import time
import types

import pytest
import uvicorn
from fastapi.testclient import TestClient

from agentsmessenger import Bus, Client, Store
from agentsmessenger import onboard, wizard
from agentsmessenger.server import create_app

OBS = "obs_test"
H = {"Authorization": f"Bearer {OBS}"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr("agentsmessenger.agent.CREDENTIALS", tmp_path / ".amsg" / "agents.json")
    return tmp_path


def test_card_maps_plain_skills():
    c = onboard.card("My Claude!", "claude-code", ["coding", "research"])
    assert c["name"] == "my-claude" and c["vendor"] == "anthropic"
    assert c["capabilities"] == ["code.task", "research"]  # prompted agents don't review by default
    bot = onboard.card("helper", "openai-bot", ["writing"])
    assert bot["capabilities"] == ["text.write", "verify"]  # always-on bots check others' work by default
    coder_bot = onboard.card("cb", "claude-bot", ["coding"])
    assert {"code.review", "verify"} <= set(coder_bot["capabilities"])
    with pytest.raises(ValueError):
        onboard.card("x", "claude-code", ["juggling"])


def test_setup_plans(home):
    kw = {"url": "http://127.0.0.1:8765", "name": "me", "token": "am_tok", "private_key": "pk", "caps": ["code.task"]}
    cc = onboard.setup("claude-code", **kw)
    assert cc["argv"][:4] == ["claude", "mcp", "add", "agents-messenger-me"] and "AMSG_TOKEN=am_tok" in cc["argv"]

    desk = onboard.setup("claude-desktop", **kw)
    path = home / "existing.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    desk["merge_json"]["path"] = str(path)
    onboard.apply(desk)
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and set(data["mcpServers"]) == {"other", "agents-messenger-me"}
    assert data["mcpServers"]["agents-messenger-me"]["env"]["AMSG_TOKEN"] == "am_tok"
    assert (home / "existing.json.bak").exists()

    codex = onboard.setup("codex", **kw)
    onboard.apply(codex)
    assert "[mcp_servers.agents-messenger-me]" in (home / ".codex" / "config.toml").read_text()


def test_viewer_onboard_endpoint():
    http = TestClient(create_app(Bus(Store()), observer_token=OBS, tick_seconds=0))
    assert "coding" in http.get("/v1/onboard/options").json()["skills"]
    r = http.post("/v1/observe/onboard", json={"name": "Desk Claude", "kind": "claude-desktop", "skills": ["writing"]})
    assert r.status_code == 401
    r = http.post("/v1/observe/onboard", headers=H,
                  json={"name": "Desk Claude", "kind": "claude-desktop", "skills": ["writing"], "cost_hint_usd": 0.02})
    body = r.json()
    assert r.status_code == 200 and body["agent"]["name"] == "desk-claude" and body["agent"]["public_key"]
    assert body["token"] in body["setup"]["snippet"]
    # The credentials work, and the agent must sign.
    me = Client(http=http, token=body["token"], signing_key=body["private_key"])
    assert me.me()["cost_hint_usd"] == 0.02
    assert http.post("/v1/observe/onboard", headers=H, json={"name": "x", "kind": "claude-code", "skills": ["nope"]}).status_code == 422
    a2a = http.post("/v1/observe/onboard", headers=H, json={"name": "far", "kind": "a2a", "skills": [],
                                                           "remote_url": "https://example.com/a2a"}).json()
    assert a2a["setup"]["command"].startswith("amsg a2a-proxy https://example.com/a2a")


@pytest.fixture
def live(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    bus = Bus(Store(str(tmp_path / "db.sqlite")))
    server = uvicorn.Server(uvicorn.Config(create_app(bus, observer_token=OBS, tick_seconds=0.5),
                                           host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            Client(url).find()
        except Exception:  # pylint: disable=broad-exception-caught  # not up yet
            time.sleep(0.05)
            continue
        break
    yield bus, url
    server.should_exit = True


def _args(url, **kw):
    base = {"kind": None, "name": None, "skill": None, "review": None, "cost": None, "remote": None,
            "yes": True, "print_only": False, "url": url, "observer_token": OBS}
    return argparse.Namespace(**{**base, **kw})


def test_amsg_add_writes_a_working_bot(home, live, monkeypatch, capsys):
    bus, url = live
    # `amsg add` for an always-on Claude bot that codes and reviews.
    assert wizard.run_add(_args(url, kind="claude-bot", name="Claude Helper", skill=["coding"], cost=0.03)) == 0
    out = capsys.readouterr().out
    assert "claude-helper joined" in out and "code.review" in out
    script = home / ".amsg" / "bots" / "claude-helper.py"
    assert script.exists()

    # A second, OpenAI-vendor coder, so the Claude bot is the independent reviewer.
    coder = Client(url, OBS)
    coder.register("gpt-coder", ["code.task"], vendor="openai", cost_hint_usd=0.01)

    # Run the generated script with a stand-in for the Anthropic SDK.
    fake = types.ModuleType("anthropic")

    class _Msgs:
        @staticmethod
        def create(**kw):
            text = "APPROVE\nadds correctly" if "checking another agent" in kw["messages"][0]["content"] else "ok"
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    fake.Anthropic = lambda: types.SimpleNamespace(messages=_Msgs())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    bot = runpy.run_path(str(script), run_name="bot")["agent"]
    stop = threading.Event()
    threading.Thread(target=lambda: bot.client.listen(bot._dispatch, stop=stop.is_set, mode="poll", wait=0.2),  # pylint: disable=protected-access
                     daemon=True).start()

    user = Client(url, OBS)
    user.register("user", [])
    tid = user.ask("Write a python function add(a, b)", prefer="cheapest")["thread_id"]
    t = bus.get_thread(tid)
    assert t.assigned_to == coder.me()["id"]  # cheaper coder gets the work
    coder.claim(tid)
    req = next(d["envelope"] for d in coder.inbox() if d.get("route"))
    coder.reply(req, "result", {"code": "def add(a, b): return a + b"})
    deadline = time.time() + 10
    while time.time() < deadline and bus.get_thread(tid).state.value != "done":
        time.sleep(0.1)
    stop.set()
    t = bus.get_thread(tid)
    assert t.state.value == "done" and t.verification["history"][-1]["notes"] == "adds correctly"
    assert t.verification["verifier"] == bot.me["id"]
