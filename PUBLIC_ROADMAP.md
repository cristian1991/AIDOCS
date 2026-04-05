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

AIDOCS is built to be extended. Here's where community contributions make the biggest impact:

### Language Descriptors

AIDOCS indexes code using TOML descriptor files (`mcp/server/aidocs_mcp/index_languages/*.toml`). Each descriptor teaches AIDOCS how to parse and understand a language — outline patterns, component semantics, file roles, and module hints.

Currently shipped: Python, TypeScript, JavaScript, JSX, TSX, Rust, Go, Java, C#, Ruby, Kotlin, PHP, Swift, Dart, Elixir, Lua, SQL, CSS, SCSS, LESS, Sass, HTML, Vue, Svelte, TOML, YAML, JSON, Prisma, Shell, PowerShell.

**Wanted:**
- Zig, Nim, OCaml, Haskell, Scala, Clojure, F#, R, Julia, Perl
- Terraform/HCL, Dockerfile, Makefile, CMake
- GraphQL, Protobuf, Thrift
- Markdown (structural — headings, links, code blocks)
- Improvements to existing descriptors (better outline patterns, component semantics)

Contributing a descriptor is one TOML file — see `mcp/INDEX_LANGUAGE_DESCRIPTORS.md` for the schema.

### Host / Harness Integrations

AIDOCS currently supports Claude Code (hooks) and OpenCode (plugin). We want more:

**Wanted:**
- **Cursor** — deeper integration beyond startup-only packaging
- **GitHub Copilot CLI** — initial hook/plugin path
- **Windsurf / Codeium** — MCP-based integration
- **Continue.dev** — MCP or plugin integration
- **Aider** — hook or wrapper integration
- **Custom MCP clients** — any client that speaks MCP stdio

Each integration needs: startup routing, managed-mode awareness, and tool guardrails.

### Action Tokens (Intent Classification)

AIDOCS classifies user prompts into action kinds (edit, understand, trace, etc.) using language-specific token files (`action_tokens/*.toml`).

Currently shipped: English.

**Wanted:**
- Spanish, French, German, Portuguese, Italian, Romanian, Dutch
- Japanese, Chinese, Korean
- Hindi, Arabic, Turkish
- Any language where developers work

Contributing is one TOML file per language with translated intent phrases.

### Benchmarks

**Wanted:**
- Realistic prompt sets that test deep retrieval vs naive grep
- Multi-language project samples (public-safe)
- Workflow-heavy scenarios (session handoff, plan execution)
- Adversarial prompts that expose weak spots

### Dashboard

**Wanted:**
- UI/UX feedback and bug reports
- Feature requests for the operator dashboard
- Accessibility improvements
- Theme customization ideas

See [Issues](https://github.com/cristian1991/AIDOCS/issues) for specific tasks or open a discussion.
