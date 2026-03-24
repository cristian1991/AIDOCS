# AIDOCS

**v0.99-beta** — Portable AI coding-agent toolkit with a routed memory system, session-based runtime, and optional MCP enforcement layer.

## Components

### AIDOCS Core (`build/`)
Standalone markdown-first memory system. Works with any AI coding agent (Claude Code, OpenCode, etc.) without dependencies.

- Routed memory: `index.aidocs` -> `INDEX.md` -> `SESSION.md`
- Session-based runtime: isolated workstreams, plans, agent artifacts
- Global command pack: `/aidocs`, `/reingest`, `/archive`, `/personality`, `/clean`
- Cross-platform install scripts (Windows + Linux/macOS)
- Zero dependencies — just markdown files and shell scripts

### AIDOCS MCP (`mcp/`)
Optional Python MCP server that adds runtime enforcement, indexing, and retrieval over the Core file system.

- 90+ MCP tools for session, memory, code, schema, and project lifecycle
- Derived SQLite indexes (memory, code symbols, schema entities) — rebuildable from files
- Code retrieval: outlines, symbol search, dependency edges, context bundles
- Schema analysis: entity classification, field search, relationship tracing
- Session enforcement: managed mode, task lifecycle, policy routing
- Requires: Python 3.11+, `fastmcp>=2.0.0`

**Principle:** Files remain the only source of truth. MCP never stores a second canonical copy. Deleting the SQLite index loses nothing — it rebuilds from files.

## Quick Start

### Core Only (no dependencies)

```bash
# Install global routing + commands
build\scripts\install-agent-routing.cmd    # Windows
bash build/scripts/install-agent-routing.sh # Linux/macOS (if available)

# In any project:
/aidocs    # Bootstrap or resume AIDOCS for the project
```

### Core + MCP

```bash
# Install MCP server
cd mcp
pip install -e .

# Configure your agent to use the MCP server:
# Claude Code: add to ~/.claude/settings.json mcpServers
# OpenCode: add to opencode.jsonc mcp section

# In any project:
/aidocs    # Now uses MCP-backed session/memory/retrieval
```

## Memory Model

Inside initialized projects, the memory system lives in project-local `/.MEMORY/`:

```
/.MEMORY/
  .aidocs/index.aidocs          -> session-start router
  INDEX.md                       -> durable-memory router
  sessions/<id>/SESSION.md       -> session state + scope
  sessions/<id>/context.md       -> session-local context
  sessions/<id>/plans/           -> implementation plans
  sessions/<id>/agents/          -> agent artifacts
  sessions/<id>/artifacts/       -> outputs/logs/reports
  rules/                         -> workflow, communication, coding rules
  domains/                       -> domain knowledge, project state
  system/                        -> architecture metadata
  CHANGELOG.md                   -> completed work history
```

## Canonical Layout

```
build/                    # Standalone markdown-first system
  .commands/              # Global command source (/aidocs, /reingest, etc.)
  .MEMORY/.aidocs/        # Canonical instruction + memory-system docs
  scripts/                # Install, check, fix scripts
  plugins/                # OpenCode global plugin
  AGENTS.md               # Project router template
  CLAUDE.md               # Claude bootstrap template

mcp/                      # Optional MCP runtime layer
  server/aidocs_mcp/      # Python MCP server (21 service modules)
  tests/                  # Test suite (private repo only)
  pyproject.toml          # Python package config
  README.md               # MCP-specific docs
  ROADMAP.md              # Feature roadmap
  HOST_INTEGRATION.md     # Host integration contract
```

## Commands

| Command | Description |
|---------|-------------|
| `/aidocs` | Bootstrap or connect a project to AIDOCS, select or create a session |
| `/reingest` | Refresh memory by user-selected scope |
| `/archive` | Promote completed work into CHANGELOG and archive logs |
| `/personality` | Set or clear user-facing communication personality |
| `/clean` | Run cleanup by scope (file, dead-code, dedupe, structural) |

## Documentation

- [MCP Tools & Architecture](mcp/README.md) — Full MCP tool list, install, architecture
- [Install Guide](README_INSTALL.md) — Detailed installation steps

## Releases

Each release provides two packages:

- **aidocs-core** — `build/` directory (standalone markdown system)
- **aidocs-mcp** — `mcp/server/` + config (Python MCP package)

Download from [Releases](https://github.com/cristian1991/AIDOCS/releases).

## License

Apache 2.0. See `LICENSE` and `NOTICE`.
