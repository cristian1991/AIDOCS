---
description: Update project-local AI system files only
agent: build
---
Run `/project-update` to sync system artifacts without re-running full `/project` initialization.

Rules
- Root = directory containing `AIDOCS.md`.
- Read in order: `AIDOCS.md` -> `.aidocs/index.aidocs` -> linked docs.
- Work only inside Root unless explicitly instructed.

Intent
- Update an already initialized project's portable AI runtime files.
- Do not redo ingestion/bootstrap flows that `/project` performs.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) Validate project already initialized (must contain `/.MEMORY/` and project `AGENTS.md` or `CLAUDE.md`).
   - if not initialized, STOP and instruct to run `/project`.
3) Sync project-local system artifacts from Root canonical sources:
   - `AIDOCS.md`
   - `.aidocs/index.aidocs`
   - shared docs linked by index (`.aidocs/*.aidocs`)
   - `.opencode/command/project.md`
   - `.opencode/command/archive.md`
   - `.opencode/command/project-update.md`
   - `.claude/commands/project.md`
   - `.claude/commands/archive.md`
   - `.claude/commands/project-update.md`
4) Refresh project `AGENTS.md` and `CLAUDE.md` as short routers using project-local relative paths (no absolute machine paths).
5) Ensure memory templates are present by copying only missing files from `.aidocs/templates/memory/**`.
   - never overwrite existing `/.MEMORY/*` during `/project-update`.
6) Do not run first-init extraction, topic-map rebuild, or docs->memory restructuring unless user explicitly requests it.
7) Output concise sync report: updated files, unchanged files, missing-source errors, and skipped heavy-init steps.

Runtime memory
- read `/.MEMORY/NOW.md` at task start before planning/actions
- after compaction/session resume, re-read `/.MEMORY/NOW.md` before continuing (hard gate)
- append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`

Extra constraints:
$ARGUMENTS
