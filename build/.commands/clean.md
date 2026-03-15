---
description: Cleanup project by user-selected scope
---
Run cleanup using a user-chosen scope.

Mandatory first step
- Ask one STOP-style question with options:
  1) `file-clean` (Recommended) — orphaned files, temp files, debug artifacts, completed plan files
  2) `dead-code-clean` — `file-clean` plus provably dead code removal
  3) `dedupe-clean` — `dead-code-clean` plus duplicate/near-duplicate consolidation
  4) `structural-clean` — `dedupe-clean` plus structural analysis and file-split proposals
  5) Type your own answer

Run selected mode only
- `file-clean`:
  - scan whole repo for orphaned temp/agent artifacts, empty placeholders, misplaced scratch files
  - present candidate file list with short rationale
  - STOP for confirmation before delete
  - report deleted/skipped items + bytes reclaimed
- `dead-code-clean`:
  - includes `file-clean`
  - find provably dead code (unused members/imports, dead commented blocks, unused CSS selectors)
  - present grouped findings
  - STOP for approval before edits
- `dedupe-clean`:
  - includes `dead-code-clean`
  - find duplicate/near-duplicate logic (functions, queries, DTOs/models, repeated blocks/styles)
  - propose consolidation per duplicate group (keep/extract target + call-site updates)
  - STOP for approval before refactor
- `structural-clean`:
  - includes `dedupe-clean`
  - find structural issues (oversized files, SRP violations, god classes, deep coupling, mixed concerns)
  - propose split plan per issue (cut points, target files, reference impact, complexity)
  - STOP for approval on each structural change individually

Always
- Require `/.MEMORY/`.
- Never remove source/config/docs/active memory/manual user files.
- Skip dynamic-reference code and intent-marked code (`TODO`, `HACK`, etc.) when claiming dead code.
- Preserve edge cases and avoid public API changes without approval.
- Log actions to today's daily file.
- If the selected mode cannot run deterministically, STOP and ask for fallback mode.

Extra constraints:
$ARGUMENTS
