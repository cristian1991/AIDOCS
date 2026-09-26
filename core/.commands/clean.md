---
description: Cleanup project by user-selected scope
command_id: clean
preferred_executor: advisory
allow_uninitialized: false
---
## Intent
- Run cleanup using a user-selected scope.

## Inputs
- Use `$ARGUMENTS` when provided.
- If `$ARGUMENTS` does not fully specify the cleanup mode, ask the mandatory STOP-style question.

## Preconditions
- `/.MEMORY/` must exist.

## Primary Execution
1. Confirm that `/.MEMORY/` exists.
2. If no cleanup mode is already specified, ask one STOP-style question with these options:
   1. `file-clean` (Recommended) - orphaned files, temp files, debug artifacts, stale session-local plan files, and stale session-local artifact files.
   2. `dead-code-clean` - `file-clean` plus provably dead code removal.
   3. `dedupe-clean` - `dead-code-clean` plus duplicate and near-duplicate consolidation.
   4. `structural-clean` - `dedupe-clean` plus structural analysis and file-split proposals.
   5. Type your own answer.
3. Run only the selected mode.
4. Log actions to today's daily file.

## Branching
- If the selected mode is `file-clean`, scan the repo for orphaned temp files, debug artifacts, empty placeholders, stale session-local artifacts, and misplaced temporary files; present candidates with short rationale; STOP for confirmation before deletion; then report deleted and skipped items plus bytes reclaimed.
- If the selected mode is `dead-code-clean`, include `file-clean`, then find provably dead code such as unused members, unused imports, dead commented blocks, and unused CSS selectors; present grouped findings; STOP for approval before edits.
- If the selected mode is `dedupe-clean`, include `dead-code-clean`, then find duplicate or near-duplicate logic such as functions, queries, DTOs, models, repeated blocks, and repeated styles; propose consolidation per duplicate group; STOP for approval before refactor.
- If the selected mode is `structural-clean`, include `dedupe-clean`, then find structural issues such as oversized files, SRP violations, god classes, deep coupling, and mixed concerns; propose a split plan per issue; STOP for approval on each structural change individually.
- If the selected mode cannot run deterministically, STOP and ask for a fallback mode.

## STOP Conditions
- Stop if `/.MEMORY/` is missing.
- Stop if the user must choose a cleanup mode.
- Stop before deletion, refactor, or structural changes when explicit approval is required.
- Stop if the selected mode cannot run deterministically.

## Output
- Report the selected mode.
- Report findings, candidate items, or proposals relevant to that mode.
- Report deleted items, skipped items, edits made, or bytes reclaimed when applicable.
- Report follow-up work that still requires approval.

## Fallback
- If the requested cleanup mode is ambiguous or cannot run deterministically, do not guess.
- Ask for a fallback mode and run only the mode the user selects.

## Rules
- Never remove source files, config files, docs, active memory files, or manual user files.
- Skip dynamic-reference code and intent-marked code such as `TODO` or `HACK` when claiming dead code.
- Preserve edge cases.
- Avoid public API changes without approval.

## Arguments
$ARGUMENTS
