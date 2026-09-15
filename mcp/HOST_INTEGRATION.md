# Host Integration Contract

This document defines how a host/client (such as OpenCode) should integrate with the AIDOCS MCP layer.

The MCP server already provides the required primitives. This document describes the intended runtime behavior above them.

## Core Rule

- `/aidocs` is the only user-facing entrypoint for enabling AIDOCS-managed mode.
- After `/aidocs` succeeds, normal prompts should go through MCP-first routing by default.
- The host should prefer structural state (`aidocs-managed` mode, explicit file targets, selected session) over raw keyword guessing.

## State Model

### Project state
- Authority is the project sqlite store (`AidocsManagedStore` tables in `.MEMORY/.index/aidocs.sqlite3`), read/written through the MCP runtime (`managed_mode_service.py`). Hosts query it via MCP tools (`aidocs_mode_get`), never by reading files.
- Exposes:
  - managed state
  - `session_id`
  - timestamps
  - activation source
- Legacy note: `/.MEMORY/config/aidocs-managed.json` is an inert legacy artifact. It is ingested once and deleted on first touch; it is never authority and hosts must not read or write it.

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
2. Host calls `aidocs_classify_prompt`.
3. Host calls `aidocs_route_prompt` with the chosen `action_kind` and any explicit targets.
4. Host obeys the route result:
   - `requires_aidocs_entry`
   - `blocked`
   - `direct_inspection_allowed`
   - `preflight_only`
   - route-guided MCP-first work
5. If the route or action requires broader orchestration, the host may then call `aidocs_orchestrate` or a more specific MCP/runtime entrypoint.
6. If a managed session is already bound, the host should keep execution inside that session and its current conductor/plan flow instead of switching to generic worktree or standalone execution setup.
7. Host should prefer the top-level `report` field for default user-facing output.
8. Host should use deeper fields only when more detail is needed:
   - `readiness_summary` for compact structured readiness
   - `operator_report` / `operator_summary` for richer managed-session understanding
   - full orchestration/bootstrap payloads for advanced inspection or debugging

## Special Cases

### Explicit file or error target
- Host passes explicit targets to `aidocs_route_prompt`.
- Direct inspection may be allowed before broader session-driven orchestration.
- After inspection, broader work should return to MCP-first flow.

### Edit tasks
- If managed mode is active and the prompt becomes an edit task, the host should ensure:
  - `ai_task(mode='begin')`
  - retrieval/bundle step
  - edit
  - `ai_task(mode='complete')`

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
1. `aidocs_classify_prompt`
2. `aidocs_route_prompt`
3. obey returned route
4. call `aidocs_orchestrate` or more specific MCP tools only when the route/work type requires it
5. if `recommended_mcp_flow` includes `ai_plan_status`, stay in the currently bound managed session and continue its conductor lane flow

### Broad understanding
1. `aidocs_classify_prompt`
2. `aidocs_route_prompt`
3. if broader orchestration is warranted, call `aidocs_orchestrate`
4. only then read exact narrowed files if needed

### Edit task
1. `aidocs_classify_prompt`
2. `aidocs_route_prompt`
3. `ai_task(mode='begin')`
4. if planning is needed, use `ai_plan_create` / `plan_validate` / `plan_preflight`
5. if lane-aware work exists, use `execution_mode_select` and `ai_plan_dispatch`
6. edit / delegated execution
7. `verification_gate` and `ai_task(mode='complete')`

## What Is Implemented Already

- `aidocs_orchestrate` with workflow context in result
- `aidocs_mode_get` / `aidocs_mode_set` / `aidocs_mode_clear`
- `aidocs_classify_prompt` / `aidocs_route_prompt` with advisory classification and routing guidance
- `aidocs_handle_prompt` as a higher-level convenience path where a host wants a single composite runtime entrypoint
- `aidocs_classify_prompt` — keyword-based, advisory, synced with policy service
- session lifecycle methods
- task lifecycle methods (`ai_task(mode='begin'|'update'|'complete')`)
- runtime-owned orchestration methods (`plan_create_from_spec`, `plan_validate`, `execution_mode_select`, `plan_dispatch_next`, `plan_dispatch_report`, `execution_loop_next`, `verification_gate`)
- memory/code/schema retrieval methods
- `workflow_triggers_for_action` — action_kind → trigger → pending actions
- `workflow_actions_compile` / `workflow_actions_get`
- execution event recording (`prompt_classified`, `workflow_trigger_evaluated`, tool invocations)
- uniform top-level `report` on major runtime entrypoints
- `readiness_summary` and operator-facing action-surface summaries with pending workflow bullets
- Claude Code hook with advisory directives, workflow surfacing, execution evidence
- OpenCode plugin with entry gate, context injection, post-edit task reminders
- OpenCode plugin with entry gate, managed-mode enforcement, raw-read gate enforcement, native execution logging, context injection, and post-edit task reminders
- host context now separates helper skill guidance from runtime-owned workflow capability markers

## What Still Requires Real Host Support

- automatically wrapping edit tasks with `task_begin` / `task_complete` (currently advisory)
- using explicit-target routing before broad file reads
- respecting blocked/STOP-worthy outputs instead of bypassing them
- session claim/release for concurrent agent coordination
- auto-executing pending workflow actions after trigger conditions are met (currently advisory)

## Claude Code Implementation

- AIDOCS ships Claude Code hook entry scripts at `core/scripts/claude-hook.ps1` (Windows) and `core/scripts/claude-hook.sh` (Linux/macOS).
- The installer wires user-level Claude hooks into `~/.claude/settings.json` for:
  - `UserPromptSubmit`
  - `PreToolUse` on `Read|Edit|Write|Glob|Grep|Bash`
- The hook script calls the repo-shipped Python hook handler, which:
  - blocks normal prompts in initialized AIDOCS projects until `/aidocs` has activated managed mode
  - injects advisory action_kind classification with specific MCP tool directives (11 action_kinds covered)
  - injects managed-mode guardrail context before key tool calls
  - surfaces pending workflow actions during edit-type tool usage
  - records `prompt_classified` and `workflow_trigger_evaluated` execution events
  - logs project root resolution failures to stderr for debugging
  - uses workflow data from orchestration result (avoids redundant file reads)

Current boundary:

- This is a real Claude Code host-enforced entry and routing layer.
- Classification is advisory — the LLM decides final tool usage.
- Task lifecycle wrapping (`task_begin`/`task_complete`) is surfaced as guidance, not auto-invoked.
- Runtime-owned workflow capabilities such as planning or completion verification may be surfaced separately from helper skills.
- Pending workflow actions are surfaced in PreToolUse context but not auto-executed.

## OpenCode Implementation

- AIDOCS ships an OpenCode plugin at `core/plugins/aidocs.js`.
- The installer copies it to `~/.config/opencode/plugins/aidocs.js`, which OpenCode auto-loads.
- The plugin uses OpenCode host hooks to:
  - inject AIDOCS system context on each chat turn
  - tell the model to require `/aidocs` first in initialized but unmanaged projects
  - block core repo tools in unmanaged initialized projects unless the current slash command is `/aidocs`
  - keep managed execution inside the currently bound session/conductor flow
  - block raw `read` when the indexed-read gate has not granted the requested path
  - inject session and compiled workflow-action context in managed mode
  - record `native_tool_use` execution events after native tool calls
  - remind about `task_complete` after edit/write tool completions in managed mode
  - recognize `/aidocs` by command identity, not only by raw prompt text
  - read lightweight command metadata from `core/.commands/*.md`

Current boundary:

- OpenCode has a real host-enforced entry gate and managed-mode system-context path.
- Prompt interpretation is still primarily model/system driven in OpenCode today.
- Unlike Claude Code, the current OpenCode plugin does not yet invoke `aidocs_classify_prompt` and `aidocs_route_prompt` on each normal prompt from hook time.
- The OpenCode plugin currently uses local project state plus command metadata, not runtime-derived per-prompt routing.
- OpenCode now distinguishes helper skill guidance from runtime-owned workflow capabilities in prompt context.
- Task lifecycle reminders are injected post-edit but not auto-invoked.
- OpenCode now also enforces raw-read gating and records native tool execution, but it still does not perform full per-prompt runtime classification at hook time.
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


## Additional Host Notes

### Cursor
- First-pass AIDOCS support is startup-only via `sessionStart`.
- Treat Cursor startup routing as a compact session-selection/bootstrap prompt layer until broader hook parity is explicitly verified.

### GitHub Copilot CLI
- Superpowers now supports `sessionStart` startup context via `additionalContext` in Copilot CLI.
- AIDOCS does not yet ship a Copilot-specific host integration path.
- Recommended next step is a design slice, not immediate implementation:
  - validate Copilot CLI hook/event surface and config shape in detail
  - map AIDOCS `SessionStart` behavior onto Copilot's startup hook contract
  - decide whether first-pass support should be startup-only (like Cursor) or include broader prompt/tool routing later

### Codex
- Codex hooks are experimental as of March 2026.
- Windows hook support is disabled.
- `PreToolUse` and `PostToolUse` are Bash-only today, so AIDOCS should treat `SessionStart` and `UserPromptSubmit` as the primary Codex integration path.
- Codex packaging should prefer repository-root-resolved commands for hook scripts so startup works when launched from subdirectories.

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
| `ai_bundle` | yes | Retrieve session-guided code context |
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

### Procedure role
- Capability and execution evidence are the default runtime story.
- Procedures are optional structure derived from workflow/config definitions.
- The system should still provide useful routing, execution evidence, and operator visibility even when no formal procedures are defined.
- Hosts should not assume procedure definitions exist before using AIDOCS effectively.

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
