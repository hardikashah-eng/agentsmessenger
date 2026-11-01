"""Python SDK for agents. Thin and synchronous on purpose."""

from __future__ import annotations

from typing import Any, Callable, Iterator

import httpx


class MessengerError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        msg = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"{status}: {msg}")


class Client:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str | None = None,
                 http: httpx.Client | None = None) -> None:
        self.token = token
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

    def find(self, capability: str | None = None) -> list[dict[str, Any]]:
        return self._req("GET", "/v1/agents", params={"capability": capability} if capability else None)

    # ------------------------------------------------------------- messages
    def send(self, intent: str, *, to_agent: str | None = None, to_capability: str | None = None,
             thread_id: str | None = None, payload: dict[str, Any] | None = None, text: str | None = None,
             subject: str | None = None, in_reply_to: int | None = None, cost_usd: float = 0.0,
             budget_usd: float | None = None, idempotency_key: str | None = None,
             artifacts: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "intent": intent, "to": {"agent": to_agent, "capability": to_capability},
            "thread_id": thread_id, "payload": payload or {}, "text": text, "subject": subject,
            "in_reply_to": in_reply_to, "cost_usd": cost_usd, "budget_usd": budget_usd,
            "idempotency_key": idempotency_key, "artifacts": artifacts or [],
        }
        return self._req("POST", "/v1/messages", json={k: v for k, v in body.items() if v is not None})

    def request(self, capability: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        """Start a thread asking whoever offers `capability`."""
        return self.send("request", to_capability=capability, payload=payload, **kw)

    def reply(self, envelope: dict[str, Any], intent: str, payload: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.send(intent, thread_id=envelope["thread_id"], in_reply_to=envelope["seq"], payload=payload, **kw)

    def inbox(self, after: int = 0, wait: float = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self._req("GET", "/v1/inbox", params={"after": after, "wait": wait, "limit": limit})

    def listen(self, handler: Callable[[dict[str, Any]], None], after: int = 0, wait: float = 20,
               stop: Callable[[], bool] = lambda: False) -> int:
        """Long-poll the inbox and call handler(delivery) for each item. Returns the last cursor."""
        while not stop():
            for d in self.inbox(after=after, wait=wait):
                after = max(after, d["envelope"]["seq"])
                handler(d)
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

    def context(self, thread_id: str, recent: int = 10) -> dict[str, Any]:
        return self._req("GET", f"/v1/threads/{thread_id}/context", params={"recent": recent})

    def claim(self, thread_id: str) -> dict[str, Any]:
        return self._req("POST", f"/v1/threads/{thread_id}/claim")

    def set_state(self, thread_id: str, state: str, reason: str | None = None) -> dict[str, Any]:
        return self._req("POST", f"/v1/threads/{thread_id}/state", json={"state": state, "reason": reason})

    def blackboard(self, thread_id: str, **patch: Any) -> dict[str, Any]:
        return self._req("PATCH", f"/v1/threads/{thread_id}/blackboard", json=patch)
