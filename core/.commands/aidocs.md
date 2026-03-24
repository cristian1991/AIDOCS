---
description: Enter or bootstrap AIDOCS for this project
command_id: aidocs
preferred_executor: mcp
allow_uninitialized: true
---
## Intent
- Activate or bootstrap AIDOCS for the target project.

## Inputs
- Use `$ARGUMENTS` as the project root when provided.
- Otherwise use the current working directory as the project root.

## Preconditions
- None.

## Primary Execution
1. Resolve the target project root.
2. If the `aidocs` MCP server is available, use MCP first.
3. Call the MCP orchestrator entry flow for the resolved project.
4. Prefer the top-level `report` field for the default user-facing summary.
5. Use `readiness_summary` only when compact structured readiness is useful.
6. Use deeper bootstrap or orchestration payloads only when the user asks for more detail or the default report is insufficient.

## Branching
- If the project is already initialized, continue through bootstrap, index sync, and session routing.
- If setup is required, initialize AIDOCS first, then continue bootstrap.
- If exactly one clearly suitable session exists, select it automatically.
- If multiple plausible sessions exist, STOP and ask which session to use.
- If no suitable session exists, STOP and ask whether to create a new session.

## STOP Conditions
- Stop if session selection requires a user choice.
- Stop if session creation requires a user choice.
- In fallback mode, stop immediately if git bootstrap fails.
- In fallback mode, stop immediately if runtime root resolution from global routing fails.

## Output
- Report whether setup was required.
- Report whether indexes were synced.
- Report the selected session or that session selection is required.
- Report whether AIDOCS-managed mode is active and which session it is bound to.
- Report the first retrieval bundle only when it is useful.
- Keep the default rendering terse.

## Fallback
- Use this path only if the `aidocs` MCP server is unavailable.
1. Resolve the target project root.
2. Resolve AIDOCS source only from global routing: `~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`.
3. Treat the resolved AIDOCS source path as the runtime or public root. Never guess it.
4. If the target project is not a git repository, bootstrap git by initializing the repo, creating the initial commit, verifying or creating the remote, and verifying upstream tracking.
5. If any git bootstrap step fails, STOP.
6. If the target project is missing AIDOCS structure (`/.MEMORY/` plus `AGENTS.md` or `CLAUDE.md`), bootstrap it.
7. Run the updater script in `check` mode.
8. Run the updater script in `fix` mode.
9. Regenerate Claude auto-memory bootstrap if needed.
10. Run project-wide index sync.
11. Inspect sessions and apply the same session-selection rules as the primary path.
12. Return the same readiness report contract as the primary path.

## Rules
- `/aidocs` is the only user-facing AIDOCS entry command.
- Treat the MCP orchestrator as internal machinery behind `/aidocs`, not as a separate user workflow.
- Do not broad-read the repo before AIDOCS bootstrap and session selection complete unless the user explicitly points at a file or error first.
- Commands are global-only. Never copy command files into the target project.

## Arguments
$ARGUMENTS
