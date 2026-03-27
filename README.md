<p align="center">
  <img src="docs/assets/cn-logo.svg" alt="CodeNexus" width="120">
</p>

<h1 align="center">AIDOCS</h1>

<p align="center">
  <strong>Portable memory, routing, and runtime toolkit for AI coding agents.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.2.0-blue" alt="version">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="license">
  <img src="https://img.shields.io/badge/python-3.11%2B-yellow" alt="python">
  <img src="https://img.shields.io/badge/tests-255%20passing-brightgreen" alt="tests">
</p>

<p align="center">
  A <a href="https://codenexus.cloud">CodeNexus</a> project
</p>

### Supported Agents

<table>
<tr>
<td align="center" width="200">
  <img src="https://img.shields.io/badge/Claude_Code-CC5500?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude Code"><br>
  <sub>Hooks: <code>UserPromptSubmit</code>, <code>PreToolUse</code></sub><br>
  <sub>Core + MCP + auto <code>.mcp.json</code></sub>
</td>
<td align="center" width="200">
  <img src="https://img.shields.io/badge/OpenCode-000000?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cGF0aCBkPSJNOCA1TDMgMTJMOCAxOU0xNiA1TDIxIDEyTDE2IDE5IiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIiLz48L3N2Zz4=&logoColor=white" alt="OpenCode"><br>
  <sub>Plugin: <code>chat.message</code>, <code>tool.execute.*</code></sub><br>
  <sub>Core + MCP + global plugin</sub>
</td>
<td align="center" width="200">
  <img src="https://img.shields.io/badge/Any_MCP_Client-4A90D9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cGF0aCBkPSJNMTIgMkw0IDdWMTdMMTIgMjJMMjAgMTdWN0wxMiAyWiIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSIyIi8+PC9zdmc+" alt="MCP"><br>
  <sub>Via <code>.mcp.json</code> / stdio</sub><br>
  <sub>Core + MCP tools</sub>
</td>
<td align="center" width="200">
  <img src="https://img.shields.io/badge/Any_Agent-gray?style=for-the-badge" alt="Any"><br>
  <sub>Reads <code>AGENTS.md</code> / <code>CLAUDE.md</code></sub><br>
  <sub>Core only (no MCP needed)</sub>
</td>
</tr>
</table>

---

## What It Does

AIDOCS gives AI coding agents a durable working model for real projects:

- persistent project memory
- session-based task context
- routed startup instead of blind repo scanning
- indexed retrieval instead of ad hoc grep-first exploration
- optional MCP/runtime integration for stronger host behavior

In practice, that means agents can resume work, follow session context, and use a structured runtime/tooling path instead of rediscovering the repo every time.

### Product Surfaces

- **Core** — portable markdown-based memory, routing, command specs, and host bootstrap files
- **MCP runtime** — indexing, routing, orchestration, retrieval, and host integration support
- **CLI** — `aidocs init`, `status`, `sync`, `benchmark`, `version`
- **Host adapters** — Claude hooks, OpenCode plugin, and generic MCP-client support

### Key Capabilities

- **Routed memory** — agents follow a startup chain instead of dumping the whole repo into context
- **Session isolation** — parallel workstreams with their own plans, context, and artifacts
- **Unified retrieval** — broad “start here”, find, trace, bundle, and schema entry points
- **Multilingual classification** — public benchmarked language support via `action_tokens`
- **Monorepo-aware indexing** — multiple modules/projects without changing the memory model
- **Operator tooling** — CLI, install scripts, benchmark mode, and host/runtime documentation

## Quick Start

### Core Only (no dependencies)

```bash
# Install global routing + commands
core\scripts\install-agent-routing.cmd    # Windows
bash core/scripts/install-agent-routing.sh  # Linux/macOS

# In any project:
/aidocs    # Bootstrap or resume AIDOCS for the project
```

### Core + MCP (recommended)

```bash
# Install MCP server
cd mcp && pip install -e .

# Claude Code: /aidocs auto-creates .mcp.json in your project
# OpenCode: installer configures opencode.json automatically

# In any project:
/aidocs    # Uses MCP-backed session/memory/retrieval
```

## Project Layout

```
aidocs.toml                # MCP/runtime configuration
aidocs-plugin.json         # OpenCode plugin configuration
action_tokens/             # Prompt-classification language files
  en.yaml, it.yaml, ...

core/                      # Portable memory/command/bootstrap layer
  .commands/                # Global commands (/aidocs, /reingest, /archive, etc.)
  plugins/                  # OpenCode global plugin
  scripts/                  # Cross-platform install scripts

mcp/                        # Optional MCP runtime + CLI layer
  server/aidocs_mcp/        # Python MCP server and CLI implementation
  tests/                    # 255 tests (private repo only)
```

## Memory Model

Inside initialized projects, memory lives in `/.MEMORY/`:

```
/.MEMORY/
  .aidocs/index.aidocs       -> session-start router
  INDEX.md                    -> durable-memory router
  sessions/<id>/
    SESSION.md                -> session state + scope
    context.md                -> session-local context
    journal.md                -> rolling decision log (auto-evicts to archive)
    plans/                    -> implementation plans
    agents/                   -> spawned agent artifacts
  rules/                      -> workflow, coding, communication rules
  domains/                    -> domain knowledge
  archive/                    -> evicted journal entries, completed sessions
```

## Commands

| Command | Description |
|---------|-------------|
| `/aidocs` | Bootstrap or resume AIDOCS, select a session |
| `/reingest` | Refresh memory by scope |
| `/archive` | Promote completed work to CHANGELOG |
| `/personality` | Set agent communication style |
| `/clean` | Cleanup by scope (file, dead-code, dedupe, structural) |

## CLI

| Command | Description |
|---------|-------------|
| `aidocs init` | Initialize AIDOCS structure in a project |
| `aidocs status` | Show project/runtime/index status |
| `aidocs sync` | Refresh memory/code/schema indexes |
| `aidocs benchmark` | Run public benchmark scenarios |
| `aidocs version` | Show package version |

## Configuration

Edit these files to customize behavior:

| File | Format | Controls |
|------|--------|----------|
| `aidocs.toml` | TOML | Journal limits, index skip dirs, module hints, JSON size limit, language filtering |
| `aidocs-plugin.json` | JSON | OpenCode directive injection, directive style |
| `action_tokens/*.yaml` | YAML | Add/remove/edit classification languages |

Example — English-only classification for fastest startup:
```toml
# aidocs.toml
[languages]
enabled = "en"
```

## Documentation

- [Install Guide](README_INSTALL.md) — global routing, plugin, hook, and MCP install paths
- [MCP Runtime](mcp/README.md) — runtime model, tool model, host caveats, CLI usage
- [Host Integration](mcp/HOST_INTEGRATION.md) — Claude/OpenCode contract and routing behavior
- [Index Language Descriptors](mcp/INDEX_LANGUAGE_DESCRIPTORS.md) — TOML schema for built-in and project-local indexing descriptors

## Releases

| Version | Highlights |
|---------|------------|
| **1.2.0** | Add `aidocs` CLI (`init`, `status`, `config`, `sync`, `benchmark`, `version`), package/install cleanup, and 255 tests |
| 1.1.0 | Tool consolidation (6 unified entry points), git analysis tools, project_init rewrite (no shell deps), MCP subprocess fix for Windows, 194 tests |
| 1.0.2 | Index hardening (16 languages, monorepo modules, os.walk pruning), `code_investigate` entry tool, CSS compound+HTML tracing, CamelCase search, session journal, `aidocs.toml` config |
| 1.0.1 | Multilingual classifier, lightweight hook path, auto MCP config |
| 1.0.0 | Initial release |

Download from [Releases](https://github.com/cristian1991/AIDOCS/releases).

## License

Apache 2.0. See `LICENSE` and `NOTICE`.
