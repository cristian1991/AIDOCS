# Tool Space Reorganization Design

## Goal

Reorganize the AIDOCS MCP tool surface so it is cleaner, more intentional, easier to use in host UIs, and better aligned with actual user/agent workflows.

This is a breaking refactor. The aim is not compatibility-first cleanup. The aim is a better tool product surface.

## Core Principle

Tool names are not merely internal identifiers.

In practice, they are exposed to users and agents through host UIs and logs. That means they should be treated as product surface, not backend implementation detail.

## Why This Matters

Current tool naming and decomposition leak too much internal architecture:

- `_get` suffixes
- store/service-oriented naming
- repeated `project_root` and `session_id` parameters
- multiple plumbing calls where one human-friendly overview would do

This makes the UI noisy even when the system is working correctly.

## Refactor Direction

This should be a taxonomy-first rewrite.

- define a new canonical tool taxonomy
- rename aggressively where needed
- separate primary tools from debug/specialist tools
- keep the `aidocs_` prefix for namespace clarity
- reorganize around user intent, not backend storage architecture

## Breaking-Change Policy

This refactor may rename/remove tools.

The recommended approach is:

- update AIDOCS internal callers/tests/plugins in one coordinated pass
- do not preserve the entire old surface as long-lived aliases
- only use temporary shims during implementation if absolutely necessary

## Naming Rules

Keep:

- `aidocs_` prefix

Change:

- remove `_get` for primary tools when possible
- prefer names optimized for readability and intent
- avoid store/service leakage in the public surface
- keep internal architecture terms out of the primary tool surface where possible

Examples of desired direction:

- `aidocs_skill_provider_status_get` -> `aidocs_skills_provider`
- `aidocs_skill_registry_get` -> `aidocs_skills_overview`
- `aidocs_skill_trigger_state_get` -> `aidocs_skills_active`
- `aidocs_session_skills_get` -> `aidocs_session_skills`
- `aidocs_project_status` -> `aidocs_project_overview`
- `aidocs_aidocs_mode_get` -> `aidocs_runtime_mode`

## Taxonomy

The public tool space should be reorganized into a few clear families.

### 1. Session

Examples:

- `aidocs_session_overview`
- `aidocs_session_resume`
- `aidocs_session_skills`
- `aidocs_session_handoff`
- `aidocs_session_journal`

### 2. Skills

Examples:

- `aidocs_skills_overview`
- `aidocs_skills_active`
- `aidocs_skills_provider`
- `aidocs_skills_select`
- `aidocs_skills_overrides`

### 3. Plan

Examples:

- `aidocs_plan_overview`
- `aidocs_plan_preflight`
- `aidocs_plan_lanes`
- `aidocs_plan_resume_lane`

### 4. Code

Examples:

- `aidocs_code_find`
- `aidocs_code_trace`
- `aidocs_code_read`
- `aidocs_code_write`
- `aidocs_code_create`

### 5. Memory

Examples:

- `aidocs_memory_overview`
- `aidocs_memory_search`
- `aidocs_memory_capture`

### 6. Runtime

Examples:

- `aidocs_runtime_overview`
- `aidocs_runtime_mode`
- `aidocs_runtime_host_state`
- `aidocs_runtime_route`

### 7. Project

Examples:

- `aidocs_project_overview`
- `aidocs_project_init`
- `aidocs_project_sync`
- `aidocs_project_status`

## Overview vs Precision

The refactor must not force coarse retrieval on agents when they need precision.

So the new tool space should explicitly support two layers:

### Overview tools

- broad summaries
- good-looking UI
- common-path workflows

### Precision tools

- exact symbol
- exact dependency
- exact line read
- exact override/provider lookup
- exact lane status

Rule:

- overview should be easy
- precision should remain possible
- neither replaces the other

## Primary vs Debug Surface

### Primary tools

These are the tools we want to look good in normal host UIs.

Examples:

- `aidocs_project_overview`
- `aidocs_session_overview`
- `aidocs_skills_overview`
- `aidocs_skills_active`
- `aidocs_plan_overview`
- `aidocs_plan_lanes`
- `aidocs_runtime_host_state`
- `aidocs_code_find`
- `aidocs_code_trace`
- `aidocs_code_read`
- `aidocs_code_write`

### Debug/specialist tools

These remain important but should not dominate the common user-facing story.

Examples:

- raw provider compatibility dumps
- raw override registry dumps
- low-level execution event inspection
- low-level conductor internals

These should be either:

- clearly named with something like `aidocs_debug_...`
- or retained as specialist surfaces, not promoted as the common entrypoint

## Context-Aware Defaults

To reduce UI noise, primary tools should infer context where safe.

Where appropriate, they should avoid requiring repeated explicit parameters for:

- current managed project
- current active session

Debug/specialist tools can still require explicit parameters.

## Aggregate Tools

The refactor should introduce better overview tools so normal UX does not require visible plumbing fan-out.

Examples:

- `aidocs_skills_overview`
  - provider status
  - selected skills
  - active skills
  - override modes

- `aidocs_session_overview`
  - session state
  - blockers
  - handoff highlights
  - compliance summary

- `aidocs_plan_overview`
  - plan state
  - current phase/lane summary
  - pending feedback gates

- `aidocs_project_overview`
  - setup
  - index freshness
  - managed mode
  - active session

These should reduce the number of visible low-level calls in host UIs.

## Presentation Layer

Where possible, tools should support more targeted display semantics.

Examples:

- `display_name`
- `display_action`
- `display_target`

Even if the host still shows raw tool names, AIDOCS should aim to provide cleaner semantics for hosts and logs to consume in the future.

## Non-Goals

This refactor should not:

- reduce deterministic precision tooling
- force agents to use only broad overview tools
- hide specialist/debug tools so deeply that diagnosis becomes impossible
- preserve old naming purely for compatibility if it harms clarity

## Success Criteria

This refactor is successful when:

- the primary tool surface is significantly cleaner
- tool names read like product capabilities, not backend functions
- overview tools reduce common visible plumbing calls
- precision tools still exist for narrow agent needs
- primary and debug surfaces are clearly distinguishable
- OpenCode/Claude UI traces look materially cleaner than before
