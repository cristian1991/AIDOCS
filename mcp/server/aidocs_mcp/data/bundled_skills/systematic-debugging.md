---
name: systematic-debugging
description: Investigate root cause before fixing bugs, regressions, failing tests, or unexpected behavior.
kind: reasoning
tags: debugging, evidence, regression
---

# systematic-debugging

Use when debugging a bug, regression, error, failing test, or unexpected runtime behavior.

Do:
- reproduce or capture the failure first
- read the full error output, not just the last line
- trace backward from symptom to source
- compare with a known-good path or pattern when possible
- test one hypothesis at a time
- add or update a regression test before the final fix when appropriate

Do not:
- fix before you have evidence
- stack multiple guesses into one change
- stop at the first plausible explanation
- claim the issue is fixed without rerunning the proving case
