---
description: Enter or bootstrap AIDOCS for this project
---
Use `/aidocs` as the only user-facing AIDOCS entry command.

Primary path
1) Resolve root (`$ARGUMENTS` or cwd).
2) If the `aidocs` MCP server is available, use it first.
3) Call the MCP orchestrator entry flow for this project.
4) Report:
    - whether setup was required
    - whether indexes were synced
    - selected session or session-selection requirement
    - whether AIDOCS-managed mode is now active and which session it is bound to
    - first retrieval bundle when useful

Fallback path (only if MCP is unavailable)
1) Resolve root (`$ARGUMENTS` or cwd).
2) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Treat that path as the runtime/public root. Never guess.
3) If the target project is not a git repository, bootstrap git first:
   - initialize repo
   - create initial commit
   - verify/create remote
   - verify upstream tracking
   - if any step fails, issue STOP
4) If the project is missing AIDOCS structure (`/.MEMORY/` plus `AGENTS.md` or `CLAUDE.md`), bootstrap it.
5) Run the updater script in `check` mode.
6) Run the updater script in `fix` mode.
7) Regenerate Claude auto-memory bootstrap if needed.
8) Run project-wide index sync.
9) Inspect sessions:
   - if exactly one clearly suitable session exists, select it
   - else if multiple plausible sessions exist, issue STOP and ask which session to use
   - else issue STOP and ask whether to create a new session
10) Return a concise readiness report.

Rules
- `/aidocs` is the user-facing entrypoint.
- The MCP orchestrator is internal machinery behind `/aidocs`, not a separate user workflow.
- Do not broad-read the repo before AIDOCS bootstrap/session selection is complete unless the user explicitly points at a file or error to inspect first.
- Commands are global-only; never copy command files into project.

Extra constraints:
$ARGUMENTS
