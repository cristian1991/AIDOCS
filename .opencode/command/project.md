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
   - If `.git` already exists, skip all bootstrap git commands.
3) Ensure remote exists:
   - if missing, try `gh repo create <name> --private --source . --remote origin --push`
   - if GH CLI create fails, STOP and ask for remote URL/repo target
4) Verify upstream tracking; STOP on failure.
5) Discover project docs/router + relevant README/spec docs.
6) Ensure memory by template copy (no reinvention): copy missing files from `.aidocs/templates/memory/**` to project `/.MEMORY/**`.
7) Ensure project archive command exists: `.opencode/command/archive.md`.
8) First-init extraction order:
   - root shared docs (`AIDOCS.md` + linked `.aidocs/*`)
   - project `AGENTS.md` / `CLAUDE.md`
   - project docs/router + relevant README/spec docs
9) Extract durable rules/caveats/decisions into categorized memory files using merge/edit (no append duplicates).
10) Update project `AGENTS.md`/`CLAUDE.md` to reference `/.MEMORY/*` as canonical operational memory.
11) Output concise result: checklist + Option A/Option B + recommendation.

Memory write policy
- Not init-only: update `/.MEMORY/NOW.md` on material state changes.
- Append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`.
- Before final coding response, checkpoint `Goal | State | Next` in `/.MEMORY/NOW.md`.

DB safety
- non-secret details only, read-only interrogation suggestions, no destructive DB ops.

Extra constraints:
$ARGUMENTS
