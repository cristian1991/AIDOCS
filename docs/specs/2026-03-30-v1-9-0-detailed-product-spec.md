# AIDOCS v1.9.0 Detailed Product Spec

> Historical design document.
>
> Parts of this spec are superseded by the current runtime-owned orchestration model implemented after the April 2026 audit.
> In particular, skill/orchestration behavior, override naming, and conductor/runtime ownership have evolved.
> Use `README.md`, `README_DEV.md`, `mcp/HOST_INTEGRATION.md`, and the current runtime tests as the source of truth for current behavior.

## Goal

Ship `v1.9.0` as the final major pre-`2.0.0` release: a deterministic, GUI-backed, single-project workflow system with bundled skills, hardened conductor execution, normalized config/state, and cleaner host/tool UX.

`v1.9.0` should make AIDOCS feel like one coherent product rather than a set of strong but partially disconnected parts.

## Product Positioning

`v1.9.0` is the release where:

- AIDOCS is the workflow authority
- skills provide intelligence and discipline
- the runtime provides determinism and enforcement
- the GUI/control plane gives human operators a usable surface
- conductor execution is trustworthy for single-project work

`v2.0.0` is explicitly reserved for the harder cross-agent / cross-host / cross-project protocol layer.

## Design Principles

### 1. Deterministic workflow authority

Workflow control belongs to AIDOCS runtime, not to prompt-only skill content and not to host/plugin reconstruction logic.

### 2. Intelligence from skills, control from runtime

Bundled `superpowers`-derived skill content should continue to shape quality, but the runtime owns:

- session state
- roadmap/plan/feedback state
- conductor state
- compatibility state
- host-visible state
- orchestration-critical override behavior

### 3. Configurable policy, derived runtime state

Meaningful settings should become normalized config. Derived facts and runtime observations should remain runtime state, not editable configuration.

### 4. One coherent operator surface

The GUI/control plane should sit on top of the same runtime/config/state model used by hosts and MCP tools. It must not become a second workflow engine.

### 5. Keep it lightweight

The GUI should not require a large Electron-class distribution just to expose configuration and control.

## Scope Pillars

## Pillar 1: Config Standardization

### Why

Current settings are split across too many places and scopes, with overlapping policy between files such as:

- `aidocs.toml`
- `aidocs-plugin.json`
- `opencode.jsonc`
- `/.MEMORY/config/*.json`
- `/.MEMORY/skill-providers.json`
- runtime snapshots under `/.MEMORY/.runtime/`

This must be normalized before the GUI and final bundled-skill model can feel coherent.

### Required model

Three config scopes plus runtime state:

1. `global`
2. `project`
3. `session`
4. `runtime state` (non-editable)

Precedence:

1. `session`
2. `project`
3. `global`
4. built-in defaults

### Config domains

The normalized config model should include at least:

- `providers`
- `skills`
- `conductor`
- `runtime`
- `hosts`
- `indexing`
- `verification`
- `security`
- `debug`

### Runtime-only domains

These should remain derived, not editable:

- active skills
- triggered skills
- provider compatibility evaluation result
- prompt intent / prompt-state activation result
- conductor live lane state
- host-state snapshots
- compiled workflow artifacts

### Immediate normalization targets

- unify `aidocs.toml` + `aidocs-plugin.json` overlap
- move `/.MEMORY/skill-providers.json` into normalized config structure
- clearly classify `aidocs-managed.json` as session-binding/runtime state
- clearly classify `workflow-actions.json` as compiled runtime artifact

## Pillar 2: Bundled Curated Skill Provider

### Why

AIDOCS should ship with a curated skill set and not require the user to separately install and register `superpowers` for the primary experience.

### Product rule

- bundled provider is the default shipped experience
- `superpowers` plugin/runtime is not required for normal operation
- skill content is mostly reused, not rewritten wholesale

### Curated skill strategy

Bundle the full high-value set we need, but classify it:

#### Direct runtime skills
- `brainstorming`
- `systematic-debugging`
- `verification-before-completion`
- `test-driven-development`
- `receiving-code-review`
- `requesting-code-review`
- `writing-skills`

#### Deterministic workflow blueprints
- `writing-plans`
- `executing-plans`
- `subagent-driven-development`
- `dispatching-parallel-agents`
- `finishing-a-development-branch`

These remain the workflow-defining skills even though Superpowers now uses inline self-review for spec/plan quality checks and may present user-facing execution choice between subagent-driven and inline execution.

#### Bootstrap/system skills
- replace `using-superpowers` with an AIDOCS-native bootstrap/operating mode

### Override policy

The override registry should remain part of `v1.9.0`:

- `aidocs_native_override`
- `provider_content_aidocs_runtime`
- `provider_native`

For orchestration-critical skills, AIDOCS-native behavior wins.

### No dual-authority normal mode

Running both the `superpowers` plugin and the AIDOCS plugin at the same time should not be the intended product mode.

Development coexistence may be tolerated, but shipping behavior must be AIDOCS-authoritative.

## Pillar 3: Conductor Hardening

### Why

The conductor is implemented, but the single-project workflow still needs hardening before any attempt at cross-agent protocol.

### Required improvements

#### Lane lifecycle refinement

Lanes should not jump directly from local green to truly done.

States should include:

- `blocked`
- `ready`
- `running`
- `awaiting_review`
- `implementation_done`
- `reopened_by_integration`
- `awaiting_user_feedback`
- `completed`

#### Full-suite-aware conductor

The conductor should:

- run lane-local verification
- run integration/full-suite verification where required
- inspect failures
- attribute likely ownership using runtime evidence
- reopen the owning lane automatically

#### Persistent lane ownership

Lane agents should remain the owners of their lane across reopen cycles.

That means the same lane agent can be reused later if:

- a downstream integration phase reveals a regression
- the conductor attributes that regression back to the lane

#### Structured intra-project lane communication

Before `2.0.0`, AIDOCS still needs a small structured signal model for one project, enforced by the conductor.

Examples:

- hidden dependency found
- undeclared file needed
- waiting on contract
- integration failure reopened
- ownership dispute

The conductor should be the single inference point for these signals.

### Conductor retrieval model

The conductor should stay query-first:

- `code_find`
- `code_trace`
- `code_bundle`
- dependency/plan/session tools

It should avoid raw file reads except for exceptional confirmation cases.

## Pillar 4: GUI / Control Plane

### Why now

The GUI should ship before cross-agent protocol work, because:

- the control plane is easier than the communication protocol
- it provides immediate operator value
- it validates the config/state model early

### GUI role

The GUI is:

- control plane
- configuration surface
- inspection surface
- operator action surface

It is **not**:

- a second workflow engine
- the place where orchestration logic lives

### Initial GUI scope

- provider setup / bundled skill visibility
- skill selection / override inspection
- session overview
- roadmap/plan overview
- conductor lane graph/status
- blocked/reopened lanes
- logs / execution evidence / operator inspection
- config editing

### Platform choice

Recommended approach: **Tauri**

Why:

- much lighter than Electron
- native desktop shell + IPC
- no ordinary localhost admin plane by default
- cross-platform enough for this kind of settings/control app

### Security model for the GUI

- GUI should not be exposed as a normal localhost web app
- agent should not be able to mutate GUI/admin settings via normal browser/playwright/network methods
- runtime must reject agent-originated config mutation for protected settings regardless of UI

## Pillar 5: Security Configuration

### Why

Security policy is currently too scattered across runtime code, host config, and rule docs.

### Required security domain

Add a dedicated `security` config domain with at least:

- protected files
- protected directories
- protected extensions
- protected globs
- self-edit policy
- GUI/control-plane access policy
- provider-override approval policy

### Editable vs non-editable configuration

Normal configuration should be agent-editable only under controlled conditions such as:

- explicit user request
- development workflows where config edits are intentionally allowed

This applies to non-security policy/config such as:

- provider settings
- host settings
- workflow settings
- project/session policy settings

However, security settings must remain non-editable by agents regardless of context.

### Hardcoded immutable security core

Some things must remain uneditable by the agent regardless of user-facing config:

- security config files themselves
- GUI/control-plane mutation paths
- core enforcement code for protected settings

No agent should be allowed to edit those through:

- line edits
- file create/write
- batch edits
- any equivalent mutation route

This rule should apply even when development features are enabled.

### Self-edit mode must not ship in release builds

Any self-edit / MCP-core self-modification mode must be excluded from release builds.

If such a mode exists at all, it should be limited to:

- a separate public development branch
- or an explicitly development-only build/profile

Release builds must not expose self-edit mode as a normal product feature.

Public PR work should also target the development branch rather than `main` / `master`.

## Pillar 6: Host / Plugin Simplification

### Why

Host behavior currently works, but too much meaning is split across:

- runtime helpers
- persisted snapshots
- plugin-side reconstruction
- hook-delivered annotations

### Required target

One canonical runtime host-state contract with sections like:

- `session_state`
- `skill_state`
- `prompt_state`
- `inspection_state`
- `host_actions`

Then:

- OpenCode plugin becomes a thinner delivery/render adapter
- Claude hook path becomes a thinner delivery/render adapter
- both consume the same core runtime decisions

### Freshness rule

- prompt-time state must be live
- session/startup state may be cached
- hosts must not silently substitute stale prompt-specific state when current prompt resolution fails

## Pillar 7: Tool-Space Reorganization

### Why

Tool names are product surface, not just internal identifiers.

The current surface is too backend-shaped and noisy.

### Required direction

- keep `aidocs_` prefix for namespace clarity
- rename aggressively toward user-oriented names
- primary tool families:
  - `project`
  - `session`
  - `skills`
  - `plan`
  - `runtime`
  - `code`
  - `memory`
- separate `primary` vs `debug/specialist` tools
- add overview tools while preserving precision tools

### Rule

- overview should be easy
- precision should remain possible
- neither replaces the other

## Pillar 8: Discoverability And Operator UX

### Why

Too much of AIDOCS still depends on the operator already knowing how it works.

### Required direction

- better overviews
- clearer provider/skill/override visibility
- clearer conductor state and ownership
- clearer host-visible runtime state
- less hidden policy

## Pillar 9: Roadmap / Spec / Plan Indexing And Retrieval

### Why

This is now explicitly in scope for `v1.9.0`.

We saw repeated friction around:

- writing roadmaps
- writing specs
- editing plans
- patch failures while trying to update planning artifacts

### Required direction

Treat planning artifacts as first-class indexed targets.

That means:

- roadmap/spec/plan indexing and retrieval quality
- exact retrieval of sections/lanes/tasks
- better editing/update ergonomics
- less raw-file fallback
- less hard-patch fragility

## What Is Explicitly Out Of Scope For v1.9.0

- cross-project agent communication protocol
- cross-host communication protocol
- federated multi-project conductor behavior
- remote provider sync/update systems

Those are `v2.0.0` concerns.

## v2.0.0 Direction

Only after `v1.9.0` is complete should AIDOCS tackle:

- cross-agent communication protocol
- cross-host / cross-project coordination
- richer multi-agent federation semantics
- evolving the GUI into a wider coordination/harness interface if the protocol proves viable

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

`v1.9.0` is the release where AIDOCS becomes a coherent deterministic single-project workflow platform.

`v2.0.0` should only begin once this foundation is strong enough to support cross-agent and cross-host protocol work without collapsing into complexity.
