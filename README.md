<p align="center">
  <img src="docs/assets/cn-logo.svg" alt="CodeNexus" width="120">
</p>

<h1 align="center">AIDOCS</h1>

<p align="center">
  <strong>Portable AI coding-agent toolkit</strong> with routed memory, session-based runtime, and optional MCP enforcement.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.1.0-blue" alt="version">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="license">
  <img src="https://img.shields.io/badge/python-3.11%2B-yellow" alt="python">
  <img src="https://img.shields.io/badge/tests-194%20passing-brightgreen" alt="tests">
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

AIDOCS gives AI coding agents **persistent memory, session management, and indexed code retrieval** across conversations.

**Without AIDOCS:** Each conversation starts fresh. The agent greps blindly, forgets what it learned, and has no context about your project structure.

**With AIDOCS:** The agent resumes from where it left off, uses indexed tools instead of grep, and follows session-guided workflows.

### Key Capabilities

- **Routed memory** — `index.aidocs` -> `INDEX.md` -> `SESSION.md` — agents follow a routing chain, not a flat file dump
- **Session isolation** — parallel workstreams with their own plans, context, and artifacts
- **6 unified MCP entry points** — `code_investigate`, `code_find` (21 modes), `code_trace` (8 modes), `code_bundle` (13 modes), `schema_query` (5 modes) + specialists
- **`code_investigate`** — "start here" tool: probes symbols/files/schema/CSS/modules, returns what was found + what to call next
- **Git analysis** — `git_fork_status`, `git_merge_plan`, `git_conflict_analysis`, `git_upstream_changes` for fork/merge workflows
- **Session journal** — rolling log of significant decisions, auto-evicts to archive when full
- **Multilingual classification** — English, Italian, Spanish, Japanese, Portuguese, German out of the box
- **Monorepo detection** — npm/pnpm/Cargo workspaces, .NET projects, informal module boundaries
- **16 language indexing** — Python, JS/TS, C#, Rust, Go, Java, Kotlin, Ruby, PHP, SQL, HTML, CSS/SCSS, Vue, Svelte, Prisma, and more
- **Configurable** — `aidocs.toml` for MCP, `aidocs-plugin.json` for OpenCode, `action_tokens/*.yaml` for languages

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
# OpenCode: installer configures opencode.jsonc automatically

# In any project:
/aidocs    # Uses MCP-backed session/memory/retrieval
```

## Project Layout

```
aidocs.toml                # Configuration (journal, index, languages, agent)
aidocs-plugin.json         # OpenCode plugin config
action_tokens/             # Prompt classification language files
  en.yaml, it.yaml, ...

core/                      # Standalone markdown-first system (zero dependencies)
  .commands/                # Global commands (/aidocs, /reingest, /archive, etc.)
  plugins/                  # OpenCode global plugin
  scripts/                  # Cross-platform install scripts

mcp/                        # Optional MCP runtime layer
  server/aidocs_mcp/        # Python MCP server (21 service modules)
  tests/                    # 194 tests (private repo only)
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

## Configuration

Edit these files to customize behavior (changes take effect on restart):

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

- [MCP Tools & Architecture](mcp/README.md) — Full tool list, install, runtime model
- [Install Guide](README_INSTALL.md) — Detailed installation steps
- [Roadmap](mcp/ROADMAP.md) — v1.2.0 plan (PyPI packaging, CLI tool, benchmarks)

## Releases

| Version | Highlights |
|---------|------------|
| **1.1.0** | Tool consolidation (6 unified entry points), git analysis tools, project_init rewrite (no shell deps), MCP subprocess fix for Windows, 194 tests |
| 1.0.2 | Index hardening (16 languages, monorepo modules, os.walk pruning), `code_investigate` entry tool, CSS compound+HTML tracing, CamelCase search, session journal, `aidocs.toml` config |
| 1.0.1 | Multilingual classifier, lightweight hook path, auto MCP config |
| 1.0.0 | Initial release |

Download from [Releases](https://github.com/cristian1991/AIDOCS/releases).

## License

Apache 2.0. See `LICENSE` and `NOTICE`.
