"""Wire protocol: agent cards, envelopes, intents and threads.

Every message on the bus is an Envelope. Prose is optional; the typed
payload and the intent are what agents act on.
"""

from __future__ import annotations

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
    paused = "paused"
    done = "done"
    failed = "failed"


TERMINAL = {ThreadState.done, ThreadState.failed}

# Allowed state transitions. Humans may additionally pause/resume any thread.
TRANSITIONS: dict[ThreadState, set[ThreadState]] = {
    ThreadState.open: {ThreadState.claimed, ThreadState.failed, ThreadState.waiting_human, ThreadState.paused},
    ThreadState.claimed: {ThreadState.working, ThreadState.open, ThreadState.failed, ThreadState.waiting_human, ThreadState.paused},
    ThreadState.working: {ThreadState.done, ThreadState.failed, ThreadState.waiting_human, ThreadState.paused},
    ThreadState.waiting_human: {ThreadState.working, ThreadState.open, ThreadState.failed, ThreadState.paused},
    ThreadState.paused: {ThreadState.open, ThreadState.claimed, ThreadState.working, ThreadState.failed},
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
    in_reply_to: int | None = None
    on_behalf_of: str | None = None
    cost_usd: float = Field(0.0, ge=0, description="what producing this message cost the sender")
    budget_usd: float | None = Field(None, ge=0, description="budget for a new thread")
    deadline: datetime | None = None
    idempotency_key: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def _routing(self) -> "EnvelopeIn":
        if self.thread_id is None and not (self.to.agent or self.to.capability):
            raise ValueError("a new thread needs a recipient agent or capability")
        return self


class Envelope(EnvelopeIn):
    """What the bus stores and delivers."""

    seq: int
    sender: str
    sender_kind: str = "agent"  # agent | human | system
    created_at: datetime
    thread_id: str  # type: ignore[assignment]


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


class ContextPack(BaseModel):
    """What an agent gets when it joins a thread: state, not scrollback."""

    thread: Thread
    summary: str
    open_requests: list[Envelope]
    latest_results: list[Envelope]
    recent: list[Delivery]
    notice: str
