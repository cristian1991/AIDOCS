# Initial MCP Methods

## aidocs_orchestrate

Purpose
- Run the AIDOCS bootstrap, session, and first-retrieval flow as one high-level entrypoint.

Inputs
- `project_root`
- `user_request`
- optional `action_kind`
- optional `session_id`
- optional `explicit_targets`
- optional `include_code_bundle`
- optional `include_tests`

Outputs
- runtime preflight result
- bootstrap/session result
- selected session id when ready
- managed-mode state bound to the selected session
- first retrieval bundle (explicit-target or session bundle)

## aidocs_mode_get

Purpose
- Read the current AIDOCS-managed mode state for this project.

Inputs
- `project_root`

Outputs
- whether managed mode is active
- bound session id when present
- source and timestamps

## aidocs_mode_set

Purpose
- Set AIDOCS-managed mode and bind it to a selected session.

Inputs
- `project_root`
- `session_id`
- optional `source`

Outputs
- active managed-mode payload

## aidocs_mode_clear

Purpose
- Clear the current AIDOCS-managed mode state for this project.

Inputs
- `project_root`

Outputs
- inactive managed-mode payload

## aidocs_route_prompt

Purpose
- Return the deterministic MCP routing decision for a normal user prompt.

Inputs
- `project_root`
- `user_request`
- `action_kind`
- optional `explicit_targets`

Outputs
- whether AIDOCS-managed mode is active
- whether direct inspection is allowed
- whether session grounding is required
- whether task lifecycle is required
- recommended MCP flow steps
- blocked reason when present

## aidocs_classify_prompt

Purpose
- Classify a normal prompt into a deterministic AIDOCS action kind.

Inputs
- `user_request`
- optional `explicit_targets`

Outputs
- chosen action kind
- lightweight reasons for the classification

## aidocs_handle_prompt

Purpose
- Handle a normal user prompt through the MCP-first routing/orchestration flow.

Inputs
- `project_root`
- `user_request`
- optional `action_kind` (`auto` by default)
- optional `explicit_targets`
- optional `include_code_bundle`
- optional `include_tests`

Outputs
- classification result
- whether the prompt was handled through MCP routing
- routing decision
- orchestration result when applicable
- next-step guidance when blocked or unmanaged

## project_bootstrap_or_resume

Purpose
- Run the mandatory project setup -> index -> session bootstrap flow.

Inputs
- `project_root`
- optional `session_id`
- optional `include_code_bundle`
- optional `include_tests`

Outputs
- current stage
- whether the project is ready for session work
- index sync results
- session-start result or session-selection requirement

## session_start

Purpose
- Run the startup/session-selection flow and return ready context.

Inputs
- `project_root`
- optional `session_id`
- optional `include_code_bundle`
- optional `sync_indexes`

Outputs
- startup files read
- whether session selection is required
- selected session when available
- selected session context
- optional code bundle

Special case
- If the project has legacy runtime state and no session-era session yet, the bootstrap flow should return `migration_required` plus a deterministic migration proposal instead of selecting a session automatically.

## session_list

Purpose
- List session folders and parse summary fields from `SESSION.md`.

Inputs
- `project_root`

Outputs
- `session_id`
- `title`
- `status`
- `owner`
- `goal`
- `last_updated`

## session_select

Purpose
- Validate and open a chosen session.

Inputs
- `project_root`
- `session_id`

Outputs
- selected session metadata
- `SESSION.md` path
- linked local session paths

## session_create

Purpose
- Create a new session folder from canonical templates.

Inputs
- `project_root`
- `session_id`
- `title`
- `owner`
- `goal`
- `scope`

Outputs
- created paths

## session_claim_status

Purpose
- List advisory agent claims on a session and whether they are stale.

Inputs
- `project_root`
- `session_id`
- optional `stale_after_minutes`

Outputs
- session id
- claim list with stale flags

## session_claim

Purpose
- Add or refresh an advisory agent claim on a session.

Inputs
- `project_root`
- `session_id`
- `agent_id`
- `run_id`
- optional `mode`

Outputs
- updated session file sections

## session_release

Purpose
- Release one advisory claim from a session.

Inputs
- `project_root`
- `session_id`
- `agent_id`
- optional `run_id`

Outputs
- updated session file sections

## session_prune_stale_claims

Purpose
- Remove stale advisory claims from a session.

Inputs
- `project_root`
- `session_id`
- optional `stale_after_minutes`

Outputs
- updated session file sections

## session_update

Purpose
- Update structured sections inside `SESSION.md`.

Inputs
- `project_root`
- `session_id`
- section payloads

Outputs
- updated file path

## task_begin

Purpose
- Begin work in a selected session and update session/context state.

Inputs
- `project_root`
- `session_id`
- optional goal/state/upcoming/blockers
- optional relevant files/commands/snippets
- optional session facts/constraints
- optional `include_code_bundle`

Outputs
- updated session
- updated context
- optional code bundle

## task_update

Purpose
- Update an active task session and optional context state.

Inputs
- `project_root`
- `session_id`
- optional state/upcoming/blockers
- optional relevant files/commands/snippets
- optional session facts/constraints
- optional `include_code_bundle`

Outputs
- updated session
- updated context
- optional code bundle

## task_complete

Purpose
- Complete task work in a session and update session state.

Inputs
- `project_root`
- `session_id`
- `result_summary`
- optional `next_status`
- optional `include_code_bundle`

Outputs
- updated session
- optional code bundle

## runtime_preflight

Purpose
- Return host/runtime policy guidance before performing an action.

Inputs
- `project_root`
- `action_kind`
- optional `session_id`
- optional `user_explicit_targets`

Outputs
- whether the action is allowed immediately
- whether session grounding is required
- selected/available session guidance
- next-step recommendation

## memory_read

Purpose
- Read canonical memory and session-linked files in a controlled way.

Inputs
- `project_root`
- `scope`
- optional `targets`

Outputs
- files read
- extracted content

## memory_capture

Purpose
- Persist durable rules/facts into the correct canonical file.

Inputs
- `project_root`
- `kind`
- `content`
- optional `target_hint`

Outputs
- file written
- normalized entry

## project_check

Purpose
- Run strict session-era structural check on a project.

Inputs
- `project_root`

Outputs
- mode
- project root
- exit code
- stdout
- stderr
- ok

## project_check_legacy

Purpose
- Run legacy-compatible structural check on a project.

Inputs
- `project_root`

Outputs
- mode
- project root
- exit code
- stdout
- stderr
- ok

## project_fix

Purpose
- Run safe deterministic structural fixes on a project.

Inputs
- `project_root`

Outputs
- mode
- project root
- exit code
- stdout
- stderr
- ok

## project_inspect_legacy

Purpose
- Inspect whether legacy runtime files/folders are still present.

Inputs
- `project_root`

Outputs
- legacy presence flags for `NOW.md`, `TODO.md`, `DONE.md`, root plans, and root agents

## project_sync_indexes

Purpose
- Refresh all derived indexes for a project in one call.

Inputs
- `project_root`
- optional `include_tests`

Outputs
- memory index sync result
- code manifest sync result
- schema sync result

## project_status

Purpose
- Return a consolidated status view for memory, code, and schema indexes.

Inputs
- `project_root`

Outputs
- memory index status
- code index status
- schema index status
- legacy runtime presence flags

## project_status_model_get

Purpose
- Read the deterministic project status model if present.

Inputs
- `project_root`

Outputs
- parsed project status model or `null`

## project_status_evaluate

Purpose
- Evaluate the deterministic project status model.

Inputs
- `project_root`

Outputs
- overall score/state
- per-area scores
- missing checks
- evidence

## project_status_area_bundle

Purpose
- Return status details plus a subsystem bundle for one declared project-status area.

Inputs
- `project_root`
- `area_id`
- optional `limit`

Outputs
- area definition
- evaluated status for that area
- subsystem bundle for the area's first target concept when available

## legacy_read_runtime

Purpose
- Inspect legacy NOW/plans state without mutating the project.

Inputs
- `project_root`

Outputs
- presence flags for legacy runtime files
- root legacy plan list
- summarized `NOW.md` fields when present

## legacy_build_session_proposal

Purpose
- Build a non-destructive session proposal from legacy NOW/plans state.

Inputs
- `project_root`
- optional `session_id`

Outputs
- whether legacy state exists
- proposed session id
- inferred title/goal/scope
- upcoming items
- blockers
- legacy plan file list
- deterministic next-step guidance
- migration decision options

## related_projects_list

Purpose
- List related projects declared in project memory config.

Inputs
- `project_root`

Outputs
- related-project entries with metadata like name, path, reason, or notes

## related_project_get

Purpose
- Get one related-project entry from project memory config.

Inputs
- `project_root`
- `name`

Outputs
- matching related-project entry or `null`

## related_project_code_search

Purpose
- Search code in a configured related project using the same generic code index.

Inputs
- `project_root`
- `name`
- `query`
- optional `limit`

Outputs
- same shape as `code_search`, but against the related project

## related_project_symbol_bundle

Purpose
- Build a symbol bundle from a configured related project.

Inputs
- `project_root`
- `name`
- `symbol`
- optional `path`
- optional `kind`
- optional `limit`

Outputs
- same shape as `code_get_symbol_bundle`, but against the related project

## related_project_subsystem_bundle

Purpose
- Build a broad subsystem bundle from a configured related project.

Inputs
- `project_root`
- `name`
- `concept`
- optional `limit`

Outputs
- same shape as `code_get_subsystem_bundle`, but against the related project

## related_project_compare_concept

Purpose
- Compare a concept between the current project and a configured related project.

Inputs
- `project_root`
- `name`
- `concept`
- optional `limit`

Outputs
- current project path
- related project metadata
- current project subsystem bundle
- related project subsystem bundle

## index_sync

Purpose
- Rebuild the derived SQLite memory/session index from canonical files.

Inputs
- `project_root`

Outputs
- indexed session count
- indexed memory-file count

## index_status

Purpose
- Report the current state of the derived SQLite index.

Inputs
- `project_root`

Outputs
- database path
- session count
- memory-file count
- memory-link count

## memory_search

Purpose
- Search the derived memory index by path, title, or body text.

Inputs
- `project_root`
- `query`
- optional `limit`

Outputs
- path
- kind
- title
- snippet

## schema_index_sync

Purpose
- Rebuild the derived schema catalog from code and SQL files.

Inputs
- `project_root`

Outputs
- indexed entity count
- indexed field count

## schema_index_status

Purpose
- Report the current state of the derived schema index.

Inputs
- `project_root`

Outputs
- database path
- schema entity count
- schema field count

## schema_find_entities

Purpose
- Find indexed schema entities such as tables, DTOs, models, and enums.

Inputs
- `project_root`
- optional `query`
- optional `limit`

Outputs
- entity name
- kind
- source type
- file path
- container
- line number

## schema_get_entity

Purpose
- Return one indexed schema/catalog entity with its fields or members.

Inputs
- `project_root`
- `entity_name`

Outputs
- entity metadata rows
- field/member rows

## schema_find_field

Purpose
- Find indexed schema fields, columns, or properties by name.

Inputs
- `project_root`
- `field_name`
- optional `limit`

Outputs
- entity name
- field name
- field type
- field kind
- source type
- file path
- line number

## schema_trace_entity_flow

Purpose
- Trace one schema/catalog entity across schema definitions and indexed code references.

Inputs
- `project_root`
- `entity_name`
- optional `limit`

Outputs
- searched entity name
- matching schema entities
- schema fields/members
- code definitions and references for the same concept

## schema_trace_relationship_path

Purpose
- Trace possible relationship paths between two schema entities.

Inputs
- `project_root`
- `source_entity`
- `target_entity`
- optional `limit`

Outputs
- source entity
- target entity
- relationship path candidates through FK/id-reference edges

## code_index_sync

Purpose
- Rebuild the derived code manifest and summary index from repository files.

Inputs
- `project_root`

Outputs
- indexed code-file count

## code_index_status

Purpose
- Report the current state of the derived code index.

Inputs
- `project_root`

Outputs
- database path
- code-file count

## code_search

Purpose
- Search the derived code index by file path and lightweight summary text.

Inputs
- `project_root`
- `query`
- optional `limit`

Outputs
- path
- language
- line_count
- summary
- role

## code_get_dependencies

Purpose
- Return lightweight dependency/import edges for one indexed code file.

Inputs
- `project_root`
- `path`

Outputs
- target
- kind (`import`, `require`, `dynamic_import`, `using`)

## code_find_dependents

Purpose
- Return files that depend on a given import/using target.

Inputs
- `project_root`
- `target`
- optional `limit`

Outputs
- path
- kind

## code_get_dependency_bundle

Purpose
- Return a dependency-aware bundle for one indexed code file.

Inputs
- `project_root`
- `path`
- optional `include_dependents`
- optional `limit`

Outputs
- root file bundle
- resolved dependency targets
- resolved local file stubs for those dependencies
- optional dependents

## code_search_symbols

Purpose
- Search indexed outline symbols directly across the codebase.

Inputs
- `project_root`
- `query`
- optional `limit`

Outputs
- path
- symbol
- kind
- line_number
- container
- is_partial

Notes
- indexed files may also carry inferred roles like `page`, `layout`, `component`, `context-provider`, `service`, or `controller` to improve bundle ranking.

## code_find_references

Purpose
- Find exact line-level references to a symbol across indexed code files.

Inputs
- `project_root`
- `symbol`
- optional `limit`

Outputs
- searched symbol
- file path
- language
- line number
- line text
- inferred layer

## code_trace_field_flow

Purpose
- Trace likely cross-layer field or setting usage across the indexed codebase.

Inputs
- `project_root`
- `field_name`
- optional `limit`

Outputs
- searched field name
- ranked matches across likely layers
- file path
- inferred layer (`data`, `logic`, `api`, `ui`, `code`)
- symbol/kind/line number when available
- snippet or summary

## code_trace_setting_usage

Purpose
- Trace likely cross-layer usage of a config or setting concept.

Inputs
- `project_root`
- `setting_name`
- optional `limit`

Outputs
- searched setting name
- ranked matches across likely layers
- file path
- inferred layer (`data`, `logic`, `api`, `ui`, `code`)
- symbol/kind/line number when available
- snippet or summary

## code_trace_service_usage

Purpose
- Trace likely definition and usage points for a service-like concept.

Inputs
- `project_root`
- `service_name`
- optional `limit`

Outputs
- searched service name
- ranked matches across likely layers
- file path
- inferred layer (`data`, `logic`, `api`, `ui`, `code`)
- symbol/kind/line number when available
- snippet or summary

## code_trace_model_usage

Purpose
- Trace likely definition and usage points for a DTO, model, or entity-like concept.

Inputs
- `project_root`
- `model_name`
- optional `limit`

Outputs
- searched model name
- definition snippets
- references
- schema entity/field evidence when available

## code_trace_component_usage

Purpose
- Trace likely definition, references, and local frontend neighbors for a component-like symbol.

Inputs
- `project_root`
- `component_name`
- optional `limit`

Outputs
- searched component name
- matching definitions
- line-level references
- neighboring frontend nodes from the local import tree

## code_find_mutation_points

Purpose
- Find likely create, update, save, delete, toggle, sync, or other mutation points for a concept.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- ranked mutation candidates
- file path
- inferred layer
- symbol/kind/line number when available
- snippet or summary

## code_find_validation_surfaces

Purpose
- Find likely validation logic, validators, required rules, and validation-related surfaces for a concept.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- ranked validation candidates
- file path
- inferred layer
- symbol/kind/line number when available
- snippet or summary

## code_find_async_boundaries

Purpose
- Find likely async, background, deferred, or queued execution boundaries.

Inputs
- `project_root`
- optional `concept`
- optional `limit`

Outputs
- searched concept
- ranked async-boundary candidates
- file path
- inferred layer
- symbol/kind/line number when available
- snippet or summary

## code_find_hotspots

Purpose
- Find likely complexity hotspots using generic code-index signals.

Inputs
- `project_root`
- optional `query`
- optional `limit`

Outputs
- searched query
- ranked hotspot files
- path
- language
- role
- line count
- outline count
- dependency count
- score
- why

## code_find_query_hotspots

Purpose
- Find likely query-complexity hotspots using generic query signals.

Inputs
- `project_root`
- optional `query`
- optional `limit`

Outputs
- searched query
- ranked query-heavy files
- path
- language
- role
- line count
- score
- why

## code_find_state_model_mismatch

Purpose
- Find likely mixed or competing state-model representations for a concept.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- ranked mismatch candidates
- file path
- inferred layer
- symbol/kind/line number when available
- mismatch type
- snippet or summary

## code_find_ui_backend_touchpoints

Purpose
- Find likely UI/backend touchpoints for a concept across indexed code.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- ranked matches across likely backend and UI layers
- file path
- inferred layer (`data`, `logic`, `api`, `ui`)
- symbol/kind/line number when available
- snippet or summary

## code_find_policy_surfaces

Purpose
- Find likely policy, RBAC, guard, permission, or authorization surfaces for a concept.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- ranked matches across likely enforcement layers
- file path
- inferred layer (`data`, `logic`, `api`, `ui`, `code`)
- symbol/kind/line number when available
- snippet or summary

## code_find_domain_clusters

Purpose
- Find a broader cross-layer domain cluster for a concept using code and schema matches together.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- searched concept
- clustered matches across symbols, files, schema entities, and schema fields
- file path
- inferred layer (`data`, `logic`, `api`, `ui`, `code`)
- symbol/kind/line number when available

## code_find_entrypoints

Purpose
- Find likely startup, bootstrap, registration, or provider entrypoints.

Inputs
- `project_root`
- optional `concept`
- optional `limit`

Outputs
- searched concept
- ranked entrypoint candidates
- file path
- inferred layer
- symbol/kind/line number when available

## code_find_routes

Purpose
- Find likely route, endpoint, controller, and page entry surfaces.

Inputs
- `project_root`
- optional `query`
- optional `limit`

Outputs
- searched query
- ranked route candidates
- file path
- inferred layer
- symbol/kind/line number when available

## code_trace_api_to_ui

Purpose
- Trace likely API-to-UI connection points for a concept.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- concept
- API-side matches
- logic-side matches
- UI-side matches

## code_find_transition_points

Purpose
- Find likely migration seams, adapters, compatibility layers, and transition hotspots.

Inputs
- `project_root`
- optional `concept`
- optional `limit`

Outputs
- searched concept
- ranked transition candidates
- file path
- inferred layer
- symbol/kind/line number when available
- snippet or summary

## code_get_outline

Purpose
- Return a lightweight outline for a specific indexed code file.

Inputs
- `project_root`
- `path`

Outputs
- symbol
- kind
- line_number

## code_find_partial_group

Purpose
- Return all indexed partial C# definitions for a symbol.

Inputs
- `project_root`
- `symbol`
- optional `limit`

Outputs
- path
- symbol
- kind
- line_number
- container
- is_partial

## code_find_data_structures

Purpose
- Return indexed DTO/model/enum/data-structure symbols and their members.

Inputs
- `project_root`
- optional `query`
- optional `limit`

Outputs
- path
- symbol
- kind (`class`, `record`, `struct`, `interface`, `enum`, `property`, `field`, `enum_member`)
- line_number
- container
- is_partial

## code_find_frontend_symbols

Purpose
- Return indexed frontend-oriented symbols such as components, hooks, functions, and initializers.

Inputs
- `project_root`
- optional `query`
- optional `kinds`
- optional `limit`

Outputs
- path
- symbol
- kind (`component`, `context_provider`, `hook`, `function`, `initializer`)
- line_number
- container
- is_partial

## code_find_initializers

Purpose
- Return indexed JS/TS global initializer hooks and startup listeners.

Inputs
- `project_root`
- optional `path`
- optional `limit`

Outputs
- path
- symbol
- kind (`initializer`)
- line_number

## code_get_symbol_snippet

Purpose
- Return an exact code snippet for an indexed outline symbol.

Inputs
- `project_root`
- `path`
- `symbol`
- optional `kind`
- optional `line_number`

Outputs
- path
- symbol
- kind
- line_number
- container
- is_partial
- language
- snippet

## code_get_symbol_bundle

Purpose
- Return a full bundle for a symbol, combining definitions, references, dependencies, partials, and schema hints.

Inputs
- `project_root`
- `symbol`
- optional `path`
- optional `kind`
- optional `limit`

Outputs
- symbol
- definition snippets
- references
- dependencies
- partial definitions
- schema entity hints
- schema field hints

## code_get_subsystem_bundle

Purpose
- Return a broad subsystem bundle for a concept using multiple generic analyzers together.

Inputs
- `project_root`
- `concept`
- optional `limit`

Outputs
- concept
- domain cluster
- UI/backend touchpoints
- policy surfaces
- transition points
- data structures
- entrypoints

## code_get_partial_bundle

Purpose
- Return code snippets for all indexed partial definitions of a C# symbol.

Inputs
- `project_root`
- `symbol`
- optional `limit`

Outputs
- repeated `code_get_symbol_snippet`-style entries for each partial definition

## code_get_file_bundle

Purpose
- Return a targeted indexed bundle for one code file.

Inputs
- `project_root`
- `path`

Outputs
- path
- language
- line_count
- summary
- outline
- initializers (when relevant)
- partial_groups (when relevant)

## code_get_component_bundle

Purpose
- Return a frontend-oriented bundle for a component file and its imported neighbors.

Inputs
- `project_root`
- `path`
- optional `limit`

Outputs
- root file bundle
- frontend symbols in the root file
- imported local frontend neighbor bundles

## code_get_service_bundle

Purpose
- Return a backend-oriented bundle for a service-like file and its related local neighbors.

Inputs
- `project_root`
- `path`
- optional `limit`

Outputs
- root file bundle
- service symbols in the root file
- dependency bundle
- related local backend/data files

## code_get_query_bundle

Purpose
- Return a query-oriented bundle for a query-heavy file, including schema hints and dependencies.

Inputs
- `project_root`
- `path`
- optional `limit`

Outputs
- root file bundle
- hotspot score and reasons when present
- dependency bundle
- schema entity hints found in the file
- schema field hints found in the file

## code_trace_query_shape

Purpose
- Trace the likely shape of a query-heavy file across entities, fields, and relationships.

Inputs
- `project_root`
- `path`
- optional `limit`

Outputs
- root file bundle
- hotspot analysis
- dependency bundle
- schema entities
- schema fields
- schema relationship paths between touched entities when found

## code_get_component_tree

Purpose
- Return a recursive local frontend import tree for a component, page, provider, or layout file.

Inputs
- `project_root`
- `path`
- optional `depth`
- optional `limit`

Outputs
- root path
- tree depth
- nodes with roles/symbols
- edges between imported local frontend files

## code_get_style_bundle

Purpose
- Return indexed CSS matches for a set of class names.

Inputs
- `project_root`
- `class_names`
- optional `limit`

Outputs
- class_names
- matching selectors/files/line numbers

## code_get_session_bundle

Purpose
- Return targeted code bundles for the files referenced by a session's `context.md`.

Inputs
- `project_root`
- `session_id`

Outputs
- session id
- file bundles for each relevant code file in session context

## code_get_context_bundle

Purpose
- Return a ranked code bundle guided by session context.

Inputs
- `project_root`
- `session_id`
- optional `include_dependencies`
- optional `include_styles`
- optional `limit`

Outputs
- session id
- primary file bundles
- dependency file bundles
- style bundle
- ordered items

## code_get_preset_bundle

Purpose
- Return a higher-level preset bundle for common retrieval cases.

Inputs
- `project_root`
- `preset`
- `value`
- optional `limit`

Supported presets
- `csharp-partial`
- `js-initializer`
- `data-structure`
- `session`
- `context`
- `dependency`
- `style`

Outputs
- preset name
- value
- bundle payload from the matching lower-level retrieval flow
