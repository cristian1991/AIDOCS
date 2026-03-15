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
4) Resolve AIDOCS source from global routing (runtime/public root; normally `build/`); sync project-local docs only:
    - create/update `/.MEMORY/CHANGELOG.md`
    - rewrite/copy `/.MEMORY/.aidocs/index.aidocs`
    - rewrite/copy `/.MEMORY/.aidocs/global-instructions.aidocs`
    - rewrite/copy `/.MEMORY/.aidocs/coding-standards.aidocs`
    - rewrite/copy `/.MEMORY/.aidocs/memory-system.aidocs`
    - copy other `/.MEMORY/.aidocs/*.aidocs` shipped by the source index
    - `/.MEMORY/.aidocs/personalities/`
    - refresh project `AGENTS.md` and `CLAUDE.md` routers in this order: `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, `/.MEMORY/INDEX.md`
    - create `/.MEMORY/agents/` for spawned-agent plans/investigations (`YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`)
5) Command model is global-only:
   - do not copy `.opencode/command/*` or `.claude/commands/*` into project
   - if global command pack is stale/missing, instruct `scripts/install-agent-routing.cmd` from AIDOCS source
6) Create memory skeleton by copying missing files from `/.MEMORY/.aidocs/templates/memory/**` to `/.MEMORY/**`.
   Also create missing `/.MEMORY/CHANGELOG.md` from `/.MEMORY/.aidocs/templates/CHANGELOG.md`.
7) Write the Claude auto-memory redirect file. Resolve `~/.claude/projects/<resolved>/memory/MEMORY.md`
   by matching existing normalized project folders first, else use a deterministic candidate from the project path.
   Rewrite the file as redirect-only content:
   ```
   # AIDOCS Memory Redirect

   Do not store memory, plans, summaries, or task output here.

   Read first:
   <absolute-path-to-project>/.MEMORY/.aidocs/index.aidocs

   Runtime state:
   <absolute-path-to-project>/.MEMORY/NOW.md

   Memory router:
   <absolute-path-to-project>/.MEMORY/INDEX.md

   Write only inside:
   <absolute-path-to-project>/.MEMORY/

   Spawned-agent artifacts:
   <absolute-path-to-project>/.MEMORY/agents/
   Use `YYYY-MM-DD-<topic>-plan.md` or `YYYY-MM-DD-<topic>-investigation.md`

    ```
    Do not preserve side content in `MEMORY.md`; it is bootstrap only.
8) Report created/updated items.
9) Next action: run `/reingest` and pick `full-reingest`.

Memory discipline
- Read `/.MEMORY/.aidocs/index.aidocs` first, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`, before work and after resume.
- Update NOW on material state changes.
- Write daily outcomes; respect priority markers from global instructions.

Extra constraints:
$ARGUMENTS
