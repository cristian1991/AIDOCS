---
description: Reingest project knowledge by user-selected scope
---
Refresh memory using a user-chosen scope.

Requirement
- A session must already be selected before running `/reingest`.
- If no session is selected, issue STOP and ask whether to resume an existing session or create a new one.

Mandatory first step
- Ask one STOP-style question with options:
  1) `full-reingest` (Recommended) — full docs/topic-map/canonical refresh
  2) `git-reingest` — changed + untracked files only
  3) `date-reingest` — files newer than the selected session/daily baseline
  4) Type your own answer

Run selected mode only
- `full-reingest`: broad discovery + canonical merge refresh.
- `git-reingest`: scope by git status/diff/untracked; update only impacted topics.
- `date-reingest`: derive baseline from the selected session checkpoint or daily-log date; update files modified since.

Always
- Merge/edit memory (no duplicate append-only growth).
- If root `FIXES_BY_OTHER_AGENTS.md` exists, merge into `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md` (dedupe by Date+Issue).
- Output: selected mode, read/skipped files (+why), updated memory files, follow-up recommendation.
- If mode cannot run deterministically, STOP and ask fallback mode.

Extra constraints:
$ARGUMENTS
