---
description: Update project-local AI system files only
---
Sync an initialized project that already uses the current session-based AIDOCS model.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require initialized project (`/.MEMORY/` + `AGENTS.md` or `CLAUDE.md`), else STOP -> `/project-init`.
3) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Treat that path as the runtime/public root (normally `build/`). Never guess.
4) Run the updater script in `check` mode to report structural drift for the current session-based model.
5) Run the updater script in `fix` mode to apply only safe deterministic fixes:
   - refresh canonical managed files from source
   - create missing canonical files/folders
   - normalize routing-critical boilerplate
   - preserve project-specific facts and user sections below the managed marker
   - do not interpret or migrate task/runtime state
6) If legacy runtime files still exist (`NOW.md`, `TODO.md`, `DONE.md`, old global plans, old agents workspace), issue STOP and tell the user to run `/legacy-update` instead of continuing with `/project-update`.
7) Regenerate Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - rewrite as redirect-only bootstrap content
   - point to project `/.MEMORY/.aidocs/index.aidocs` first and `/.MEMORY/INDEX.md` second as the memory router
   - tell Claude to inspect `/.MEMORY/sessions/*/SESSION.md` and select a session before task work
   - forbid storing memory, plans, summaries, or task output there
   - do not preserve side content; `MEMORY.md` is not a secondary memory store
8) Hard gate: re-read updated `AGENTS.md`/`CLAUDE.md`, `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/INDEX.md`, and linked docs in-session.
9) Run health checks (PASS/WARN/FAIL):
   - source resolution
   - global command pack presence
   - canonical memory consistency
   - managed marker presence in managed files
   - `/.MEMORY/config/personality.md`
   - `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md`
   - Claude managed MEMORY block
10) Output concise report: updated/unchanged/errors + health + command pack version.

Memory discipline
- Read `/.MEMORY/.aidocs/index.aidocs` first, then `/.MEMORY/INDEX.md`, then select/read a session before work and after resume.
- Append notable update outcomes to daily log.

Extra constraints:
$ARGUMENTS
