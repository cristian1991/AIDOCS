# AIDOCS MCP

**v2.0.0** — Portable AI coding-agent toolkit with indexed code retrieval, session management, and persistent memory.

## What's New in 2.0.0

- **Unified AccessGate** — 6-level security cascade replacing 4 scattered implementations
- **Hard gate enforcement** — raw file tools (Read/Grep/Glob/Edit/Write) blocked in managed mode; agents must use AIDOCS indexed tools
- **Per-file discovery** — replaces blanket `allow_read=True` with granular path grants
- **Optional `root` param** — all 85+ tools default to managed session project
- **CC auto-memory disabled** — `memory_capture` is the only memory path in managed projects
- **`code_text_search`** — literal text search with `|`/`OR` multi-match and regex mode
- **`code_str_replace` / `code_batch_str_replace`** — string-match edits for quick changes
- **Slimmed tool responses** — edit success returns ~30 tokens instead of echoing full content
- **All agent text externalized** — gate messages, tool descriptions loaded from TOML at startup
- **Smart installer** — manifest-based three-way merge, tag-based CLAUDE.md/AGENTS.md updates

## Principles

- Files remain the only source of truth
- MCP reads, validates, and writes existing AIDOCS files — never a second canonical memory
- MCP is optional; the Markdown system works without it
- Indexes are project-wide; sessions guide retrieval, not index scope
- SQLite indexes are derived only — rebuildable from files

## Install

```bash
pip install aidocs-mcp              # from PyPI
pip install aidocs-mcp[dev]         # with pytest
pip install aidocs-mcp[ast]         # with tree-sitter for JS/TS AST parsing
```

Or from source:
```bash
cd mcp
pip install -e ".[dev,ast]"
```

## Gate Architecture

6-level cascade, first match wins:

| Level | Gate | Action |
|-------|------|--------|
| 1 | Managed Mode | Block raw file tools when managed |
| 2 | Infrastructure | Block writes to aidocs.toml, aidocs_mcp/* |
| 3 | Sensitive Files | Block .env, credentials, keys |
| 4 | Memory Path | .MEMORY/ reads free, workflow writes intent-gated |
| 5 | Read Gate | Per-file discovery, `known_exact_path` bypass |
| 6 | Edit Gate | Requires prior read/discovery |

## Architecture

```
mcp/
  server/aidocs_mcp/        # Python service modules
    mcp_server.py            # FastMCP tool registration
    access_gate.py           # Unified 6-level security cascade
    service_hub.py           # Composition root
    runtime_service.py       # High-level orchestration
    session_store.py         # Session CRUD + lifecycle
    memory_store.py          # Memory read/write/capture
    code_index_store.py      # Code symbol/dependency/text search index
    file_ops.py              # File edit operations with gate integration
    server_code_tools.py     # Code search/find/trace/bundle tools
    server_code_edit_tools.py # Code edit/create/replace tools
    server_session_tools.py  # Session management tools
    ...
  pyproject.toml
  README.md
```
  - `aidocs descriptors --validate`
  - `aidocs descriptors --match <path>`

## Index Snapshots

- Local copied test snapshots can be inspected with:
  - `aidocs snapshots`
  - `aidocs snapshots --json`

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
