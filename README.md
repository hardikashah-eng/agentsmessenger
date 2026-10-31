# Agents Messenger (working name: Quorum)

An agent-native communication platform: humans and agents work against shared **work items**, every turn is a typed **event**, agents act **on behalf of** people with scoped, expiring delegation, and the only thing that interrupts a human is a **decision request**.

## What's here

`design/prototype/` holds the initial UI/UX prototype:

| File | Contents |
|---|---|
| `Main.dc.html` | Desktop prototype: decision inbox, work item with event log and delegation contracts, agents and policy |
| `Mobile.dc.html` | Mobile decision request |
| `UX.dc.html` | Object model (Principal → Work item → Event → Decision request) and five UX principles |
| `UseCases.dc.html` | Six use cases: today vs. Quorum |
| `canvas.json` | Board layout |

The `.dc.html` files are Design Component sources from the Claude design canvas; they need its runtime (`support.js`) to render.

## Core primitives

- **Principal** — human, agent or service; agents act via delegated, scoped, expiring tokens
- **Work item** — goal, owner, budget, deadline, participants, artifacts
- **Event** — plan, tool call, eval, policy match, human note; prose is a rendering
- **Decision request** — typed ask with evidence, reversibility, blast radius and a default
- **Policy** — evaluated per event before the action runs
- **Trace** — every event, signature and token, replayable and bisectable
