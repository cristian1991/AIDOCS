---
description: Initialize project context and memory
agent: build
---
Run `/project-init` with minimal noise.

Rules
- Work only inside the target project unless explicitly instructed.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) If project is not git: init -> initial commit.
   - If `.git` exists, skip bootstrap git commands.
3) Ensure remote exists:
   - if missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if GH CLI create fails, STOP and ask for remote URL/repo target
4) Verify upstream tracking; STOP on failure.
5) Copy portable system into project root (self-contained, machine-portable):
   - copy `.aidocs/index.aidocs` + linked shared docs required by the index
   - copy `.opencode/command/project-init.md` + `.opencode/command/project-update.md` + `.opencode/command/archive.md` + `.opencode/command/personality.md` + `.opencode/command/memory-update.md` + `.opencode/command/clean.md` + `.opencode/command/uber-clean.md` + `.opencode/command/refactor.md` + `.opencode/command/uber-refactor.md`
   - copy `.claude/commands/project-init.md` + `.claude/commands/project-update.md` + `.claude/commands/archive.md` + `.claude/commands/personality.md` + `.claude/commands/memory-update.md` + `.claude/commands/clean.md` + `.claude/commands/uber-clean.md` + `.claude/commands/refactor.md` + `.claude/commands/uber-refactor.md`
   - copy `.aidocs/personalities/` directory (all personality definition files)
   - refresh project `AGENTS.md`/`CLAUDE.md` with project-local relative routing (no absolute paths)
   - if any required source artifact is missing, STOP and ask
6) Ensure memory structure by template copy: copy missing files from `.aidocs/templates/memory/**` to `/.MEMORY/**`.
7) Output concise report: checklist + portable artifacts synced + memory structure created.
8) Instruct user: "Run `/memory-update` to populate memory from project docs."

Runtime memory
- read `/.MEMORY/NOW.md` at task start before planning/actions
- after compaction/session resume, re-read `/.MEMORY/NOW.md` before continuing (hard gate)
- update `/.MEMORY/NOW.md` on material state changes
- maintain queue-oriented `NOW.md` (`Active` + ordered `Upcoming` + `Resume Steps`)
- for multi-step automation, keep at least 3 upcoming items when available
- apply priority model `!`..`!!!!!` from global instructions for capture/ordering/escalation
- user priority markers override agent importance judgment
- if user marks `!!!` or higher, persist immediately to canonical memory + daily (do not defer to `/archive`)
- append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`
- before final coding response, checkpoint `Current State`, `Next Action`, and refreshed `Upcoming`

DB safety
- non-secret details only, read-only DB command suggestions, no destructive DB ops

Extra constraints:
$ARGUMENTS
