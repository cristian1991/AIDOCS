---
name: test-driven-validation
description: Prioritize validation evidence, edge cases, and feedback from test runs.
kind: verification
tags: testing, validation, edge-cases
---

# test-driven-validation

Use when behavior changes need proof through tests or verification commands.

Do:
- write or update the smallest test that proves the intended behavior
- prefer a meaningful failing test when one is possible
- treat setup noise or missing initialization failures as weak signal, not useful red
- add regression coverage for bug fixes when practical
- run the proving commands and report the real results

Do not:
- worship red for its own sake
- count an unrelated setup failure as meaningful test-first progress
- claim success without fresh verification evidence
- add tests that only prove implementation details when behavior can be tested directly
