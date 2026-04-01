# External Skill Provider Integration Design

## Goal

Integrate bundled skill content derived from `superpowers` into AIDOCS so that:

- skill content ships with AIDOCS as a bundled curated provider
- AIDOCS owns the orchestration/runtime/enforcement layer
- OpenCode and Claude can use the imported skills without depending on the external provider's plugin/runtime

This should make the next version rely on AIDOCS as the workflow authority while still reusing upstream-compatible skill content.

## Core Principle

AIDOCS should not rewrite more skill content than necessary.

Instead:

- AIDOCS ships a curated bundled provider containing the skill content and fragments it actually needs
- AIDOCS provides the registry, triggering, orchestration, session state, logging, and enforcement

So the split becomes:

- `AIDOCS` = runtime / orchestrator / enforcement / host integration + bundled curated provider
- upstream `superpowers` remains a source/reference, not a required runtime dependency

## Scope

### First milestone

- OpenCode + Claude
- bundled in-repo provider integration
- AIDOCS-owned orchestration/runtime
- no dependency on the `superpowers` plugin for normal operation once integration is complete

### Out of scope for this milestone

- remote provider sync/update management
- full fork sync logic
- rewriting the entire upstream skill library into AIDOCS-native content

## Provider Model

AIDOCS should support a provider-based skill system.

Providers:

- `aidocs_builtin`
- `project_local`
- `aidocs_bundled_superpowers`
- future providers such as optional `skills_sh_external`

Each provider should expose skills as structured records with metadata like:

- `provider`
- `skill_id`
- `name`
- `description`
- `path`
- `version` when available
- `license`
- `host_compatibility`
- `requires_assets`
- `origin`

The provider model must remain provider-agnostic rather than special-casing `superpowers` in the orchestration layer.

## Provider Source Model

The first implementation should ship the curated provider in-repo.

That means:

- the user does not need to register `superpowers` manually
- AIDOCS ships the curated skill/provider content out of the box
- optional additional providers may come later, but are not required for the main experience

Remote sync/pinning management is not required in this first milestone.

## Compatibility Model

Compatibility should be owned by AIDOCS, not delegated to the provider.

AIDOCS should track:

- configured provider path
- detected provider version
- known compatible versions or version range
- last validated version

Provider states:

- `not_configured`
- `missing`
- `detected_compatible`
- `detected_unknown`
- `detected_incompatible`
- `incompatible_but_user_override`
- `incompatible_disabled`

## Incompatible Version Handling

When an incompatible external provider version is detected, AIDOCS should present:

- current AIDOCS version
- detected external provider version
- officially compatible provider versions or range
- provider state summary
- user choice:
  - disable the provider
  - keep enabled anyway

This is intentionally not a hard disable because minor incompatibilities may still be usable.

The bigger risk is workflow/semantic drift, not a single missing skill.

## Runtime Authority Split

### AIDOCS MCP/runtime should own

- provider registry
- imported skill discovery/indexing
- session-selected skills
- skill applicability/selection logic
- trigger logic
- skill priority/chaining
- workflow enforcement
- evidence/logging of which skills were active and why
- compatibility handling

### AIDOCS OpenCode plugin should own

- host bootstrap injection
- OpenCode-side provider/skill visibility
- lightweight host-specific context shaping
- any host-local path registration needed for imported skills

### AIDOCS Claude integration should own

- hook wiring
- startup/prompt/tool-time integration
- using runtime-selected skills in Claude-side guidance/enforcement

## OpenCode And Claude First

The first milestone should explicitly target:

- OpenCode plugin parity for imported skills
- Claude host integration using the already stronger AIDOCS hook path

OpenCode and Claude should share the same AIDOCS runtime decisions. They should differ only in delivery mechanism, not in core skill selection logic.

## Development Mode vs Product Mode

During development, running both the `superpowers` plugin and the AIDOCS integration at the same time may be acceptable for experimentation.

However, the shipping target is:

- AIDOCS plugin/runtime is the authority
- bundled curated skill files are consumed from the AIDOCS release itself
- the `superpowers` plugin/runtime is no longer required for normal operation

## Automatic Triggering

The first milestone should support full automatic triggering from the start, based on what the agent is doing.

The trigger model should be layered:

1. session-selected skills
2. task/intent classification
3. hard workflow transitions
4. provider compatibility and availability

### Task/intent categories

Examples of categories AIDOCS should classify:

- brainstorming
- planning
- debugging
- implementation
- verification
- review
- branch finishing

### Trigger behavior

AIDOCS should:

1. detect current intent/state
2. gather eligible skills from active providers
3. rank them by:
   - explicit session selection
   - workflow/process priority
   - provider compatibility
   - host/runtime applicability
4. activate the needed skills
5. log what triggered and why

## Why Files Are Not Enough

Skill content alone is not sufficient.

External skills provide the instructional content, but AIDOCS must provide:

- the runtime that chooses when to apply them
- the host integration that injects them appropriately
- the enforcement that makes memory/workflow rules deterministic enough for production use

So this feature is not merely importing Markdown. It is building AIDOCS-native orchestration around external skill content.

## OpenCode Plugin Parity Requirements

The integrated AIDOCS OpenCode path should eventually cover the behaviors that make the `superpowers` plugin compelling for OpenCode users, including:

- external skill visibility from disk
- startup/bootstrap context awareness
- tool mapping / host adaptation awareness where appropriate
- automatic skill availability without manual symlink-style setup

But the authority must remain with AIDOCS.

## Integration-Ready Definition

The first milestone is integration-ready when all of the following are true:

### Provider + registry

- AIDOCS can register an external provider from a local filesystem path
- AIDOCS can detect provider version and compatibility
- imported skills appear in registry listings with provider attribution

### Session/runtime

- sessions can select imported skills
- runtime knows which imported skills are active and why
- imported skills participate in trigger decisions

### OpenCode

- OpenCode plugin can surface imported skills and include them in AIDOCS bootstrap behavior
- OpenCode no longer requires the external `superpowers` plugin for normal operation

### Claude

- Claude hook/host integration can incorporate imported-skill state through AIDOCS runtime decisions

### Compatibility

- incompatible versions show compatibility info and user choice
- choice can be persisted

### Tests

Minimum required tests:

- provider discovery from local path
- version compatibility handling
- incompatible-version user-choice path
- imported skill listing/selection
- automatic trigger behavior for at least a few core workflow categories
- OpenCode plugin behavior with imported skill state
- Claude path still functioning with imported skill state present
- graceful behavior when provider is missing or disabled

## Non-Goals

This milestone does not require:

- remote provider syncing
- provider auto-updating
- rewriting external skills into AIDOCS-native content
- matching every tiny behavioral nuance of every external provider from day one

The goal is practical AIDOCS-owned orchestration over external skill content, not immediate total parity with every upstream ecosystem detail.
