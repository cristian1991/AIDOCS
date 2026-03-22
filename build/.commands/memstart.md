---
description: Warm startup context from memory routers
---
Prepare session context for an initialized project without changing code.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/INDEX.md`, and `/.MEMORY/sessions/`, else STOP and suggest `/project-update`.
3) Read project router (`AGENTS.md` and/or `CLAUDE.md`) if present.
4) Read `/.MEMORY/.aidocs/index.aidocs`.
5) Read these core setup files from the local `/.MEMORY/.aidocs/` tree:
   - `/.MEMORY/.aidocs/global-instructions.aidocs`
   - `/.MEMORY/.aidocs/coding-standards.aidocs`
   - `/.MEMORY/.aidocs/memory-system.aidocs`
6) Read `/.MEMORY/INDEX.md`.
7) Inspect active sessions under `/.MEMORY/sessions/*/SESSION.md`.
8) If exactly one clearly suitable session exists, read that `SESSION.md`; if multiple plausible sessions exist or none is suitable, issue STOP and ask whether to resume an existing session or create a new one.
9) Read only the high-signal files linked by the selected session and the memory index that are relevant for startup readiness. Do not do a broad repo scan.
10) Output a concise readiness report:
   - startup files read
   - selected session (or session-selection issue)
   - major durable rule/system areas available
   - active blockers if present
   - missing/stale setup files, with `/project-update` recommendation if needed

Boundaries
- Read/analysis only; do not edit code or memory unless the user asks.
- Do not read the full repo.
- Do not invent missing setup files; report them.

Use case
- Best for session start, restart, resume-after-compaction, or before deep project work.

Extra constraints:
$ARGUMENTS
