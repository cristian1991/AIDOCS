<p align="center">
  <img src="cn-logo.svg" alt="CodeNexus" width="120">
</p>

<h1 align="center">AIDOCS</h1>

<p align="center">
  <strong>AI Doctrine Orchestrator, Control &amp; Security</strong>
</p>

<p align="center">
  The orchestration and governance layer for AI coding agents — persistent memory, indexed retrieval, conductor + lanes, a dashboard, and a security gate cascade that makes bad agent behavior catchable, not silent.
</p>

<p align="center">
  <!-- Badges read live values from a generated status file, not hand-written claims. -->
  <a href="mcp/.deploy-reports/RELEASE_STATUS.md"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cristian1991/AIDOCS/main/mcp/.deploy-reports/status.json&v=2" alt="tests"></a>
  <img src="https://img.shields.io/github/v/release/cristian1991/AIDOCS?label=version&color=blue" alt="version">
  <img src="https://img.shields.io/badge/license-BSL%201.1-lightgrey" alt="license">
  <img src="https://img.shields.io/badge/python-3.11%2B-yellow" alt="python">
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

## What it does

AIDOCS is an overlay that makes AI coding agents smarter and safer **without replacing them**. It works with Claude Code, Codex, OpenCode, Cursor, and any MCP-compatible host. Six capabilities, each with a runtime path, an enforcement story, and honest limits:

- **Security gates** — a multi-stage cascade (lifecycle/task gate → taxonomy judge → output guard → shell-egress chokepoint → tool policies + RBAC → audit ledger) that surfaces a bad call as a refusal with a doctrine line, not a destroyed tree or a leaked secret.
- **Indexed retrieval** — code search by symbol, reference, meaning, or relationship instead of blind grep. Local embedding-based semantic search, no external API.
- **Persistent memory** — agents resume work instead of rediscovering your repo every session. Per-project `/.MEMORY/` tree, routed bootstrap, typed memory capture with auto-linking.
- **Multi-host** — an overlay on your existing tools, with an honest per-host capability matrix instead of one-size-fits-all claims.
- **Conductor, co-conductor & lanes** — a long-lived conductor (and co-conductor peers) that dispatches tasks to isolated lane agents, each with owned/blocked files, tool grants, and bidirectional comms.
- **Dashboard** — a Tauri desktop app to monitor, control, and configure everything; no terminal required.

See [`FEATURES.md`](FEATURES.md) for the capability-by-capability deep dive.

---

## Why AIDOCS exists

AIDOCS is built for a future in which individuals can explore, learn, build, and **govern** their own AI agents without asking anyone's permission.

The source is visible because the future should be understandable. The license makes that intent legally binding rather than aspirational:

- **Free in production for individuals and non-profits**, today and permanently for each released version.
- **Source-available for everyone** — read it, study it, modify it, redistribute it, use it for development, testing, research, and internal non-production work.
- **Converts to Apache 2.0 on 2030-05-31** (the Change Date). Every version eventually becomes fully open — the openness is scheduled, not promised.
- **A commercial license is required only when a for-profit entity runs AIDOCS in production.** That boundary is what funds the work that keeps AIDOCS alive, secure, and improving — so the free path for individuals and non-profits can stay free.

Visibility is not surrender, and freedom for builders is not a loophole for vendors. See [`LICENSE`](LICENSE) for the binding terms, [`COMMERCIAL.md`](COMMERCIAL.md) for the plain-language commercial boundary, and [`NOTICE`](NOTICE) for authorship.

> Use the tools well. Build things worth building. Leave the castle stronger than you found it.

---

## How it's built

AIDOCS is one law expressed across many surfaces. Every host call — whether a Claude Code hook, an OpenCode plugin call, or a raw MCP `tools/call` — converges on the same gate cascade and the same audit ledger.

```text
Hosts:  Claude Code · OpenCode · Codex · any MCP host
          │   host hooks (PreTool / UserPrompt / Stop / …)   ·   .mcp.json
          ▼
Adapters (thin)  →  single gate entrypoint
          │
          ▼
Gate cascade:  lifecycle/task gate → taxonomy judge →
               output guard → tool policies + RBAC → audit ledger
          │
          ├─→  Capabilities:   memory · index · conductor/lanes · dashboard
          └─→  Shell egress (one chokepoint):  judge → run → output guard → audit
```

Two principles govern the whole system:

- **Evidence may enter the archive; law enters only through the throne.** Untrusted content (web, files, worker output, compaction) becomes evidence, never auto-promoted to memory, doctrine, or config without an audited operator path.
- **A green lie is worse than a red truth.** The badges above read from a generated artifact produced by the last successful deploy run — never a hand-written claim. Reports name what actually ran, what was skipped, and what is still debt.

Read [`SECURITY.md`](SECURITY.md) for the gate cascade and its honest limits, and [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md) for how to read the published quality artifacts.

---

## Status

**Pre-1.0 beta (`v2.3.0bN`).** The foundation — memory, retrieval, conductor, dashboard, the security gate cascade, and a private deploy gate — is shipped and in daily use.

What's deliberately still on the frontier, and labeled as such: physical OS-level sandboxing (today's judge + chokepoint are real but heuristic), a single unified decision engine across inner and outer gates, structured (non-regex) command parsing, and the A2A conductor protocol. The roadmap is a list of frontiers, not a list of claims — see [`PUBLIC_ROADMAP.md`](PUBLIC_ROADMAP.md) for the honest split between what AIDOCS proves today and what it does not prove yet.

---

## Quick start

### Windows

Download `AIDOCS-Setup.exe` from [Releases](https://github.com/cristian1991/AIDOCS/releases). Includes Python, setup wizard, and dashboard.

### Linux/macOS

```bash
curl -fsSL https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.sh | bash
```

### Already have Python 3.11+

```bash
pip install aidocs-mcp        # install the package
aidocs setup                  # configure MCP, hooks, project init
aidocs doctor                 # verify
```

Then open your project in any supported host and type `/aidocs` to activate managed mode.

See [`README_INSTALL.md`](README_INSTALL.md) for the full install matrix and the optional `[nlp]` extras.

---

## Documentation

The public docs are organized by what you're trying to do. The README is the gate sign; this is the castle map.

**Understand the system**
- [`FEATURES.md`](FEATURES.md) — capability-by-capability deep dive of what ships today
- [`PUBLIC_ROADMAP.md`](PUBLIC_ROADMAP.md) — proven-today vs. frontier, by what each item strengthens

**Install & integrate**
- [`README_INSTALL.md`](README_INSTALL.md) — install paths (Python, Windows installer, source) + `[nlp]` extras
- [`HOSTS.md`](HOSTS.md) — truthful per-host capability matrix (Claude Code / OpenCode / Codex / generic MCP)
- [`mcp/HOST_INTEGRATION.md`](mcp/HOST_INTEGRATION.md) — host integration contracts

**Use the capabilities**
- [`MEMORY_AND_INDEXING.md`](MEMORY_AND_INDEXING.md) — `/.MEMORY/` layout, session lifecycle, retrieval surface
- [`CONDUCTOR_AND_LANES.md`](CONDUCTOR_AND_LANES.md) — multi-agent orchestration
- [`DASHBOARD.md`](DASHBOARD.md) — the desktop app
- [`mcp/INDEX_LANGUAGE_DESCRIPTORS.md`](mcp/INDEX_LANGUAGE_DESCRIPTORS.md) — descriptor schema for built-in and project-local indexing

**Trust & truth**
- [`SECURITY.md`](SECURITY.md) — gate cascade, known limits, deploy-gate authority, vulnerability reporting
- [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md) — how to read the badges and what they prove
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to land a PR

**Legal & credits**
- [`LICENSE`](LICENSE) — Business Source License 1.1 (the binding terms)
- [`COMMERCIAL.md`](COMMERCIAL.md) — the plain-language commercial boundary
- [`NOTICE`](NOTICE) — authorship and attribution
- [`ACKNOWLEDGMENTS.md`](ACKNOWLEDGMENTS.md) — **special thanks** to the third-party, vendored, and bundled tools AIDOCS uses

---

## Repository layout

```text
AIDOCS/
├─ mcp/          MCP server runtime (aidocs_mcp), tests, release reports
├─ core/         hooks, command/skill packs, memory templates, doctrine rules
├─ apps/         aidocs-dashboard (Tauri desktop app)
├─ docs/         submission / reviewer-facing documentation
├─ third_party/  vendored dependencies (e.g. mempalace)
└─ *.md          public docs (this README + the tree above)
```

Per-release highlights and live test counts come from the badges and [`mcp/.deploy-reports/RELEASE_STATUS.md`](mcp/.deploy-reports/RELEASE_STATUS.md). Binaries are on [Releases](https://github.com/cristian1991/AIDOCS/releases).

## License

AIDOCS is source-available under the **Business Source License 1.1**, converting to **Apache 2.0 on 2030-05-31**. Free in production for individuals and non-profits; a commercial license is required for for-profit production use. See [`LICENSE`](LICENSE), [`COMMERCIAL.md`](COMMERCIAL.md), and [`NOTICE`](NOTICE).
