# Conductor-Style Multi-Agent Execution Design

## Goal

Enable AIDOCS to execute plans with multiple subagents in parallel when the work is truly independent, while keeping related work together in a single contextual lane.

This is not generic task fan-out. It is dependency-aware, conflict-aware, conductor-style coordination.

## Core Metaphor

AIDOCS should behave like a conductor, not just an orchestrator.

- the plan is the score
- lanes are sections of the orchestra
- dependencies are timing cues
- review gates are rehearsals/checkpoints
- user feedback is a live hold/resume signal
- final integration is the ensemble pass

The conductor should understand what can enter now, what must wait, what must never overlap, and when to pause for the human.

## Primary Objective

The main win is safe parallelization of larger, disjoint workstreams.

Examples of good parallelism:

- componentizing different pages independently
- building separate disjoint subsystems in parallel
- running one agent per coherent lane of related tasks

Examples of bad parallelism:

- multiple agents editing the same file
- multiple agents working on tightly coupled tasks that depend on evolving intermediate outputs
- splitting related subtasks so aggressively that each agent must rebuild the same context from scratch

## Plan Model

Plans should declare:

- `Phase`
- `Lane`
- `Files`

Plans may also declare:

- `depends_on`

The plan should not require extra `locks` or `scope` metadata by default. The conductor is responsible for interpreting the plan and code context safely.

### Authoring rules

- `Files` are mandatory for every lane.
- `depends_on` is optional.
- `depends_on` should be used sparingly, only for true hard blockers or important ordering hints that would otherwise be expensive or ambiguous for the conductor to infer.
- The conductor remains responsible for most dependency reasoning.

### Example shape

```md
## Phase 2: Homepage CMS Extraction

### Lane homepage-shell
Files:
- `src/pages/HomePage.tsx`
- `src/cms/homepage-schema.ts`

- [ ] Extract page shell into CMS-driven layout
- [ ] Add shell tests

### Lane homepage-hero
Files:
- `src/components/home/Hero.tsx`
- `src/cms/hero-block.ts`

- [ ] Convert hero section into CMS block
- [ ] Add block tests

### Lane homepage-feature-grid
Files:
- `src/components/home/FeatureGrid.tsx`
- `src/cms/feature-grid-block.ts`

- [ ] Convert feature grid into CMS block
- [ ] Add block tests

### Lane homepage-integration
depends_on:
- homepage-shell
- homepage-hero
- homepage-feature-grid
Files:
- `src/pages/HomePage.tsx`
- `src/cms/render-homepage.ts`

- [ ] Wire all homepage blocks together
- [ ] Run integrated page tests
```

## Execution Model

### Phases

Phases are sequential barriers.

- the next phase cannot start until the current phase is resolved
- phases remain useful as human-readable grouping and coarse dependency boundaries

### Lanes

A lane is a context-preserving execution stream.

- one agent owns one lane
- tasks inside a lane run sequentially
- related work stays in the same lane so context is reused

## Dependency-Driven Parallelism

Dependency structure is the primary execution model.

The conductor should:

1. parse the lane dependency graph
2. compute unblocked lanes
3. inspect whether those lanes are actually safe to run in parallel
4. dispatch only the safe subset

Parallelism should fall out of the graph, not be forced by a simple flag.

### Optional hard dependency hints

`depends_on` is an author-provided hard ordering hint, not the primary source of truth for all relationships.

- if present, AIDOCS should treat it as a hard blocker unless the user explicitly overrides it
- if absent, the conductor should still reason from declared files, indexed relationships, and overall plan context

This keeps plans lean while still allowing the plan author to cheaply encode high-value ordering constraints discovered during planning.

## Safety Rules

### Hard block: file overlap

If two lanes touch the same file, AIDOCS must not run them in parallel.

This is mandatory. A warning is not sufficient.

### Hard/soft block: obvious context dependency

If two lanes are dependency-free on paper but clearly share evolving context, the conductor should refuse or pause parallel execution.

Examples:

- one lane changes model assumptions while another depends on them
- one lane depends on outputs that another lane has not stabilized yet

## Conductor Responsibilities

The conductor layer should:

- build the lane dependency graph
- detect unblocked lanes
- detect declared-file overlap
- inspect obvious unsafe coupling from indexed/project context
- choose safe parallel sets
- keep one agent attached to one lane
- collect lane results and reviews
- unlock dependent lanes after completion

## Indexed-Query-First Coordination

The conductor should prefer indexed/query-level understanding over raw file reads.

Default conductor tools should be things like:

- `code_find`
- `code_trace`
- `code_bundle`
- `code_get_dependencies`
- session/plan/roadmap tools

The conductor should avoid low-level file reading unless:

- exact confirmation is needed for a suspected conflict
- the indexed layer cannot answer the question
- a lane agent reports a concrete ambiguity

This keeps the conductor focused on:

- structure
- dependency
- flow
- conflict detection

not low-level implementation mechanics.

## Contract-Compatible Parallelism

Not every relationship should be treated as a wait condition.

If the conductor can establish a stable contract up front, two lanes may still run in parallel even when they are related.

Example:

- one lane rewrites model/API internals
- another lane updates frontend calls and property usage

These can run in parallel if the conductor establishes the target interface clearly enough first.

So the conductor should distinguish:

- true hard blockers
- contract-compatible related lanes

and only serialize the work when the contract is not stable enough.

## Pre-Dispatch And In-Flight Conflict Detection

Conflict detection should happen in two moments.

### Before dispatch

The conductor should inspect:

- declared `Files`
- dependency graph
- indexed code relationships
- obvious shared targets

This is the preferred time to detect conflicts.

### During execution

The conductor should also detect emergent conflicts while agents are working.

Examples:

- an agent edits a file not originally declared
- an agent discovers a missing dependency
- an agent reports that it needs output from another lane
- runtime/index signals reveal overlap that the original plan did not capture

When this happens, the conductor should pause the affected lane or lanes and wait for resolution.

## Interactive Conductor

The conductor must remain interactive while lanes are running.

It should still receive user input and be able to:

- accept clarification that a suspected conflict is not real
- accept a dependency correction
- resume a paused lane
- cancel or reprioritize a lane
- adjust the lane graph safely without restarting the whole phase

This is critical for live multi-agent work.

## Lane States

Recommended lane states:

- `blocked`
- `ready`
- `running`
- `awaiting_review`
- `implementation_done`
- `reopened_by_integration`
- `awaiting_user_feedback`
- `completed`

## Review Model

Reviews should happen per lane, not per tiny task.

For each lane:

1. lane agent completes related tasks in that lane
2. AIDOCS runs spec-compliance review for the lane
3. AIDOCS runs code-quality review for the lane
4. if lane-local checks pass, the lane becomes `implementation_done`
5. dependent lanes may unlock when appropriate
6. later integration/full-suite failures may reopen the owning lane automatically
7. only the conductor should mark a lane truly `completed`

## Runtime Loop

The conductor runtime loop should be:

1. load the plan graph
2. compute unblocked lanes
3. remove conflicting lanes
4. dispatch safe lanes in parallel
5. collect lane results
6. run review per lane
7. mark lanes `implementation_done` when lane-local verification passes
8. run phase/integration/full-suite verification where required
9. reopen owning lanes automatically when later failures are attributed to them
10. unlock dependents only when their prerequisites are truly satisfied
11. pause if user clarification/feedback is required
12. continue until the phase or plan is complete

## Pre-2.0 Hardening Needed Before Federated Coordination

Before any cross-project or cross-host coordination protocol, the conductor should still be hardened inside one project.

That hardening should include:

- full-suite-aware verification
- automatic lane reopening when later failures are attributed to an earlier lane
- persistent lane ownership so the same lane agent can be reused across reopen cycles
- stronger failure attribution using deterministic runtime evidence
- structured intra-project lane signals enforced through the conductor

The conductor should remain the single inference point for these decisions, using MCP/runtime data as inputs rather than spreading orchestration inference across agents or host layers.

## Non-Goals

This design does not aim to:

- parallelize every task possible
- infer all dependencies automatically
- allow overlapping-file edits with buffering/merge complexity

The point is smart parallelization, not maximum parallelization.

## Validation Requirements

Implementation should eventually prove:

- lane graph parsing works
- unblocked lanes are computed correctly
- overlapping-file lanes do not run in parallel
- obvious in-flight conflicts can pause lanes
- one agent stays attached to one lane across sequential lane tasks
- full-suite failures can reopen the owning lane automatically
- lane ownership persists across reopen cycles
- user input can resume or restructure paused lanes
- conductor-level coordination uses indexed/query tools more than raw file reads
