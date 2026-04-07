# Bundled Provider Completion Design

## Goal

Complete AIDOCS skill integration by making the bundled curated provider the default shipped experience, with no manual provider registration and no dependency on the external `superpowers` plugin for normal operation.

This is a pre-`1.9.0` priority because the bundled-provider experience must be solid before the broader `1.9.0` stabilization release is considered complete.

## Core Principle

The simplest strong shipped experience wins.

So for the primary product path:

- bundled provider is default
- bundled skills are available out of the box
- AIDOCS runtime remains the workflow authority
- optional external providers are secondary and may be deferred if they create friction

## Why This Is Needed

The current runtime can integrate provider-sourced skills, but the user experience is still too dependent on explicit configuration and provider concepts.

That creates avoidable friction like:

- "why don't I see the skills?"
- manual registration/setup steps
- provider-qualified skill IDs in normal use

Bundled-provider completion removes that friction.

## Product Rule

For the shipped AIDOCS experience:

- the curated bundled provider is primary
- the external `superpowers` plugin/runtime is not required
- AIDOCS remains the authority for runtime, orchestration, compatibility, host behavior, and overrides

## Bundled Provider Requirements

### 1. In-repo bundled skill content

The curated bundled skill set ships with AIDOCS in-repo.

### 2. Auto-available provider

The bundled provider must be available automatically without manual registration.

### 3. Clean canonical skill names

For the default bundled path, normal user-facing/runtime-facing skill IDs should be clean canonical names such as:

- `brainstorming`
- `systematic-debugging`
- `verification-before-completion`

The user should not need to think in provider-qualified names for the default experience.

### 4. Preserved provenance

Provider/source provenance should still exist in inspection/debug views and runtime metadata.

## Coexistence With Optional External Providers

Optional external providers may still exist later, but they are secondary.

Recommended precedence:

1. `AIDOCS-native overrides`
2. `bundled provider`
3. `project-local skills`
4. `optional external providers`

This keeps the default product story simple.

## Defer-If-Needed Rule

If optional external provider support creates friction for this milestone, it may be deferred.

The bundled provider should not be blocked by generalized multi-provider complexity.

## Migration From Provider-Qualified IDs

Older session/runtime state may contain provider-qualified skill IDs like:

- `superpowers_external/brainstorming`

Bundled-provider completion should include a migration rule:

- map provider-qualified IDs for the curated bundled set to canonical bundled names in normal use

Examples:

- `superpowers_external/brainstorming` -> `brainstorming`
- `superpowers_external/systematic-debugging` -> `systematic-debugging`

This should preserve behavior while reducing user-facing noise.

## Runtime Behavior

Bundled-provider completion should preserve these existing runtime rules:

- bundled skills participate in automatic triggering
- override policy still applies
- AIDOCS-native overrides still win for orchestration-critical skills
- host integrations consume bundled skill state without an external plugin dependency

## Host Behavior

### OpenCode

- bundled skills are available out of the box
- no separate `superpowers` plugin required for normal use

### Claude

- bundled skill state is surfaced through the existing AIDOCS runtime/hook path
- no external plugin required for normal use

## Completion Criteria

Bundled-provider completion is done when all of the following are true:

### Registry/runtime

- bundled provider is available by default
- no manual registration is required
- bundled skills resolve with canonical clean names
- provider-qualified legacy session IDs are migrated cleanly

### Triggering

- bundled skills participate in automatic triggering
- override policy still works
- AIDOCS-native overrides still win where required

### Host behavior

- OpenCode works with bundled skills out of the box
- Claude works with bundled skills out of the box
- no external `superpowers` plugin is required for normal use

### Inspection

- default/user-facing views show clean canonical skill names
- debug/inspection surfaces still show provider provenance when needed

## Non-Goals

This milestone does not require:

- full generalized multi-provider UX
- remote provider updates
- external provider sync machinery
- preserving provider-qualified names in the default user experience

## Summary

Bundled-provider completion is the step where AIDOCS skill integration stops feeling like a configured add-on and starts feeling like part of the product.
