# AIDOCS Workflow Inspection Design

## Purpose

Create a ground-truth inspection of the current AIDOCS system so the current workflow can be understood, audited, and corrected.

This is not a feature design and not a doc refresh. It is a system inspection whose main job is to show what actually happens now.

## Primary Goal

Produce a debug-style workflow map that answers, for every important runtime path:

- what triggers the path
- what host/runtime function runs next
- what state is read
- what decisions are made
- what branch is taken
- what state is written
- what the user or host sees next

The main deliverable should make it easy to trace the live AIDOCS decision graph from setup through runtime behavior.

## Truth Model

The inspection must prioritize evidence in this order:

1. actual current code paths
2. actual files/state read and written
3. runtime entrypoints and host hooks/plugins
4. docs/specs only as secondary hints or contrast material
5. tests only as evidence of asserted behavior, not truth

The inspection should not assume the docs, tests, or older specs are correct.

## Scope

The inspection is organized into exactly these top-level sections:

1. `0. AIDOCS setup`
2. `1. AIDOCS MCP setup in project`
3. `2. AIDOCS runtime start in project`
4. `3. What happens when`

These sections are intentionally lifecycle-based rather than code-module-based.

## Section Breakdown

### 0. AIDOCS setup

Cover the actual global/system setup flow:

- install script entrypoints
- global files created or updated
- OpenCode plugin installation path
- Claude hook installation path
- command-pack/global routing installation path
- optional MCP install path
- conditions that change behavior across platforms or hosts

### 1. AIDOCS MCP setup in project

Cover project-local enablement and setup behavior:

- project initialization path
- `.mcp.json` or equivalent MCP wiring path
- `/.MEMORY/` creation or repair path
- config/runtime marker creation
- index/bootstrap side effects
- what is required vs optional for project-local MCP setup

### 2. AIDOCS runtime start in project

Cover how AIDOCS becomes active for a project at runtime:

- `/aidocs` entry path
- bootstrap-or-resume logic
- session selection or creation path
- managed-mode activation path
- startup context generation
- readiness/index-sync branches
- host-visible result payload path

### 3. What happens when

This section must be trigger-based and branch-heavy.

It should be split into live decision flows such as:

- user sends a normal prompt
- user runs `/aidocs`
- user provides explicit file or error target
- user begins an edit/write action
- host intercepts a tool call
- task/update/complete path is reached
- project is unmanaged
- project has no session
- project has multiple sessions
- host is Claude Code
- host is OpenCode

Additional subflows may be added if they are part of the actual runtime graph.

## Output Format

The primary deliverable should be a static HTML page.

Recommended output path:

- `docs/inspection/index.html`

Supporting source material can live under:

- `docs/inspection/`

The HTML page should act like a debug map rather than a prose document.

## Debug-Step Schema

Every step in the inspection should use a strict debug-style structure.

Each step should capture:

- trigger
- active layer
  - installer
  - host
  - plugin/hook
  - MCP runtime
  - filesystem/config state
- exact function, command, hook, or script entrypoint
- inputs consumed
- files/state read
- decision condition
- next function or branch target
- files/state written
- host-visible or user-visible result

Example style:

- User sends normal prompt in Claude Code
- Claude hook `UserPromptSubmit` fires
- hook handler calls runtime state resolution path
- if managed mode inactive -> return `/aidocs` requirement guidance
- else -> classification/routing path
- route output shapes advisory context
- no write occurs at this step

The inspection should prefer this trace style over architecture prose.

## Decision Graph Requirement

The inspection must explicitly surface all real decision points that materially change flow.

That includes branches like:

- initialized vs uninitialized project
- managed vs unmanaged mode
- no session vs one session vs multiple sessions
- OpenCode vs Claude host path
- explicit target vs no explicit target
- route allows direct inspection vs requires AIDOCS entry vs blocked vs preflight-only
- no gate file vs gate file present
- read allowed vs read blocked
- command identity `/aidocs` vs ordinary prompt

The final artifact should make these branches visually obvious and navigable.

## Host Separation Model

The inspection should show three lanes wherever possible:

- shared/common AIDOCS runtime flow
- Claude Code flow
- OpenCode flow

The goal is to make commonality and divergence obvious without forcing the reader to mentally diff separate documents.

The host split should focus on current actual behavior, not the target model.

## Documentation Philosophy

This inspection should avoid broad explanatory prose unless it helps decode a branch.

Preferred style:

- short trace steps
- branch labels
- function names
- state names
- file paths
- concise notes on what changed at each decision point

Avoid:

- aspirational descriptions
- feature marketing language
- long paragraphs about architecture unless needed for branch comprehension

## Secondary Contrast Layer

If cheap and unambiguous, the inspection may include a very small secondary note for:

- presumed intended flow
- docs/spec mismatch

But this is optional and must not distract from the main task of describing current behavior.

The primary narrative must remain the actual current flow.

## Evidence Sources

The inspection should be derived from:

- install scripts
- CLI/runtime entrypoints
- host hook/plugin code
- runtime orchestration code
- managed-mode/session state files
- config files and markers
- direct code-path tracing or reproduction where needed

Docs and tests may be cited only as supporting references, not primary truth.

## Deliverables

The inspection effort should produce:

1. a static HTML debug-map page
2. supporting source notes or structured data under `docs/inspection/`
3. a decision-path inventory that makes all material branches explicit
4. a concise gap list only where current flow is visibly confusing, contradictory, or host-divergent

## Success Criteria

The inspection is successful when:

- a reader can start from install and trace the actual system step by step
- OpenCode and Claude commonality/divergence are easy to see
- each major trigger path can be followed as a decision graph
- state reads/writes and branch conditions are explicit
- the artifact helps diagnose what is wrong with the current system instead of merely describing what AIDOCS claims to be

## Recommendation

Treat this as a runtime/debug audit with a graphable step schema, not as documentation cleanup. The HTML artifact should prioritize decision flow visibility over completeness of prose.
