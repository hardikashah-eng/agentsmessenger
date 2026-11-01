"""A2A bridge, both directions.

Inbound: every registered agent is also reachable as an A2A agent.
    GET  /a2a/{agent}/.well-known/agent-card.json   (alias: agent.json)
    POST /a2a/{agent}                               JSON-RPC 2.0:
         message/send, tasks/get, tasks/cancel
  An A2A task is a messenger thread (task id = thread id). The caller
  authenticates with its own messenger agent token (Bearer).

Outbound: A2AProxy registers a remote A2A agent as a messenger agent.
  Requests routed to it are forwarded with message/send, polled with
  tasks/get, and the outcome is posted back as a `result` (or `ask_human`
  when the remote needs input).

Implements the core of the A2A JSON-RPC binding (protocol 0.3); streaming
(message/stream) and push notifications are not bridged yet.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .bus import Bus, Forbidden, NotFound
from .guards import GuardError
from .models import Address, AgentRecord, Envelope, EnvelopeIn, Intent, Thread, ThreadState, TERMINAL

A2A_PROTOCOL_VERSION = "0.3.0"

STATE_MAP = {
    ThreadState.open: "submitted",
    ThreadState.claimed: "submitted",
    ThreadState.working: "working",
    ThreadState.waiting_human: "input-required",
    ThreadState.paused: "working",
    ThreadState.done: "completed",
    ThreadState.failed: "failed",
}
FINAL_A2A = {"completed", "failed", "canceled", "rejected"}

# JSON-RPC / A2A error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, TASK_NOT_CANCELABLE = -32001, -32002


# ----------------------------------------------------------------- mapping

def parts_to_content(parts: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None, list[str]]:
    payload: dict[str, Any] = {}
    texts: list[str] = []
    artifacts: list[str] = []
    for p in parts or []:
        kind = p.get("kind") or p.get("type")
        if kind == "text":
            texts.append(str(p.get("text", "")))
        elif kind == "data" and isinstance(p.get("data"), dict):
            payload.update(p["data"])
        elif kind == "file":
            f = p.get("file") or {}
            if f.get("uri"):
                artifacts.append(f["uri"])
    return payload, ("\n".join(texts) or None), artifacts


def envelope_to_parts(e: Envelope) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if e.text:
        parts.append({"kind": "text", "text": e.text})
    if e.payload:
        parts.append({"kind": "data", "data": e.payload})
    for uri in e.artifacts:
        parts.append({"kind": "file", "file": {"uri": uri}})
    return parts or [{"kind": "text", "text": ""}]


def envelope_to_message(e: Envelope, t: Thread) -> dict[str, Any]:
    return {
        "kind": "message", "messageId": str(e.seq), "taskId": t.id, "contextId": t.id,
        "role": "user" if e.sender == t.creator else "agent",
        "parts": envelope_to_parts(e),
        "metadata": {"intent": e.intent.value, "sender": e.sender, "senderKind": e.sender_kind},
    }


def thread_to_task(bus: Bus, t: Thread, history: int = 20) -> dict[str, Any]:
    msgs = [m for m in bus.store.thread_messages(t.id, 0, 10_000) if m.sender_kind != "system"]
    state = "canceled" if t.blackboard.get("a2a_canceled") else STATE_MAP[t.state]
    status: dict[str, Any] = {"state": state, "timestamp": t.updated_at.isoformat()}
    if state in ("input-required", "failed") and msgs:
        status["message"] = envelope_to_message(msgs[-1], t)
    artifacts = [
        {"artifactId": f"seq-{m.seq}", "name": "result", "parts": envelope_to_parts(m)}
        for m in msgs if m.intent == Intent.result
    ]
    return {
        "kind": "task", "id": t.id, "contextId": t.id, "status": status, "artifacts": artifacts,
        "history": [envelope_to_message(m, t) for m in msgs[-history:]] if history else [],
        "metadata": {"messenger": {"state": t.state.value, "owner": t.owner, "spentUsd": t.spent_usd}},
    }


def agent_card(a: AgentRecord, base_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": a.name,
        "description": a.description or f"{a.name} on Agents Messenger",
        "url": f"{base_url.rstrip('/')}/a2a/{a.name}",
        "preferredTransport": "JSONRPC",
        "version": "0.1.0",
        "provider": {"organization": a.owner or "Agents Messenger", "url": base_url},
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {"id": c, "name": c, "description": f"{a.name} offers {c}", "tags": c.split(".")}
            for c in a.capabilities
        ] or [{"id": "general", "name": "general", "description": "Direct requests", "tags": []}],
    }


# ----------------------------------------------------------------- inbound

def mount_a2a(app: FastAPI, bus: Bus) -> None:

    def _card(request: Request, agent: str) -> dict[str, Any]:
        a = bus.store.get_agent(agent)
        if not a:
            raise HTTPException(404, "no such agent")
        return agent_card(a, str(request.base_url))

    @app.get("/a2a/{agent}/.well-known/agent-card.json", tags=["a2a"])
    def card(request: Request, agent: str) -> dict[str, Any]:
        return _card(request, agent)

    @app.get("/a2a/{agent}/.well-known/agent.json", tags=["a2a"], include_in_schema=False)
    def card_legacy(request: Request, agent: str) -> dict[str, Any]:
        return _card(request, agent)

    @app.post("/a2a/{agent}", tags=["a2a"])
    async def rpc(agent: str, request: Request, authorization: str | None = Header(None)) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return _err(None, PARSE_ERROR, "parse error")
        rid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return _err(rid, INVALID_REQUEST, "invalid JSON-RPC request")
        tok = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        try:
            caller = bus.authenticate(tok or "")
        except Forbidden:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "unauthorized"}},
                                status_code=401)
        target = bus.store.get_agent(agent)
        if not target:
            return _err(rid, INVALID_PARAMS, f"no agent {agent!r}")
        params = body.get("params") or {}
        try:
            method = body["method"]
            if method == "message/send":
                result = await _message_send(bus, caller, target, params)
            elif method == "tasks/get":
                result = _tasks_get(bus, caller, params)
            elif method == "tasks/cancel":
                result = _tasks_cancel(bus, caller, params)
            else:
                return _err(rid, METHOD_NOT_FOUND, f"method {method!r} not supported")
        except NotFound as e:
            return _err(rid, TASK_NOT_FOUND, str(e))
        except Forbidden as e:
            return _err(rid, -32000, str(e))
        except GuardError as e:
            return _err(rid, -32000, e.message, {"code": e.code})
        except (KeyError, TypeError, ValueError) as e:
            return _err(rid, INVALID_PARAMS, f"invalid params: {e}")
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": err})


async def _message_send(bus: Bus, caller: AgentRecord, target: AgentRecord, params: dict[str, Any]) -> dict[str, Any]:
    msg = params["message"]
    payload, text, artifacts = parts_to_content(msg.get("parts", []))
    cfg = params.get("configuration") or {}
    task_id = msg.get("taskId")
    if task_id:
        t = bus.get_thread(task_id)
        bus.send(caller, EnvelopeIn(thread_id=t.id, intent=Intent.inform, payload=payload, text=text,
                                    artifacts=artifacts, idempotency_key=msg.get("messageId")), transport="a2a")
    else:
        capability = (msg.get("metadata") or {}).get("skill")
        env = EnvelopeIn(
            to=Address(agent=target.name), intent=Intent.request, payload=payload, text=text, artifacts=artifacts,
            subject=(text or capability or "A2A request")[:120], idempotency_key=msg.get("messageId"),
        )
        stored = bus.send(caller, env, transport="a2a")
        task_id = stored.thread_id
    if cfg.get("blocking"):
        deadline = time.monotonic() + float(cfg.get("timeoutSeconds", 30))
        while time.monotonic() < deadline:
            t = bus.get_thread(task_id)
            if t.state in TERMINAL or t.state == ThreadState.waiting_human:
                break
            await asyncio.sleep(0.2)
    return thread_to_task(bus, bus.get_thread(task_id), int(cfg.get("historyLength", 20)))


def _participant(bus: Bus, caller: AgentRecord, task_id: str) -> Thread:
    t = bus.get_thread(task_id)
    if caller.id not in t.participants:
        raise NotFound(f"no task {task_id!r}")
    return t


def _tasks_get(bus: Bus, caller: AgentRecord, params: dict[str, Any]) -> dict[str, Any]:
    t = _participant(bus, caller, params["id"])
    return thread_to_task(bus, t, int(params.get("historyLength", 20)))


def _tasks_cancel(bus: Bus, caller: AgentRecord, params: dict[str, Any]) -> dict[str, Any]:
    t = _participant(bus, caller, params["id"])
    if t.state in TERMINAL:
        raise GuardError("not_cancelable", "task already finished")
    if caller.id != t.creator:
        raise Forbidden("only the task's creator can cancel it")
    bus.update_blackboard(caller, t.id, {"a2a_canceled": True})
    bus.set_state(caller, t.id, ThreadState.failed, "canceled via A2A")
    return thread_to_task(bus, bus.get_thread(t.id))


# ---------------------------------------------------------------- outbound

class A2AClient:
    """Minimal A2A JSON-RPC client."""

    def __init__(self, url: str, token: str | None = None, http: httpx.Client | None = None) -> None:
        self.url = url
        self._http = http or httpx.Client(timeout=60)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def card(self) -> dict[str, Any]:
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            r = self._http.get(self.url.rstrip("/") + path, headers=self._headers)
            if r.status_code == 200:
                return r.json()
        raise RuntimeError(f"no agent card at {self.url}")

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = self._http.post(self.url, headers=self._headers,
                            json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params})
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"A2A error {body['error'].get('code')}: {body['error'].get('message')}")
        return body["result"]

    def send(self, payload: dict[str, Any], text: str | None, task_id: str | None = None,
             blocking: bool = True, skill: str | None = None) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"kind": "text", "text": text})
        if payload:
            parts.append({"kind": "data", "data": payload})
        msg: dict[str, Any] = {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
                               "parts": parts or [{"kind": "text", "text": ""}]}
        if task_id:
            msg["taskId"] = task_id
        if skill:
            msg["metadata"] = {"skill": skill}
        return self.call("message/send", {"message": msg, "configuration": {"blocking": blocking}})

    def get(self, task_id: str) -> dict[str, Any]:
        return self.call("tasks/get", {"id": task_id})


def task_outcome(task: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """(a2a_state, payload, text) gathered from a task's artifacts or status message."""
    state = task["status"]["state"]
    payload: dict[str, Any] = {}
    texts: list[str] = []
    for art in task.get("artifacts") or []:
        p, t, _ = parts_to_content(art.get("parts", []))
        payload.update(p)
        if t:
            texts.append(t)
    if not texts and task["status"].get("message"):
        p, t, _ = parts_to_content(task["status"]["message"].get("parts", []))
        payload = payload or p
        if t:
            texts.append(t)
    return state, payload, ("\n".join(texts) or None)


class A2AProxy:
    """Runs a messenger agent that forwards its requests to a remote A2A agent."""

    def __init__(self, messenger: Any, remote: A2AClient, poll_seconds: float = 1.0, max_wait: float = 600) -> None:
        self.messenger = messenger  # agentsmessenger.Client authenticated as the proxy agent
        self.remote = remote
        self.poll_seconds = poll_seconds
        self.max_wait = max_wait

    def handle(self, delivery: dict[str, Any]) -> None:
        e = delivery["envelope"]
        if e["intent"] != "request" or e["sender_kind"] == "system":
            return
        tid = e["thread_id"]
        thread = self.messenger.thread(tid)
        if thread.get("owner") not in (None, self.messenger.me()["id"]):
            return
        try:
            self.messenger.claim(tid)
            self.messenger.set_state(tid, "working")
            task = self.remote.send(e.get("payload") or {}, e.get("text"), blocking=True,
                                    skill=(e.get("to") or {}).get("capability"))
            deadline = time.monotonic() + self.max_wait
            while task["status"]["state"] not in FINAL_A2A | {"input-required"} and time.monotonic() < deadline:
                time.sleep(self.poll_seconds)
                task = self.remote.get(task["id"])
            state, payload, text = task_outcome(task)
            meta = {"a2a_task_id": task["id"], "a2a_state": state}
            if state == "completed":
                self.messenger.reply(e, "result", {**payload, "_a2a": meta}, text=text)
                self.messenger.set_state(tid, "done")
            elif state == "input-required":
                self.messenger.reply(e, "ask_human", {**payload, "_a2a": meta},
                                     text=text or "The remote agent needs more input.")
            else:
                self.messenger.reply(e, "error", {**payload, "_a2a": meta}, text=text or f"remote task {state}")
                self.messenger.set_state(tid, "failed", f"remote task {state}")
        except Exception as ex:  # pylint: disable=broad-exception-caught  # report, don't crash the proxy
            try:
                self.messenger.reply(e, "error", {"error": str(ex)}, text=f"A2A proxy error: {ex}")
                self.messenger.set_state(tid, "failed", "proxy error")
            except Exception:  # pylint: disable=broad-exception-caught  # best effort while already failing
                pass

    def run(self, stop: Any = None) -> None:
        self.messenger.listen(self.handle, stop=stop or (lambda: False))
