# Skill Override Policy Design

> Historical design document.
>
> This file describes an earlier override model that used `aidocs_native_override` terminology.
> The current implementation uses runtime-owned capability markers such as `aidocs_runtime_owned` and separates helper skills from runtime-owned workflow authority.
> Keep this document for history, but do not treat it as the current source of truth.

## Goal

Define how AIDOCS chooses between its own orchestration/runtime behavior and bundled/provider skill content, especially for `superpowers`-derived skills shipped with AIDOCS.

The purpose is to keep AIDOCS as the workflow authority while still benefiting from high-quality bundled/provider skill content.

## Core Principle

Not all skills should be treated the same.

There are three categories:

1. bundled/provider skills that can run normally
2. bundled/provider skills whose content is valuable but whose runtime/orchestration must be controlled by AIDOCS
3. orchestration-critical skills that AIDOCS must override natively

## Why An Override Policy Is Needed

Bundled `superpowers`-derived skills still assume a process architecture that does not fully match AIDOCS.

The main mismatch zone is not general behavioral skills. It is orchestration-critical skills, especially:

- plan generation
- execution sequencing
- review sequencing
- multi-agent coordination

If AIDOCS tries to use those process skills unchanged as the workflow authority, it creates split-brain behavior between:

- skill-driven workflow control
- AIDOCS session/runtime/conductor control

So AIDOCS needs an explicit override policy.

## Override Registry

AIDOCS should maintain a small registry of skill handling decisions.

Each entry should carry at least:

- `skill_id`
- `provider_match`
- `mode`
- `reason`

### Example conceptual entries

- `writing-plans` -> `aidocs_native_override`
- `subagent-driven-development` -> `aidocs_native_override`
- `executing-plans` -> `aidocs_native_override`
- `brainstorming` -> `provider_content_aidocs_runtime`
- `systematic-debugging` -> `provider_native`
- `verification-before-completion` -> `provider_native`

## Modes

Keep the model intentionally small.

### 1. `aidocs_native_override`

AIDOCS implementation wins.

- bundled/provider skill content may still be referenced for compatibility, inspiration, or migration purposes
- but the provider does not control runtime behavior

Use this for orchestration-critical skills where AIDOCS must be the workflow authority.

### 2. `provider_content_aidocs_runtime`

Bundled/provider skill content is used, but AIDOCS controls:

- activation
- sequencing
- surrounding workflow state
- enforcement/logging where needed

Use this for skills where the content is valuable, but the runtime should remain with AIDOCS.

### 3. `provider_native`

The bundled/provider skill can run normally.

AIDOCS may still log, select, and expose it, but it does not replace or wrap the core behavior beyond normal provider integration.

## Initial Recommended Split

### Keep provider-native

- `systematic-debugging`
- `verification-before-completion`
- `receiving-code-review`
- many narrow behavioral/process skills

### Use bundled/provider content with AIDOCS runtime control

- `brainstorming`
- startup/bootstrap-related skill content
- host-facing guidance skills

### AIDOCS-native override

- `writing-plans`
- `subagent-driven-development`
- `executing-plans`

These are the critical mismatch skills because they define execution architecture itself.

Recent Superpowers changes reinforce this split rather than weakening it:

- execution choice between `subagent-driven-development` and `executing-plans` is user-visible again
- plan/spec review is now inline self-review instead of subagent review loops

AIDOCS should therefore override the execution architecture while remaining compatible with the updated skill content and terminology.

## Resolution Order

When a skill is about to activate, AIDOCS should resolve it in this order:

1. `AIDOCS override registry`
2. `session-selected compatible bundled/provider skill`
3. `session-selected built-in or project-local skill`
4. `automatic trigger choice among compatible skills`

This ensures orchestration-critical behavior is deterministic.

## Runtime Behavior

### If `aidocs_native_override`

Example: `writing-plans`

- AIDOCS does not activate the external process skill directly
- it uses the AIDOCS-native planning implementation
- external provider content does not become the workflow authority

### If `provider_content_aidocs_runtime`

Example: `brainstorming`

- external provider content is used as the skill guidance/content source
- AIDOCS decides when it triggers and how it fits with session/runtime rules

### If `provider_native`

Example: `systematic-debugging`

- external provider skill can run as-is through the provider system
- AIDOCS logs and tracks activation but does not replace the core behavior

## Why This Split Is The Right One

It allows AIDOCS to:

- keep using the strongest bundled/provider skill content where it fits
- avoid rewriting the entire skill library
- replace only the small set of skills where flow-model mismatch is structural

This keeps the rewrite surface narrow and high-value.

## Non-Goals

This policy does not aim to:

- replace every bundled/provider skill with an AIDOCS-native equivalent
- eliminate provider-backed skills entirely
- solve full trigger semantics for every skill family at once

It only defines who is authoritative when AIDOCS and the provider would otherwise disagree.

## Validation Requirements

Implementation should eventually prove:

- override registry is inspectable
- runtime chooses the correct mode deterministically
- AIDOCS-native overrides win for the selected orchestration skills
- provider-content/runtime-controlled skills use bundled/provider content but AIDOCS activation rules
- provider-native skills still work normally through the provider system
- session-selected skills still participate correctly under the override policy
