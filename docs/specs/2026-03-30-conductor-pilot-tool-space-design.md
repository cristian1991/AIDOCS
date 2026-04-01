# Conductor Pilot: Tool-Space Reorganization

## Goal

Run a real AIDOCS conductor-native pilot on the tool-space reorganization work in order to validate whether the conductor can safely and usefully execute lane-based parallel work in practice.

This is not a generic implementation plan. It is a controlled pilot designed to answer whether AIDOCS conductor is ready for wider use.

## Why This Pilot

The conductor now exists in code, but most recent work has still been executed through `superpowers`-style task-by-task orchestration.

This pilot is intended to answer:

1. whether a genuinely lane-native plan works in practice
2. whether AIDOCS can keep execution authority without falling back into task-centric process flow
3. whether the conductor provides real value on a medium-sized, partially parallelizable feature

## Pilot Scope

Use the tool-space reorganization work as the test feature.

Why this scope is appropriate:

- it has a real dependency shape
- it has some genuinely parallelizable work after a shared foundation
- it is risky enough to be meaningful but bounded enough to observe clearly

## Conductor Authority Rules

For this pilot:

- AIDOCS conductor is the only execution authority
- do not let external orchestration-critical `superpowers` skills drive the workflow

Specifically, the following should not control execution for this pilot:

- `writing-plans`
- `subagent-driven-development`
- `executing-plans`

Behavioral/process skills may still be useful in a subordinate role, for example:

- debugging
- verification
- review discipline

But the conductor controls lane execution.

## Lane Structure

### Phase 1: Taxonomy Foundation

#### Lane `taxonomy-core`

Files:

- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/tests/test_tool_surface_taxonomy.py`

Work:

- rename the public primary tool surface to the new taxonomy
- preserve precision/debug tools distinctly
- add taxonomy tests

#### Lane `overview-payloads`

depends_on:

- `taxonomy-core`

Files:

- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/tests/test_tool_overview_payloads.py`
- `mcp/tests/test_runtime_service.py`

Work:

- add project/session/skills/plan overview payloads
- add context-aware defaults where safe
- add overview payload tests

### Phase 2: Host Integrations

#### Lane `opencode-host`

depends_on:

- `overview-payloads`

Files:

- `core/plugins/aidocs.js`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_tool_precision_paths.py`

Work:

- switch OpenCode common-path calls to new overview tools
- preserve precision-path behavior for narrow reads
- add/update OpenCode-facing tests

#### Lane `claude-host`

depends_on:

- `overview-payloads`

Files:

- `mcp/server/aidocs_mcp/claude_hook.py`
- `mcp/tests/test_claude_external_skill_integration.py`
- `mcp/tests/test_claude_hook.py`

Work:

- switch Claude common-path state to new overview/runtime tools
- preserve Claude hook safety behavior
- add/update Claude-facing tests

These two host lanes are intended to run in parallel.

### Phase 3: Integration And Verification

#### Lane `integration`

depends_on:

- `opencode-host`
- `claude-host`

Files:

- `mcp/tests/test_host_integration.py`
- `mcp/tests/test_tool_precision_paths.py`
- `mcp/tests/test_tool_surface_taxonomy.py`

Work:

- run cross-host verification on the new tool surface
- fix integration regressions
- verify overview tools reduced visible plumbing without harming precision tools

#### Lane `full-suite`

depends_on:

- `integration`

Files:

- `mcp/tests/**`

Work:

- run full MCP suite
- record pilot outcome

## Safety Rules

- no overlapping-file lanes may run in parallel
- if real work reveals emergent overlap, the conductor must pause affected lanes
- one agent stays attached to one lane
- lane review happens after lane completion, not after every tiny task

## Interaction Rules

- the conductor remains interactive while lanes are running
- the user may clarify, pause, resume, or reprioritize lanes during the pilot
- if the conductor detects a conflict, it should pause and explain instead of guessing

## What Counts As Success

The pilot is successful if:

- lane-native planning works without ad hoc regrouping during execution
- `opencode-host` and `claude-host` genuinely run in parallel
- the conductor remains the execution authority throughout the run
- lane progress/status is observable and useful
- the final integrated result still passes the full MCP suite

## What Counts As Failure

The pilot should be considered unsuccessful or inconclusive if:

- work repeatedly falls back into task-by-task sequential control
- lane boundaries prove too ambiguous in practice
- host lanes reveal hidden coupling that defeats the planned split
- the conductor is present in code but not actually shaping the execution flow

## Expected Output Of The Pilot

At the end of the run, we should be able to answer clearly:

- Is conductor execution practical today?
- Is it only safe for carefully curated lane-native plans?
- What still needs refinement before broader adoption?
