# AIDOCS Developer README

Implementation guide for developers auditing, extending, or debugging AIDOCS.

## Repository Map

AIDOCS is split into a few high-value surfaces:

- `core/`
  Portable layer: command packs, startup routers, host packaging, and shared memory templates.
- `mcp/`
  Runtime layer: MCP server, CLI, indexing, orchestration, retrieval, workflow compilation, and execution evidence.
- `/.MEMORY/`
  Live project-state and canonical memory for this repo.
- `action_tokens/`
  Per-language user-intent descriptors used by runtime classification and interpretation.
- `action_hooks/`
  TOML-backed default interaction text templates for host/runtime guidance.
- `core/.skills/`
  Bundled skills shipped by AIDOCS itself.

If you are looking for behavior, start in `mcp/server/aidocs_mcp/`.
If you are looking for shipped host assets, start in `core/`.
If you are looking for the current intended model, start in `/.MEMORY/`.

## Where To Look

| If you need to inspect... | Start here |
|---|---|
| startup/session routing | `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/.aidocs/memory-system.aidocs`, `/.MEMORY/INDEX.md` |
| session lifecycle | `mcp/server/aidocs_mcp/session_store.py`, `runtime_service.py` |
| task lifecycle | `mcp/server/aidocs_mcp/mcp_server.py`, `runtime_service.py` |
| managed mode | `/.MEMORY/config/aidocs-managed.json`, `managed_mode_service.py` |
| workflow compilation | `/.MEMORY/rules/workflow.md`, `workflow_action_service.py` |
| OpenCode behavior | `core/plugins/aidocs.js`, `aidocs-plugin.json`, `opencode.jsonc` |
| Claude behavior | `core/hooks/hooks.json`, `core/scripts/claude-hook.*`, `claude_hook.py` |
| Cursor packaging | `core/.cursor-plugin/plugin.json`, `core/.cursor-plugin/hooks/*` |
| skills | `core/.skills/`, `skill_provider.py`, `runtime_service.py`, `/.MEMORY/config/skill-providers.json` |
| runtime tool surface | `mcp/server/aidocs_mcp/mcp_server.py` |
| current intended architecture | `/.MEMORY/system/architecture.md`, `mcp/HOST_INTEGRATION.md` |

## Token Surfaces

There are two different token/config surfaces and they should not be mixed:

| Surface | Owns | Examples |
|---|---|---|
| `action_tokens/*.yaml` | user-intent descriptors by language | action classification phrases, helper trigger phrases, vague-step patterns |
| `action_hooks/*.toml` | runtime/host -> agent guidance text | startup warnings, managed-mode guidance, action directives, host-facing error text |

`action_tokens/en.yaml` supports:

- normal action-kind keys such as `edit`, `trace`, `understand`
- reserved interpretation keys prefixed with `__`

Reserved interpretation keys:

- `__plan_validation_vague_patterns`
  phrases that make `plan_validate` reject a step as too vague
- `__skill_trigger_<name>_intent`
  user wording that should activate a helper skill or runtime-owned capability
- `__skill_trigger_<name>_workflow`
  internal workflow/action-state words that keep that helper or capability active

- `__intent_guard_<name>`
  special stricter guard-only overrides used only when the canonical normal action key is not enough

Current special guard-only cases that intentionally remain reserved:

- `__intent_guard_write`
  creation/generation intent is kept stricter than general `edit`
- `__intent_guard_bash`
  reserved for a possible stricter shell-intent policy; bash is currently free in `intent_guard`
- `__intent_guard_destructive`
  destructive intent must always remain explicit and separate

Rule:

- if the text describes what the user means, it belongs in `action_tokens`
- if the text describes what the runtime/host tells the agent, it belongs in `action_hooks`
- normal action keys are canonical whenever semantics match (`edit`, `write_memory`, `git_commit`, `git_push`, `git_pull`)
- reserved `__...` groups should only exist when the intent is internal-only or needs stricter subsystem-specific behavior
- lifecycle tools like `task_begin`, `task_update`, and `task_complete` are internal runtime operations, not primary user-intent token groups

## Non-Negotiable Invariants

- Files are the source of truth. MCP indexes and runtime artifacts are derived only.
- `/.MEMORY/.aidocs/index.aidocs` is the startup router, not `INDEX.md`.
- `/.MEMORY/INDEX.md` is the durable-memory router, not the startup router.
- Runtime task state is session-based. Active work belongs under `/.MEMORY/sessions/<session-id>/`.
- Plans belong inside sessions, not `/.MEMORY/plans/`.
- Hosts should refresh managed/session state from AIDOCS runtime data instead of inventing their own parallel truth.
- `/aidocs` is the only user-facing entry command for enabling managed mode.
- Workflow authority belongs to runtime, not to skill prose or provider doctrine.

If code, tests, docs, and manifests disagree, treat the shipped manifest plus focused tests as the first truth source, then repair drift elsewhere.

## Runtime Data Model

Canonical state:

- `/.MEMORY/rules/*`
- `/.MEMORY/system/*`
- `/.MEMORY/domains/*`
- `/.MEMORY/config/*` when the file is explicitly a runtime binding, such as `aidocs-managed.json`
- selected session files in `/.MEMORY/sessions/<session-id>/`

Derived or rebuildable state:

- SQLite indexes
- `/.MEMORY/config/workflow-actions.json`
- `/.MEMORY/.runtime/*`
- generated status bundles and execution-event summaries

Typical bug pattern: code starts treating derived state as canonical, or docs describe derived artifacts as if users should edit them directly.

## Startup And Session Flow

The expected startup order is:

1. `/.MEMORY/.aidocs/index.aidocs`
2. `/.MEMORY/.aidocs/global-instructions.aidocs`
3. `/.MEMORY/.aidocs/coding-standards.aidocs`
4. `/.MEMORY/.aidocs/memory-system.aidocs`
5. `/.MEMORY/INDEX.md`
6. inspect `/.MEMORY/sessions/*/SESSION.md`
7. read the selected session
8. open only task-relevant linked files

The most common architecture bug in AIDOCS history has been drift back toward the older `NOW.md` model. If you see code or docs trying to use `/.MEMORY/NOW.md` or root-level `/.MEMORY/plans/`, treat that as drift unless there is an explicit migration surface involved.

## Skills Architecture

Skills are a separate system from memory.

- Memory stores durable facts, rules, and session state.
- Skills store reusable behavior guidance.

Shipped skill surfaces:

- bundled skills live in `core/.skills/`
- external providers are registered in `/.MEMORY/config/skill-providers.json`
- session selection is surfaced via MCP tools such as `session_skills_get` and `session_skills_set`
- activation/runtime state is persisted in `/.MEMORY/.runtime/sessions/<session-id>/host-skill-state.json`

Important implementation details:

- bundled provider id is defined in `skill_provider.py` as `aidocs_bundled_superpowers`
- external providers are local-path based today
- external skills are loaded from `skills/*/SKILL.md`
- bundled skills are still plain `.md` files under `core/.skills/`
- runtime trigger rules currently live in `runtime_service.py` via `_SKILL_TRIGGER_RULES`

Important boundaries:

- selected skills and active skills are not the same thing
- provider compatibility and override state affect whether a selected skill is actually active
- host adapters may surface active/imported skills in context, but the canonical state belongs to AIDOCS runtime files and MCP APIs
- `imported_skill_state.active_skills` is the best snapshot for provider-compatible imported skills; some summaries such as `skills_overview.active_skills` reflect trigger-resolved activity for the current runtime path instead
- workflow-oriented provider skills now map to `runtime_owned_capabilities`, not helper guidance
- helper skills may influence reasoning, but they must not decide planning, dispatch, continuation, or completion

Useful terms:

| Term | Meaning |
|---|---|
| selected skills | skills chosen for a session |
| active skills | skills currently activated after trigger/compatibility evaluation |
| provider state | whether a provider is compatible, missing, disabled, or override-allowed |
| override mode | whether runtime uses provider-native behavior or AIDOCS-owned runtime behavior |
| host skill state | session-scoped runtime snapshot persisted under `/.MEMORY/.runtime/sessions/<session-id>/host-skill-state.json` |

Common skill bugs:

- docs forget the system exists, so host/runtime behavior looks mysterious
- provider-qualified skill ids and leaf names drift apart
- host state shows active skills but docs only describe memory/session routing
- bundled and external providers behave slightly differently, especially around overrides and mode attribution
- provider and bundled skills with the same canonical name can duplicate activation unless trigger dedupe is explicit

## Runtime-Owned Orchestration

The current orchestration spine is runtime-owned.

Shipped runtime-owned surfaces:

- `plan_create_from_spec`
- `plan_validate`
- `execution_mode_select`
- `plan_dispatch_next`
- `plan_dispatch_report`
- `execution_loop_next`
- `verification_gate`

Current runtime-owned capability markers:

- `planning`
- `execution_mode_selection`
- `execution_loop`
- `completion_verification`

Important boundary:

- provider workflow skills are not the execution authority anymore
- helper skills can still activate, but only to influence reasoning inside the runtime-owned flow

## Host Support Matrix

### Claude Code

Supported today:

- `UserPromptSubmit`
- `PreToolUse`
- managed-mode gating
- advisory prompt classification
- read/tool guardrail context
- workflow surfacing
- runtime-owned workflow capability surfacing

Important boundary:

- hook invocations are subprocesses, so hook-local memory is not a reliable enforcement mechanism
- real enforcement belongs in MCP tools/runtime services

### OpenCode

Supported today:

- startup and normal-turn system context injection
- `/aidocs` entry gating
- managed-mode-aware tool gating
- raw read gating
- native tool-use event logging
- post-edit task reminders
- runtime-owned workflow capability surfacing

Important boundary:

- OpenCode still does not perform full runtime `aidocs_classify_prompt` + `aidocs_route_prompt` at hook time for every normal prompt
- if docs imply Claude-equivalent per-prompt runtime routing, that is a documentation bug

### Cursor

Supported today:

- startup-only `sessionStart` packaging

Important boundary:

- no broader prompt/tool routing should be claimed until actually implemented and verified

### GitHub Copilot CLI

Supported today:

- no shipped integration path

Important boundary:

- this is a roadmap/design area, not a supported host adapter

## Host Surfaces In Code

- Claude Code: `core/scripts/claude-hook.ps1`, `core/scripts/claude-hook.sh`, `core/hooks/hooks.json`, `mcp/server/aidocs_mcp/claude_hook.py`
- OpenCode: `core/plugins/aidocs.js`, `aidocs-plugin.json`, `opencode.jsonc`
- Cursor: `core/.cursor-plugin/plugin.json`, `core/.cursor-plugin/hooks/*`
- Generic MCP: `.mcp.json`, `mcp/server/aidocs_mcp/mcp_server.py`

## Advanced Shipped Features

These are real, test-backed surfaces that are easy to miss if you only read the main README.

| Feature | What it does | Start here |
|---|---|---|
| session resume bundle | returns a compact but rich resume payload with session, plan, handoff, journal, and compliance context | `runtime_service.py`, `test_runtime_service.py` |
| structured handoff | persists cross-agent handoff sections and actionable handoff steps | `session_store.py`, `test_runtime_service.py` |
| plan conductor | lane-aware execution model for plans with dependency and file-overlap safety | `plan_conductor.py`, `test_plan_conductor_runtime.py`, `test_plan_conductor_interaction.py` |
| plan creation and validation | turns deterministic spec text into session plans and rejects vague/unverifiable plans | `runtime_service.py`, `test_plan_create_validate.py` |
| execution mode selection | chooses `inline`, `delegated_serial`, or `delegated_parallel` from conductor state | `runtime_service.py`, `test_execution_mode_select.py` |
| subagent task packets | creates narrow scope/verification packets for delegated lanes | `runtime_service.py`, `test_subagent_packets.py` |
| execution loop | drives delegated/block/inline/complete transitions from runtime state | `runtime_service.py`, `test_execution_loop.py` |
| verification gate | blocks done-without-evidence and reopens lanes on attributed full-suite failures | `runtime_service.py`, `test_verification_gate.py` |
| action surface | compares what should happen, can happen, and did happen for a query/session | `action_surface_service.py`, `test_action_surface_service.py` |
| execution evidence | records runs/events for prompts, tools, hooks, and verification activity | `execution_index_store.py`, `test_runtime_service.py` |
| related-project analysis | lets AIDOCS compare/search configured related repos using indexed bundles | `related_project_service.py`, `test_related_project_service.py` |
| language descriptors | built-in and project-local TOML descriptors for extensible indexing behavior | `language_descriptors.py`, `test_language_descriptors.py` |
| query/read gating | blocks raw reads until narrowed retrieval or query grants the path | `query_gate.py`, `test_query_gate_ux.py`, `test_tiered_read_access.py` |

Common doc gap pattern: onboarding docs emphasize bootstrap and host adapters, while tests cover deeper operator/runtime surfaces that still need explicit mention.

## Important Settings

### `aidocs.toml`

High-impact settings:

- `[journal]` controls session journal retention and trivial-action filtering
- `[index]` controls skip dirs, module hints, and JSON-size limits
- `[languages]` controls which `action_tokens/*.yaml` files participate in prompt classification
- `[tools]` sets default and maximum tool timeouts
- `[agent]` controls directive style and whether rules/directives are injected during bootstrap
- `[dev].dev_mode` controls whether agents may edit AIDOCS MCP source through guarded edit tools
- `[code_quality].comment_enforcement` controls comment-style enforcement level

Common flaw to watch for: duplicated settings across `aidocs.toml`, plugin config, and host-specific files drifting out of sync.

### `aidocs-plugin.json`

This controls OpenCode plugin behavior such as:

- whether message directives are injected
- directive verbosity
- whether compaction is ignored
- whether startup context is injected only once

### Host Config Files

- `opencode.jsonc` is the local OpenCode config surface for MCP server wiring and instruction routing.
- `.mcp.json` is the generic MCP client config surface.
- `core/hooks/hooks.json` is the shipped Claude manifest.
- `core/.cursor-plugin/plugin.json` is the shipped Cursor package manifest.
- `/.MEMORY/config/skill-providers.json` is the provider registry for external skills.
- `/.MEMORY/config/related-projects.md` is the config file for related-project comparison/search.
- `action_hooks/*.toml` is the shipped default interaction text catalog; `aidocs.toml` can override those templates under `interaction.*`.

## Fast Checks

| Goal | Command / file |
|---|---|
| structural sanity | `aidocs_project_check` |
| workflow compile sanity | `aidocs_workflow_actions_compile` |
| Claude integration sanity | `pytest tests/test_claude_hook.py -q` |
| host packaging sanity | `pytest tests/test_host_integration.py -q` |
| skill integration sanity | `pytest tests/test_opencode_external_skill_integration.py tests/test_claude_external_skill_integration.py -q` |
| interaction text config sanity | `pytest tests/test_config_resolution.py -q` |
| inspect provider registry | `/.MEMORY/config/skill-providers.json` |
| inspect active host skill state | `/.MEMORY/.runtime/sessions/<session-id>/host-skill-state.json` |

## Where Bugs Usually Hide

### 1. Documentation Drift

Symptoms:

- docs mention `NOW.md`
- docs mention root `/.MEMORY/plans/`
- docs claim a host hook or plugin behavior that is not in the shipped manifest

Check:

- `/.MEMORY/.aidocs/index.aidocs`
- `/.MEMORY/INDEX.md`
- current host manifests and tests

### 2. Manifest-Code Mismatch

Symptoms:

- handler code supports events the public manifest does not wire
- package manifests point to files outside their own package

Check:

- `core/hooks/hooks.json`
- `core/.cursor-plugin/plugin.json`
- installer scripts
- focused host tests

### 3. Derived-State Confusion

Symptoms:

- code expects users to edit compiled workflow JSON or runtime artifacts directly
- runtime files start being treated as durable memory

Check:

- `workflow_action_service.py`
- `/.MEMORY/config/workflow-actions.json`
- `/.MEMORY/.runtime/`

### 4. Host Capability Overclaiming

Symptoms:

- docs say OpenCode is doing route-time MCP classification when it is really using plugin-local logic
- docs imply Copilot or Cursor support beyond what is shipped

Check:

- `mcp/HOST_INTEGRATION.md`
- `core/plugins/aidocs.js`
- actual host manifests and packaging

### 5. Skill-State Drift

Symptoms:

- selected skills do not match active skills
- provider compatibility or override state is unclear
- hosts surface imported skills but docs do not explain where they come from
- runtime-owned workflow capabilities are mistaken for helper skills

Check:

- `/.MEMORY/config/skill-providers.json`
- `/.MEMORY/.runtime/sessions/<session-id>/host-skill-state.json`
- `skill_provider.py`
- `runtime_service.py`
- `session_skills_get`, `session_skills_set`, `skill_trigger_state_get`

### 6. Placeholder Canonical Docs

Symptoms:

- `Last verified: YYYY-MM-DD`
- empty system docs that should explain real invariants

Check:

- `/.MEMORY/system/*`
- `/.MEMORY/rules/*`
- templates under `core/.MEMORY/.aidocs/templates/`

### 7. Deep Runtime Surfaces Hidden By Onboarding Docs

Symptoms:

- docs make AIDOCS sound simpler than the tested runtime really is
- features like handoff steps, plan conductor, action surface, related-project analysis, or descriptor-based indexing feel "surprising"
- docs omit plan creation/validation, execution mode, dispatch packets, execution loop, or verification gate even though they are test-backed

Check:

- `test_runtime_service.py`
- `test_plan_conductor_runtime.py`
- `test_action_surface_service.py`
- `test_related_project_service.py`
- `test_language_descriptors.py`

## Common Regressions

| Regression | Symptom | Check |
|---|---|---|
| old memory model returns | docs/code mention `NOW.md` or root `/.MEMORY/plans/` | `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/domains/memory-system.md` |
| manifest drift | code handles events not wired in shipped manifests | `core/hooks/hooks.json`, `core/.cursor-plugin/plugin.json`, focused host tests |
| derived-state confusion | users are pointed at `.runtime` or compiled workflow JSON as if canonical | `/.MEMORY/system/architecture.md`, `workflow_action_service.py` |
| OpenCode overclaim | docs imply route-time MCP classification on every prompt | `mcp/HOST_INTEGRATION.md`, `core/plugins/aidocs.js` |
| skill-state mismatch | selected skills do not explain active skills | provider registry, runtime host skill state, override logic |
| helper-vs-workflow confusion | helper skills are treated like orchestration authority | `skill_override_store.py`, `runtime_owned_capabilities`, host context rendering |
| runtime-feature underdocumentation | tests show rich runtime behavior but docs only mention bootstrap basics | runtime-service, conductor, action-surface, related-project, descriptor tests |
| template drift | fixed live docs but stale templates still generate bad defaults | `core/.MEMORY/.aidocs/templates/` |

## Validation Checklist For Changes

For memory/layout changes:

- run `aidocs_project_check`
- check `/.MEMORY/.aidocs/index.aidocs`
- check `/.MEMORY/INDEX.md`
- confirm session-local paths are still authoritative

For workflow changes:

- run `aidocs_workflow_actions_compile`
- inspect `aidocs_workflow_actions_get`
- confirm unsupported guidance bullets are not being compiled as rules

For Claude changes:

- run `pytest tests/test_claude_hook.py -q`
- run `pytest tests/test_host_integration.py -q`
- compare against `core/hooks/hooks.json`

For OpenCode changes:

- run `pytest tests/test_host_integration.py -q`
- run `pytest tests/test_opencode_external_skill_integration.py -q`
- verify `core/plugins/aidocs.js` still matches the documented boundary

For skill-system changes:

- verify bundled skills under `core/.skills/`
- verify provider registry in `/.MEMORY/config/skill-providers.json`
- verify runtime host skill state under `/.MEMORY/.runtime/sessions/<session-id>/host-skill-state.json`
- run the focused external-skill integration suites
- confirm docs still describe selected, active, provider, and override concepts correctly
- confirm helper skills never decide execution mode, dispatch, continuation, or completion

For runtime orchestration changes:

- run `pytest tests/test_plan_create_validate.py tests/test_execution_mode_select.py tests/test_subagent_packets.py tests/test_execution_loop.py tests/test_verification_gate.py -q`
- confirm host context separates helper skills from `runtime_owned_capabilities`
- confirm provider workflow skills do not re-enter `active_skills`

For documentation changes that describe supported behavior:

- confirm the behavior is present in code and in the shipped manifest or installer path
- scan focused tests for the relevant surface so docs do not omit shipped capabilities

## Important Code Areas

- `mcp/server/aidocs_mcp/mcp_server.py` — MCP tool registration and timeout wrappers
- `mcp/server/aidocs_mcp/runtime_service.py` — orchestration and managed runtime flow
- `mcp/server/aidocs_mcp/policy_service.py` — routing and preflight policy
- `mcp/server/aidocs_mcp/workflow_action_service.py` — workflow rule compilation
- `mcp/server/aidocs_mcp/claude_hook.py` — Claude host handler
- `mcp/server/aidocs_mcp/skill_provider.py` — bundled/external skill provider loading and validation
- `mcp/server/aidocs_mcp/skill_override_store.py` — runtime-owned capability mapping for provider workflow skills
- `mcp/server/aidocs_mcp/plan_conductor.py` — lane-aware plan execution rules
- `mcp/server/aidocs_mcp/action_surface_service.py` — should/can/did capability comparison
- `mcp/server/aidocs_mcp/related_project_service.py` — related-project config and comparison surfaces
- `mcp/server/aidocs_mcp/language_descriptors.py` — descriptor-driven indexing configuration
- `core/plugins/aidocs.js` — OpenCode plugin logic
- `core/scripts/install-agent-routing.*` — platform installation and host asset wiring

## Current Honest Status

AIDOCS is now structurally clean and internally consistent enough to document stable behavior.

The biggest remaining gaps are not broken flows; they are capability gaps or under-built surfaces:

- OpenCode is not yet at Claude parity for per-prompt runtime route execution
- Cursor is intentionally minimal
- Copilot is not implemented
- Skills are now real helper guidance, but runtime owns orchestration and completion truth
- some templates still have placeholder verification metadata and should be normalized later
