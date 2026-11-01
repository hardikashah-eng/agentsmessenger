"""A small framework for writing messenger agents.

    from agentsmessenger.agent import Agent

    agent = Agent.connect("summarizer", ["text.summarize"], vendor="local")

    @agent.handles("text.summarize")
    def summarize(payload, task):
        task.progress("reading")
        return {"summary": payload["text"][:100]}

    agent.run()

The framework claims each request, moves the thread to working, runs the
handler in a worker thread, posts the result (or error), and closes the
thread. Handlers can call task.ask_human(...) to wait for the observer.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import Client, MessengerError
from .signing import generate_keypair

CREDENTIALS = Path(os.environ.get("AMSG_CREDENTIALS", Path.home() / ".amsg" / "agents.json"))


@dataclass
class Result:
    payload: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    cost_usd: float = 0.0


class HumanDeclined(Exception):
    pass


class Task:
    def __init__(self, agent: "Agent", envelope: dict[str, Any]) -> None:
        self.agent = agent
        self.envelope = envelope
        self.thread_id: str = envelope["thread_id"]
        self.payload: dict[str, Any] = envelope.get("payload") or {}
        self.text: str | None = envelope.get("text")
        self.capability: str | None = (envelope.get("to") or {}).get("capability")

    @property
    def client(self) -> Client:
        return self.agent.client

    def progress(self, text: str, **payload: Any) -> None:
        self.client.reply(self.envelope, "inform", payload, text=text)

    def note(self, **patch: Any) -> None:
        """Write shared state on the thread's blackboard."""
        self.client.blackboard(self.thread_id, **patch)

    def ask_human(self, question: str, timeout: float = 3600, **payload: Any) -> str:
        """Ask the observer and block until they answer. Returns their text."""
        asked = self.client.reply(self.envelope, "ask_human", payload, text=question)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for d in self.client.messages(self.thread_id, after=asked["seq"]):
                e = d["envelope"]
                if e["sender_kind"] == "human":
                    return e.get("text") or json.dumps(e.get("payload") or {})
            time.sleep(1)
        raise TimeoutError("no answer from the human observer")

    def confirm(self, question: str, timeout: float = 3600, **payload: Any) -> bool:
        answer = self.ask_human(question + " (yes/no)", timeout, **payload).strip().lower()
        return answer.startswith(("y", "approve", "ok", "go"))


Handler = Callable[[dict[str, Any], Task], "dict[str, Any] | Result | str | None"]


class Agent:
    def __init__(self, client: Client, max_workers: int = 4) -> None:
        self.client = client
        self.handlers: dict[str, Handler] = {}
        self.default: Handler | None = None
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._me: dict[str, Any] | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------- connecting
    @classmethod
    def connect(cls, name: str, capabilities: list[str] | None = None, *, url: str | None = None,
                observer_token: str | None = None, sign: bool = True, **card: Any) -> "Agent":
        """Reuse saved credentials for `name`, or register it (needs the observer
        token, or open registration). Credentials are kept in ~/.amsg/agents.json."""
        url = url or os.environ.get("AMSG_URL", "http://127.0.0.1:8765")
        observer_token = observer_token or os.environ.get("AMSG_OBSERVER_TOKEN")
        creds = _load_creds()
        key = f"{url}|{name}"
        saved = creds.get(key)
        if saved:
            c = Client(url, saved["token"], signing_key=saved.get("private_key"))
            try:
                c.me()
                c.update_card(name, capabilities or [], public_key=saved.get("public_key"), **card)
                return cls(c)
            except MessengerError:
                pass  # server was reset; register again
        priv, pub = generate_keypair() if sign else (None, None)
        c = Client(url, observer_token)
        out = c.register(name, capabilities or [], public_key=pub, **card)
        c.signing_key = priv
        creds[key] = {"token": out["token"], "private_key": priv, "public_key": pub}
        _save_creds(creds)
        return cls(c)

    # ----------------------------------------------------------- handlers
    def handles(self, capability: str | None = None) -> Callable[[Handler], Handler]:
        """Register a handler for a capability; with no capability, the default
        handler for direct requests."""
        def deco(fn: Handler) -> Handler:
            if capability is None:
                self.default = fn
            else:
                self.handlers[capability] = fn
            return fn
        return deco

    @property
    def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = self.client.me()
        return self._me

    # -------------------------------------------------------------- running
    def _dispatch(self, delivery: dict[str, Any]) -> None:
        e = delivery["envelope"]
        if e["intent"] != "request" or e["sender"] == self.me["id"]:
            return
        if e.get("in_reply_to") is not None:
            return  # follow-up requests inside a thread are left to handlers via task.client
        cap = (e.get("to") or {}).get("capability")
        fn = self.handlers.get(cap) if cap else self.default
        if fn is None:
            return
        self._pool.submit(self._run, fn, e)

    def _run(self, fn: Handler, e: dict[str, Any]) -> None:
        tid = e["thread_id"]
        try:
            self.client.claim(tid)
        except MessengerError:
            return  # another agent claimed it first
        task = Task(self, e)
        try:
            self.client.set_state(tid, "working")
            out = fn(task.payload, task)
            res = out if isinstance(out, Result) else Result(text=out) if isinstance(out, str) else Result(payload=out or {})
            self.client.reply(e, "result", res.payload, text=res.text, cost_usd=res.cost_usd)
            self.client.set_state(tid, "done")
        except HumanDeclined as ex:
            self._fail(e, f"declined by human: {ex}")
        except Exception as ex:
            self._fail(e, f"{type(ex).__name__}: {ex}")

    def _fail(self, e: dict[str, Any], reason: str) -> None:
        try:
            self.client.reply(e, "error", {"error": reason}, text=reason)
            self.client.set_state(e["thread_id"], "failed", reason[:200])
        except MessengerError:
            pass

    def sync_capabilities(self) -> None:
        """Advertise every capability that has a handler."""
        me = self.client.me()
        caps = sorted(set(me["capabilities"]) | set(self.handlers))
        if caps != me["capabilities"]:
            card = {k: me[k] for k in ("description", "protocols", "vendor", "owner", "cost_hint_usd", "public_key", "a2a_url")}
            self._me = self.client.update_card(me["name"], caps, **card)

    def run(self, mode: str = "sse") -> None:
        """Serve forever (Ctrl-C to stop)."""
        self.sync_capabilities()
        print(f"[{self.me['name']}] serving {sorted(self.handlers) or ['direct requests']}", flush=True)
        try:
            self.client.listen(self._dispatch, stop=self._stop.is_set, mode=mode)
        except KeyboardInterrupt:
            pass
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def stop(self) -> None:
        self._stop.set()


def _load_creds() -> dict[str, Any]:
    try:
        return json.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}


def _save_creds(creds: dict[str, Any]) -> None:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.write_text(json.dumps(creds, indent=2))
    try:
        CREDENTIALS.chmod(0o600)
    except OSError:
        pass
