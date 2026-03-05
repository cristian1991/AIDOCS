---
description: Update project-local AI system files only
---
Sync an initialized project to current AIDOCS contracts (without full init).

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require initialized project (`/.MEMORY/` + `AGENTS.md` or `CLAUDE.md`), else STOP -> `/project-init`.
3) Resolve AIDOCS source only from global routing (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`). Never guess.
4) Sync project-local artifacts from source:
   - `.aidocs/index.aidocs` + linked `.aidocs/*.aidocs`
   - `.aidocs/personalities/`
   - refresh project routers (`AGENTS.md`, `CLAUDE.md`)
   - commands are global-only; never copy command files into project
5) Copy only missing memory templates from `.aidocs/templates/memory/**` (never overwrite existing memory).
6) Normalize memory routing: canonical categories active; legacy dirs preserved + forward-merged when obvious.
7) Regenerate Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - update managed AIDOCS block only; preserve unmanaged user content
   - managed block includes bootstrap + canonical map + condensed `!!!+` rules/domains, <= 200 lines
8) Hard gate: re-read updated `AGENTS.md`/`CLAUDE.md`, `.aidocs/index.aidocs`, and linked docs in-session.
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
- Read NOW before work and after resume.
- Append notable update outcomes to daily log.

Extra constraints:
$ARGUMENTS
