---
description: Warm startup context from memory routers
---
Prepare session context for an initialized project without changing code.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require `/.MEMORY/.aidocs/index.aidocs` and `/.MEMORY/INDEX.md`, else STOP and suggest `/project-update`.
3) Read project router (`AGENTS.md` and/or `CLAUDE.md`) if present.
4) Read `/.MEMORY/.aidocs/index.aidocs`.
5) Read these core setup files from the local `/.MEMORY/.aidocs/` tree:
   - `/.MEMORY/.aidocs/global-instructions.aidocs`
   - `/.MEMORY/.aidocs/coding-standards.aidocs`
   - `/.MEMORY/.aidocs/memory-system.aidocs`
6) Read `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`.
7) Read only the high-signal files directly linked by the project memory index that are relevant for startup readiness (rules, system, config, active plans, related-project notes when present). Do not do a broad repo scan.
8) Output a concise readiness report:
   - startup files read
   - current mission from `NOW.md`
   - major durable rule/system areas available
   - active plans or blockers if present
   - missing/stale setup files, with `/project-update` recommendation if needed

Boundaries
- Read/analysis only; do not edit code or memory unless the user asks.
- Do not read the full repo.
- Do not invent missing setup files; report them.

Use case
- Best for session start, restart, resume-after-compaction, or before deep project work.

Extra constraints:
$ARGUMENTS
