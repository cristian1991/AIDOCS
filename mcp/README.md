# AIDOCS MCP

**v1.0.1** — Optional MCP runtime layer for AIDOCS Core.

`core/` remains the canonical Markdown-first system.
`mcp/` adds runtime enforcement, indexing, and retrieval over that file-backed system.

## Principles

- Files remain the only source of truth.
- MCP never stores a second canonical memory.
- MCP reads, validates, and writes the existing AIDOCS files.
- MCP is optional; the Markdown system must still work without it.
- Indexes are project-wide; sessions guide retrieval and ranking, not index scope.
- SQLite indexes are derived only — rebuildable from files at any time.

## Install

```bash
cd mcp
pip install -e .          # standard install
pip install -e ".[dev]"   # with pytest
pip install -e ".[ast]"   # with tree-sitter for JS/TS AST parsing
```

## Architecture

```
mcp/
  server/aidocs_mcp/      # 21 Python service modules
    mcp_server.py          # FastMCP tool registration (90+ tools)
    service_hub.py         # Composition root
    runtime_service.py     # High-level orchestration
    session_store.py       # Session CRUD + lifecycle
    memory_store.py        # Memory read/write/capture
    index_store.py         # Memory/session SQLite index
    code_index_store.py    # Code symbol/dependency index
    schema_index_store.py  # Schema entity/field index
    policy_service.py      # Preflight + routing policy
    managed_mode_service.py # Project managed-mode state
    workflow_action_service.py # Workflow rule compilation
    legacy_migration_service.py # NOW.md/plans migration
    updater_service.py     # Cross-platform script bridge
    frontend_ast.py        # JS/TS tree-sitter AST parsing
    ...
  pyproject.toml           # Python package config
  README.md                # This file
  ROADMAP.md               # Feature roadmap
  HOST_INTEGRATION.md      # Host integration contract
```

## Runtime Model

- Canonical files: `core/` templates + project-local `/.MEMORY/**`
- MCP server: optional execution layer over those files
- Local index: SQLite, derived only, rebuildable
- Code retrieval: symbol outlines, dependency edges, context bundles

## Release Status

- Current release: `1.0.1`

## OpenCode Caveats

- OpenCode can now mirror multilingual `action_tokens`, but that classification is still advisory.
- Claude currently uses runtime classification directly; OpenCode still relies on plugin-side context shaping plus command rewriting.
- The installer creates `action_tokens/opencode/` links or fallback copies so OpenCode-visible language packs are accessible to users.

## Implemented Tools (90+)
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
