---
description: Update project-local AI system files only
agent: build
---
Run `/project-update` to sync system artifacts without re-running full `/project-init` initialization.

Rules
- Work only inside the target project unless explicitly instructed.

Intent
- Update an already initialized project's portable AI runtime files.
- Do not redo ingestion/bootstrap flows that `/project-init` performs.

Source resolution (!!!)
- AIDOCS source path MUST be read from the global routing file (`~/.claude/CLAUDE.md` or `~/.config/opencode/AGENTS.md`), which contains `AIDOCS source: <path>`.
- Never compute, search, or guess the source path. The installer `.cmd` is the sole authority that sets this path.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) Validate project already initialized (must contain `/.MEMORY/` and project `AGENTS.md` or `CLAUDE.md`).
   - if not initialized, STOP and instruct to run `/project-init`.
3) Sync project-local system artifacts from AIDOCS source:
   - `.aidocs/index.aidocs`
   - shared docs linked by index (`.aidocs/*.aidocs`)
   - `.opencode/command/project-init.md`
   - `.opencode/command/project-update.md`
   - `.opencode/command/archive.md`
   - `.opencode/command/personality.md`
   - `.opencode/command/memory-update.md`
   - `.opencode/command/clean.md`
   - `.opencode/command/uber-clean.md`
   - `.opencode/command/refactor.md`
   - `.opencode/command/uber-refactor.md`
   - `.claude/commands/project-init.md`
   - `.claude/commands/project-update.md`
   - `.claude/commands/archive.md`
   - `.claude/commands/personality.md`
   - `.claude/commands/memory-update.md`
   - `.claude/commands/clean.md`
   - `.claude/commands/uber-clean.md`
   - `.claude/commands/refactor.md`
   - `.claude/commands/uber-refactor.md`
   - `.aidocs/personalities/` directory (all personality definition files)
4) Refresh project `AGENTS.md` and `CLAUDE.md` as short routers using project-local relative paths (no absolute machine paths).
5) Ensure memory templates are present by copying only missing files from `.aidocs/templates/memory/**`.
   - never overwrite existing `/.MEMORY/*` during `/project-update`.
6) Regenerate Claude Code auto-memory file:
   - Compute path: take project root absolute path, replace `:` with empty and all `\` or `/` with `-`. Target: `~/.claude/projects/<encoded>/memory/MEMORY.md`. Create directory if missing.
   - This file is auto-loaded into every session's system prompt.
   - It MUST contain: memory system bootstrap instructions (how to use `/.MEMORY/`), canonical structure reference, and condensed critical learnings from `/.MEMORY/domains/*.md` and `/.MEMORY/rules/*.md`.
   - Extract `!!!` and above rules and write a condensed summary to MEMORY.md.
   - Keep MEMORY.md under 200 lines (truncated after that). Full details stay in `/.MEMORY/` files.
   - MEMORY.md is READ-ONLY during normal sessions — only `/project-update` writes to it.
7) Hard gate after sync: in the same session, re-read updated project routing/instructions before any further task execution:
   - `AGENTS.md` and/or `CLAUDE.md`
   - `.aidocs/index.aidocs`
   - linked shared docs from the index
   (no new session required)
8) Do not run first-init extraction, topic-map rebuild, or docs->memory restructuring unless user explicitly requests it.
9) Output concise sync report: updated files, unchanged files, missing-source errors, and skipped heavy-init steps.

Runtime memory
- read `/.MEMORY/NOW.md` at task start before planning/actions
- after compaction/session resume, re-read `/.MEMORY/NOW.md` before continuing (hard gate)
- append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`

Extra constraints:
$ARGUMENTS
