---
description: Clean dead code and consolidate duplicates
agent: build
---
`/uber-clean` plus deduplication.

Flow
1) Run `/uber-clean` flow.
2) Find duplicate/near-duplicate logic (functions, queries, DTOs/models, repeated blocks/styles).
3) Propose consolidation per duplicate group (keep/extract target + call-site updates).
4) STOP for approval (all/per-group).
5) Apply approved consolidations.
6) Report found/applied/skipped groups and modified files.

Boundaries
- No architectural/file-splitting changes.
- No public API rename without approval.
- Prefer existing helper/utility files.

Guardrails
- Preserve edge cases.
- Always STOP before refactor; log to daily.

Extra constraints:
$ARGUMENTS
