"""HTTP API. Agents are the primary clients; the observer (human) gets a
read-mostly API and a web viewer."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .a2a import mount_a2a
from .bus import HUMAN, Bus, Forbidden, NotFound
from .guards import GuardError
from . import onboard
from .router import classify, reliability
from .signing import generate_keypair
from .models import (
    AgentCard, AgentRecord, ContextPack, Delivery, Envelope, EnvelopeIn, Intent, MemoryItem, MemoryShare, MemoryStub,
    MemoryWrite, Thread, ThreadState, Visibility,
)


class Registered(BaseModel):
    agent: AgentRecord
    token: str


class StateChange(BaseModel):
    state: ThreadState
    reason: str | None = None


class HumanMessage(BaseModel):
    text: str
    intent: Intent = Intent.inform
    payload: dict[str, Any] = {}


class PauseBody(BaseModel):
    reason: str | None = None


class AssignBody(BaseModel):
    agent: str


class VerdictBody(BaseModel):
    verdict: str
    notes: str = ""


class BidBody(BaseModel):
    price_usd: float
    eta_s: float | None = None
    plan: str = ""
    confidence: float | None = None


class OnboardBody(BaseModel):
    name: str
    kind: str
    skills: list[str]
    can_review: bool | None = None
    cost_hint_usd: float | None = None
    description: str = ""
    owner: str = ""
    remote_url: str | None = None


class RoutePreview(BaseModel):
    text: str | None = None
    capability: str | None = None
    payload: dict[str, Any] = {}
    prefer: str | None = None


log = logging.getLogger("agentsmessenger")


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(bus: Bus | None = None, observer_token: str | None = None, open_registration: bool | None = None,
               tick_seconds: float = 2.0) -> FastAPI:
    bus = bus or Bus()
    observer_token = observer_token or os.environ.get("AMSG_OBSERVER_TOKEN") or ("obs_" + secrets.token_urlsafe(24))
    if open_registration is None:
        open_registration = os.environ.get("AMSG_OPEN_REGISTRATION", "0") == "1"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def ticker() -> None:
            # Reassign work nobody picked up and reviews nobody answered.
            while True:
                await asyncio.sleep(tick_seconds)
                try:
                    bus.tick()
                except Exception:  # pylint: disable=broad-exception-caught  # keep the loop alive; log and retry
                    log.exception("router tick failed")
        task = asyncio.create_task(ticker()) if tick_seconds > 0 else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Agents Messenger", version="0.5.0", lifespan=lifespan,
                  description="An agent-native messenger: agents talk, humans observe.")
    app.state.bus = bus
    app.state.observer_token = observer_token

    # ---------------------------------------------------------------- errors
    @app.exception_handler(GuardError)
    async def _guard(_: Request, e: GuardError) -> JSONResponse:
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound) -> JSONResponse:
        return JSONResponse({"error": "not_found", "message": str(e)}, status_code=404)

    @app.exception_handler(Forbidden)
    async def _fb(_: Request, e: Forbidden) -> JSONResponse:
        return JSONResponse({"error": "forbidden", "message": str(e)}, status_code=403)

    # ------------------------------------------------------------------ auth
    def is_observer(token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token, observer_token)  # type: ignore[arg-type]

    def agent_auth(authorization: str | None = Header(None)) -> AgentRecord:
        tok = _bearer(authorization)
        if not tok or is_observer(tok):
            raise HTTPException(401, "agent bearer token required")
        try:
            return bus.authenticate(tok)
        except Forbidden as exc:
            raise HTTPException(401, "invalid agent token") from exc

    def observer_auth(authorization: str | None = Header(None)) -> None:
        if not is_observer(_bearer(authorization)):
            raise HTTPException(401, "observer token required")

    def any_auth(authorization: str | None = Header(None)) -> AgentRecord | None:
        tok = _bearer(authorization)
        if is_observer(tok):
            return None
        if not tok:
            raise HTTPException(401, "bearer token required")
        try:
            return bus.authenticate(tok)
        except Forbidden as exc:
            raise HTTPException(401, "invalid token") from exc

    # ------------------------------------------------------------- directory
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/agents", response_model=Registered, status_code=201)
    def register(card: AgentCard, authorization: str | None = Header(None)) -> Registered:
        if not open_registration and not is_observer(_bearer(authorization)):
            raise HTTPException(401, "registration requires the observer token (or AMSG_OPEN_REGISTRATION=1)")
        agent, token = bus.register(card)
        return Registered(agent=agent, token=token)

    @app.get("/v1/agents", response_model=list[AgentRecord])
    def list_agents(capability: str | None = None, _: Any = Depends(any_auth)) -> list[AgentRecord]:
        return bus.find_agents(capability)

    @app.get("/v1/me", response_model=AgentRecord)
    def me(agent: AgentRecord = Depends(agent_auth)) -> AgentRecord:
        return agent

    @app.put("/v1/me", response_model=AgentRecord)
    def update_me(card: AgentCard, agent: AgentRecord = Depends(agent_auth)) -> AgentRecord:
        return bus.update_card(agent, card)

    @app.get("/v1/keys")
    def keys() -> dict[str, Any]:
        """Public keys for verifying envelopes offline."""
        return {
            "server": {"alg": "Ed25519", "public_key": bus.signer.public_key},
            "agents": {a.name: a.public_key for a in bus.find_agents() if a.public_key},
            "require_signatures": bus.require_signatures,
        }

    # -------------------------------------------------------------- messages
    @app.post("/v1/messages", response_model=Envelope, status_code=201)
    def send(env: EnvelopeIn, agent: AgentRecord = Depends(agent_auth)) -> Envelope:
        return bus.send(agent, env)

    @app.get("/v1/inbox", response_model=list[Delivery])
    async def inbox(after: int = 0, limit: int = Query(100, le=500), wait: float = Query(0, ge=0, le=30),
                    agent: AgentRecord = Depends(agent_auth)) -> list[Delivery]:
        """Long-poll: with wait>0, holds the request until something arrives."""
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            items = bus.inbox(agent, after, limit)
            if items or asyncio.get_running_loop().time() >= deadline:
                return items
            await asyncio.sleep(0.25)

    @app.get("/v1/stream", include_in_schema=True)
    async def stream(request: Request, after: int = 0, timeout: float = 0, max_events: int = 0,
                     token: str | None = None, last_event_id: str | None = Header(None)) -> StreamingResponse:
        """Server-Sent Events: each delivery for this agent as `event: delivery`.
        Resume with Last-Event-ID or ?after=. `token` query param is for clients
        (like EventSource) that can't set headers."""
        tok = _bearer(request.headers.get("authorization")) or token
        if not tok or is_observer(tok):
            raise HTTPException(401, "agent token required")
        try:
            agent = bus.authenticate(tok)
        except Forbidden as exc:
            raise HTTPException(401, "invalid agent token") from exc
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        def fetch(cursor: int) -> list[tuple[int, str, str]]:
            a = bus.store.get_agent(agent.id) or agent
            return [(d.envelope.seq, "delivery", d.model_dump_json()) for d in bus.inbox(a, cursor)]

        return StreamingResponse(_sse(request, bus, start, fetch, timeout, max_events), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- threads
    @app.get("/v1/threads/{thread_id}", response_model=Thread)
    def get_thread(thread_id: str, who: AgentRecord | None = Depends(any_auth)) -> Thread:
        return bus.redact(bus.get_thread(thread_id), who.id if who else None)

    @app.get("/v1/threads/{thread_id}/messages", response_model=list[Delivery])
    def thread_messages(thread_id: str, after: int = 0, who: AgentRecord | None = Depends(any_auth)) -> list[Delivery]:
        return bus.read_thread(who, thread_id, after)

    @app.get("/v1/threads/{thread_id}/context", response_model=ContextPack)
    def context(thread_id: str, recent: int = Query(10, ge=0, le=100), max_tokens: int | None = Query(None, ge=200, le=200_000),
                who: AgentRecord | None = Depends(any_auth)) -> ContextPack:
        """State, not scrollback. With max_tokens, the pack is shrunk to fit (see `omitted`)."""
        return bus.context_pack(who, thread_id, recent, max_tokens)

    @app.post("/v1/threads/{thread_id}/claim", response_model=Thread)
    def claim(thread_id: str, agent: AgentRecord = Depends(agent_auth)) -> Thread:
        return bus.redact(bus.claim(agent, thread_id), agent.id)

    @app.post("/v1/threads/{thread_id}/state", response_model=Thread)
    def set_state(thread_id: str, body: StateChange, agent: AgentRecord = Depends(agent_auth)) -> Thread:
        return bus.redact(bus.set_state(agent, thread_id, body.state, body.reason), agent.id)

    @app.patch("/v1/threads/{thread_id}/blackboard", response_model=Thread)
    def blackboard(thread_id: str, patch: dict[str, Any], agent: AgentRecord = Depends(agent_auth)) -> Thread:
        return bus.redact(bus.update_blackboard(agent, thread_id, patch), agent.id)

    @app.post("/v1/threads/{thread_id}/bids", status_code=201)
    def bid(thread_id: str, body: BidBody, agent: AgentRecord = Depends(agent_auth)) -> dict[str, Any]:
        """Sealed bid on a request you were invited to. Rebidding while open replaces your bid."""
        return bus.submit_bid(agent, thread_id, body.price_usd, body.eta_s, body.plan, body.confidence)

    # ---------------------------------------------------------------- router
    @app.post("/v1/route/preview")
    def route_preview(body: RoutePreview, who: AgentRecord | None = Depends(any_auth)) -> dict[str, Any]:
        """Who the router would pick for a request, and why, without sending anything."""
        env = EnvelopeIn(intent=Intent.request, to={"capability": body.capability}, payload=body.payload,
                         text=body.text, prefer=body.prefer, bidding="auction")  # full ranking, no newcomer boost
        plan = bus.plan_route(who.id if who else "human", env)
        return {"kind": plan["kind"] if plan else classify(body.capability, body.payload, body.text, None),
                "verification": bus.router.verify, "candidates": plan["candidates"] if plan else []}

    @app.get("/v1/agents/stats")
    def agent_stats(_: Any = Depends(any_auth)) -> list[dict[str, Any]]:
        """Each agent's track record, ratings by type of work, badges and credits."""
        badges = bus.badges()
        board = bus.leaderboard()
        out = []
        for a in bus.find_agents():
            st = bus.store.stats(a.id)
            ratings = {kind: {k: row[k] for k in ("rank", "rating", "tasks")}
                       for kind, rows in board.items() for row in rows if row["agent"] == a.id}
            out.append({"agent": a.id, "name": a.name, "vendor": a.vendor, "owner": a.owner, "capabilities": a.capabilities,
                        "paused": a.paused, "cost_hint_usd": a.cost_hint_usd, "rates": a.rates, **st,
                        "reliability": round(reliability(st), 3), "load": bus.store.load(a.id), "online": bus.is_online(a),
                        "badges": badges.get(a.id, []), "ratings": ratings, "balance_usd": bus.store.balance(a.id)})
        return out

    @app.get("/v1/leaderboard")
    def leaderboard(role: str = Query("worker", pattern="^(worker|reviewer)$"), _: Any = Depends(any_auth)) -> dict[str, Any]:
        """Agents ranked per type of work (#1 coding, #2 writing, ...)."""
        return {"role": role, "boards": bus.leaderboard(role), "badges": bus.badges()}

    @app.get("/v1/ledger")
    def ledger(who: AgentRecord | None = Depends(any_auth)) -> dict[str, Any]:
        """Simulated credits: your entries and balance (the observer sees everything)."""
        account = who.id if who else None
        return {"account": account or "all", "balance_usd": bus.store.balance(account or HUMAN),
                "entries": bus.store.ledger(account)}

    # ---------------------------------------------------------------- memory
    @app.post("/v1/memory", response_model=MemoryItem, status_code=201)
    def remember(body: MemoryWrite, agent: AgentRecord = Depends(agent_auth)) -> MemoryItem:
        """Create or update (by key) one of your memory items."""
        return bus.remember(agent, body)

    @app.get("/v1/memory", response_model=list[MemoryItem])
    def list_memory(q: str | None = None, tag: str | None = None, visibility: Visibility | None = None,
                    owner: str | None = None, thread_id: str | None = None,
                    since: int | None = Query(None, ge=0, description="only items changed after this rev (includes tombstones)"),
                    include_values: bool = False, max_tokens: int | None = Query(None, ge=1),
                    limit: int = Query(50, le=500), who: AgentRecord | None = Depends(any_auth)) -> list[MemoryItem]:
        """Memory you may read, as stubs (value=null) unless include_values=true."""
        owner_id = bus.resolve_agent(owner).id if owner else None
        return bus.list_memory(who, include_values=include_values, max_tokens=max_tokens, q=q, tag=tag,
                               visibility=visibility.value if visibility else None, owner=owner_id,
                               thread_id=thread_id, since=since, limit=limit)

    @app.get("/v1/memory/{memory_id}", response_model=MemoryItem)
    def read_memory(memory_id: str, path: str | None = Query(None, description="dotted path, e.g. findings.0"),
                    who: AgentRecord | None = Depends(any_auth)) -> MemoryItem:
        return bus.read_memory(who, memory_id, path)

    @app.post("/v1/memory/{memory_id}/share", response_model=MemoryItem)
    def share_memory(memory_id: str, body: MemoryShare, agent: AgentRecord = Depends(agent_auth)) -> MemoryItem:
        return bus.share_memory(agent, memory_id, body)

    @app.delete("/v1/memory/{memory_id}", response_model=MemoryStub)
    def forget(memory_id: str, who: AgentRecord | None = Depends(any_auth)) -> MemoryStub:
        return bus.forget(who, memory_id)

    # -------------------------------------------------------------- observer
    @app.get("/v1/observe/threads", response_model=list[Thread], dependencies=[Depends(observer_auth)])
    def obs_threads(limit: int = Query(100, le=500)) -> list[Thread]:
        return bus.store.list_threads(limit)

    @app.get("/v1/observe/feed", response_model=list[Envelope], dependencies=[Depends(observer_auth)])
    def obs_feed(after: int = 0, limit: int = Query(200, le=1000)) -> list[Envelope]:
        return list(bus.store.messages_since(after, limit))

    @app.get("/v1/observe/stream")
    async def obs_stream(request: Request, after: int = 0, timeout: float = 0, max_events: int = 0,
                         token: str | None = None, last_event_id: str | None = Header(None)) -> StreamingResponse:
        """SSE of every envelope on the bus (`event: envelope`)."""
        if not is_observer(_bearer(request.headers.get("authorization")) or token):
            raise HTTPException(401, "observer token required")
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        def fetch(cursor: int) -> list[tuple[int, str, str]]:
            return [(e.seq, "envelope", e.model_dump_json()) for e in bus.store.messages_since(cursor)]

        return StreamingResponse(_sse(request, bus, start, fetch, timeout, max_events), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/observe/threads/{thread_id}/messages", response_model=Envelope, status_code=201,
              dependencies=[Depends(observer_auth)])
    def obs_say(thread_id: str, body: HumanMessage) -> Envelope:
        return bus.send(None, EnvelopeIn(thread_id=thread_id, intent=body.intent, text=body.text, payload=body.payload))

    @app.get("/v1/onboard/options")
    def onboard_options() -> dict[str, Any]:
        """Agent kinds and plain-language skills for the 'Add agent' form."""
        return {"kinds": onboard.KINDS, "skills": {k: v["label"] for k, v in onboard.SKILLS.items()}}

    @app.post("/v1/observe/onboard", dependencies=[Depends(observer_auth)])
    def onboard_agent(body: OnboardBody, request: Request) -> dict[str, Any]:
        """Register an agent from plain choices; returns its credentials (shown once) and setup steps."""
        url = str(request.base_url).rstrip("/")
        if body.kind == "a2a":
            if not body.remote_url:
                raise HTTPException(422, "remote_url is required for an A2A agent")
            return {"setup": {"title": "Connect the remote A2A agent", "steps": [
                "Run this on a machine that can reach both the messenger and the remote agent."],
                "command": f"amsg a2a-proxy {body.remote_url} --name {onboard.slug(body.name)} --url {url}"}}
        try:
            priv, pub = generate_keypair()
            c = onboard.card(body.name, body.kind, body.skills, can_review=body.can_review,
                             cost_hint_usd=body.cost_hint_usd, description=body.description, owner=body.owner,
                             public_key=pub)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        agent, token = bus.register(AgentCard(**c))
        plan = onboard.setup(body.kind, url=url, name=agent.name, token=token, private_key=priv,
                             caps=agent.capabilities, agent_card=c)
        public = {k: v for k, v in plan.items() if k in ("title", "steps", "command", "snippet")}
        if "write_file" in plan:
            public["file"] = {"path": plan["write_file"]["path"], "content": plan["write_file"]["content"]}
            public["env"] = f"AMSG_TOKEN={token} AMSG_SIGNING_KEY={priv}"
        return {"agent": agent, "token": token, "private_key": priv, "setup": public}

    @app.post("/v1/observe/threads/{thread_id}/assign", response_model=Thread, dependencies=[Depends(observer_auth)])
    def obs_assign(thread_id: str, body: AssignBody) -> Thread:
        """Give a thread to a specific agent (overrides the router)."""
        return bus.assign(thread_id, body.agent)

    @app.post("/v1/observe/threads/{thread_id}/verdict", response_model=Thread, dependencies=[Depends(observer_auth)])
    def obs_verdict(thread_id: str, body: VerdictBody) -> Thread:
        """Approve or reject the latest result yourself (overrides any reviewer)."""
        return bus.human_verdict(thread_id, body.verdict, body.notes)

    @app.post("/v1/observe/threads/{thread_id}/pause", response_model=Thread, dependencies=[Depends(observer_auth)])
    def obs_pause_thread(thread_id: str, body: PauseBody | None = None) -> Thread:
        return bus.human_pause_thread(thread_id, True, body.reason if body else None)

    @app.post("/v1/observe/threads/{thread_id}/resume", response_model=Thread, dependencies=[Depends(observer_auth)])
    def obs_resume_thread(thread_id: str) -> Thread:
        return bus.human_pause_thread(thread_id, False)

    @app.post("/v1/observe/agents/{agent_ref}/pause", response_model=AgentRecord, dependencies=[Depends(observer_auth)])
    def obs_pause_agent(agent_ref: str) -> AgentRecord:
        return bus.human_pause_agent(agent_ref, True)

    @app.post("/v1/observe/agents/{agent_ref}/resume", response_model=AgentRecord, dependencies=[Depends(observer_auth)])
    def obs_resume_agent(agent_ref: str) -> AgentRecord:
        return bus.human_pause_agent(agent_ref, False)

    # ---------------------------------------------------------------- viewer
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def viewer() -> str:
        return resources.files("agentsmessenger").joinpath("viewer.html").read_text(encoding="utf-8")

    mount_a2a(app, bus)
    return app


async def _sse(request: Request, bus: Bus, cursor: int, fetch: Callable[[int], list[tuple[int, str, str]]],
               timeout: float, max_events: int, heartbeat: float = 15.0) -> AsyncIterator[bytes]:
    """Generic SSE loop. Only queries the store when the bus has new messages."""
    loop = asyncio.get_running_loop()
    if cursor < 0:  # after=-1 means "only what arrives from now on"
        cursor = bus.store.last_seq
    started = last_beat = loop.time()
    checked = -1
    sent = 0
    yield b"retry: 2000\n\n"
    while True:
        if await request.is_disconnected():
            return
        if bus.store.last_seq != checked:
            checked = bus.store.last_seq
            batch = fetch(cursor)
            if batch:
                checked = -1  # there may be more than one page; look again next tick
            for seq, event, data in batch:
                cursor = max(cursor, seq)
                yield f"id: {seq}\nevent: {event}\ndata: {data}\n\n".encode()
                sent += 1
                if max_events and sent >= max_events:
                    return
        now = loop.time()
        if timeout and now - started >= timeout:
            return
        if now - last_beat >= heartbeat:
            last_beat = now
            yield b": ping\n\n"
        await asyncio.sleep(0.2)
