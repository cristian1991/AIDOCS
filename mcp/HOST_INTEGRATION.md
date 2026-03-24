# Host Integration Contract

This document defines how a host/client (such as OpenCode) should integrate with the AIDOCS MCP layer.

The MCP server already provides the required primitives. This document describes the intended runtime behavior above them.

## Core Rule

- `/aidocs` is the only user-facing entrypoint for enabling AIDOCS-managed mode.
- After `/aidocs` succeeds, normal prompts should go through MCP-first routing by default.
- The host should prefer structural state (`aidocs-managed` mode, explicit file targets, selected session) over raw keyword guessing.

## State Model

### Project state
- Stored in project memory:
  - `/.MEMORY/config/aidocs-managed.json`
- Contains:
  - `active`
  - `session_id`
  - timestamps
  - activation source

### Host session state
- The host should cache:
  - current project root
  - whether AIDOCS-managed mode is active
  - currently bound session id
- The host should refresh that state from MCP when needed, not invent its own parallel truth.

## Normal Prompt Loop

### Unmanaged project
1. User sends a normal prompt.
2. Host checks `aidocs_mode_get`.
3. If `active = false`, host does not auto-bootstrap silently.
4. Host responds that `/aidocs` must be run first.

### Managed project
1. User sends a normal prompt.
2. Host calls `aidocs_handle_prompt`.
3. Host obeys the returned mode:
   - `requires_aidocs_entry`
   - `blocked`
   - `direct_inspection_allowed`
   - `mcp_orchestrated`
   - `preflight_only`
4. Host should prefer the top-level `report` field for default user-facing output.
5. Host should use deeper fields only when more detail is needed:
   - `readiness_summary` for compact structured readiness
   - `operator_report` / `operator_summary` for richer managed-session understanding
   - full orchestration/bootstrap payloads for advanced inspection or debugging

## Special Cases

### Explicit file or error target
- Host passes explicit targets to `aidocs_handle_prompt`.
- Direct inspection may be allowed before broader session-driven orchestration.
- After inspection, broader work should return to MCP-first flow.

### Edit tasks
- If managed mode is active and the prompt becomes an edit task, the host should ensure:
  - `task_begin`
  - retrieval/bundle step
  - edit
  - `task_complete`

### Project maintenance
- The host should route project maintenance through MCP/tooling first:
  - `project_check`
  - `project_fix`
  - `project_status`
  - `project_status_evaluate`

## Recommended MCP Call Order

### `/aidocs`
1. `aidocs_orchestrate`
2. `aidocs_mode_set` is performed internally by the orchestrator on success

### Normal managed prompt
1. `aidocs_handle_prompt`
2. obey returned mode

### Broad understanding
1. `aidocs_handle_prompt`
2. use returned orchestration bundle
3. only then read exact narrowed files if needed

### Edit task
1. `aidocs_handle_prompt`
2. `task_begin`
3. bundle retrieval
4. edit
5. `task_complete`

## What Is Implemented Already

- `aidocs_orchestrate` with workflow context in result
- `aidocs_mode_get` / `aidocs_mode_set` / `aidocs_mode_clear`
- `aidocs_route_prompt` / `aidocs_handle_prompt` with advisory classification
- `aidocs_classify_prompt` — keyword-based, advisory, synced with policy service
- session lifecycle methods
- task lifecycle methods (`task_begin`, `task_update`, `task_complete`)
- memory/code/schema retrieval methods
- `workflow_triggers_for_action` — action_kind → trigger → pending actions
- `workflow_actions_compile` / `workflow_actions_get`
- execution event recording (`prompt_classified`, `workflow_trigger_evaluated`, tool invocations)
- uniform top-level `report` on major runtime entrypoints
- `readiness_summary` and operator-facing action-surface summaries with pending workflow bullets
- Claude Code hook with advisory directives, workflow surfacing, execution evidence
- OpenCode plugin with entry gate, context injection, post-edit task reminders

## What Still Requires Real Host Support

- automatically wrapping edit tasks with `task_begin` / `task_complete` (currently advisory)
- using explicit-target routing before broad file reads
- respecting blocked/STOP-worthy outputs instead of bypassing them
- session claim/release for concurrent agent coordination
- auto-executing pending workflow actions after trigger conditions are met (currently advisory)

## Claude Code Implementation

- AIDOCS ships a Claude Code hook entry script at `core/scripts/claude-hook.ps1`.
- The installer wires user-level Claude hooks into `~/.claude/settings.json` for:
  - `UserPromptSubmit`
  - `PreToolUse` on `Read|Edit|Write|Glob|Grep|Bash`
- The hook script calls the repo-shipped Python handler `aidocs_mcp.claude_hook`, which:
  - blocks normal prompts in initialized AIDOCS projects until `/aidocs` has activated managed mode
  - injects advisory action_kind classification with specific MCP tool directives (11 action_kinds covered)
  - injects managed-mode guardrail context before key tool calls
  - surfaces pending workflow actions during edit-type tool usage
  - records `prompt_classified` and `workflow_trigger_evaluated` execution events
  - logs project root resolution failures to stderr for debugging
  - uses workflow data from orchestration result (avoids redundant file reads)

Current boundary:

- This is a real Claude Code host-enforced entry, routing, and execution-evidence layer.
- Classification is advisory — the LLM decides final tool usage.
- Task lifecycle wrapping (`task_begin`/`task_complete`) is surfaced as guidance, not auto-invoked.
- Pending workflow actions are surfaced in PreToolUse context but not auto-executed.

## OpenCode Implementation

- AIDOCS ships an OpenCode plugin at `core/plugins/aidocs.js`.
- The installer copies it to `~/.config/opencode/plugins/aidocs.js`, which OpenCode auto-loads.
- The plugin uses OpenCode host hooks to:
  - inject AIDOCS system context on each chat turn
  - tell the model to require `/aidocs` first in initialized but unmanaged projects
  - block core repo tools in unmanaged initialized projects unless the current slash command is `/aidocs`
  - inject session and compiled workflow-action context in managed mode
  - remind about `task_complete` after edit/write tool completions in managed mode
  - recognize `/aidocs` by command identity, not only by raw prompt text
  - read lightweight command metadata from `core/.commands/*.md`

Current boundary:

- OpenCode has a real host-enforced entry gate and managed-mode system-context path.
- Prompt interpretation is still primarily model/system driven in OpenCode today.
- Unlike Claude Code, the current OpenCode plugin does not yet invoke `aidocs_classify_prompt` and `aidocs_route_prompt` on each normal prompt from hook time.
- The OpenCode plugin currently uses local project state plus command metadata, not runtime-derived per-prompt routing.
- Task lifecycle reminders are injected post-edit but not auto-invoked.
- The next stronger slice is runtime-driven prompt routing: call lightweight MCP classification and route methods on each prompt, then inject the returned advisory guidance.
- After that, add session claim/release and richer tool-time policy based on MCP routing decisions.

### OpenCode target model
- Target behavior should match Claude's lightweight path for normal prompts:
  1. `aidocs_classify_prompt`
  2. `aidocs_route_prompt`
  3. inject advisory context from the route result
- Keep `/aidocs` itself as the heavier entry/bootstrap path via `aidocs_orchestrate`.
- Keep classification advisory, not directive.
- Prefer runtime-derived route decisions over plugin-local keyword or prompt heuristics whenever the host can call MCP at hook time.

### OpenCode current limitation
- The current OpenCode plugin SDK surface exposes MCP server management and normal tool execution, but it does not yet provide a first-class hook-time API for directly invoking arbitrary MCP tools from the plugin.
- Because of that limitation, OpenCode cannot yet fully mirror Claude's per-prompt runtime-driven classification and routing inside the plugin alone.
- Until that capability exists, OpenCode should:
  - keep using command-aware and managed-mode-aware system context
  - prefer command metadata over raw prompt heuristics
  - avoid claiming that runtime-driven prompt routing is already enforced when it is not

## Action Classification

### Advisory model
- The MCP runtime classifies user prompts into action_kinds using keyword matching.
- Classification is **advisory, not directive** — it provides signal to the host/model but does not override LLM judgment.
- The host should present classification as a suggestion: "AIDOCS suggests action kind: `edit`" — not "Classified action: `edit`".

### Supported action_kinds
| Action Kind | Session Required | Description |
|---|---|---|
| `edit` | yes | Code/file modifications — should be wrapped with task_begin/task_complete |
| `write_memory` | yes | Persist durable facts/rules via `memory_capture` |
| `task_begin` | yes | Register a new task |
| `task_update` | yes | Record task progress |
| `task_complete` | yes | Finalize a task |
| `code_bundle` | yes | Retrieve session-guided code context |
| `trace` | no | Trace references and data flow — prefer MCP `code_trace_*` tools |
| `understand` | no | Read-only analysis — prefer MCP retrieval tools |
| `inspect` | no | Direct target inspection (with explicit targets) |
| `read_error` | no | Error analysis |
| `read_file` | no | Direct file read (with explicit targets) |
| `project_update` | yes | Sync/refresh AIDOCS state |
| `archive` | yes | Archive/changelog work |

### Action directives
- Each action_kind has a specific directive injected into the host context.
- Directives tell the model which MCP tools to prefer for that action type.
- Example: `write_memory` → "Use the `memory_capture` MCP tool. Do NOT write memory files manually."
- Example: `edit` → "Use `task_begin` before starting work and `task_complete` when done."

## Workflow Integration

### Action → Trigger → Workflow Actions
- When an action_kind completes, AIDOCS checks for pending workflow triggers.
- Mapping: `edit` → `task_complete` trigger, `write_memory` → `memory_write` trigger, etc.
- Pending workflow actions are surfaced in:
  - **Operator report** — as a bullet: "Pending workflow actions after `edit`: `task_complete → update_memory`"
  - **PreToolUse context** — during edit-type tools: "When this edit task completes, these workflow actions are pending: ..."
- Use `workflow_triggers_for_action` MCP tool to query this mapping.

### Workflow enforcement model
- Currently **advisory** — pending actions are surfaced but not auto-executed.
- The host decides whether to invoke pending actions or let the model handle them.
- Future: may become host-enforced for critical workflows.

## Execution Evidence

### Event recording
- The hook records execution events at key lifecycle points:
  - `prompt_classified` — when a prompt is classified with an action_kind
  - `workflow_trigger_evaluated` — when workflow triggers are checked, with pending action count
  - `hook_intercept` — raw hook observations (UserPromptSubmit, PreToolUse)
  - Tool invocations — recorded by MCP instrumentation layer
- All events include: `event_kind`, `source_kind`, `session_id`, `action_kind`, `status`, `payload`

### Ad-hoc execution
- Execution events are recorded even when no formal procedure exists.
- Procedures can be attached to execution evidence retroactively.
- The system does not require upfront procedure authoring to capture useful evidence.

### Relationship families
- Schema relationships are categorized into families:
  - `structural` — FK, navigation, type hierarchy (from code/EF extraction)
  - `procedural` — procedure→capability links, workflow definitions
  - `execution` — execution→capability, execution→procedure links
- This allows queries like: "did execution follow the intended procedure?"

## Design Principle

- MCP provides the primitives and deterministic state.
- The host must actually call those primitives at the right time.
- Without host participation, MCP remains available but not fully enforced.
- Classification is advisory — the LLM is the final decision-maker on tool usage.
- Execution evidence is captured unconditionally — procedures are opt-in structure, not gates.

## Response Layering Contract

- Default display:
  - `report`
- Compact structured state:
  - `readiness_summary`
- Rich operator state:
  - `operator_report`
  - `operator_summary`
- Deep inspection/debug:
  - `bootstrap`
  - `orchestration`
  - retrieval bundles and full sync payloads

Practical host rule:

- Prefer `report` unless the user explicitly asks for more detail or the host is rendering an advanced inspection surface.

### Example: terse default rendering

When a runtime method returns a top-level `report`, the host should normally render only that layer:

```text
AIDOCS is ready in stage `session_active`.
- Active session: 2026-03-24-production-index-architecture.
- Operator state: partial.
- Index coverage: memory=94, code=840, schema=63, capabilities=111, procedures=10, links=0.
Next step: Define or capture the intended procedure/workflow for this action.
```

### Example: detailed rendering

If the user asks for more detail, the host can then expand into deeper layers in this order:

1. `readiness_summary`
2. `operator_summary` or `operator_report`
3. full `orchestration` / `bootstrap` payloads

Example:

```text
Default report
- <top-level `report`>

Readiness summary
- ready: true
- selected_session_id: 2026-03-24-production-index-architecture
- operator_state: partial

Operator summary
- derived_queries: [...]
- attention_items: [...]

Deep payload
- orchestration.retrieval
- bootstrap.sync
```
