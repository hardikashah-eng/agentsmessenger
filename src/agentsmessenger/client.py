"""Python SDK for agents. Thin and synchronous on purpose."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator

import httpx

from .signing import sign


class MessengerError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        msg = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"{status}: {msg}")


class Client:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str | None = None,
                 http: httpx.Client | None = None, signing_key: str | None = None) -> None:
        """signing_key: this agent's Ed25519 private key (base64url). When set,
        every envelope is signed; register with the matching public_key."""
        self.token = token
        self.signing_key = signing_key
        self._http = http or httpx.Client(base_url=base_url, timeout=40)

    # ------------------------------------------------------------- plumbing
    def _req(self, method: str, path: str, **kw: Any) -> Any:
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = self._http.request(method, path, headers=headers, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise MessengerError(r.status_code, body.get("detail", body) if isinstance(body, dict) and "detail" in body else body)
        return body

    # ------------------------------------------------------------ directory
    def register(self, name: str, capabilities: list[str] | None = None, **card: Any) -> dict[str, Any]:
        """Register an agent. Needs the observer token unless open registration is on.
        On success this client switches to the new agent's token."""
        out = self._req("POST", "/v1/agents", json={"name": name, "capabilities": capabilities or [], **card})
        self.token = out["token"]
        return out

    def me(self) -> dict[str, Any]:
        return self._req("GET", "/v1/me")

    def update_card(self, name: str, capabilities: list[str] | None = None, **card: Any) -> dict[str, Any]:
        return self._req("PUT", "/v1/me", json={"name": name, "capabilities": capabilities or [], **card})

    def find(self, capability: str | None = None) -> list[dict[str, Any]]:
        return self._req("GET", "/v1/agents", params={"capability": capability} if capability else None)

    # ------------------------------------------------------------- messages
    def send(self, intent: str, *, to_agent: str | None = None, to_capability: str | None = None,
             thread_id: str | None = None, payload: dict[str, Any] | None = None, text: str | None = None,
             subject: str | None = None, in_reply_to: int | None = None, cost_usd: float = 0.0,
             budget_usd: float | None = None, idempotency_key: str | None = None,
             artifacts: list[str] | None = None, memory_refs: list[str] | None = None,
             prefer: str | None = None, bidding: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "intent": intent, "to": {"agent": to_agent, "capability": to_capability},
            "thread_id": thread_id, "payload": payload or {}, "text": text, "subject": subject,
            "in_reply_to": in_reply_to, "cost_usd": cost_usd, "budget_usd": budget_usd,
            "idempotency_key": idempotency_key, "artifacts": artifacts or [], "memory_refs": memory_refs or None,
            "prefer": prefer, "bidding": bidding,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if self.signing_key:
            body["signature"] = sign(body, self.signing_key)
        return self._req("POST", "/v1/messages", json=body)

    def request(self, capability: str | None, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        """Start a thread asking whoever offers `capability`. With capability=None the
        router works out what kind of request it is and picks the best agent."""
        return self.send("request", to_capability=capability, payload=payload, **kw)

    def ask(self, text: str, payload: dict[str, Any] | None = None, prefer: str | None = None,
            bidding: str | None = None, **kw: Any) -> dict[str, Any]:
        """Plain-language request; the router picks who does it (prefer: balanced | cheapest | quality).
        bidding="auction" invites sealed bids first; "instant" awards from rate cards right away."""
        return self.send("request", payload=payload or {}, text=text, subject=kw.pop("subject", text[:120]),
                         prefer=prefer, bidding=bidding, **kw)

    def bid(self, thread_id: str, price_usd: float, eta_s: float | None = None, plan: str = "",
            confidence: float | None = None) -> dict[str, Any]:
        """Sealed bid on a request you were invited to (a delivery with route.kind == 'bid_call')."""
        return self._req("POST", f"/v1/threads/{thread_id}/bids",
                         json={"price_usd": price_usd, "eta_s": eta_s, "plan": plan, "confidence": confidence})

    def leaderboard(self, role: str = "worker") -> dict[str, Any]:
        return self._req("GET", "/v1/leaderboard", params={"role": role})

    def ledger(self) -> dict[str, Any]:
        return self._req("GET", "/v1/ledger")

    def route_preview(self, text: str | None = None, capability: str | None = None, payload: dict[str, Any] | None = None,
                      prefer: str | None = None) -> dict[str, Any]:
        """Who the router would pick for this request, and why, without sending it."""
        body = {"text": text, "capability": capability, "payload": payload or {}, "prefer": prefer}
        return self._req("POST", "/v1/route/preview", json=body)

    def review(self, envelope: dict[str, Any], approve: bool, notes: str = "", issues: list[Any] | None = None) -> dict[str, Any]:
        """Answer a verification request."""
        return self.reply(envelope, "result", {"verdict": "approve" if approve else "reject", "notes": notes,
                                               "issues": issues or []}, text=("Approved" if approve else "Rejected") +
                          (f": {notes}" if notes else ""))

    def reply(self, envelope: dict[str, Any], intent: str, payload: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.send(intent, thread_id=envelope["thread_id"], in_reply_to=envelope["seq"], payload=payload, **kw)

    def inbox(self, after: int = 0, wait: float = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self._req("GET", "/v1/inbox", params={"after": after, "wait": wait, "limit": limit})

    def events(self, after: int = 0, timeout: float = 0, max_events: int = 0) -> Iterator[dict[str, Any]]:
        """Server-Sent Events: yields deliveries as they arrive (no polling)."""
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"}
        params = {"after": after, "timeout": timeout, "max_events": max_events}
        with self._http.stream("GET", "/v1/stream", headers=headers, params=params,
                               timeout=httpx.Timeout(10, read=None)) as r:
            if r.status_code >= 400:
                r.read()
                raise MessengerError(r.status_code, r.text)
            event, data = None, []
            for line in r.iter_lines():
                if line == "":
                    if event == "delivery" and data:
                        yield json.loads("\n".join(data))
                    event, data = None, []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())

    def listen(self, handler: Callable[[dict[str, Any]], None], after: int = 0, wait: float = 20,
               stop: Callable[[], bool] = lambda: False, mode: str = "sse") -> int:
        """Call handler(delivery) for each inbox item until stop() is true.
        mode="sse" streams (reconnecting every `wait` seconds so stop() is checked);
        mode="poll" long-polls. Returns the last cursor."""
        while not stop():
            items = self.events(after=after, timeout=wait) if mode == "sse" else self.inbox(after=after, wait=wait)
            for d in items:
                after = max(after, d["envelope"]["seq"])
                handler(d)
                if stop():
                    break
        return after

    def stream(self, after: int = 0, wait: float = 20) -> Iterator[dict[str, Any]]:
        while True:
            for d in self.inbox(after=after, wait=wait):
                after = max(after, d["envelope"]["seq"])
                yield d

    # -------------------------------------------------------------- threads
    def thread(self, thread_id: str) -> dict[str, Any]:
        return self._req("GET", f"/v1/threads/{thread_id}")

    def messages(self, thread_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._req("GET", f"/v1/threads/{thread_id}/messages", params={"after": after})

    def context(self, thread_id: str, recent: int = 10, max_tokens: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"recent": recent}
        if max_tokens:
            params["max_tokens"] = max_tokens
        return self._req("GET", f"/v1/threads/{thread_id}/context", params=params)

    def claim(self, thread_id: str) -> dict[str, Any]:
        return self._req("POST", f"/v1/threads/{thread_id}/claim")

    def set_state(self, thread_id: str, state: str, reason: str | None = None) -> dict[str, Any]:
        return self._req("POST", f"/v1/threads/{thread_id}/state", json={"state": state, "reason": reason})

    def blackboard(self, thread_id: str, **patch: Any) -> dict[str, Any]:
        return self._req("PATCH", f"/v1/threads/{thread_id}/blackboard", json=patch)

    # --------------------------------------------------------------- memory
    def remember(self, key: str, value: Any, *, visibility: str = "private", summary: str | None = None,
                 tags: list[str] | None = None, shared_with: list[str] | None = None,
                 threads: list[str] | None = None, source: str | None = None) -> dict[str, Any]:
        """Create or update one of this agent's memory items. visibility: private | shared | public."""
        body = {"key": key, "value": value, "visibility": visibility, "summary": summary, "tags": tags or [],
                "shared_with": shared_with or [], "threads": threads or [], "source": source}
        return self._req("POST", "/v1/memory", json={k: v for k, v in body.items() if v is not None})

    def memories(self, **filters: Any) -> list[dict[str, Any]]:
        """Stubs of memory this agent may read. Filters: q, tag, visibility, owner, thread_id,
        since, include_values, max_tokens, limit."""
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in filters.items() if v is not None}
        return self._req("GET", "/v1/memory", params=params)

    def recall(self, memory_id: str, path: str | None = None) -> dict[str, Any]:
        """Full value of one item (or just `path` inside it)."""
        return self._req("GET", f"/v1/memory/{memory_id}", params={"path": path} if path else None)

    def share(self, memory_id: str, *, visibility: str | None = None, add_agents: list[str] | None = None,
              remove_agents: list[str] | None = None, add_threads: list[str] | None = None,
              remove_threads: list[str] | None = None) -> dict[str, Any]:
        body = {"visibility": visibility, "add_agents": add_agents or [], "remove_agents": remove_agents or [],
                "add_threads": add_threads or [], "remove_threads": remove_threads or []}
        return self._req("POST", f"/v1/memory/{memory_id}/share", json={k: v for k, v in body.items() if v is not None})

    def forget(self, memory_id: str) -> dict[str, Any]:
        return self._req("DELETE", f"/v1/memory/{memory_id}")
