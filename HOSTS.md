# AIDOCS Host Support

AIDOCS integrates with multiple AI coding hosts. Each exposes a different surface for interception — this page is the **truthful, machine-checked** capability matrix so you can pick what to use without guessing.

> The matrix is generated from `mcp/server/aidocs_mcp/host_support_matrix.py` and enforced against the real adapter code by `mcp/tests/host/test_host_support_matrix.py`. If an adapter and this table disagree, the test fails. No aspirational claims.

## Hook-surface matrix

AIDOCS classifies hosts on **two orthogonal axes** — never conflated — and derives the public label from both:

- **Host surface capability** (what the host's hook contract *exposes*): `full-surface` · `partial-surface` · `mcp-only` · `startup-only` · `unknown` · `legacy`.
- **AIDOCS adapter status** (what AIDOCS has *built*): `wired` (adapter + proof tests) · `candidate` (host-capable, adapter pending — **NOT WIRED**) · `spike-needed` · `unsupported` · `legacy`.

**A host is only labelled `full` when it is `full-surface` AND `wired` (adapter + proof tests).** Capability alone never earns `full`.

Per-surface capability (does the host *expose* a usable hook): `supported` · `partial` · `mcp-only` · `none` · `unknown` · `legacy`.

| Host | UPS | PreTool | Perm | PostTool | Stop | Redact | Surface | Adapter | **Label** |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **Claude Code** | supported | supported | supported | supported | supported | supported | full-surface | wired | **FULL** |
| **OpenCode** | partial | supported | supported | partial | partial | none | full-surface | wired | **FULL** |
| **Codex CLI** (host) | supported | supported | supported | partial | unknown | partial | full-surface | wired | **FULL** |
| **OpenAI Agents** | none | supported | none | partial | partial | none | partial-surface | wired | **PARTIAL** |
| **Conductor worker** | mcp-only | mcp-only | mcp-only | mcp-only | mcp-only | none | mcp-only | wired | **MCP-ONLY** |
| **Generic MCP** | mcp-only | mcp-only | mcp-only | mcp-only | none | none | mcp-only | wired | **MCP-ONLY** |
| **GitHub Copilot** | supported | supported | supported | supported | supported | none | full-surface | candidate | **FULL CANDIDATE — NOT WIRED** |
| **Windsurf / Cascade** | supported | supported | supported | supported | unknown | none | full-surface | candidate | **FULL CANDIDATE — NOT WIRED** |
| **Kilo Code / CLI** | partial | supported | supported | supported | unknown | none | full-surface | candidate | **FULL CANDIDATE — NOT WIRED** |
| **Cline SDK/CLI** | supported | supported | partial | unknown | unknown | none | full-surface | spike-needed | **FULL CANDIDATE — SPIKE NEEDED** |
| **Cline IDE** | none | partial | partial | none | unknown | none | partial-surface | unsupported | **PARTIAL** |
| **Zed / ACP** | none | partial | partial | unknown | unknown | none | partial-surface | unsupported | **PARTIAL** |
| **Gemini CLI** | unknown | mcp-only | unknown | unknown | unknown | none | mcp-only | candidate | **PARTIAL/MCP CANDIDATE** |
| **Goose** (ACP) | unknown | mcp-only | unknown | unknown | unknown | none | mcp-only | candidate | **PARTIAL/MCP CANDIDATE** |
| **Cursor** | mcp-only | mcp-only | unknown | mcp-only | none | none | mcp-only | unsupported | **MCP-ONLY UNTIL PROVEN** |
| **Continue** | legacy | mcp-only | unknown | unknown | unknown | none | legacy | unsupported | **LEGACY** |
| **Roo Code** | legacy | legacy | legacy | legacy | legacy | none | legacy | unsupported | **LEGACY** |

> **`candidate` is a build target, not a support claim — the label literally says NOT WIRED.** Copilot (`userPromptSubmitted`/`preToolUse`/`postToolUse`/`agentStop`), Windsurf Cascade (`pre_user_prompt` + `pre_read/write/command/mcp` blocking hooks), and Kilo (an OpenCode fork — same `tool.execute.*`/`permission.ask`/transform taxonomy) are `full-surface`, so they become `FULL` *only after* an adapter is built and proof-tested. Cline's native safe/unsafe classification (and YOLO auto-approve) must **never** be trusted as law — AIDOCS wraps it or keeps it `partial`.

### PreTool vs PermissionRequest (distinct surfaces)

These are **not** the same hook and must not be conflated:

- **PreTool** runs before *every* tool call, regardless of permission status (authorize / block / mutate args).
- **PermissionRequest** fires *only when a permission dialog is about to appear*. It can allow, deny, modify tool input, or apply permission updates (e.g. add allow rules, change session permission mode). On Claude Code, a hook returning **allow does NOT override `deny`/`ask` rules** — exactly what AIDOCS law wants: tighten, never loosen.

All three hooked hosts **expose** PermissionRequest — Claude Code (`PermissionRequest`), Codex (`PermissionRequest`, matches MCP tool names), and OpenCode (`permission.ask` + event bus `permission.asked`/`permission.replied`) — so the *capability* column reads `supported`. **But AIDOCS does not yet register the dedicated event on any host**; it exercises permission control through PreTool's `permissionDecision` (deny/ask), which already covers allow/deny/ask. Wiring the dedicated PermissionRequest event (to use its apply-permission-updates power) is a tracked remaining item — the per-surface note records this so the capability claim is never mistaken for a wired handler.

### A host can appear twice — on purpose

Codex (and Claude) can be a **host** (its own CLI hooks fire) *or* a **conductor-spawned worker** (the AIDOCS conductor runs the sub-CLI; protection is the MCP gate + lane policy, not the worker's own hooks). Different boundary, different row.

## One law, many surfaces

Every supported path converges on the **same** canonical components — shared, not re-implemented per host (enforced by the test):

- **Prompt pipeline** → `PromptMutator` (security / freeze / classification). Claude `UserPromptSubmit` and the OpenCode/CLI bridge both route through it.
- **Tool enforcement** → the shared gate (`ToolGate` / `AgentOrchestrator.check_tool` / outer-gate `tools/call`). Claude PreTool, OpenAI `on_tool_start`, and MCP all converge here.
- **Output guard** → the same secret-scan/redaction guard (Claude PostTool + OpenAI `on_tool_end` + AIDOCS-owned MCP tools).
- **Freeze + audit** → one freeze service and one execution-index ledger across all paths.

## Per-host detail

### Claude Code — full reference host
Hooks: `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `Stop`, `SubagentStop`, `PreCompact`. Managed mode blocks raw `Read`/`Grep`/`Glob` on indexed files with an actionable redirect; PostTool can return a shape-preserving redacted copy via `updatedToolOutput`. PermissionRequest can tighten approvals without overriding the host's deny rules.

### OpenCode — rich plugin adapter
AIDOCS maps its concepts onto OpenCode plugin hooks (no Claude-style names):

| AIDOCS concept | OpenCode hook |
|---|---|
| Prompt mutation (UPS-ish) | `chat.message`, `experimental.chat.system.transform`, `experimental.chat.messages.transform` |
| PreTool | `tool.execute.before` |
| PostTool | `tool.execute.after` |
| PermissionRequest | `permission.ask` + event bus `permission.asked` / `permission.replied` |
| SessionStart-ish | `session.created` / `session.updated` / `server.connected` + system transform |
| Stop / turn audit | `session.idle` / `session.status` / `session.error` |
| Compaction | `experimental.session.compacting` / `experimental.compaction.autocontinue` |
| Shell env injection | `shell.env` |
| Command hook | `command.execute.before` / event bus `command.executed` |

The prompt hooks run the canonical `PromptMutator` (security/freeze/classification), but **operator-intent config mutation is deliberately withheld** — AIDOCS refuses to grant config/bash-policy authority from a parallel JS surface. `tool.execute.after` has no result-replacement field, so OpenCode **cannot** do shape-preserving output redaction (path blocking is the real protection).

### Codex CLI — now a hooked host (updated 2026-06-14)
Codex ships a Claude-compatible hook set: `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse` — and the tool hooks **can match MCP tool names**, so Codex can gate around AIDOCS MCP calls. Because the AIDOCS hook entrypoint dispatches on `hook_event_name` (not on host), a Codex CLI pointed at the AIDOCS hook script runs the same canonical pipeline. PostTool stays **feedback suppression** (`decision:block` / `continue:false`), not shape-preserving redaction.

**Known wiring gap (remaining):** the hook entrypoint currently stamps `host_kind="claude_code"` unconditionally, so a Codex session is mis-attributed. Harmless for UPS/PreTool (host-agnostic), but the redaction axis would wrongly advertise Claude's `updatedToolOutput` for Codex until host-kind is resolved from the payload/env. See *Roadmap*.

### Generic MCP host (Cursor, Windsurf, Continue.dev, …)
`.mcp.json` only — no host lifecycle hooks. Memory capture/recall, indexed retrieval, code search/trace/bundle, skill registry, and task lifecycle all work; **protection exists only for tools that route through the MCP server** (the outer gate). The host's own `read`/`write`/shell are not gated. Use Claude Code, OpenCode, or hooked Codex if you need the full security cascade end-to-end.

## Roadmap notes

- **Core/adapter split + host-kind resolution.** `claude_hook.py` currently fuses the host-agnostic core (PromptMutator/ToolGate/freeze/audit) with the Claude envelope adapter and hardcodes `host_kind`. Extracting a thin per-host adapter layer over a shared core (and resolving host-kind from payload/env) is what makes Codex/OpenCode attribution correct end-to-end. Tracked in [`PUBLIC_ROADMAP.md`](PUBLIC_ROADMAP.md).
- **A2A protocol** for richer conductor ↔ lane comms: see [`CONDUCTOR_AND_LANES.md`](CONDUCTOR_AND_LANES.md).
