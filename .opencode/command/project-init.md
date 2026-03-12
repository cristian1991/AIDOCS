---
description: Initialize project context and memory
agent: build
---
Initialize an already-selected project directory.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Git bootstrap only if missing `.git` (init + initial commit).
3) Ensure remote/upstream:
   - if remote missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if remote/upstream cannot be verified, STOP and ask.
4) Resolve AIDOCS source from global routing (runtime/public root; normally `build/`); sync project-local docs only:
   - rewrite/copy `.aidocs/index.aidocs`
   - rewrite/copy `.aidocs/global-instructions.aidocs`
   - rewrite/copy `.aidocs/coding-standards.aidocs`
   - rewrite/copy `.aidocs/memory-system.aidocs`
   - copy other `.aidocs/*.aidocs` shipped by the source index
   - `.aidocs/personalities/`
   - refresh project `AGENTS.md` and `CLAUDE.md` routers in this order: `.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, `/.MEMORY/INDEX.md`
   - create project-root `/agents/` for spawned-agent plans/investigations (`YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`)
5) Command model is global-only:
   - do not copy `.opencode/command/*` or `.claude/commands/*` into project
   - if global command pack is stale/missing, instruct `scripts/install-agent-routing.cmd` from AIDOCS source
6) Create memory skeleton by copying missing files from `.aidocs/templates/memory/**` to `/.MEMORY/**`.
7) Also create/update Claude redirect bootstrap `~/.claude/projects/<resolved>/memory/MEMORY.md`:
   - resolve existing match first, else deterministic candidate
   - rewrite as redirect-only bootstrap content
   - point to project `.aidocs/index.aidocs` first, `/.MEMORY/NOW.md` second, and `/.MEMORY/INDEX.md` as the memory router
   - forbid storing memory, plans, summaries, or task output there
   - direct spawned-agent plans/investigations to project `/agents/` with dated logical filenames
8) Report created/updated items.
9) Next action: run `/reingest` and pick `full-reingest`.

Memory discipline
- Read `.aidocs/index.aidocs` first, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`, before work and after resume.
- Update NOW on material state changes.
- Write daily outcomes; respect priority markers from global instructions.

Extra constraints:
$ARGUMENTS
