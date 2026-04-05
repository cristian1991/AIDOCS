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

## Tool Model

85+ tools organized by purpose. Agents should start with entry points, not memorize all tools.

### Entry Points

- `orchestrate` — `/aidocs` bootstrap/orchestration
- `classify_prompt` + `route_prompt` — advisory routing
- `code_investigate` — broad "start here" investigation
- `code_find` — unified symbol/reference search
- `code_trace` — relationship tracing
- `code_bundle` — context retrieval
- `schema_query` — database schema

### Core Runtime

- managed mode: `mode_get`, `mode_set`, `mode_clear`
- sessions: `session_start`, `session_list`, `session_select`, `session_create`
- tasks: `task_begin`, `task_update`, `task_complete`
- memory: `memory_read`, `memory_capture`, `memory_search`
- project: `project_init`, `project_bootstrap_or_resume`, `project_sync_indexes`

### Code Operations

- read: `code_get_lines`, `code_text_search`, `code_search`
- edit: `code_edit_lines`, `code_str_replace`, `code_batch_str_replace`, `code_batch_edit`
- create: `code_create_file`, `code_insert_lines`
- analysis: `code_find_dead_code`, `code_find_stale_references`, `code_suggest_extractions`

### Precision Helpers

- `code_get_method_signature` / `code_get_method_signatures`
- `code_get_constructor_params` / `code_get_constructor_params_batch`
- `code_get_enum_values`, `code_get_entity_properties`, `code_get_service_api`

## Run

```bash
pip install aidocs-mcp
aidocs --version
aidocs-mcp   # start MCP server
```
