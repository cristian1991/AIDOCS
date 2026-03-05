---
description: Remove orphaned files and dead code from project
---
`/clean` plus dead-code removal.

Flow
1) Run `/clean` flow.
2) Find provably dead code (unused members/imports, dead commented blocks, unused CSS selectors).
3) Present grouped findings.
4) STOP for approval (all/per-file/per-item).
5) Remove approved items.
6) Report modified files + removed/skipped items.

Boundaries
- No refactor/restructure/merging.
- Skip dynamic-reference code and intent-marked code (`TODO`, `HACK`, etc.).
- If uncertain, ask.

Guardrails
- Verify references before claiming unused.
- Always STOP before edits; log to daily.

Extra constraints:
$ARGUMENTS
