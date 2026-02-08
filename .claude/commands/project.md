---
description: Initialize project context and memory
---
Run `/project` with minimal noise.

Rules
- Root = directory containing `AIDOCS.md`.
- Read in order: `AIDOCS.md` -> `.aidocs/index.aidocs` -> linked docs.
- Work only inside Root unless explicitly instructed.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) If project is not git: init -> initial commit.
   - If `.git` already exists, skip all bootstrap git commands.
3) Ensure remote exists:
   - if missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if GH CLI create fails, STOP and ask for remote URL/repo target
4) Verify upstream tracking; STOP on failure.
5) Discover project docs/router + relevant README/spec docs.
6) Ensure memory by template copy: copy missing files from `.aidocs/templates/memory/**` to project `/.MEMORY/**`.
7) Ensure project archive command exists: `.claude/commands/archive.md`.
8) First-init extraction order: root shared docs -> project entry files -> project docs/router + README/spec docs.
9) Extract durable rules/caveats/decisions into categorized memory files with merge/edit semantics.
10) Update `AGENTS.md`/`CLAUDE.md` to reference `/.MEMORY/*` as canonical operational memory.
11) Output concise result: checklist + Option A/Option B + recommendation.

Memory write policy
- update `/.MEMORY/NOW.md` on material state changes
- append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`
- before final coding response, checkpoint `Goal | State | Next`

DB safety
- non-secret details only, read-only DB command suggestions, no destructive DB ops

Extra constraints:
$ARGUMENTS
