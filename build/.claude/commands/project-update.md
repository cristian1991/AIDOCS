---
description: Update project-local AI system files only
---
Sync an initialized project to current AIDOCS contracts (without full init).

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require initialized project (`/.MEMORY/` + `AGENTS.md` or `CLAUDE.md`), else STOP -> `/project-init`.
3) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Treat that path as the runtime/public root (normally `build/`). Never guess.
4) Sync project-local artifacts from source:
    - create/update `/.MEMORY/CHANGELOG.md`
    - rewrite `/.MEMORY/.aidocs/index.aidocs`
   - rewrite `/.MEMORY/.aidocs/global-instructions.aidocs`
   - rewrite `/.MEMORY/.aidocs/coding-standards.aidocs`
   - rewrite `/.MEMORY/.aidocs/memory-system.aidocs`
   - refresh other shipped `/.MEMORY/.aidocs/*.aidocs`
   - `/.MEMORY/.aidocs/personalities/`
   - refresh project routers (`AGENTS.md`, `CLAUDE.md`) in this order: `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, `/.MEMORY/INDEX.md`
   - ensure `/.MEMORY/agents/` exists for spawned-agent plans/investigations (`YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`)
   - commands are global-only; never copy command files into project
5) Copy missing memory templates from `/.MEMORY/.aidocs/templates/memory/**`.
   Also create missing `/.MEMORY/CHANGELOG.md` from `/.MEMORY/.aidocs/templates/CHANGELOG.md`.
6) Normalize memory routing: refresh routing-critical `/.MEMORY/INDEX.md` text, keep project-specific memory facts intact, and preserve legacy dirs with forward-merge when obvious.
   Remove legacy `/.MEMORY/TODO.md` and `/.MEMORY/DONE.md` if present.
7) Regenerate Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - rewrite as redirect-only bootstrap content
   - point to project `/.MEMORY/.aidocs/index.aidocs` first, `/.MEMORY/NOW.md` second, and `/.MEMORY/INDEX.md` as the memory router
   - forbid storing memory, plans, summaries, or task output there
   - direct spawned-agent plans/investigations to project `/.MEMORY/agents/` with dated logical filenames
   - do not preserve side content; `MEMORY.md` is not a secondary memory store
8) Hard gate: re-read updated `AGENTS.md`/`CLAUDE.md`, `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, `/.MEMORY/INDEX.md`, and linked docs in-session.
9) Skip heavy ingestion/restructuring unless user requested it.
10) Run health checks (PASS/WARN/FAIL):
   - source resolution
   - global command pack presence
   - canonical memory consistency (+ legacy compatibility state)
   - `/.MEMORY/config/personality.md`
   - `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md`
   - Claude managed MEMORY block
11) Output concise report: updated/unchanged/errors/migration + health + command pack version.

Memory discipline
- Read `/.MEMORY/.aidocs/index.aidocs` first, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`, before work and after resume.
- Append notable update outcomes to daily log.

Extra constraints:
$ARGUMENTS
