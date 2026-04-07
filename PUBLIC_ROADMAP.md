# AIDOCS Public Roadmap

Current release: **v2.1.0b**

## Shipped

### v2.1.0b — Orchestration, Security, Dashboard

- Agent-agnostic orchestration (`AgentOrchestrator` with single `check_tool()` entry)
- Heuristic judge (30+ rules, config-driven dangerous patterns)
- Output guard (credential/injection scanning, auto-redaction)
- Circuit breakers (per-server exponential backoff)
- Edit history with diff-based rollback
- SQLite config store (TOML for static definitions only)
- RBAC (users, roles, 15 permissions)
- Code runner (structured bash replacements)
- Tool policies (admin allow/deny glob patterns)
- MCP registry browser with search and install
- Skill scanner (content/supply-chain/vulnerability risk)
- Context compaction with token reset on host compaction
- Deferred tool loading (50 eager, 70+ on demand)
- PostCompact hook for automatic token counter reset
- Conductor MCP tools (start/send/stop/status)
- OpenAI Agents SDK adapter
- Dashboard: monitoring, conductor, skills, MCP registry, settings pages
- 1178 tests

### v2.0.x — Foundation

- Unified AccessGate (6-level security cascade)
- Hard gate enforcement in managed mode
- Dashboard (Tauri desktop app) with token tracking, scoped settings
- Per-file discovery (replaced blanket allow_read)
- Language descriptor files (.ext.toml format)
- Optional `root` param on all MCP tools

---

## v2.2.0 — Install That Actually Works

**Goal: anyone can go from zero to working AIDOCS in under 3 minutes.**

### One-Command Install

- [x] `pip install aidocs-mcp && aidocs setup` — Python users
- [x] `install.sh` / `install.ps1` — bundled Python, zero deps
- [x] Windows: dashboard exe with setup wizard + "Install Python" button
- [x] Auto-detect Claude Code / VS Code / OpenCode / Codex and configure
- [x] `/aidocs` command works on first launch — hooks use `python -m` (no relative paths)
- [x] OpenCode plugin auto-installed to `~/.config/opencode/plugins/`
- [x] CLI auth detection — prompts sign-in if not authenticated
- [x] `aidocs setup` — 6-step interactive wizard
- [ ] `npx aidocs-setup` — single command for Node.js users
- [ ] macOS/Linux: `brew install aidocs`
- [ ] Publish to PyPI (so `pip install` gets v2.1.0b+)

### Install Diagnostics

- [x] `aidocs doctor` — checks Python, MCP, hooks, hosts, project
- [x] Colored terminal output with pass/fail
- [x] VS Code Claude extension detection
- [ ] `aidocs doctor --fix` auto-fix mode

### Docker (deferred to v2.5.0)

- [ ] `docker run` with MCP server
- [ ] Docker Compose with dashboard
---

## v2.3.0 — Onboarding & First-Run Experience

**Goal: a new user understands what AIDOCS does and sees value in their first session.**

### Guided First Run

- [ ] Dashboard first-run wizard: "Welcome → Pick your agent → Initialize project → First session"
- [ ] Interactive tutorial session that walks through: init, session, indexed search, edit, journal
- [ ] Sample project with pre-built .MEMORY/ so users can see what "good" looks like
- [ ] In-dashboard help tooltips on every page (what is a session? what is managed mode?)

### Better Error Messages

- [ ] Every gate block message includes: what happened, why, and what to do instead
- [ ] "Indexed-query prerequisite not satisfied" → "Read this file by searching for it first. Try: `code_find('functionName')` then `code_get_lines`"
- [ ] Link to relevant docs page in every error

### Documentation Overhaul

- [ ] User-facing docs site (not developer READMEs)
- [ ] 5-minute quickstart video
- [ ] "AIDOCS for Claude Code users" guide
- [ ] "AIDOCS for VS Code users" guide
- [ ] Troubleshooting page with every known install/setup issue

---

## v2.4.0 — Real Security & Sandboxing

**Goal: security claims are backed by actual isolation, not regex patterns.**

### Bash Sandboxing

- [ ] Process-level sandboxing for bash tool execution (not string matching)
- [ ] Allowlist/denylist with glob patterns for commands
- [ ] Filesystem namespace isolation — agents can only access project directory
- [ ] Network policy — block outbound connections unless explicitly allowed
- [ ] Timeout enforcement with process kill

### Judge Upgrade

- [ ] LLM-backed intent validation for high-risk operations (optional, uses local model)
- [ ] Semantic pattern matching instead of regex (catches rephrased destructive commands)
- [ ] User confirmation flow for operations above risk threshold

### Audit & Compliance

- [ ] Structured audit log format (JSON lines, exportable)
- [ ] Session replay — see exactly what an agent did, step by step
- [ ] Compliance report generation (SOC2-style: who accessed what, when, what changed)
- [ ] Signed edit history — tamper-evident chain of file modifications

---

## v2.5.0 — Production-Ready Multi-Agent

**Goal: conductor actually works end-to-end with real agents on real projects.**

### Conductor v2 — A2A Protocol Integration

Current conductor is fire-and-forget (`claude -p` / `codex -q`). Once a lane agent
is spawned, the conductor can't communicate with it, pause it, or receive progress.
v2.5.0 replaces subprocess dispatch with A2A (Agent-to-Agent) protocol for real
bidirectional orchestration.

**Why A2A (not MCP):**
- MCP = agent talks to tools (AIDOCS is the tool provider)
- A2A = agent talks to agents (conductor ↔ lane agents)
- A2A has task states (submitted → working → input-required → completed/failed)
- A2A supports streaming progress, cancellation, and mid-task input
- MCP stays as the local instrumentation layer, A2A adds remote agent communication

**Architecture:**
```
AIDOCS Conductor (MCP + A2A client)
  ├── A2A → Lane 1: Claude agent (via claude-a2a wrapper)
  ├── A2A → Lane 2: Codex agent (via codex-a2a)
  ├── A2A → Lane 3: Another conductor (conductor-to-conductor)
  └── MCP → Local tools (indexing, memory, gate, dashboard)
```

**Granular lane control:**
- [ ] A2A task dispatch — conductor creates tasks, agents accept/reject
- [ ] Streaming progress — live updates from agent to conductor to dashboard
- [ ] Input-required state — agent pauses, asks conductor for clarification, conductor responds
- [ ] Cancellation — conductor cancels a running lane task gracefully
- [ ] Pause/resume — conductor suspends a lane, agent checkpoints state, resumes later
- [ ] Process supervision — if agent process dies unexpectedly, conductor detects via heartbeat timeout and restarts with checkpoint
- [ ] Conductor-to-conductor — conductors on different projects can delegate lanes to each other

**Agent crash recovery:**
- [ ] Each lane agent writes periodic checkpoints (last completed step, current state)
- [ ] If process exits unexpectedly (crash, OOM, network drop), conductor detects via A2A timeout
- [ ] Conductor respawns agent with checkpoint context: "You were working on X, completed steps A/B, pick up from C"
- [ ] Max retry count per lane (default 2) before marking lane as failed
- [ ] Dashboard shows crash/restart history per lane

**Lane agent wrappers (A2A servers):**
- [ ] `claude-a2a`: wraps Claude Code session as A2A server — hooks feed progress back, stdin enables input
- [ ] `codex-a2a`: use Liu's existing codex-a2a or build our own wrapper
- [ ] `generic-a2a`: any subprocess that reads stdin/writes stdout, wrapped as A2A task

**Dashboard integration:**
- [ ] Live lane activity — see what each agent is typing/doing in real time
- [ ] Inject messages — operator sends guidance to specific lane agent via dashboard
- [ ] Lane timeline — visual history of task states, crashes, restarts, completions

### Multi-Model Support

- [ ] Tested adapters for: Claude Code, OpenCode, Cursor, Windsurf, Continue.dev
- [ ] OpenAI Agents SDK adapter tested in production
- [ ] Generic MCP adapter documentation and examples
- [ ] Model-specific prompt tuning for gate messages and tool descriptions

### Semantic Search

- [ ] Embedding-based code search (local model, no external API)
- [ ] "Find the authentication flow" works even if code doesn't use those exact words
- [ ] Hybrid search: symbol index + semantic + text search, ranked by relevance
- [ ] RAG for project documentation (not just code)

### Team & Collaboration

- [ ] Multi-user dashboard with user accounts
- [ ] Shared sessions — two developers working on the same project see each other's sessions
- [ ] Session handoff between humans ("I started this, you finish it")
- [ ] Conflict detection when two agents edit the same file

### Cross-Machine Sync

- [ ] `.MEMORY/` sync across machines (git-based or cloud-based)
- [ ] Work on laptop, resume on desktop with full session context
- [ ] Optional encrypted cloud backup of project memory

---

## v3.0.0 — Enterprise & Scale

- SSO/SAML authentication
- Central policy management across org (push tool policies to all projects)
- Token budget enforcement per team/project
- Private MCP registry for internal tools
- On-prem deployment guide for regulated industries
- SLA-backed support tier
- Multi-project conductor (orchestrate across repos)
- Federated memory (shared knowledge across related projects)

---

## Quality Gates

Every release must pass before shipping:

1. **Install test**: fresh Windows + macOS + Linux VM, zero prerequisites, install and `/aidocs` works
2. **External user test**: at least one non-author user completes a real task
3. **Regression suite**: all tests pass, no new warnings
4. **Dashboard test**: every page loads, every button works, no console errors
5. **Security test**: output guard catches test fixtures, judge blocks known-dangerous patterns

---

## Priority Order

The order above is intentional:

1. **Install** (v2.2.0) — nothing matters if people can't install it
2. **Onboarding** (v2.3.0) — nothing matters if people don't understand it
3. **Security** (v2.4.0) — claims must be real before enterprise users arrive
4. **Multi-agent** (v2.5.0) — the differentiator, but only after the foundation is solid
5. **Enterprise** (v3.0.0) — the monetization path, but only after production validation

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute.
