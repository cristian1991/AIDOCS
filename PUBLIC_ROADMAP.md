# AIDOCS Public Roadmap

Current release: **v2.0.1**

## Shipped in 2.0.x

- Unified AccessGate with 6-level security cascade
- Hard gate enforcement — agents must use indexed tools in managed mode
- AIDOCS Dashboard (Tauri desktop app) with token tracking, scoped settings, CodeMirror TOML editor
- Per-session token estimation from MCP tool call sizes
- CC auto-memory disabled in managed projects (`memory_capture` is the only path)
- All settings available at Global/Project/Session scopes
- Language descriptor files in `.ext.toml` format
- Optional `root` param on all 85+ MCP tools

## In Progress

### Conductor (SOON)

Lane-aware plan conductor with cross-agent communication and control.

- Parallel lane execution across multiple agents
- Dependency tracking between lanes
- Agent-to-agent message passing
- Operator command injection from dashboard
- Cross-harness communication (Claude Code, OpenCode, Cursor)

### Dashboard Enhancements

- Memory browser — inspect rules, domains, session context from dashboard
- Session create/archive from dashboard
- Live token counter (currently estimates from tool call sizes)
- Managed mode toggle from dashboard

## Planned

### Cross-Agent Communication

- File-based or MCP-based agent wake protocol
- Agent SDK integration for programmatic agent management
- Dashboard as orchestrator — spawn, pause, resume agents

### Stronger Collaboration Continuity

- Cross-session linking and context carry-forward
- Structured handoff improvements
- Freshness and trust signals for old summaries

### Benchmark Maturity

- Realistic vague-prompt benchmark sets
- Code + schema + workflow-heavy scenarios
- Public benchmark contracts

### Host Parity

- OpenCode: full per-prompt MCP classification (currently plugin-local)
- Cursor: beyond startup-only packaging
- GitHub Copilot CLI: initial integration

## Community Contributions Welcome

- Benchmark prompt sets and sample repos
- Language descriptor contributions (new `.ext.toml` files)
- Dashboard UI feedback and feature requests
- Host adapter testing (OpenCode, Cursor)

See [Issues](https://github.com/cristian1991/AIDOCS/issues) for specific tasks.
