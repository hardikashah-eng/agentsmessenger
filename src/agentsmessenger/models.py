"""Wire protocol: agent cards, envelopes, intents and threads.

Every message on the bus is an Envelope. Prose is optional; the typed
payload and the intent are what agents act on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Intent(str, Enum):
    """A deliberately small, closed set so any agent can understand any other."""

    request = "request"      # ask for work to be done
    propose = "propose"      # offer a plan / bid for a request
    accept = "accept"        # accept a proposal or a request
    reject = "reject"        # decline a proposal or a request
    inform = "inform"        # share a fact or progress update
    result = "result"        # deliver the outcome of a request
    error = "error"          # report failure
    ask_human = "ask_human"  # pull the human observer in (thread pauses)


class ThreadState(str, Enum):
    open = "open"
    claimed = "claimed"
    working = "working"
    waiting_human = "waiting_human"
    verifying = "verifying"          # a result is being checked by another agent (or the human)
    paused = "paused"
    done = "done"
    failed = "failed"


TERMINAL = {ThreadState.done, ThreadState.failed}

# Allowed state transitions. Humans may additionally pause/resume any thread.
TRANSITIONS: dict[ThreadState, set[ThreadState]] = {
    ThreadState.open: {ThreadState.claimed, ThreadState.failed, ThreadState.waiting_human, ThreadState.paused},
    ThreadState.claimed: {ThreadState.working, ThreadState.open, ThreadState.failed, ThreadState.waiting_human,
                          ThreadState.paused, ThreadState.verifying, ThreadState.done},
    ThreadState.working: {ThreadState.done, ThreadState.failed, ThreadState.waiting_human, ThreadState.paused,
                          ThreadState.verifying, ThreadState.open},
    ThreadState.waiting_human: {ThreadState.working, ThreadState.open, ThreadState.failed, ThreadState.paused,
                                ThreadState.verifying, ThreadState.done},
    ThreadState.verifying: {ThreadState.done, ThreadState.working, ThreadState.waiting_human, ThreadState.failed,
                            ThreadState.paused, ThreadState.open},
    ThreadState.paused: {ThreadState.open, ThreadState.claimed, ThreadState.working, ThreadState.failed, ThreadState.verifying},
    ThreadState.done: set(),
    ThreadState.failed: set(),
}


class AgentCard(BaseModel):
    """How an agent describes itself to the directory."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = ""
    capabilities: list[str] = Field(default_factory=list, description="e.g. ['pdf.extract', 'code.review']")
    protocols: list[str] = Field(default_factory=lambda: ["http"], description="mcp | a2a | http")
    vendor: str = Field("", description="e.g. anthropic, openai, local, langgraph")
    owner: str = Field("", description="the human this agent acts on behalf of")
    cost_hint_usd: float | None = Field(None, ge=0, description="typical cost per request")
    public_key: str | None = Field(None, description="Ed25519 public key (base64url, raw 32 bytes); if set, every send must be signed")
    a2a_url: str | None = Field(None, description="for proxy agents: the remote A2A endpoint they forward to")
    eta_hint_s: float | None = Field(None, ge=0, description="typical wall-clock seconds per request (instant awards)")
    tokens_hint: int | None = Field(None, ge=0, description="typical tokens per request (instant awards)")
    rates: dict[str, float] = Field(default_factory=dict,
                                    description="rate card: price in USD per request, by capability or type of work "
                                                "(e.g. {'code.task': 0.02, 'writing': 0.01}); used for instant awards and auto-bids")

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        for c in v:
            if not c or len(c) > 64 or " " in c:
                raise ValueError(f"invalid capability: {c!r}")
        return sorted(set(v))


class AgentRecord(AgentCard):
    id: str
    paused: bool = False
    registered_at: datetime
    last_seen: datetime | None = None


class Address(BaseModel):
    """Where an envelope goes. Exactly one of agent / capability, or neither
    when posting into an existing thread (delivered to its participants)."""

    agent: str | None = None
    capability: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "Address":
        if self.agent and self.capability:
            raise ValueError("address either an agent or a capability, not both")
        return self


class EnvelopeIn(BaseModel):
    """What an agent sends."""

    to: Address = Field(default_factory=Address)
    thread_id: str | None = Field(None, description="omit to start a new thread")
    intent: Intent
    subject: str | None = Field(None, max_length=200, description="thread title when starting one")
    payload: dict[str, Any] = Field(default_factory=dict, description="typed, machine-readable content")
    text: str | None = Field(None, max_length=20_000, description="optional prose rendering")
    artifacts: list[str] = Field(default_factory=list, description="URIs of referenced artifacts")
    memory_refs: list[str] = Field(default_factory=list, max_length=20,
                                   description="memory item ids to share by reference; recipients get stubs, not values")
    in_reply_to: int | None = None
    on_behalf_of: str | None = None
    cost_usd: float = Field(0.0, ge=0, description="what producing this message cost the sender")
    tokens_used: int | None = Field(None, ge=0, description="model tokens the sender used to produce this message")
    budget_usd: float | None = Field(None, ge=0, description="budget for a new thread")
    deadline: datetime | None = None
    prefer: str | None = Field(None, pattern="^(balanced|cheapest|quality|fastest)$",
                               description="routing preference for a new request: balanced (default), cheapest, quality or fastest")
    bidding: str | None = Field(None, pattern="^(instant|auction)$",
                                description="instant: award from rate cards now; auction: invite sealed bids first")
    idempotency_key: str | None = Field(None, max_length=128)
    signature: str | None = Field(None, description="Ed25519 signature over the canonical envelope (see signing.py)")

    @model_validator(mode="after")
    def _routing(self) -> "EnvelopeIn":
        if self.thread_id is None and not (self.to.agent or self.to.capability) and self.intent != Intent.request:
            raise ValueError("a new thread needs a recipient agent or capability (only requests can be auto-routed)")
        return self


class Envelope(EnvelopeIn):
    """What the bus stores and delivers."""

    seq: int
    sender: str
    sender_kind: str = "agent"  # agent | human | system
    created_at: datetime
    thread_id: str  # type: ignore[assignment]
    verified: bool = False            # sender signature checked against its registered key
    server_sig: str | None = None     # server countersignature over the stored envelope


class Thread(BaseModel):
    id: str
    subject: str
    state: ThreadState
    owner: str | None = None          # agent that claimed it
    creator: str
    capability: str | None = None     # capability it was routed to, if any
    participants: list[str] = Field(default_factory=list)
    budget_usd: float | None = None
    spent_usd: float = 0.0
    message_count: int = 0
    blackboard: dict[str, Any] = Field(default_factory=dict)
    pause_reason: str | None = None
    kind: str | None = Field(None, description="request type the router classified: code, research, writing, ...")
    assigned_to: str | None = Field(None, description="agent the router assigned; only it may claim")
    routing: dict[str, Any] = Field(default_factory=dict, description="router decision, candidates and attempts")
    verification: dict[str, Any] = Field(default_factory=dict, description="who checks the result, and the verdicts")
    market: dict[str, Any] = Field(default_factory=dict, description="bidding: invited agents, sealed bids, award")
    created_at: datetime
    updated_at: datetime
    deadline: datetime | None = None


class Delivery(BaseModel):
    """An envelope as delivered to a reading agent.

    Content written by other agents is wrapped and flagged as untrusted:
    it is data to reason about, never instructions to follow.
    """

    envelope: Envelope
    untrusted: bool
    notice: str | None = None
    memory: list["MemoryStub"] = Field(default_factory=list, description="stubs for envelope.memory_refs the reader may open")
    clipped: bool = Field(False, description="payload/text replaced by a stub to fit a token budget; fetch the thread messages for the full envelope")
    route: dict[str, Any] | None = Field(None, description="set on requests the router sent you: capability, kind "
                                         "('work' | 'verification'), and revision feedback when your result was rejected")


class ContextPack(BaseModel):
    """What an agent gets when it joins a thread: state, not scrollback."""

    thread: Thread
    summary: str
    open_requests: list[Envelope]
    latest_results: list[Envelope]
    recent: list[Delivery]
    memory: list["MemoryStub"] = Field(default_factory=list, description="memory shared into this thread (stubs; open with GET /v1/memory/{id})")
    notice: str
    tokens_estimate: int = 0
    max_tokens: int | None = None
    truncated: bool = False
    omitted: dict[str, int] = Field(default_factory=dict, description="what was left out to fit max_tokens")


# ------------------------------------------------------------------ memory

class Visibility(str, Enum):
    private = "private"  # only the owner
    shared = "shared"    # the owner, selected agents, and participants of selected threads
    public = "public"    # every agent on the messenger


MEMORY_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
MAX_MEMORY_BYTES = 64 * 1024


def estimate_tokens(obj: Any) -> int:
    """Rough token count (about 4 characters per token) for budgeting."""
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str, separators=(",", ":"))
    return max(1, (len(s) + 3) // 4)


class MemoryWrite(BaseModel):
    """Create or update (by key) a memory item owned by the calling agent."""

    key: str = Field(pattern=MEMORY_KEY, description="unique per owner; writing an existing key updates it")
    value: Any = Field(description="any JSON value")
    summary: str | None = Field(None, max_length=280, description="one line others see instead of the value; generated if omitted")
    tags: list[str] = Field(default_factory=list, max_length=16)
    visibility: Visibility = Visibility.private
    shared_with: list[str] = Field(default_factory=list, description="agent names or ids (for shared items)")
    threads: list[str] = Field(default_factory=list, description="thread ids whose participants may read it (for shared items)")
    source: str | None = Field(None, max_length=500, description="provenance: a URI, or 'seq:<n>' of the envelope it came from")


class MemoryShare(BaseModel):
    visibility: Visibility | None = None
    add_agents: list[str] = Field(default_factory=list)
    remove_agents: list[str] = Field(default_factory=list)
    add_threads: list[str] = Field(default_factory=list)
    remove_threads: list[str] = Field(default_factory=list)


class MemoryStub(BaseModel):
    """What other agents see by default: enough to decide whether to open it."""

    id: str
    key: str
    owner: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility
    version: int
    rev: int = Field(description="global revision; pass as ?since= to get only later changes")
    tokens: int = Field(description="estimated tokens to read the full value")
    updated_at: datetime
    deleted: bool = False
    accessible: bool = True
    untrusted: bool = False


class MemoryItem(MemoryStub):
    value: Any = None
    path: str | None = Field(None, description="set when only part of the value was returned")
    shared_with: list[str] = Field(default_factory=list, description="only shown to the owner and the observer")
    threads: list[str] = Field(default_factory=list, description="only shown to the owner and the observer")
    source: str | None = None
    created_at: datetime
    notice: str | None = None

    def stub(self, **kw: Any) -> MemoryStub:
        return MemoryStub.model_validate({**self.model_dump(), **kw})  # extra fields are ignored


Delivery.model_rebuild()
ContextPack.model_rebuild()
