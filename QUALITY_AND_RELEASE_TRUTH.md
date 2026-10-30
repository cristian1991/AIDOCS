# Quality and Release Truth

This page explains where AIDOCS' published quality signals come from and what they actually prove — so you can trust the badges without taking them on faith. It deliberately does **not** document the internal release pipeline; a security product shouldn't publish a map of its own testing and deploy process.

## The published artifacts

Two files in this repository carry the public quality signal:

1. **`mcp/.deploy-reports/status.json`** — machine-readable badge source (shields.io endpoint format) that the README badges read.
2. **`mcp/.deploy-reports/RELEASE_STATUS.md`** — the human-readable peer of the same data.

Both are **generated, never hand-edited**. The README must not hand-write a pass/fail count or a version — every number you see in a badge originates in the actual release run's output.

## How to read the numbers

- A green **tests** badge means the headline test suite passed on the run that produced the current `status.json`.
- **`unknown` is not `0`.** If a suite didn't run on a given run, the field reads `unknown`/`stale` — missing evidence is never laundered into a green.
- A non-zero failure count colors the badge red. The badge refuses to render green on missing or partial data.

## What stands behind a release

Every release passes a **private gate** before it ships, and the gate — not public CI — is the authority. It runs automated checks and the full test suite, and signs and ships only on success; a failing check aborts the release before anything is published.

Two honesty rules govern all of it:

- **Numbers are generated, not claimed.** Badges and summaries come from the real run; stale or missing evidence is labeled, never presented as success.
- **A green lie is worse than a red truth.** What's published says what actually ran and what didn't.

## Public CI is a peer-audit lane, not the authority

The GitHub Actions workflows here exist for public-facing peer audit — pull-request checks and scheduled re-scans against fresh advisory databases. They complement, rather than replace, the private release gate where authority lives.

## What this page is not

It is not a description of the internal release pipeline, the test layout, or the deploy infrastructure. It is the public contract: what the published signals mean and why you can trust them. The third-party tools AIDOCS relies on are credited in [`ACKNOWLEDGMENTS.md`](ACKNOWLEDGMENTS.md).
