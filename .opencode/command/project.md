---
description: Initialize project context and memory
agent: build
---
Run `/project` with minimal noise.

Rules
- Root = directory containing `AIDOCS.md`.
- Read in order: `AIDOCS.md` -> `.aidocs/index.aidocs` -> linked docs.
- Work only inside Root unless explicitly instructed.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) If project is not git: init -> initial commit.
   - If `.git` exists, skip bootstrap git commands.
3) Ensure remote exists:
   - if missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if GH CLI create fails, STOP and ask for remote URL/repo target
4) Verify upstream tracking; STOP on failure.
5) Deploy portable system into project root (self-contained, machine-portable):
   - copy `AIDOCS.md` + `.aidocs/index.aidocs` + linked shared docs required by the index
   - copy `.opencode/command/project.md` + `.opencode/command/archive.md`
   - copy `.claude/commands/project.md` + `.claude/commands/archive.md`
   - refresh project `AGENTS.md`/`CLAUDE.md` with project-local relative routing (no absolute paths)
   - if any required source artifact is missing, STOP and ask
6) Discover project docs/router + relevant README/spec docs.
7) Ensure memory by template copy: copy missing files from `.aidocs/templates/memory/**` to `/.MEMORY/**`.
8) First-init extraction sources:
    - root shared docs
    - project entry files
    - project docs/router + relevant README/spec docs
9) Build topic map and create/update `/.MEMORY/domains/<topic>.md`.
10) Ingest/restructure docs into canonical memory categories (merge/edit; no append duplicates).
11) Output concise report: checklist + portable artifacts synced + memory files updated + ingestion coverage (read/skipped/reasons + mapping).

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
- non-secret details only, read-only interrogation suggestions, no destructive DB ops.

Extra constraints:
$ARGUMENTS
