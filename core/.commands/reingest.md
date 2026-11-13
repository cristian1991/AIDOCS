---
description: Reingest project knowledge by user-selected scope
command_id: reingest
preferred_executor: advisory
allow_uninitialized: false
---
## Intent
- Refresh project memory using a user-selected reingest scope.

## Inputs
- Use `$ARGUMENTS` when provided.
- If `$ARGUMENTS` does not fully specify the reingest mode, ask the mandatory STOP-style question.

## Preconditions
- A session must already be selected before running `/reingest`.

## Primary Execution
1. Confirm that a session is already selected.
2. If no reingest mode is already specified, ask one STOP-style question with these options:
   1. `full-reingest` (Recommended) - full docs, topic-map, and canonical refresh.
   2. `git-reingest` - changed and untracked files only.
   3. `date-reingest` - files newer than the selected session or daily baseline.
   4. Type your own answer.
3. Run only the selected mode.
4. Merge or edit memory files instead of appending duplicate content.
5. If root `FIXES_BY_OTHER_AGENTS.md` exists, merge it into `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md` and dedupe by `Date + Issue`.

## Branching
- If no session is selected, STOP and ask whether to resume an existing session or create a new one.
- If the selected mode is `full-reingest`, perform broad discovery and canonical merge refresh.
- If the selected mode is `git-reingest`, scope work to changed, diffed, and untracked files and update only impacted topics.
- If the selected mode is `date-reingest`, derive the baseline from the selected session checkpoint or daily-log date and update only files modified since that baseline.
- If the selected mode cannot run deterministically, STOP and ask for a fallback mode.

## STOP Conditions
- Stop if no session is selected.
- Stop if the user must choose a reingest mode.
- Stop if the selected mode cannot run deterministically.

## Output
- Report the selected mode.
- Report read files and skipped files, including why files were skipped.
- Report updated memory files.
- Report a follow-up recommendation.

## Fallback
- If the requested mode is ambiguous or cannot run deterministically, do not guess.
- Ask for a fallback mode and run only the mode the user selects.

## Rules
- Do not create duplicate append-only memory growth.
- Keep the reingest scoped to the selected mode only.
- Preserve canonical memory by merging and deduping instead of blindly appending.

## Arguments
$ARGUMENTS
