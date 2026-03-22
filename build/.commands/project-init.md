---
description: Initialize project context and memory
---
Initialize an already-selected project directory.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Git bootstrap only if missing `.git` (init + initial commit).
3) Ensure remote/upstream:
   - if remote missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if remote/upstream cannot be verified, STOP and ask.
4) Resolve AIDOCS source from global routing (runtime/public root; normally `build/`); copy the full shipped `/.MEMORY/` scaffold into the project:
   - create missing files/dirs from `<AIDOCS source>/.MEMORY/**`
   - refresh routing/system files under `/.MEMORY/.aidocs/**`
   - keep existing project-specific canonical memory facts/history unless the user explicitly asks to replace them
   - ensure `/.MEMORY/sessions/` and `/.MEMORY/archive/sessions/` exist
5) If project `AGENTS.md` and/or `CLAUDE.md` already exist:
   - read them first
   - promote durable project-specific rules/preferences into canonical memory files under `/.MEMORY/**`
   - then regenerate short router versions that point to `/.MEMORY/.aidocs/index.aidocs` and `/.MEMORY/INDEX.md`
6) Command model is global-only:
   - do not copy command files into project
   - if global command pack is stale/missing, instruct `scripts/install-agent-routing.cmd` from AIDOCS source
7) Use `/.MEMORY/.aidocs/templates/**` only as fallback for any still-missing canonical files after the full-memory copy.
8) Also create/update Claude redirect bootstrap `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - if it already exists, read it first and promote any durable rules/facts/history into canonical files under `/.MEMORY/**` before replacing the shim
   - rewrite as redirect-only bootstrap content
   - point to project `/.MEMORY/.aidocs/index.aidocs` first and `/.MEMORY/INDEX.md` second as the durable-memory router
   - tell Claude to inspect `/.MEMORY/sessions/*/SESSION.md` and select a session before task work
   - forbid storing memory, plans, summaries, or task output there
9) Report created/updated + integrated items.
10) Next action: create or select a session, then run `/reingest` if deeper memory population is needed.

Memory discipline
- Read `/.MEMORY/.aidocs/index.aidocs` first, then `/.MEMORY/INDEX.md`, then select/read a session before work and after resume.
- Update session files on material state changes.
- Write daily outcomes; respect priority markers from global instructions.

Extra constraints:
$ARGUMENTS
