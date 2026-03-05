---
description: Remove orphaned and one-time-use files from project
---
File-level clutter cleanup only.

Flow
1) Require `/.MEMORY/`.
2) Scan whole repo for orphaned temp/agent artifacts (completed plans, temp/debug files, empty placeholders, misplaced scratch files).
3) Present candidate file list with short rationale.
4) STOP for confirmation.
5) Delete approved items.
6) Report deleted/skipped + bytes reclaimed.

Boundaries
- Do not edit code contents.
- Never remove source/config/docs/active memory/manual user files.
- If uncertain, ask.

Guardrail
- Always STOP before delete; log actions to daily.

Extra constraints:
$ARGUMENTS
