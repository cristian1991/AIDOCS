<p align="center">
  <img src="docs/assets/cn-logo.svg" alt="CodeNexus" width="120">
</p>

<h1 align="center">AIDOCS</h1>

<p align="center">
  <strong>The orchestration layer for AI coding agents — persistent memory, conductor, dashboard, and multi-agent workflows.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/github/v/release/cristian1991/AIDOCS?label=version&color=blue" alt="version">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="license">
  <img src="https://img.shields.io/badge/python-3.11%2B-yellow" alt="python">
  <img src="https://img.shields.io/badge/tests-1363%20passing-brightgreen" alt="tests">
</p>

<p align="center">
  A <a href="https://codenexus.cloud">CodeNexus</a> project
</p>

<h3 align="center">Supported Agents</h3>

<p align="center">
<table align="center">
<tr>
<td align="center" valign="top" width="200">
  <img src="https://img.shields.io/badge/Claude_Code-CC5500?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude Code"><br>
  <sub>Hooks + MCP + Dashboard</sub><br>
  <sub><strong>Full orchestration</strong></sub>
</td>
<td align="center" valign="top" width="200">
  <img src="https://img.shields.io/badge/OpenCode-000000?style=for-the-badge" alt="OpenCode"><br>
  <sub>Plugin + MCP + Serve mode</sub><br>
  <sub><strong>Full orchestration</strong></sub>
</td>
<td align="center" valign="top" width="200">
  <img src="https://img.shields.io/badge/Codex-10A37F?style=for-the-badge&logo=openai&logoColor=white" alt="Codex"><br>
  <sub>MCP + CLI conductor</sub><br>
  <sub><strong>Full orchestration</strong></sub>
</td>
<td align="center" valign="top" width="200">
  <img src="https://img.shields.io/badge/Any_MCP_Host-4A90D9?style=for-the-badge" alt="MCP"><br>
  <sub>Via <code>.mcp.json</code></sub><br>
  <sub>Tools + memory</sub>
</td>
</tr>
</table>
</p>

---

## What It Does

AIDOCS is the orchestration layer that makes AI coding agents smarter without replacing them. It works with Claude Code, Codex, OpenCode, Cursor, and any MCP-compatible host.

- **Persistent memory** — agents resume work instead of rediscovering your repo every time
- **Conductor** — long-lived orchestrator that dispatches tasks, manages lane agents, resolves conflicts
- **Dashboard** — desktop app to monitor, control, and configure everything (no terminal needed)
- **Indexed retrieval** — code search by symbol, meaning, or relationship instead of blind grep
- **Security gates** — heuristic judge, output guard, tool policies, RBAC
- **Multi-agent routing** — assign the right model to the right task (Claude for code, GPT for refactoring, Gemini for docs)
- **Zero migration** — works as an overlay on your existing tools, no framework lock-in

### Product Surfaces

- **MCP Runtime** — 120+ tools: indexing, retrieval, orchestration, conductor, security gates
- **Dashboard** — Tauri desktop app: conductor chat, monitoring, settings, task routing, setup wizard
- **CLI** — `aidocs setup`, `doctor`, `init`, `status`, `sync`, `benchmark`
- **Host Adapters** — Claude Code hooks, OpenCode plugin, Codex CLI, generic MCP

### Key Capabilities

- **Conductor** — long-lived agent that manages tasks, dispatches lane agents, routes models per task type
- **Dashboard control** — start/stop/pause agents, send tasks, see live output, no terminal needed
- **Persistent memory** — routed session startup, journal, plans, handoffs across conversations
- **Semantic search** — embedding-based code search (local model, no API) + symbol/text search
- **Security gates** — heuristic judge, output guard, tool policies, RBAC, lane isolation
- **Task routing** — assign Claude for implementation, GPT for refactoring, Gemini for docs
- **Conductor comms** — agents ask conductor questions, auto-resolve scope conflicts, pause/resume lanes
- **Edit history** — diff-based rollback with agent/lane attribution

## Quick Start

### Windows

Download `AIDOCS-Setup.exe` from [Releases](https://github.com/cristian1991/AIDOCS/releases). Includes Python, setup wizard, and dashboard. Double-click and go.

### Linux/macOS

One command, installs Python + AIDOCS:
```bash
curl -fsSL https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.sh | bash
```

### Already have Python 3.11+?

```bash
pip install aidocs-mcp        # install the package
aidocs setup                  # configures everything: MCP, hooks, project init
aidocs doctor                 # verify the install
```

### Then

Open your project in your IDE and type `/aidocs`. That's it.
Open your project in any supported IDE and type `/aidocs` to activate managed mode.

## Project Layout

```
aidocs.toml                  # Runtime configuration (static definitions)
action_tokens/               # Prompt-classification language files (en, it, ...)

core/
  .commands/                  # Slash commands (/aidocs, /reingest, /archive, etc.)
  hooks/                      # Hook definitions (hooks.json)
  plugins/                    # OpenCode plugin (aidocs.js)
  scripts/                    # Install scripts (install.sh, install.ps1, setup.sh/ps1)

mcp/
  server/aidocs_mcp/          # Python MCP server, CLI, and all runtime services
  tests/                      # 1287 tests

apps/aidocs-dashboard/
  src/                        # React frontend (TypeScript)
  src-tauri/                  # Rust backend (Tauri)
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

| Command | Description |
|---------|-------------|
| `aidocs setup [path]` | Auto-configure MCP + hooks + project (run this first) |
| `aidocs doctor` | Diagnose installation issues |
| `aidocs init [path]` | Initialize AIDOCS structure in a project |
| `aidocs status [path]` | Show project/runtime/index status |
| `aidocs sync [path]` | Refresh memory/code/schema indexes |
| `aidocs benchmark [path]` | Run public benchmark scenarios |
| `aidocs version` | Show package version |

## Configuration

| File | Purpose |
|------|---------|
| `aidocs.toml` | Static definitions — journal limits, index dirs, module hints, gate settings |
| SQLite config store | Runtime settings — editable from dashboard or CLI, scoped (global/project/session) |
| `action_tokens/*.toml` | Prompt-classification keywords per language |
| Dashboard Settings page | Visual editor for all runtime settings |


## Advanced Features

Beyond the quick-start flow, AIDOCS ships:

- **Conductor modes** — inline (single agent) or parallel (multi-agent with lane isolation)
- **Conductor comms** — agents ask questions, conductor auto-resolves scope conflicts, operators inject guidance via dashboard
- **Task routing** — per-task-type model assignment (configurable from dashboard)
- **Session handoffs** — resume bundles for continuing work across conversations
- **Plan execution** — deterministic plan creation, dispatch, verification, completion tracking
- **Semantic search** — embedding-based code search with local model (optional: `pip install sentence-transformers`)
- **Skills** — bundled behavior packs (`deep-retrieval`, `test-driven-validation`, `systematic-debugging`, `brainstorming`)
- **Deferred tool loading** — 50 eager tools, 70+ discovered on demand via keywords
- **Extensible indexing** — custom language descriptors for any file format
- **Related-project search** — cross-repo comparison when configured

## Host Support

| Host | Integration | Control Level |
|------|------------|---------------|
| **Claude Code** | Hooks (PreToolUse, PostCompact, etc.) + MCP | Full — intercept, block, inject context |
| **OpenCode** | Plugin (aidocs.js) + MCP + serve mode | Full — lane control, message injection |
| **Codex** | MCP + CLI conductor | Full — task dispatch, model selection |
| **Cursor / Windsurf** | MCP only | Tools + memory (no hook interception) |

## Documentation

- [Developer README](README_DEV.md) — deeper architecture, settings, host boundaries, and flaw-finding guide
- [Install Guide](README_INSTALL.md) — global routing, plugin, hook, MCP, and dashboard install paths
- [MCP Runtime](mcp/README.md) — runtime model, tool model, host caveats, CLI usage
- [Host Integration](mcp/HOST_INTEGRATION.md) — Claude/OpenCode contract and routing behavior
- [Index Language Descriptors](mcp/INDEX_LANGUAGE_DESCRIPTORS.md) — TOML schema for built-in and project-local indexing descriptors
- [Public Roadmap](PUBLIC_ROADMAP.md) — what's coming in future versions
- [Contributing](CONTRIBUTING.md) — how to contribute language descriptors, host integrations, and more

## Releases

| Version | Highlights |
|---------|------------|
| **2.2.0b** | Setup wizard (CLI + dashboard + bundled Python), `aidocs setup` + `aidocs doctor`, conductor long-lived interactive mode with context injection, conductor comms (SQLite message queue, auto-scope, lane control), live output streaming, task-type routing with model selection, semantic search, OpenCode parity (plugin + conductor backend), install scripts for all platforms, 1287 tests |
| **2.1.0b** | Agent-agnostic orchestration, heuristic judge, output guard, metrics, circuit breakers, edit history/rollback, SQLite config store, RBAC, tool policies, MCP registry, skill scanner, context compaction, deferred tool loading, PostCompact hook, conductor MCP tools, dashboard monitoring/conductor/skills/MCP pages |
| **2.0.x** | Dashboard overhaul, unified AccessGate (6-level cascade), hard gate enforcement, per-file discovery, slimmed responses, TOML externalization |
| **1.x** | CLI, tool consolidation, index hardening (16 languages), multilingual classifier, monorepo modules |

Download from [Releases](https://github.com/cristian1991/AIDOCS/releases).

## License

Apache 2.0. See `LICENSE` and `NOTICE`.
