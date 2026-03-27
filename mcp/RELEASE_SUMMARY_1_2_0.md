# v1.2.0 Completion Summary

This summary captures the main product-hardening work completed for the `v1.2.0` roadmap.

## What Improved

### CLI maturation
- `aidocs init`, `status`, `sync`, `benchmark`, `config`, and `version` now form a more complete operator-facing CLI surface.
- Structured JSON output now exists for the main CLI commands.
- `benchmark` gained scenario sets, multilingual classification coverage, code/schema retrieval coverage, comparative indexed-vs-raw measurement, and export-to-file support.

### Planning defaults
- Sessions now create a canonical `plans/PLAN.md` by default.
- The plan structure now explicitly includes partial goals and an end goal.
- Task lifecycle updates can maintain plan state alongside session/context state, so planning is not left to ad hoc notes.

### Collaboration continuity foundations
- Sessions now have structured handoffs with purpose, dead ends, estimated effort, freshness, related-session context, and step-based collaboration state.
- Resume bundles can package session, context, plan, handoff, handoff steps, journal, and repo summary into one collaboration-oriented retrieval surface.
- Handoffs now behave as living collaboration state rather than one-off exit notes, with actionable and recently changed steps available for successor agents.

### Runtime and bootstrap cleanup
- version lookup now resolves from a shared source instead of hardcoded CLI text
- CLI init reuses shared runtime/bootstrap logic instead of maintaining a separate bootstrap path
- public packaging verification now has an explicit guard step in automation

### Production-grade precision and workflow retrieval
- precision surfaces now include exact method signatures, batched method signatures, constructor params, batched constructor params, enum values, service API lookup, entity property lookup, and batch schema/entity retrieval
- `code_investigate` now supports deeper and more tunable workflow-oriented discovery with `depth` and `focus`
- `code_trace` now supports bounded depth to reduce noise in large projects, and `api_to_ui` has improved concrete service/method tracing
- broader discovery modes like `transitions`, `mutations`, and factory discovery have been tightened to reduce obvious noise and improve practical relevance
- service API lookup now has explicit not-found behavior and stronger partial-class aggregation

### Docs and product story cleanup
- root README is more product-first and less implementation-dump-heavy
- MCP README now teaches the unified tool model instead of dumping a giant tool list
- host integration docs now center the current `/aidocs` + classify/route/orchestrate model
- procedures are documented as optional advanced structure instead of implied first-class prerequisites

### Benchmark foundation
- a public benchmark contract now exists
- benchmark docs are explicit about public vs private benchmark content
- CI can produce public benchmark artifacts

This foundation is included so AIDOCS can validate tool quality honestly. It is not the primary release story for `v1.2.0`; the primary goal is production-grade tooling.

### Docs-site readiness
- a future AIDOCS docs IA is defined for `docs.codenexus.cloud/aidocs`
- the docs split can now move forward from a concrete information architecture instead of a vague direction

## Validation

- `pytest mcp/tests -q` -> `255 passed`
- benchmark/export/config/version JSON behavior covered in CLI tests
- public-tree verification script added for release/public sync hygiene

## What v1.2.0 Means

`v1.2.0` is the point where AIDOCS becomes much more coherent as a product surface:

- stronger CLI
- clearer host behavior contract
- better release discipline and a benchmark foundation for later validation
- cleaner docs/product story
- better readiness for a future hosted docs split

## What Is Still Next

The next major area after this release is deeper collaboration continuity beyond the current foundation:

- richer cross-project linking
- stronger step-aware incremental handoff consumption
- more advanced trust/staleness signals
- optional host-aware session/context import in a later release line
