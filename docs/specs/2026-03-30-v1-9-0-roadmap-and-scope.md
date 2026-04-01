# AIDOCS v1.9.0 Roadmap And Scope

## Goal

Define the final pre-2.0 release boundary for AIDOCS: a deterministic, GUI-backed, single-project workflow system with bundled skills, hardened conductor execution, and a cleaner host/tool surface.

`v1.9.0` is the stabilization release before the harder `v2.0.0` work on cross-host / cross-agent / cross-project protocol.

## Release Theme

Make AIDOCS excellent as a deterministic single-project workflow system.

That means:

- runtime-owned workflow authority
- bundled curated skills
- GUI/control plane readiness
- stronger conductor lifecycle
- better host/plugin/tool UX
- more reliable roadmap / spec / plan handling

## Pillars

### 1. Config Standardization

Normalize configuration into stable scopes and policy domains so the system becomes easier to reason about and GUI-ready.

Key directions:

- global / project / session scopes
- explicit precedence
- policy config vs runtime state separation
- stable schema for later GUI editing

### 2. Bundled Skill Provider

AIDOCS should ship with a curated bundled provider derived from superpowers-compatible skill content.

Key directions:

- bundled provider enabled by default
- no manual user registration for the primary experience
- override policy for orchestration-critical skills
- keep external-style content where it helps, but keep AIDOCS as the authority

### 3. Conductor Hardening

The conductor is now implemented, but it still needs to become truly trustworthy for single-project execution.

Key directions:

- full-suite-aware conductor
- automatic lane reopening
- persistent lane ownership/context
- better failure attribution
- structured intra-project lane signals
- lane state model that distinguishes `implementation_done` from true `completed`

### 4. GUI / Control Plane

Ship the real GUI before cross-agent protocol work.

Key directions:

- configuration editing
- provider/skill selection and compatibility views
- session/roadmap/plan overview
- conductor lane graph/status/blocked state
- inspection and operator-facing summaries

The GUI should be a control/inspection layer over deterministic runtime state, not a separate workflow engine.

### 5. Host / Plugin Simplification

Keep runtime as the single source of truth and make OpenCode/Claude thinner adapters.

Key directions:

- canonical host-state payload
- prompt-time live state vs cached session state separation
- less plugin reconstruction logic
- Claude/OpenCode share the same core decision layer

### 6. Tool Space Reorganization

Reorganize the MCP tool surface so it is cleaner and more intentional.

Key directions:

- keep `aidocs_` prefix
- rename tools around user intent instead of backend storage
- cleaner primary surface
- preserved precision tools
- separated debug/specialist tools
- better UI traces

### 7. Discoverability And Operator UX

Make the system understandable without requiring the operator to already know the internals.

Key directions:

- better overviews
- clearer host-visible state
- surfaced provider/skill/override/conductor decisions
- less hidden magic

### 8. Roadmap / Spec / Plan Indexing And Retrieval

This is now an explicit `v1.9.0` priority.

Why:

- roadmap/spec/plan authoring and retrieval caused repeated friction
- roadmap updates and spec lookups have not felt as robust as code retrieval
- multiple hard patch/edit failures happened while working around plan/roadmap artifacts
- plan indexing is only partially realized today

Key directions:

- treat roadmap/spec/plan artifacts as first-class indexed retrieval targets
- improve exact retrieval of:
  - roadmap sections
  - spec sections
  - plan phases/lanes/tasks
- improve write/update ergonomics for these artifacts
- reduce hard-patch/edit fragility when updating structured planning docs
- support cleaner operator/agent navigation of planning artifacts without broad raw-file reads

This area should be treated similarly to code retrieval quality, not as a secondary documentation concern.

## Non-Goals For v1.9.0

Do not try to complete the following in this release:

- cross-project agent communication protocol
- cross-host communication protocol
- federated or distributed multi-project conductor behavior
- remote provider sync/update systems
- full replacement of all external skill content with AIDOCS-native rewrites

## What Moves To v2.0.0

`v2.0.0` should focus on:

- cross-agent communication protocol
- cross-host / cross-project coordination
- richer federation semantics built on the stable `v1.9.0` control plane
- evolving the GUI into a broader coordination/harness interface when the protocol layer is ready

## Success Criteria For v1.9.0

`v1.9.0` is successful when:

- AIDOCS is clearly the deterministic workflow authority
- bundled skills work out of the box
- conductor can take single-project work to trustworthy green state
- GUI/control plane is useful and grounded in stable config/state
- host/plugin behavior is simpler and cleaner
- tool surface is materially improved
- roadmap/spec/plan retrieval and editing are much more reliable than they are today

## Summary

`v1.9.0` is the release where AIDOCS stops being “a set of promising parts” and becomes a coherent, deterministic single-project workflow platform.

Only after that should `v2.0.0` attempt the harder cross-agent / cross-host coordination problem.
