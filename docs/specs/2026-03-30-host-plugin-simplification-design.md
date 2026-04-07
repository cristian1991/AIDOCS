# Host And Plugin Simplification Design

## Goal

Simplify the AIDOCS host/plugin integration layer so that OpenCode and Claude consume a cleaner, more deterministic runtime-produced state contract instead of reconstructing meaning across multiple snapshots and host-specific logic paths.

This is a host-first cleanup milestone that prepares the ground for better discoverability, richer skill semantics, and deeper AIDOCS-native orchestration later.

## Why This Milestone Comes First

The current AIDOCS skill/runtime integration works, but host/plugin behavior is still more complex than it should be.

The main issues are:

- too much meaning is split between runtime, persisted snapshots, plugin reconstruction logic, and hook-delivered annotations
- OpenCode plugin logic has accumulated bridge behavior that should really live in runtime
- prompt-time and session-time state are not cleanly separated enough

Before improving discoverability or richer skill semantics, AIDOCS should simplify the authority boundaries.

## Core Principle

AIDOCS runtime should compute the truth.

Hosts should mostly consume that truth, not reconstruct it.

This is especially important because AIDOCS is aiming for deterministic enforcement and low prompt bloat, not a more prompt-heavy approximation of the `superpowers` model.

## Target Outcome

After this milestone:

- AIDOCS runtime is the canonical producer of host-visible session/skill state
- OpenCode plugin becomes thinner and less reconstructive
- Claude hook/runtime path consumes the same core decision layer
- prompt-time state is live and deterministic
- session/startup state can still be cached where appropriate

## What Should Be Simplified

### 1. Single runtime source of truth

Host-visible state should not be assembled ad hoc from:

- runtime helpers
- persisted snapshots
- plugin-side reconstruction
- hook-delivered annotations

Instead, runtime should produce one canonical contract.

### 2. Thinner OpenCode plugin

The plugin currently carries too much logic around:

- startup/session handling
- prompt-time routing glue
- imported-skill state handling
- mode reconstruction/fallback logic
- stale-snapshot suppression logic

More of that should move into runtime so the plugin becomes a transport/render adapter.

### 3. Better alignment between OpenCode and Claude

The hosts can still differ in delivery mechanism:

- OpenCode plugin
- Claude hook path

But they should not differ much in the actual decisions about:

- skill state
- override state
- session readiness
- provider compatibility
- prompt-time triggered skills

## Canonical Host-State Contract

AIDOCS runtime should produce one structured payload for hosts.

Recommended sections:

- `session_state`
  - managed/unmanaged
  - selected session
  - roadmap/plan readiness
- `skill_state`
  - selected skills
  - active skills for this context
  - override modes
  - provider states
- `prompt_state`
  - current intent
  - current triggered skills
  - whether prompt-time activation succeeded
- `inspection_state`
  - debug/inspection metadata safe to show to users or tools
- `host_actions`
  - what the host should inject/show/do

The host should consume this contract directly instead of reconstructing missing meaning where possible.

## State Freshness Rules

### Prompt-time state must be live

Prompt-time values must be computed for the current prompt:

- current intent
- currently triggered skills
- override mode for the current prompt
- any prompt-specific activation result

This must not come from stale persisted snapshots.

### Session/startup state may be cached

These can be persisted or cached as long as they are refreshed when inputs change:

- selected session
- selected skills
- provider configuration
- compatibility status
- long-lived session summary

### Important rule

- `prompt_state` = live runtime decision
- `session_state` = cached/persisted runtime state when appropriate

If prompt-time resolution fails, the host must not silently substitute old prompt-specific skill state from a persisted snapshot.

## Determinism Over Prompt Weight

`superpowers` works largely through strong instructional shaping.

AIDOCS should take a different path here:

- use code to compute canonical host state
- inject only the distilled result needed by the host/agent
- avoid increasing prompt verbosity to compensate for runtime ambiguity

So the design priority is:

- more deterministic runtime code
- less host-side inference
- less prompt bloat

## OpenCode And Claude Responsibilities

### Runtime should own

- session readiness decisions
- provider compatibility decisions
- imported/selected/active skill decisions
- override mode decisions
- prompt-time trigger decisions
- inspection/debug metadata

### OpenCode plugin should own

- rendering runtime-produced host state into OpenCode bootstrap/prompt context
- OpenCode-specific delivery mechanics

### Claude path should own

- rendering runtime-produced host state into startup/prompt/tool-time hook behavior
- Claude-specific delivery mechanics

## Non-Goals

This milestone should not:

- redesign skill trigger taxonomy
- add major new skill semantics
- replace external skill content
- rework conductor execution logic
- add new provider types
- broaden host support beyond OpenCode + Claude
- increase prompt verbosity to paper over runtime ambiguity

It should also avoid:

- moving decision logic into the plugin when runtime can own it
- adding more snapshots where live runtime state is more correct
- introducing new heuristic fallback paths that make behavior less deterministic

## Success Criteria

This milestone is successful when:

- one canonical runtime host-state payload exists
- OpenCode plugin is thinner and less reconstructive
- Claude/OpenCode consume the same core decision layer
- prompt-time state is live and not replaced by stale snapshot fallbacks
- session/startup state is clearly separated from prompt-time state
- host behavior is simpler to reason about than before
