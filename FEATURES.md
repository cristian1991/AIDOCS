# AIDOCS Features

AIDOCS is an orchestration layer for AI coding agents. This document is the public deep-dive on what the project ships today — landing page is [`README.md`](README.md).

Numbers (test counts, version) live in [`mcp/.deploy-reports/RELEASE_STATUS.md`](mcp/.deploy-reports/RELEASE_STATUS.md) and the badges at the top of `README.md`. They are regenerated from the last successful deploy run — this page deliberately does not duplicate them.

## Persistent memory

- Per-project `/.MEMORY/` tree: session state, plans, journal, archive, domain knowledge, rules
- Routed bootstrap: a session-start router and durable-memory router decide what context loads when an agent resumes
- Memory capture API: pin facts to typed lanes (user / feedback / project / reference) with auto-linking between entries
- Journal auto-eviction: long-running sessions roll old entries into archive without truncating identity
- Palace-backed memory home: memory bodies and semantics live in a local embedding palace layered over a canonical store — the store stays the source of truth and the palace is rebuildable from it, so a lost palace costs speed, never memory. Entries anchor to the code index at the smallest leaf (file → class → function → symbol)
- Store-canonical, not file-canonical: the `/.MEMORY/` markdown tree is a fallback projection regenerated from the store, not a hand-authored source

See [`MEMORY_AND_INDEXING.md`](MEMORY_AND_INDEXING.md) for the full layout.

## Indexed retrieval

- Symbol / reference / text search across the project tree
- Embedding-based semantic search (local model — no external API required)
- Trace tools: who calls / who reads / who writes / dependency-flow
- Module + symbol bundles: ranked containers rather than raw line ranges
- Scored symbol ranking: the closest definitions lead, so search noise doesn't bury the match
- Evidence-lane aggregation: the investigate surface returns evidence lanes alongside the code probes, so a concept query answers with surrounding evidence, not just code
- Discovery continuity: a session remembers what it has already surfaced, so follow-up reads don't re-pay the discovery cost
- Schema-query for SQL projects

The retrieval surface is the primary code-reading path inside AIDOCS-managed projects; raw read/grep is gated to keep agents from fishing blindly.

## Conductor + lanes

- Long-lived conductor that dispatches tasks to lane agents
- Lane isolation: each lane has its own owned files, blocked files, and tool grants
- Per-task-type model routing (configurable from the dashboard)
- Inline mode (single agent in the conversation) or parallel mode (multi-agent with isolation)
- Conductor comms: lane agents can ask questions, the conductor can inject guidance, scope conflicts resolve through structured messages

See [`CONDUCTOR_AND_LANES.md`](CONDUCTOR_AND_LANES.md).

## Security gates

- Lifecycle + task gate: tools that touch shared state require an active session/task
- Taxonomy-based heuristic judge: structured rule verdicts, not max-risk telemetry
- Output guard: credential / injection scanning with auto-redaction
- Shell-egress chokepoint: one supervised path for all shell execution — with **governed bash** on an ask-by-default policy: an off-allowlist command asks for confirmation instead of blocking outright or running blind, a dev-toolchain command family is recognized so routine build/test tools stay usable, and destructive-shape floors are tightened
- Tool policies + RBAC: admin allow/deny patterns scoped to user/role
- Complexity-pinned gate paths: every security-gate code path sits at cyclomatic-complexity rank C or better, with the hottest functions rank-pinned so a complexity regression fails the gate

See [`SECURITY.md`](SECURITY.md) for the public-safe model, scope, and known limits.

## Dashboard

Tauri desktop app for:

- Monitoring projects, sessions, lane activity, token usage
- Conductor chat + lane control
- Settings (scoped: global / project / session)
- Skills + MCP registry browsing
- Setup wizard

See [`DASHBOARD.md`](DASHBOARD.md).

## CLI

`aidocs setup`, `doctor`, `init`, `status`, `sync`, `benchmark`, `version`. Run `aidocs setup` first; it configures MCP wiring, hooks, and project init in one pass.

## Host adapters

| Host | Integration | Notes |
|------|-------------|-------|
| Claude Code | Hooks + MCP | Hook-level enforcement |
| OpenCode | Plugin + MCP + serve mode | Plugin gates raw tools |
| Codex | MCP + CLI conductor | Task dispatch via CLI |
| Any MCP host | `.mcp.json` | Tools + memory; no hook interception |

See [`HOSTS.md`](HOSTS.md) for the truthful per-host capability matrix.

## Skills

Bundled behavior packs that change agent style for a single turn or session. The shipped set is in `core/.commands/`; new skills are project-local files dropped under `.claude/skills/` or `.opencode/skills/`.

## Session grounding & feedback

- Session grounding ledger: the tool surface remembers the session's own conversation, so an agent's later calls stay anchored in what it has already seen and done
- Live run progress: long-running jobs report elapsed time, status, and a bounded tail of output while they run, instead of going dark until they finish
- `tool_report` feedback: agents can report whether a tool helped or misled — audience-gated telemetry that feeds tool-quality improvement
- Backlog surfacing: the open backlog announces itself at the prompt, the edit rail, and on stop — epoch-deduped so it surfaces once per epoch, not on every call

## Quality + release authority

Every release passes a private deploy gate before it ships — static analysis, security scanning, and the full test suite, with blocking and report-first lanes. The gate auto-writes a sanitized summary and a machine-readable `status.json` that the README badges consume.

See [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md) for the gate model, what counts as authority, and how to read the published artifacts.
