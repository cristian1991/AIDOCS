# Workflow Inspection Artifacts

- `index.html`
  Main static workflow-debug page for the current AIDOCS flow map.

- `flows.json`
  Structured source-of-truth inventory for traced workflow steps, branches, and lane assignments.

- `sources.md`
  Evidence ledger of files and entrypoints used for the audit.

- `gaps.md`
  Short list of observed current-flow gaps or host divergences.

The inspection is intentionally grounded in current code paths first.
Docs and tests are secondary references, not truth.
