---
description: Full cleanup, deduplication, and structural analysis
agent: build
---
`/refactor` plus structural analysis/splitting.

Flow
1) Run `/refactor` flow.
2) Find structural issues (oversized files, SRP violations, god classes, deep coupling, mixed concerns).
3) Propose split plan per issue (cut points, target files, reference impact, complexity).
4) STOP for approval on each split.
5) Apply approved splits one by one.
6) Report issues, applied/deferred splits, created/modified files.

Boundaries
- No auto structural changes.
- No public API contract changes without explicit approval.
- Keep within existing architecture style.

Guardrails
- Backup before split to `/.BACKUP/uber-refactor/`.
- STOP before each structural change; log to daily.

Extra constraints:
$ARGUMENTS
