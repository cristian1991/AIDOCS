---
description: Upgrade a legacy project into the session-based memory model
---
Upgrade an initialized legacy project that still uses `NOW.md`, legacy global plans, or other pre-session runtime structures.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require initialized project (`/.MEMORY/` + `AGENTS.md` or `CLAUDE.md`), else STOP -> `/project-init`.
3) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Treat that path as the runtime/public root (normally `build/`). Never guess.
4) Run the updater script in `check-legacy` mode to inspect the project under the legacy-compatible rules.
5) Run the updater script in `fix` mode to apply only safe deterministic fixes:
   - refresh canonical managed files from source
   - create missing canonical files/folders
   - normalize routing-critical boilerplate
   - preserve project-specific facts and user sections below the managed marker
   - do not interpret legacy runtime state automatically
6) Inspect legacy runtime state (`NOW.md`, `TODO.md`, `DONE.md`, old global plans, old agents workspace) and issue STOP to ask which migration path to use:
   1. create a new clean session
   2. create a session based on the current `NOW.md` and legacy plans
7) If the user chooses migration from legacy runtime:
   - derive a new session id and title
   - create `/.MEMORY/sessions/<session-id>/SESSION.md`
   - create `context.md`
   - move or split relevant legacy plans into `/.MEMORY/sessions/<session-id>/plans/`
   - promote only durable facts into canonical memory
   - leave legacy files in place unless the user explicitly asks to archive/remove them
8) Regenerate Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - rewrite as redirect-only bootstrap content
   - point to project `/.MEMORY/.aidocs/index.aidocs` first and `/.MEMORY/INDEX.md` second as the memory router
   - tell Claude to inspect `/.MEMORY/sessions/*/SESSION.md` and select a session before task work
   - forbid storing memory, plans, summaries, or task output there
   - do not preserve side content; `MEMORY.md` is not a secondary memory store
9) Hard gate: re-read updated `AGENTS.md`/`CLAUDE.md`, `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/INDEX.md`, and linked docs in-session.
10) Output concise report: structural fixes, migration choice used, legacy files left in place, and follow-up recommendations.

Guardrails
- The script handles only safe structure updates.
- The agent performs all interpretation-heavy migration choices.
- Never migrate legacy runtime automatically without an explicit user-selected path.

Extra constraints:
$ARGUMENTS
