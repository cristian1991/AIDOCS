# Conductor and Lanes

The conductor is the long-lived orchestrator that turns "an agent works on a task" into "a team of agents works on a project". This page describes the current model and what's planned.

## What the conductor does today

- **Dispatches tasks** to lane agents (Claude, OpenCode, Codex, generic MCP)
- **Routes models** per task type (configurable from the dashboard — e.g. Claude for implementation, GPT for refactoring, Gemini for docs)
- **Isolates lanes** — each lane has owned files, blocked files, and tool grants. A lane working on the auth layer cannot edit the billing layer.
- **Handles comms** — lane agents can post questions, the conductor can inject guidance, scope conflicts auto-resolve through structured messages stored in a SQLite queue
- **Pauses / resumes** lanes — the dashboard or another agent can suspend a lane, which checkpoints the lane's state for a later resume
- **Tracks plans** — deterministic plan create / dispatch / verify / complete with a per-step audit trail
- **Runs in inline or parallel mode** — inline = one agent in the conversation; parallel = multiple agents with lane isolation

## Modes

| Mode | When to use | What's different |
|------|-------------|------------------|
| **Inline** | Single-thread work; one agent in the conversation | Conductor lives alongside the active agent; comms are direct |
| **Parallel** | Multiple lanes working independently | Conductor dispatches subprocess agents per lane; comms route through the SQLite message queue + dashboard |

## Lane mechanics

A lane is a scoped sandbox for one agent:

- **Owned files** — files only this lane is allowed to edit
- **Blocked files** — files this lane is explicitly refused (e.g. shared infrastructure that another lane owns)
- **Tool grants** — which AIDOCS / host tools this lane can call
- **Lane state** — current task, last activity, journal, message inbox/outbox

When a lane agent tries to edit a file outside its grants, the gate refuses with a doctrine line explaining the lane boundary. The agent's correct move is to ask the conductor for permission via the comms queue, not to retry.

## Comms model (today)

Today's conductor ↔ lane channel is a SQLite message queue:

- **Questions** — lane agent asks "should I X?"; the conductor responds via the queue, message lands in the lane's inbox on next poll
- **Guidance** — operator injects from the dashboard; same queue
- **Scope conflicts** — when two lanes need overlapping files, the conductor brokers through structured "request grant" messages
- **Lane control** — pause / resume / cancel by message verb, executed by the lane agent on next poll

This is fire-and-forget at the process level — once the conductor spawns a subprocess agent it cannot directly interrupt it; cooperation depends on the lane agent polling its inbox between tool calls.

## Planned (v2.5.0): A2A protocol

The fire-and-forget limit is the headline reason the roadmap targets A2A. With A2A:

- **Task states** — submitted → working → input-required → completed/failed, observable from the conductor side
- **Streaming progress** — live updates from lane agent to conductor to dashboard
- **Cancellation** — graceful interrupt mid-task (not just "next poll")
- **Pause/resume** — the lane agent checkpoints on demand
- **Process supervision** — heartbeat-based crash detection + auto-respawn with checkpoint context
- **Conductor-to-conductor** — conductors on different projects can delegate lanes to each other

MCP stays as the local instrumentation layer (AIDOCS provides tools). A2A adds the remote agent communication layer (conductor ↔ lane agents). They are complementary, not competing.

See [`PUBLIC_ROADMAP.md`](PUBLIC_ROADMAP.md) for the v2.5.0 scope.

## Why this design

A single long-running agent runs out of context. Spawning one-shot agents per task loses continuity. The conductor + lane model gets you:

- Continuity (conductor persists)
- Parallelism (lanes work independently)
- Scope safety (lane isolation)
- Model heterogeneity (route the right model to the right job)

without paying the context-explosion cost of stuffing everything into one agent's view.

## How tasks land in a lane

1. Operator (or a conductor decision) creates a task with scope, target files, and preferred model
2. Conductor matches the task to an available lane (or spawns a new lane)
3. Lane agent starts, reads the task brief from its inbox, begins work
4. Lane agent reports progress (today: journal entries + occasional inbox-poll messages; v2.5.0: streaming via A2A)
5. Lane agent posts completion verdict; conductor marks the task done and surfaces the result to the operator

Tasks can chain — a completed task can spawn follow-ups, which the conductor dispatches to the same lane or a new one based on overlap with the original scope.

## Edit history + rollback

Every edit a lane agent makes is recorded with file path, byte diff, lane, agent identity, and timestamp. The history is queryable; a rollback rewinds an edit while preserving the audit record so the rollback itself is auditable.

This is not full version control — git is still the authority for shared history. The edit log is a finer-grained per-agent attribution layer on top.
