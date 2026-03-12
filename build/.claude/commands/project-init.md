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
4) Resolve AIDOCS source from global routing; sync project-local docs only:
    - `.aidocs/index.aidocs` + linked `.aidocs/*.aidocs`
    - `.aidocs/personalities/`
    - refresh project `AGENTS.md` and `CLAUDE.md` routers in this order: `/.MEMORY/INDEX.md`, `/.MEMORY/NOW.md`, `.aidocs/index.aidocs`
    - create project-root `/agents/` for spawned-agent plans/investigations (`YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`)
5) Command model is global-only:
   - do not copy `.opencode/command/*` or `.claude/commands/*` into project
   - if global command pack is stale/missing, instruct `scripts/install-agent-routing.cmd` from AIDOCS source
6) Create memory skeleton by copying missing files from `.aidocs/templates/memory/**` to `/.MEMORY/**`.
7) Write the Claude auto-memory redirect file. Resolve `~/.claude/projects/<resolved>/memory/MEMORY.md`
   by matching existing normalized project folders first, else use a deterministic candidate from the project path.
   Rewrite the file as redirect-only content:
   ```
   # AIDOCS Memory Redirect

   Do not store memory, plans, summaries, or task output here.

   Read first:
   <absolute-path-to-project>/.MEMORY/INDEX.md

   Runtime state:
   <absolute-path-to-project>/.MEMORY/NOW.md

   Write only inside:
   <absolute-path-to-project>/.MEMORY/

   Spawned-agent artifacts:
   <absolute-path-to-project>/agents/
   Use `YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`

   Plans:
   <absolute-path-to-project>/.MEMORY/plans/
   ```
   Do not preserve side content in `MEMORY.md`; it is bootstrap only.
8) Report created/updated items.
9) Next action: run `/reingest` and pick `full-reingest`.

Memory discipline
- Read `/.MEMORY/INDEX.md` first, then `/.MEMORY/NOW.md`, before work and after resume.
- Update NOW on material state changes.
- Write daily outcomes; respect priority markers from global instructions.

Extra constraints:
$ARGUMENTS
