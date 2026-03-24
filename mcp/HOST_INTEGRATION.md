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

- `aidocs_orchestrate`
- `aidocs_mode_get`
- `aidocs_mode_set`
- `aidocs_mode_clear`
- `aidocs_route_prompt`
- `aidocs_handle_prompt`
- session lifecycle methods
- task lifecycle methods
- memory/code/schema retrieval methods

## What Still Requires Real Host Support

- automatically calling `aidocs_handle_prompt` for normal prompts after `/aidocs`
- automatically wrapping edit tasks with `task_begin` / `task_complete`
- using explicit-target routing before broad file reads
- respecting blocked/STOP-worthy outputs instead of bypassing them

## Claude Code Implementation

- AIDOCS now ships a Claude Code hook entry script at `core/scripts/claude-hook.ps1`.
- The installer wires user-level Claude hooks into `~/.claude/settings.json` for:
  - `UserPromptSubmit`
  - `PreToolUse` on `Read|Edit|Write|Glob|Grep|Bash`
- The hook script calls the repo-shipped Python handler `aidocs_mcp.claude_hook`, which:
  - blocks normal prompts in initialized AIDOCS projects until `/aidocs` has activated managed mode
  - injects AIDOCS MCP-first routing context into managed prompts
  - injects managed-mode guardrail context before key tool calls

Current boundary:

- This is a real Claude Code host-enforced entry and routing layer.
- It does not yet auto-open full `task_begin` / `task_complete` state transitions on Claude's behalf.
- The next stronger slice would be richer tool-time enforcement and lifecycle-aware edit wrapping.

## OpenCode Implementation

- AIDOCS now ships an OpenCode plugin at `core/plugins/aidocs.js`.
- The installer copies it to `~/.config/opencode/plugins/aidocs.js`, which OpenCode auto-loads.
- The plugin uses OpenCode host hooks to:
  - inject AIDOCS system context on each chat turn
  - tell the model to require `/aidocs` first in initialized but unmanaged projects
  - block core repo tools in unmanaged initialized projects unless the current slash command is `/aidocs`
  - inject session and compiled workflow-action context in managed mode

Current boundary:

- OpenCode now has a real host-enforced entry gate and managed-mode system-context path.
- Prompt interpretation in OpenCode is primarily model/system driven; deterministic AIDOCS keyword routing remains available as fallback inside MCP runtime methods.
- The next stronger slice would be automatic task lifecycle wrapping and richer tool-time policy based on MCP routing decisions.

## Design Principle

- MCP provides the primitives and deterministic state.
- The host must actually call those primitives at the right time.
- Without host participation, MCP remains available but not fully enforced.
