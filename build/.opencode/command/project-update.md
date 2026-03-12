---
description: Update project-local AI system files only
agent: build
---
Sync an initialized project to current AIDOCS contracts (without full init).

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require initialized project (`/.MEMORY/` + `AGENTS.md` or `CLAUDE.md`), else STOP -> `/project-init`.
3) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Never guess.
4) Sync project-local artifacts from source:
   - `.aidocs/index.aidocs` + linked `.aidocs/*.aidocs`
   - `.aidocs/personalities/`
   - refresh project routers (`AGENTS.md`, `CLAUDE.md`) in this order: `/.MEMORY/INDEX.md`, `/.MEMORY/NOW.md`, `.aidocs/index.aidocs`
   - ensure project-root `/agents/` exists for spawned-agent plans/investigations (`YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`)
   - commands are global-only; never copy command files into project
5) Copy missing memory templates from `.aidocs/templates/memory/**`.
6) Normalize memory routing: refresh routing-critical `/.MEMORY/INDEX.md` text, keep project-specific memory facts intact, and preserve legacy dirs with forward-merge when obvious.
7) Regenerate Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - rewrite as redirect-only bootstrap content
   - point to project `/.MEMORY/INDEX.md` first and `/.MEMORY/NOW.md` second
   - forbid storing memory, plans, summaries, or task output there
   - direct spawned-agent plans/investigations to project `/agents/` with dated logical filenames
   - do not preserve side content; `MEMORY.md` is not a secondary memory store
8) Hard gate: re-read updated `AGENTS.md`/`CLAUDE.md`, `/.MEMORY/INDEX.md`, `/.MEMORY/NOW.md`, `.aidocs/index.aidocs`, and linked docs in-session.
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
- Read `/.MEMORY/INDEX.md` first, then `/.MEMORY/NOW.md`, before work and after resume.
- Append notable update outcomes to daily log.

Extra constraints:
$ARGUMENTS
