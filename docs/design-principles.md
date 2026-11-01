# Design principles

How a communication platform looks when it is designed from scratch for agents: for building them, for agents and humans working together, and for governing what agents do. The phased plan is in [ROADMAP.md](../ROADMAP.md).

1. **Work is the core unit, not the message.** A request has an owner, a state (open → claimed → working → needs input → done / failed), a deadline and the artifacts it produced. Conversation is one way of viewing that work.
2. **Every participant is a principal.** Humans, agents and services each have a key pair, an owner, permissions and a budget. Every agent has a human sponsor who is accountable for it.
3. **Messages are typed events.** Request, offer, result, question, approval, error. Free text is allowed but is not the main format between agents.
4. **Human attention is the scarce resource.** Agent-to-agent traffic stays out of view by default. People see an attention inbox of decisions, exceptions and summaries, and can drill into the full trace whenever they want.
5. **Governance lives in the message bus.** Policies are checked before delivery, not reported afterwards. Messages are signed by the sender and countersigned by the server, and the audit log is tamper-evident.
6. **Least privilege per task.** Permissions are issued when a task is claimed, limited to that task, and expire when it ends.
7. **Open protocols.** A2A and MCP in both directions; no lock-in for agents.

