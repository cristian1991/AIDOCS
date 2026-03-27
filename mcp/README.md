# AIDOCS MCP

**v1.1.1** — Optional MCP runtime layer for AIDOCS Core.

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
    mcp_server.py          # FastMCP tool registration (88 tools)
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

- Current release: `1.1.1`

## OpenCode Caveats

- OpenCode can now mirror multilingual `action_tokens`, but that classification is still advisory.
- Claude currently uses runtime classification directly; OpenCode still relies on plugin-side context shaping plus command rewriting.
- The installer creates `action_tokens/opencode/` links or fallback copies so OpenCode-visible language packs are accessible to users.

## Benchmarking

- Run `aidocs benchmark . --json --iterations 10 --scenario-set public` for the public benchmark set.
- See `mcp/BENCHMARKS.md` for benchmark rules, output scope, and public/private scenario-set guidance.

## Tool Model

The MCP server exposes 88 tools, but agents should not start by memorizing all 88.

### Start Here

Use these as the default entry points:

- `aidocs_orchestrate` — `/aidocs` bootstrap/orchestration entry
- `aidocs_classify_prompt` + `aidocs_route_prompt` — lightweight advisory routing
- `code_investigate` — broad “start here” investigation
- `code_find` — unified find surface
- `code_trace` — unified trace surface
- `code_bundle` — unified retrieval/context surface
- `schema_query` — unified schema surface

### Core Runtime Surfaces

These make up the main operational AIDOCS runtime:

- managed mode: `aidocs_mode_get`, `aidocs_mode_set`, `aidocs_mode_clear`
- session lifecycle: `session_start`, `session_list`, `session_select`, `session_read`, `session_create`, `session_update`
- task lifecycle: `task_begin`, `task_update`, `task_complete`
- memory: `memory_read`, `memory_capture`, `memory_search`
- project operations: `project_init`, `project_bootstrap_or_resume`, `project_sync_indexes`, `project_status`

### Advanced / Specialist Surfaces

These remain useful when the host or agent already knows what it needs:

- code specialists such as `code_get_outline`, `code_get_symbol_snippet`, `code_search`, `code_get_dependencies`
- precision helpers such as `code_get_method_signature`, `code_get_method_signatures`, `code_get_constructor_params`, `code_get_enum_values`, `code_get_service_api`
- lighter schema helpers such as `schema_query(mode="properties")` and `schema_query(mode="batch_entity")`
- git analysis tools such as `git_fork_status`, `git_merge_plan`, `git_conflict_analysis`, `git_upstream_changes`
- action-surface and execution-evidence tools for operator-level debugging and runtime analysis
- capability inspection tools for understanding the indexed MCP surface
- procedure and procedure-link tools as advanced/optional structure derived from workflow definitions, not required for normal AIDOCS use

### Important Guidance

- Prefer unified entry points over old granular search habits.
- Prefer advisory runtime routing over raw keyword guessing.
- Treat many specialist tools as advanced surfaces, not the default starting point.
- Treat procedures as optional structure for workflow/execution analysis, not as a prerequisite for runtime value.
- For deep test or validation work, use test-inclusive indexing only when intentionally needed, then prefer the precision chain over guessing: service API -> method signatures -> constructor params -> enum values -> entity properties.
- Use `mcp/HOST_INTEGRATION.md` for the host behavior contract instead of inferring it from the raw tool list.

Run (after installing dependencies)
```bash
cd mcp
pip install -e .
aidocs --version
aidocs benchmark . --json --iterations 10
aidocs-mcp
```
