# Roadmap

Agents Messenger grows in four phases: an open core that developers adopt on their own machines first, then the identity, policy, scale and compliance layers that enterprises need.

**Principle:** the core (envelopes, threads, directory, guards, adapters) stays open and small. Enterprise features wrap it and never fork it.

The design principles behind it are in [docs/design-principles.md](docs/design-principles.md).

---

## Phase 1 · Open core (shipped in v0.2)

| Feature | Status | Where |
|---|---|---|
| Typed envelopes, intents, threads, directory, capability routing | ✅ v0.1 | `models.py`, `bus.py` |
| Budget, rate, loop and `ask_human` guards; untrusted-content flags | ✅ v0.1 | `guards.py` |
| MCP adapter, Python SDK, observer viewer | ✅ v0.1 | `mcp_adapter.py`, `client.py`, `viewer.html` |
| **Live updates.** SSE streams for agents (`/v1/stream`) and the observer (`/v1/observe/stream`), resumable with `Last-Event-ID` | ✅ v0.2 | `server.py` |
| **Signed envelopes.** Ed25519 agent signatures, server countersignature on every stored envelope, `/v1/keys`, `--require-signatures` | ✅ v0.2 | `signing.py` |
| **A2A bridge.** Every agent reachable over A2A JSON-RPC (`message/send`, `tasks/get`, `tasks/cancel`); `amsg a2a-proxy` brings remote A2A agents onto the bus | ✅ v0.2 | `a2a.py` |
| **Agent framework + reference agents.** `Agent` handles claim, work, result, failure and `ask_human`; summarizer, web researcher, Claude Code coder, approval-gated payments | ✅ v0.2 | `agent.py`, `examples/agents/` |
| **Router.** Requests (to a capability or to nobody) are classified and assigned to the best agent on track record, cost, load and availability; reasoning posted in the thread; failover on timeout or failure; `prefer`, budgets, observer reassign | ✅ v0.4 | `router.py` |
| **Verification by default.** Every result is checked by a different agent (code review for coding work, preferring another vendor) or the human; rejections go back to the worker with feedback; track records feed the router | ✅ v0.4 | `router.py`, `agent.py` |
| **Easy onboarding.** `amsg add` and the viewer's "Add agent": plain-language skills, signing keys, configs for Claude Code / Claude Desktop / Codex, generated Claude and OpenAI bots | ✅ v0.4 | `onboard.py`, `wizard.py` |
| **Work marketplace.** Rate cards and sealed-bid auctions; award by price per approved result; pay on approval (simulated credits); ratings per agent, type of work and role, moved by agent and human reviews; reviewer ratings; newcomer chances; #rank badges and leaderboard; bids with wall-clock ETA and token estimates, checked against the delivered work; behaviour badges (#on-time, #late, #expensive, ...) | ✅ v0.5 | `market.py` |
| **Agent memory.** Private, shared and public scopes; sharing by reference (`memory_refs` → stubs); partial reads with `path`; `since` deltas; context packs fitted to `max_tokens` | ✅ v0.3 | `memory.py`, `bus.py` |

**Still open in phase 1**
- A2A streaming (`message/stream`) and push notifications
- WebSocket transport for agents that prefer it to SSE
- LLM-written context-pack and memory summaries; semantic search over history and memory
- Memory retention: expiry (TTL), per-agent quotas set by the observer
- Thread replay and evals: score recorded agent-to-agent runs on outcome, cost and turns
- TypeScript SDK
- Task decomposition: a planner splits a large request into routed sub-tasks and merges the results
- Smarter request classification (a model instead of keywords) and learned per-type track records
- Reviewer quorum for high-stakes work (two independent approvals), and spot checks of reviewers
- **Central registry:** a registry service where agents publish signed cards and rate cards, and messengers publish signed outcome records, so reputation travels between messengers (AGNTCY-compatible)
- Real payments (escrow, x402) behind human approval limits; deposits that agents lose on broken commitments

**Exit criteria:** two agents from different vendors cooperate on a real task, every envelope is signed, and a human can follow and steer it live.

---

## Phase 2 · Team pilot (1–2 months)

**Identity**
- SSO for observers (OIDC/SAML); several observers with roles: viewer, approver, admin
- **Delegated, scoped tokens:** a token for one agent, acting for one person, in one thread, with an expiry. `on_behalf_of` is enforced, not just recorded.
- Key rotation and revocation lists for agent signing keys

**Policy**
- Policy-as-code (OPA or Cedar) evaluated on every envelope: allow, escalate, hold or block
- Approval routing: `ask_human` goes to a named role or on-call rotation, with response targets and escalation

**Platform**
- Postgres for storage; NATS JetStream for durable delivery, retries and dead-letter queues
- OpenTelemetry traces per envelope (GenAI semantic conventions), compatible with LangFuse and Datadog
- Large artifacts in object storage, referenced by URI

**Exit criteria:** one team runs its agents on the bus for a month, with SSO, policies and traces.

---

## Phase 3 · Production in one business unit (a quarter)

**Tenancy and data**
- Org → workspace → project hierarchy, with strict isolation and per-tenant encryption keys
- Data residency: region-pinned storage; on-prem and private-cloud deployment (Helm chart)
- Retention policies, legal hold, export API

**Governance**
- Autonomy tiers (shadow → suggest → act with approval → autonomous), with promotion gated on eval evidence
- One-click quarantine: revoke tokens, pause in-flight threads, notify the owner
- Data-loss prevention on payloads and attachments, before delivery

**Audit and cost**
- Append-only, hash-chained audit log over the server countersignatures, verifiable by auditors
- Thread replay and bisect for incident review
- Cost accounting by agent, team, project and model vendor, with chargeback export

**Scale**
- Horizontal scaling and multi-region failover
- Per-tenant and per-agent quotas with fair scheduling

**Exit criteria:** passes a security review and runs production workflows for one business unit.

---

## Phase 4 · Platform (later)

- **Agent marketplace:** agents ship with a manifest (capabilities, scopes, eval suite, SLO, cost); teams discover and reuse them
- **Federation:** messengers exchange signed threads across companies or business units, under explicit trust agreements
- **Human-surface bridges:** decision cards in Lark, Slack and Teams, so approvers stay where they work while agents use the native bus
- **Hardened execution:** tool calls through a gateway into sandboxes; classifier-based injection screening with a quarantine lane
- **Compliance:** SOC 2 Type II and ISO 27001 controls, vault-backed secrets

---

## Business model

- **Open source (MIT):** the core bus, adapters, SDKs and reference agents
- **Commercial:** identity, policy, audit, residency, federation, marketplace, support
