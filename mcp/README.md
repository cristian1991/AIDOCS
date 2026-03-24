# AIDOCS MCP

Optional MCP layer for AIDOCS Core.

`build/` remains the canonical Markdown-first system.
`mcp/` adds runtime enforcement, indexing, and retrieval over that file-backed system.

Principles
- Files remain the only source of truth.
- MCP never stores a second canonical memory.
- MCP reads, validates, and writes the existing AIDOCS files.
- MCP is optional; the Markdown system must still work without it.
- Indexes are project-wide; sessions guide retrieval and ranking, not index scope.

Planned responsibilities
- Enforce startup/session selection flow
- Enforce memory write lifecycle
- Provide structured memory read/write APIs
- Provide safe project update and legacy migration helpers
- Build local derived indexes for memory and code
- Support targeted code reads and wider code sweeps

Proposed layout
```text
mcp/
  README.md
  ROADMAP.md
  pyproject.toml
  server/
    aidocs_mcp/
  tests/
```

Planned runtime model
- Canonical files: `build/` and project-local `/.MEMORY/**`
- MCP server: optional execution layer
- Local index: SQLite, derived only, rebuildable
- Code retrieval: jcodemunch-style symbol/index layer

Relationship to AIDOCS Core
- `build/` = human-readable core
- `mcp/` = optional runtime engine
- both must stay aligned to the same contracts

Current implementation status
- first file-backed services exist:
  - managed file rewriting
  - session listing/reading/creation/update
  - canonical memory read/capture
- first MCP tools exist:
  - `aidocs_orchestrate`
  - `aidocs_mode_get`
  - `aidocs_mode_set`
  - `aidocs_mode_clear`
  - `aidocs_classify_prompt`
  - `aidocs_route_prompt`
  - `aidocs_handle_prompt`
  - `project_bootstrap_or_resume`
  - `session_start`
  - `session_list`
  - `session_select`
  - `session_read`
  - `session_create`
  - `session_claim_status`
  - `session_claim`
  - `session_release`
  - `session_prune_stale_claims`
  - `session_update`
  - `task_begin`
  - `task_update`
  - `task_complete`
  - `runtime_preflight`
  - `memory_read`
  - `memory_capture`
  - `project_check`
  - `project_check_legacy`
  - `project_fix`
  - `project_inspect_legacy`
  - `project_sync_indexes`
  - `project_status`
  - `project_status_model_get`
  - `project_status_evaluate`
  - `project_status_area_bundle`
  - `legacy_read_runtime`
  - `legacy_build_session_proposal`
  - `related_projects_list`
  - `related_project_get`
  - `related_project_code_search`
  - `related_project_symbol_bundle`
  - `related_project_subsystem_bundle`
  - `related_project_compare_concept`
  - `index_sync`
  - `index_status`
  - `memory_search`
  - `schema_index_sync`
  - `schema_index_status`
  - `schema_find_entities`
  - `schema_get_entity`
  - `schema_find_field`
  - `schema_trace_entity_flow`
  - `schema_trace_relationship_path`
  - `code_index_sync`
  - `code_index_status`
  - `code_search`
  - `code_get_dependencies`
  - `code_find_dependents`
  - `code_get_dependency_bundle`
  - `code_search_symbols`
  - `code_find_references`
  - `code_trace_field_flow`
  - `code_trace_setting_usage`
  - `code_trace_service_usage`
  - `code_trace_model_usage`
  - `code_trace_component_usage`
  - `code_find_mutation_points`
  - `code_find_validation_surfaces`
  - `code_find_async_boundaries`
  - `code_find_hotspots`
  - `code_find_query_hotspots`
  - `code_find_state_model_mismatch`
  - `code_find_ui_backend_touchpoints`
  - `code_find_policy_surfaces`
  - `code_find_domain_clusters`
  - `code_find_entrypoints`
  - `code_find_routes`
  - `code_trace_api_to_ui`
  - `code_find_transition_points`
  - `code_get_outline`
  - `code_find_partial_group`
  - `code_find_data_structures`
  - `code_find_frontend_symbols`
  - `code_find_initializers`
  - `code_get_symbol_snippet`
  - `code_get_symbol_bundle`
  - `code_get_subsystem_bundle`
  - `code_get_partial_bundle`
  - `code_get_file_bundle`
  - `code_get_component_bundle`
  - `code_get_service_bundle`
  - `code_get_query_bundle`
  - `code_trace_query_shape`
  - `code_get_component_tree`
  - `code_get_session_bundle`
  - `code_get_context_bundle`
  - `code_get_preset_bundle`

Run (after installing dependencies)
```bash
cd mcp
pip install -e .
aidocs-mcp
```
