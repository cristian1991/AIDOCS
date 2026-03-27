# AIDOCS Roadmap (v1.2.0)

## Purpose

This roadmap defines the next consolidation and product-hardening phase after the 1.1.x line.

The goal is not just to add features. The goal is to make AIDOCS easier to operate, easier to integrate, and easier for agents to use correctly.

## Current State

AIDOCS already has strong foundations:

- file-backed canonical memory and session model
- MCP runtime with unified code and schema entry points
- Claude hook integration with runtime-guided routing
- OpenCode plugin integration with command-aware gating and advisory classification
- multilingual `action_tokens`
- a new `aidocs` CLI surface

But the system still has visible gaps:

- host behavior is not fully aligned across Claude and OpenCode
- versioning and release surfaces can drift
- docs and roadmap state are behind the current implementation
- some runtime surfaces are broader than necessary or not fully productized
- CLI bootstrap logic duplicates runtime logic in places
- procedures and action-surface capabilities are present, but their product role is still unclear

## Priority Snapshot

| Priority | Item | Current interpretation |
|---|---|---|
| P0 | Remove deprecated / confusing surfaces | Continue trimming or demoting surfaces that compete with the unified entry-point model |
| P0 | PyPI package and release hygiene | Make `pip install aidocs-mcp` and `aidocs` CLI packaging/versioning boring and reliable |
| P1 | CLI hardening | The CLI exists; now make it a thin, production-ready surface over shared runtime logic |
| P2 | Benchmarks | Build the benchmark foundation now, but treat full benchmark positioning as a later release concern |
| P2 | Documentation site | Content cleanup comes first; publishing/hosting comes after the docs stabilize |
| P2 | Test hardening | Move beyond raw test count and strengthen CLI, multilingual, installer, and packaging coverage |
| P3 | Enterprise feature design | Keep this out of the critical path until the core product surfaces are stable |

## v1.2.0 End Goals

By the end of v1.2.0, AIDOCS should feel like a coherent product rather than a strong collection of parts.

### End Goal 1: Clear Product Surfaces
- `core/` is the canonical portable instruction and command-spec layer.
- `mcp/` is the reference runtime and indexing layer.
- `aidocs` CLI is the operator-facing local shell entrypoint.
- Claude and OpenCode integrations are documented as distinct host adapters with explicit capability boundaries.

### End Goal 2: Stable Host Behavior
- Claude remains the strongest runtime-driven integration.
- OpenCode uses the best available command-aware and classification-aware path without overstating enforcement.
- `/aidocs` is consistently treated as the entry command across host surfaces.
- host docs describe what is enforced, what is advisory, and what still depends on model judgment.

### End Goal 3: Reduced Cognitive Load
- the public MCP tool story is simple and teaches agents where to start
- deprecated or confusing surfaces are removed, hidden, or clearly marked advanced
- docs reflect current reality, not historical transitions

### End Goal 4: Strong Packaging and Release Hygiene
- versioning comes from a single source of truth
- CLI version output, package metadata, docs, and release tags stay aligned
- public release packaging avoids private artifacts by design
- installer behavior is cross-platform and documented accurately

### End Goal 5: Reliable Classification and Routing
- `action_tokens` are good enough across supported languages to provide useful advisory classification
- routing guidance is phrased consistently across runtime, hooks, plugin, and docs
- explicit-target handling and managed-mode behavior are easier for hosts to adopt correctly

## v1.2.0 Partial Goals

These are the milestones that should exist before the end-state is considered complete.

### Partial Goal A: Product Cleanup
- Replace stale roadmap and release references.
- Align README, MCP README, host contract, and install docs.
- Remove or demote outdated transitional language.

### Partial Goal B: CLI Hardening
- Make `aidocs init`, `status`, `sync`, `config`, and `version` production-ready.
- Remove duplicated bootstrap logic where runtime services already exist.
- Add machine-readable output options where helpful.

### Partial Goal B1: Planning Defaults
- Make complete planning a default for non-trivial work.
- Ensure sessions start with a canonical plan scaffold.
- Ensure plans explicitly include partial goals and an end goal.

### Partial Goal C: Host Contract Alignment
- Update the host contract to describe current Claude and OpenCode reality accurately.
- Keep `/aidocs` as the only user-facing AIDOCS entry command.
- Keep the distinction between advisory classification and host enforcement explicit.

### Partial Goal D: Tool Surface Simplification
- Keep unified code/schema entry points as the default path.
- Further reduce any leftover confusion from old tool mental models.
- Decide whether procedure-oriented surfaces are core, advanced, or internal.

### Partial Goal D1: Tool Decision Support and Retrieval Depth
- Improve tools so agents can code from retrieved facts instead of memory.
- Package method signatures, namespaces, enums, constructors, and schema semantics more directly.
- Reduce multi-step friction between “I found the right area” and “I can now write correct code against it.”

### Partial Goal E: Release Discipline
- Single-source versioning.
- Public/private release boundaries documented and repeatable.
- Installer and package outputs verified during release prep.

### Partial Goal F: Benchmark and Validation Discipline
- Add controlled benchmark scenarios for indexing, retrieval, and prompt classification.
- Expand tests around CLI behavior, multilingual prompts, installer outputs, and release packaging boundaries.
- Use benchmark infrastructure to improve tool quality first, not to justify release claims before the tools are production-grade.

### Partial Goal G: Documentation Publishing Readiness
- Finish content cleanup before publishing a docs site.
- Define which docs stay in-repo and which docs belong in a hosted docs surface.
- Split hosted product docs by product path instead of mixing AIDOCS and ADB in one flat docs IA.

### Partial Goal H: Collaboration Continuity Foundations
- Add a structured handoff model.
- Add resume bundles that combine plan, handoff, context, journal, and repo summary.
- Add first freshness/staleness signals for collaboration context.
- Improve same-project collaboration continuity without pretending to provide full shared host sessions.

## Workstreams

## 1. Docs and Product Story

### Why
The code has moved faster than the docs. This creates confusion for users and for agents.

### Work
- rewrite roadmap to current reality
- align version, test-count, and capability messaging across docs
- make OpenCode caveats explicit and short
- keep README focused on user-facing value, not internal transition history
- move deep architectural nuance into host/runtime docs instead of the main landing page

### Done when
- README, MCP README, install guide, and host contract agree on current behavior
- roadmap reflects active priorities, not already-completed migrations

## 2. CLI as a First-Class Surface

### Why
The new CLI is the right operator-facing surface, but it should not become a parallel implementation of runtime logic.

### Work
- refactor CLI init logic to reuse runtime/bootstrap services instead of copying template logic directly
- ensure `status` reflects managed mode, sessions, and index health clearly
- add optional structured output for automation
- make config targets stable and intentional
- derive CLI version from package metadata instead of hardcoding it

### Done when
- CLI commands are thin, reliable adapters over shared runtime logic
- CLI output is useful for both humans and simple automation

## 3. Host Integration Parity

### Why
Claude and OpenCode do not have equal hook/runtime capability. The product should acknowledge that without fragmenting behavior unnecessarily.

### Work
- keep Claude on the runtime-driven classify + route path
- keep OpenCode on command-aware and token-aware guidance until hook-time MCP invocation is possible
- avoid claiming stronger OpenCode enforcement than actually exists
- continue using command metadata and multilingual token mirroring in OpenCode
- document capability gaps and target behavior clearly

### Done when
- users can understand what differs between Claude and OpenCode without reading source code
- host adapters feel intentionally different, not inconsistently implemented

## 4. Classification and Routing Quality

### Why
`action_tokens` are now multilingual and important. They should be strong enough to guide behavior without pretending to be perfect.

### Work
- improve token coverage and collision handling
- test more mixed-language and short imperative prompts
- keep classification output advisory and consistently worded
- tighten route guidance for explicit targets, edit tasks, and maintenance tasks
- ensure plugin and runtime language-pack handling stay aligned

### Done when
- classification is good enough to support the common prompt patterns across supported languages
- host guidance built on top of classification is predictable and low-drift

## 5. Tool and Capability Surface Cleanup

### Why
AIDOCS is stronger when agents have a small number of clear entry points and a few advanced escape hatches.

### Work
- keep unified code/schema tools as the default teaching path
- revisit any remaining advanced or niche surfaces that lack a clear product role
- decide whether procedures, action-surface bundles, and execution evidence are public operator tools or internal architecture support
- trim or reclassify what does not need to be prominently exposed

### Done when
- the public tool story is simple
- advanced surfaces are still available but no longer compete with the main path

## 5b. Tool Decision Support and Retrieval Depth

### Why
Real-world usage showed that the current tools are often close to preventing mistakes, but they still leave too much decisive information hidden behind extra steps or shallow results.

This leads to exactly the wrong failure mode: the agent has enough information available in theory, but still writes code from memory instead of retrieved facts.

### Work
- make `code_investigate` deeper and more coding-useful
  - include method signatures
  - include DTOs and enum hints where relevant
  - include schema/entity touchpoints where relevant
- include namespace in symbol search results
- add `code_get_method_signature`
- add `code_get_enum_values`
- add constructor/record parameter lookup
- improve `schema_query` so field output better distinguishes:
  - required
  - optional
  - defaulted
  - computed/stored
- improve PreToolUse guidance so the flow is:
  - `code_find(query)`
  - then `code_get_symbol_snippet(path, symbol)`
  instead of assuming the symbol is already known exactly
- consider `code_find(mode="factories")` for helper/service-construction discovery in tests and setup-heavy codebases

### Must-Have for v1.2.0
- namespace in symbol search results
- `code_get_method_signature`
- `code_get_enum_values`
- deeper `code_investigate`
- better PreToolUse guidance flow

### Second Wave
- constructor/record parameter lookup
- richer `schema_query` field semantics
- `code_find(mode="factories")`

### Done when
- the agent can more often write correct code from retrieved tool results instead of mental-model guesses
- the most common method-signature, enum, namespace, and constructor mistakes are easier to prevent with one or two tool calls

## 6. Packaging, Installer, and Release Hygiene

### Why
Recent release work showed that packaging correctness matters as much as code correctness.

### Work
- unify version sourcing
- verify release archives and public/private boundaries before publishing
- keep an explicit public-tree verification step in automation so forbidden private paths fail before public sync/release publication
- keep Windows and Linux/macOS installer behavior aligned
- keep plugin install, command sync, hook wiring, and language-pack exports consistent
- document what is shipped publicly vs privately

### Done when
- release prep is boring and repeatable
- public releases never accidentally expose private artifacts

## 6b. Collaboration Continuity Foundations

### Why
Real use showed that AIDOCS needed stronger collaboration continuity earlier than expected.

Instead of treating structured handoffs and resume bundles as a later optional layer, they have become part of the actual product-hardening required before release.

### Work
- add canonical handoff files to sessions
- add structured handoff update/read flows
- support append-style collaborative updates
- support dead-ends and estimated effort in handoffs
- add predecessor-aware same-project continuity without naive unbounded successor chains
- add resume bundles that package the collaboration state for successor agents
- add initial freshness/staleness trust signals

### Done when
- successor agents can resume active work from a coherent collaboration bundle
- handoffs behave like living collaboration state instead of one-off notes
- the product has a credible first continuity model even without host-native shared sessions

## 7. Benchmarks and Test Hardening

### Why
The project is now broad enough that raw feature count is less useful than confidence, repeatability, and comparative performance.

### Work
- create controlled benchmark scenarios for index sync, retrieval paths, and classification quality
- compare unified indexed-tool workflows against raw file-search workflows where useful
- add stronger CLI tests around init, config, sync, and status behavior
- add multilingual prompt cases and mixed-language prompt cases
- add installer and packaging checks that protect public/private boundaries

### Done when
- benchmark scenarios are repeatable and informative
- tests cover the real operational risks, not just unit-level logic
- benchmark infrastructure exists without becoming the main release story before deep retrieval/tool quality is strong enough

## 8. Documentation Site Readiness

### Why
The project now has enough documentation value to justify a hosted docs surface, but only after the core story is stable.

### Work
- stabilize README, MCP README, install guide, host integration, and roadmap structure
- identify which docs should be landing-page docs versus operator/reference docs
- prepare a minimal docs-site structure without duplicating unstable content
- define a public AIDOCS docs information architecture before full site migration
- keep `codenexus.cloud` as the umbrella marketing site
- target product docs under:
  - `docs.codenexus.cloud/aidocs`
  - `docs.codenexus.cloud/autodeploybase`
- avoid a single mixed-product Docusaurus narrative if ADB remains the primary product and AIDOCS remains secondary

### Done when
- docs content is stable enough to publish without immediate structural churn
- the hosted docs plan reduces confusion instead of mirroring it
- product boundaries are obvious in both marketing and docs navigation

## 9. Enterprise Feature Design

### Why
Enterprise-oriented work may matter later, but it should not distort the near-term roadmap.

### Work
- capture design constraints and likely enterprise requirements without expanding the shipping surface now
- keep this work exploratory and architecture-oriented, not release-blocking

### Done when
- enterprise design ideas are documented clearly
- none of that work blocks the core 1.2.0 product-hardening priorities

## Priority Order

### P0
- docs and roadmap alignment
- single-source versioning
- CLI hardening around shared runtime logic
- release/public packaging discipline

### P1
- OpenCode contract cleanup and plugin/runtime alignment
- `action_tokens` quality improvements
- tool-surface simplification and procedure-surface decision
- tool decision support and retrieval depth improvements from real-world feedback
- collaboration continuity foundations
- production-grade tool quality for real agent work

### P2
- richer CLI machine-readable output
- stronger host automation around workflow triggers
- deeper operator views once the core surfaces are stable
- benchmarks and benchmark presentation
- documentation-site readiness
- test hardening

### P3
- enterprise feature design

## Explicit Non-Goals for v1.2.0

- full OpenCode parity with Claude hook-time MCP routing, unless the host SDK gains the needed capability
- perfect multilingual understanding beyond advisory classification
- large new feature branches that increase product surface without reducing confusion elsewhere
- adding more overlapping tools without first proving they reduce operator or agent effort

## Release Criteria for v1.2.0

Before shipping v1.2.0:

- docs and roadmap match shipped behavior
- CLI version and package version are derived consistently
- installer parity exists for supported platforms
- public release packaging is verified clean
- host integration contract is accurate for Claude and OpenCode
- core routing, CLI, and runtime tests pass

## Summary

v1.2.0 should be the release where AIDOCS becomes cleaner, more explainable, and easier to trust.

The focus is:

- fewer ambiguous surfaces
- stronger shared runtime behavior
- cleaner host integration boundaries
- better multilingual guidance
- safer releases
