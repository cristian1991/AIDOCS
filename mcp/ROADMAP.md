# MCP Tool Consolidation Plan (v1.1.0)

## Problem

126 tools cause analysis paralysis for agents. Many tools overlap and agents can't
distinguish when to use which. The agent defaults to raw Grep/Read because it's
familiar, even when indexed tools would be faster and more complete.

## Solution: Merge into 5 entry points + specialists

### Tier 1: Entry Points (agents should start here)

| New Tool | Replaces | Description |
|----------|----------|-------------|
| `code_investigate` | (new in 1.0.2) | Navigation guide: probes symbols/files/schema/CSS/modules, returns what was found + which tools to call next |
| `code_find` | `code_search_symbols`, `code_find_references`, `code_find_routes`, `code_find_hotspots`, `code_find_entrypoints`, `code_find_duplicates`, `code_find_partial_group`, `code_find_partial_consumers`, `code_find_api_consumers`, `code_find_frontend_symbols`, `code_find_data_structures`, `code_find_initializers` | Unified find with `mode` parameter |
| `code_trace` | `code_trace_field_flow`, `code_trace_service_usage`, `code_trace_model_usage`, `code_trace_component_usage`, `code_trace_api_to_ui`, `code_trace_css_class`, `code_trace_query_shape`, `code_trace_setting_usage` | Unified trace with `mode` parameter |
| `code_bundle` | `code_get_file_bundle`, `code_get_service_bundle`, `code_get_component_bundle`, `code_get_query_bundle`, `code_get_subsystem_bundle`, `code_get_dependency_bundle`, `code_get_partial_bundle`, `code_get_context_bundle`, `code_get_session_bundle`, `code_get_preset_bundle`, `code_get_symbol_bundle` | Unified bundle with `depth` parameter |
| `schema_query` | `schema_find_entities`, `schema_get_entity`, `schema_find_field`, `schema_trace_entity_flow`, `schema_trace_relationship_path` | Unified schema with `mode` parameter |

### Tier 2: Specialists (called by Tier 1 or directly when the agent knows what it needs)

Keep as-is but mark as "advanced" in descriptions:
- `code_get_outline` — lightweight, always useful
- `code_get_symbol_snippet` — targeted read
- `code_search` — file-level search
- `code_get_modules` / `code_get_module_files` — project structure
- `code_index_sync` / `code_index_status` — index management

### Tier 3: Analysis tools (merge into `code_find` modes)

These become modes of `code_find`:
- `code_find_mutation_points` -> `code_find(mode="mutations")`
- `code_find_validation_surfaces` -> `code_find(mode="validation")`
- `code_find_async_boundaries` -> `code_find(mode="async")`
- `code_find_policy_surfaces` -> `code_find(mode="policy")`
- `code_find_ui_backend_touchpoints` -> `code_find(mode="touchpoints")`
- `code_find_state_model_mismatch` -> `code_find(mode="mismatches")`
- `code_find_domain_clusters` -> `code_find(mode="clusters")`
- `code_find_transition_points` -> `code_find(mode="transitions")`

## Migration Strategy

1. Add new unified tools alongside existing ones (v1.1.0-beta)
2. Update directives to prefer unified tools
3. Mark old tools as deprecated in descriptions
4. Remove old tools in v1.2.0

## Tool Count Projection

| Release | Tools | Notes |
|---------|-------|-------|
| v1.0.1 | 126 | Current |
| v1.0.2 | 127 | +code_investigate |
| v1.1.0 | ~30 | Unified entry points + specialists |
| v1.2.0 | ~25 | Remove deprecated |
