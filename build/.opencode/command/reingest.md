---
description: Reingest project knowledge by user-selected scope
agent: build
---
Refresh memory using a user-chosen scope.

Mandatory first step
- Ask one STOP-style question with options:
  1) `full-reingest` (Recommended) — full docs/topic-map/canonical refresh
  2) `git-reingest` — changed + untracked files only
  3) `date-reingest` — files newer than NOW baseline
  4) Type your own answer

Run selected mode only
- `full-reingest`: broad discovery + canonical merge refresh.
- `git-reingest`: scope by git status/diff/untracked; update only impacted topics.
- `date-reingest`: derive baseline from NOW date/checkpoint; update files modified since.

Always
- Merge/edit memory (no duplicate append-only growth).
- If root `FIXES_BY_OTHER_AGENTS.md` exists, merge into `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md` (dedupe by Date+Issue).
- Output: selected mode, read/skipped files (+why), updated memory files, follow-up recommendation.
- If mode cannot run deterministically, STOP and ask fallback mode.

Extra constraints:
$ARGUMENTS
