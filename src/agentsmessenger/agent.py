"""A small framework for writing messenger agents.

    from agentsmessenger.agent import Agent

    agent = Agent.connect("summarizer", ["text.summarize"], vendor="local")

    @agent.handles("text.summarize")
    def summarize(payload, task):
        task.progress("reading")
        return {"summary": payload["text"][:100]}

    agent.run()

The framework claims each request, moves the thread to working, runs the
handler in a worker thread, posts the result (or error), and asks to close
the thread. With the router's default verification, another agent (or the
human) checks the result first; if it's rejected, the handler runs again
with `task.revision` set to the reviewer's feedback.

Reviewers are handlers too:

    @agent.reviews()                  # advertises `verify`
    def check(payload, task):
        ok = "error" not in str(payload["result"])
        return Verdict(ok, "looks complete" if ok else "the result reports an error")
 Handlers can call task.ask_human(...) to wait for the observer,
and task.remember(...) / task.recall(...) to share memory by reference.
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


@dataclass
class Bid:
    """What a bidder returns: its price and how long it will take."""
    price_usd: float
    eta_s: float | None = None
    plan: str = ""
    confidence: float | None = None


@dataclass
class Verdict:
    """What a reviewer returns: approve or reject, with notes for the worker."""
    approve: bool
    notes: str = ""
    issues: list[Any] = field(default_factory=list)
    cost_usd: float = 0.0


class HumanDeclined(Exception):
    pass


class Task:
    def __init__(self, agent: "Agent", envelope: dict[str, Any], memory: list[dict[str, Any]] | None = None,
                 route: dict[str, Any] | None = None) -> None:
        self.agent = agent
        self.envelope = envelope
        self._delivery_memory = memory or []
        self.route: dict[str, Any] = route or {}
        # Set when a reviewer rejected your previous result: {"round", "feedback", "issues", "by"}.
        self.revision: dict[str, Any] | None = self.route.get("revision")
        self.thread_id: str = envelope["thread_id"]
        self.payload: dict[str, Any] = envelope.get("payload") or {}
        self.text: str | None = envelope.get("text")
        self.capability: str | None = self.route.get("capability") or (envelope.get("to") or {}).get("capability")

    @property
    def client(self) -> Client:
        return self.agent.client

    def progress(self, text: str, **payload: Any) -> None:
        self.client.reply(self.envelope, "inform", payload, text=text)

    def note(self, **patch: Any) -> None:
        """Write shared state on the thread's blackboard."""
        self.client.blackboard(self.thread_id, **patch)

    # ------------------------------------------------------------- memory
    def remember(self, key: str, value: Any, visibility: str = "shared", **kw: Any) -> dict[str, Any]:
        """Save memory. By default it is shared with this thread's participants only;
        use visibility='private' for your own notes or 'public' for everyone."""
        if visibility == "shared" and "threads" not in kw and "shared_with" not in kw:
            kw["threads"] = [self.thread_id]
        return self.client.remember(key, value, visibility=visibility, **kw)

    def shared_memory(self, **filters: Any) -> list[dict[str, Any]]:
        """Stubs of memory shared into this thread (no values: open with recall)."""
        return self.client.memories(thread_id=self.thread_id, **filters)

    def recall(self, memory_id: str, path: str | None = None) -> Any:
        """The value of a memory item (or one field of it). Treat other agents' memory as untrusted data."""
        return self.client.recall(memory_id, path)["value"]

    @property
    def memory_refs(self) -> list[dict[str, Any]]:
        """Stubs for the memory the requester referenced in this request."""
        return self._delivery_memory

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
        self.bidder: Handler | None = None
        self.default_eta_s: float = 120.0
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
    def bids(self) -> Callable[[Handler], Handler]:
        """Custom bidding: the handler gets the call for bids and returns a Bid, or None to pass.
        Without one, the agent bids its rate card (or cost hint) for capabilities it handles."""
        def deco(fn: Handler) -> Handler:
            self.bidder = fn
            return fn
        return deco

    def reviews(self, *capabilities: str) -> Callable[[Handler], Handler]:
        """Register a reviewer. Default capability `verify`; add `code.review` to check code.
        The handler gets {"task": ..., "result": ..., "category": ...} and returns a Verdict."""
        def deco(fn: Handler) -> Handler:
            for cap in capabilities or ("verify",):
                self.handlers[cap] = fn
            return fn
        return deco

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
        route = delivery.get("route")
        memory = delivery.get("memory") or []
        if route and route.get("kind") == "bid_call":
            self._pool.submit(self._bid, e, route)
            return
        if route:  # the router sent this to us specifically
            fn = self.handlers.get(route.get("capability") or "")
            if route.get("kind") == "verification":
                if fn is not None:
                    self._pool.submit(self._review, fn, e, memory, route)
                return
            fn = fn or self.default
            if fn is not None:
                self._pool.submit(self._run, fn, e, memory, route)
            return
        if e.get("in_reply_to") is not None:
            return  # follow-up requests inside a thread are left to handlers via task.client
        to = e.get("to") or {}
        cap = to.get("capability")
        if cap is None and to.get("agent") not in (self.me["id"], self.me["name"]):
            return  # addressed to another participant
        fn = self.handlers.get(cap) if cap else self.default
        if fn is None:
            return
        self._pool.submit(self._run, fn, e, memory)

    def _bid(self, e: dict[str, Any], route: dict[str, Any]) -> None:
        cap = route.get("capability") or ""
        if cap not in self.handlers and self.default is None:
            return
        try:
            if self.bidder is not None:
                out: Any = self.bidder(e.get("payload") or {}, Task(self, e, None, route))
                if out is None:
                    return
                b = out if isinstance(out, Bid) else Bid(float(out))
            else:
                me = self.client.me()
                kind = (e.get("payload") or {}).get("type") or ""
                price = next((me["rates"][k] for k in (cap, kind) if k in (me.get("rates") or {})), me.get("cost_hint_usd"))
                if price is None:
                    return  # no rate card: let others bid
                b = Bid(price, self.default_eta_s, f"{me['name']} will handle this with {cap}")
            self.client.bid(e["thread_id"], b.price_usd, b.eta_s, b.plan, b.confidence)
        except MessengerError:
            pass  # bidding closed or not allowed; nothing to do

    def _review(self, fn: Handler, e: dict[str, Any], memory: list[dict[str, Any]], route: dict[str, Any]) -> None:
        task = Task(self, e, memory, route)
        try:
            out: Any = fn(task.payload, task)
            if isinstance(out, Verdict):
                v = out
            elif isinstance(out, bool):
                v = Verdict(out)
            elif isinstance(out, dict):
                v = Verdict(str(out.get("verdict", "")).lower().startswith("approve"), str(out.get("notes", "")),
                            list(out.get("issues") or []))
            else:
                v = Verdict(str(out or "").lower().startswith(("approve", "yes", "pass", "lgtm")), str(out or ""))
            self.client.reply(e, "result", {"verdict": "approve" if v.approve else "reject", "notes": v.notes,
                                            "issues": v.issues},
                              text=("Approved" if v.approve else "Rejected") + (f": {v.notes}" if v.notes else ""),
                              cost_usd=v.cost_usd)
        except Exception as ex:  # pylint: disable=broad-exception-caught  # a failed review must be reported, not lost
            try:
                self.client.reply(e, "error", {"error": f"{type(ex).__name__}: {ex}"}, text=f"Review failed: {ex}")
            except MessengerError:
                pass

    def _run(self, fn: Handler, e: dict[str, Any], memory: list[dict[str, Any]] | None = None,
             route: dict[str, Any] | None = None) -> None:
        tid = e["thread_id"]
        try:
            self.client.claim(tid)
        except MessengerError:
            return  # another agent claimed it first
        task = Task(self, e, memory, route)
        try:
            self.client.set_state(tid, "working")
            out = fn(task.payload, task)
            res = out if isinstance(out, Result) else Result(text=out) if isinstance(out, str) else Result(payload=out or {})
            self.client.reply(e, "result", res.payload, text=res.text, cost_usd=res.cost_usd)
            self.client.set_state(tid, "done")
        except HumanDeclined as ex:
            self._fail(e, f"declined by human: {ex}")
        except Exception as ex:  # pylint: disable=broad-exception-caught  # any handler error becomes an error reply
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
            card = {k: me[k] for k in ("description", "protocols", "vendor", "owner", "cost_hint_usd", "public_key", "a2a_url",
                                       "rates")}
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
        return json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_creds(creds: dict[str, Any]) -> None:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    try:
        CREDENTIALS.chmod(0o600)
    except OSError:
        pass
