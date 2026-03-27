# AIDOCS Roadmap (v2.0.0)

This is the consolidated roadmap for AIDOCS after the 1.2.x product-hardening cycle and the first collaboration-continuity foundations.

It combines the unfinished or still-unreached goals from:

- the former `v1.2.0` product-hardening roadmap
- the former `v1.3.0` collaboration-continuity roadmap
- the former user-extensible indexing direction/spec
- the former public contributor roadmap

## Theme

Make AIDOCS excellent at real agent work on real projects:

- stronger deep retrieval
- stronger collaboration continuity
- user-extensible indexing
- honest benchmarks
- safer contribution and release operations

## Current State

Already implemented strongly:

- routed startup and managed-mode MCP workflow
- strong session/plan/handoff/resume foundations
- production-grade precision retrieval surfaces
- CLI maturity and structured JSON output
- benchmark foundation and automation
- public/private release-security model foundations

Still incomplete or not yet at end-goal level:

- collaboration continuity beyond the first foundation
- user-extensible indexing
- benchmark maturity as a full release story
- deeper cross-project collaboration linking
- stronger task-lifecycle enforcement
- hosted docs rollout
- a small number of broad discovery/trace modes that still need ongoing refinement in large projects

## v2.0.0 End Goals

### End Goal 1: Production-Grade Deep Retrieval
- The major retrieval surfaces are consistently useful for real deep work, not only for orientation.
- Precision tools, broad discovery tools, and trace tools work together with tunable breadth/depth/focus.
- Agents can do most implementation and review work without falling back to blind grep/read cycles.

### End Goal 2: Real Collaboration Continuity
- Sessions support living collaboration state, not just notes.
- Handoffs, plans, journals, and resume bundles work together as a coherent continuity layer.
- Successor agents can resume work from changed/open/reset state instead of rereading everything.

### End Goal 3: User-Extensible Indexing
- Users can safely extend indexing behavior without modifying core code.
- AIDOCS supports tiered language/indexing capability instead of pretending equal semantic depth everywhere.
- Community-driven language upgrades have a clear architecture path.

### End Goal 4: Benchmarks That Actually Mean Something
- Benchmarks reflect realistic project work.
- Public scenario sets are credible and repeatable.
- Benchmark outputs validate tool quality instead of replacing it.

### End Goal 5: Safe Open Contribution Model
- Public PR testing is useful but unprivileged.
- Private validation remains protected.
- Release automation and artifact boundaries are explicit and safe.

## Priority Snapshot

| Priority | Area | Goal |
|---|---|---|
| P0 | deep retrieval quality | make the core MCP/code/schema toolchain consistently excellent for real work |
| P0 | collaboration continuity | complete the next collaboration layer after the 1.2.x foundations |
| P1 | user-extensible indexing | let users extend indexing safely and declaratively |
| P1 | task lifecycle and operator summaries | make state/reporting more complete without relying on agent memory |
| P2 | benchmark maturity | evolve benchmark infrastructure into a real product-quality validation system |
| P2 | docs-site implementation | move from docs IA readiness to actual hosted docs rollout |
| P3 | future host/session adapters | defer true host-session import or richer host-native continuity to later work |

## Workstreams

## 1. Deep Retrieval Quality

### Why
The most important promise of AIDOCS is that agents can work deeply and correctly, not just search broadly.

### Continue / finish
- keep improving broad modes like `code_investigate`, `code_trace`, `code_find` so they remain useful on large real projects
- preserve the current strong precision chain:
  - service API
  - method signatures
  - constructor params
  - enum values
  - entity properties
  - batch retrieval
- keep tuning breadth/depth/focus/noise controls based on real feedback
- close the last-mile editing-context gap where agents still need exact local HTML/text blocks to make safe replacements
- add a line/snippet-context retrieval surface for surgical editing workflows where symbol-based retrieval is not enough

### Done when
- broad modes are consistently usable, not only precision tools
- agents can do deep workflow work with less ad hoc fallback

## 2. Collaboration Continuity, Phase 2

### Why
The first continuity layer exists, but it is still only a foundation.

### Continue / finish
- richer step-state handoff semantics
- stronger incremental consumption of changed/open/reset work
- better cross-project linking and related-session context
- operator-facing collaboration summaries
- stronger freshness/trust signals
- better handling of failed approaches and retesting loops

### Done when
- collaboration state is easy to resume and update continuously
- multi-session work feels connected rather than ad hoc

## 3. Task Lifecycle Enforcement

### Why
Task state still depends too much on agents remembering to close loops manually.

### Continue / finish
- edit-count or work-count based lifecycle nudges
- stronger journaling guarantees
- session summaries that reflect what actually happened, not just what was intended

### Done when
- unreported work becomes harder to lose
- journals and handoffs stay accurate with less manual discipline required

## 4. User-Extensible Indexing

### Why
Real projects use more languages and structures than the built-in list can cover deeply.

### Continue / finish
- declarative language registration
- include/exclude and module/role hint extensions
- support tiers for indexing quality
- heuristic structure rules
- validation for custom indexing config
- clear community contribution path for richer language support
- richer hybrid-language semantics for files like Razor where symbol-only models are insufficient
- descriptor/family support for constructs such as partial refs, tag-helper refs, translation keys, and model-binding references where they materially improve retrieval

### Done when
- unsupported languages can still gain useful indexing support without core patches
- users understand the difference between weak, heuristic, and rich support levels

## 5. Benchmark Maturity

### Why
The benchmark system now exists, but it should mature only after tool quality is strong enough to justify it.

### Continue / finish
- real project corpora
- public/private scenario set split
- result history and trend comparison
- richer benchmark artifacts
- benchmark guidance tied to tool quality, not vanity claims

### Done when
- benchmarks are useful release evidence and quality regression signals
- they remain honest and task-realistic

## 6. Docs-Site Rollout

### Why
The docs IA is planned; the hosted docs still need to be delivered.

### Continue / finish
- build the real docs surface for:
  - `docs.codenexus.cloud/aidocs`
- keep the product split clear relative to sibling CodeNexus products
- turn current README/runtime/host/benchmark docs into the hosted structure

### Done when
- AIDOCS has a real hosted docs surface that matches the current product story

## 7. Safe Contribution and Release Operations

### Why
Public contribution and private validation need to stay safe as the project grows.

### Continue / finish
- maintain the dual public/private CI model
- keep benchmark/public/private boundaries explicit
- keep public-tree and artifact verification strong
- refine maintainer-triggered private validation workflows

### Done when
- contributors can participate safely
- private validation stays protected

## Public Contributor Focus

High-value contribution areas:

1. user-extensible indexing
2. deep retrieval quality on hard real projects
3. benchmark corpora and benchmark review
4. collaboration continuity UX
5. docs and onboarding quality

## Non-Goals

- pretending AIDOCS is a full host-native shared session system
- claiming universal first-class language support on day one
- using benchmark results as marketing proof before the tools are genuinely strong enough

## Release Criteria for v2.0.0

- the main retrieval surfaces are consistently strong on real projects
- collaboration continuity is materially beyond the 1.2.x foundation
- user-extensible indexing exists in a safe declarative form
- benchmark maturity is real enough to support release validation honestly
- docs and contribution workflows match the actual product

## Summary

`v2.0.0` should be the release where AIDOCS becomes:

- production-grade for deep work
- extensible for more ecosystems
- much better at collaboration continuity
- credible in its benchmark and contribution story
