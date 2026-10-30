# AIDOCS Public Roadmap

Current release and headline test counts live in [`mcp/.deploy-reports/RELEASE_STATUS.md`](mcp/.deploy-reports/RELEASE_STATUS.md) and the badges at the top of [`README.md`](README.md). This page is the honest split between **what AIDOCS proves today** and **what it does not prove yet**. Where the runtime can only do part of a thing, the item says so — a roadmap is a list of frontiers, not a list of claims.

> **Status today:** pre-1.0 beta line (`v2.3.0bN`). The foundation — memory, retrieval, conductor, dashboard, the security gate cascade, and a private deploy gate — is shipped and in daily use. The frontier below is what stands between the current beta and a `1.0` public launch.

---

## Shipped

What's described here is wired in the current beta and exercised by the deploy gate. Capability detail lives in [`FEATURES.md`](FEATURES.md); how to read the release signals lives in [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md).

### Foundation (capabilities in the current beta)

- **Persistent memory** — per-project `/.MEMORY/` tree, routed bootstrap, typed memory capture with auto-linking, journal auto-eviction to archive; a **palace-backed memory home** (an embedding palace layered over a canonical store, rebuildable from it, anchored to the code index at the smallest leaf) with the markdown tree demoted to a regenerated fallback projection
- **Indexed retrieval** — symbol / reference / text search with **scored symbol ranking**, **local embedding-based semantic search** (no external API), trace + bundle tools, schema-query, **evidence-lane aggregation** on the investigate surface, and **discovery continuity** (a session remembers what it has surfaced); raw read/grep gated in managed mode so agents read with intent
- **Conductor + lanes** — long-lived conductor, per-lane owned/blocked files + tool grants + task state, inline or parallel mode, and **bidirectional lane comms** (lane agents ask questions, the conductor injects guidance, scope conflicts route through grant requests)
- **Security gate cascade** — lifecycle/task gate, taxonomy-based heuristic judge (structured rule verdicts), output guard with auto-redaction, a single supervised shell-egress chokepoint with **governed bash** (ask-by-default, dev-toolchain command family), tool policies + RBAC; every security-gate code path is complexity-pinned at rank C or better
- **Dashboard** — ONE frontend, two shells: the Tauri desktop app and the gate-served web dashboard ship the same React source behind a single data-adapter seam. Monitoring, conductor chat + lane control, scoped settings, skills + MCP registry, setup wizard, urgency-tiered backlog/todos, and a **memory knowledge-graph page** (explore-mode graph that grows as you click, full memory bodies on select). Every build is stamped with its commit SHA in the footer, and a drift guard warns when the served web bundle falls behind the frontend source
- **Host adapters** — Claude Code (hooks + MCP), OpenCode (plugin + MCP + serve), Codex (MCP + CLI conductor), any MCP host (`.mcp.json`), with an honest per-host parity matrix in [`HOSTS.md`](HOSTS.md)
- **CLI** — `aidocs setup / doctor / init / status / sync / benchmark / version`
- **Session grounding & feedback** — a session grounding ledger (the tool surface remembers its own conversation), live run progress on long jobs (elapsed / status / bounded tail), a `tool_report` feedback channel, and epoch-deduped backlog surfacing at prompt / edit rail / stop
- **WebMCP outer gate (cloud)** — an OAuth-authenticated remote MCP surface that runs the **same** gate cascade server-side, with **per-org multi-tenant execution isolation** (separate project registry, config, and selection per org), `org_select` tenant binding, and identity + entitlement resolved against CodeNexus accounts. Each org connects its **own GitHub credential** for private-repo import — custody stays on the identity service, and the gate fetches a just-in-time token at clone time and never persists it, so one org's credential is unreachable to another. Tenant binding is **transactional and parity-checked**: per-request config-context isolation, every execution surface (MCP + invoke) under the same selected-project law, and no silent default-writes for users who belong to multiple orgs. Within an org, **intra-org project allowlists** scope visibility further: an org OWNER/ADMIN sees and binds every project, while a member sees and binds only the projects explicitly granted to them — enforced uniformly across project listing/selection, sessions, dashboard snapshot, and exec-root resolution (same authority on the WebMCP wire as in the dashboard). Shipped through the same signed governed deploy

### Release-pipeline & supply-chain integrity (current beta)

Releases ship through a private gate that proves **the exact bytes it tested are the exact bytes it serves** — the runtime is isolated from what it serves, and every release is re-verified before promotion, with rollback. Test selection is honest: missing evidence is labeled, never counted as a pass.

- How the published signals work: [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md)

### Release history

- **v2.3.0b6** — Dashboard truth + memory throne room: **one frontend for both shells** with commit-SHA-stamped builds and a web-bundle drift guard (public-mirror-guard class); the dashboard's python process storm replaced by a **single persistent snapshot worker** (zero recurring spawns at idle); a **memory knowledge-graph page in both builds** (progressive-disclosure graph, full memory bodies, governed writes only); **urgency tiers** for backlog/todo (`critical/urgent/high/normal/low` with migration + operator markers); in-process **network-egress governance** (allowlist-checked chokepoint for runtime-initiated calls); and a governed remote-deploy tool (`ai_deploy`) whose authority is super-admin-only, allowlisted, and audited
- **v2.3.0b5** — WebMCP **per-org GitHub credentials + private-repo import** (custody kept on the identity service; the gate fetches a JIT token at clone time and never persists it) and **tenancy-parity hardening** before write-through: transactional per-request tenant-config reset, `/v1/invoke` brought under the same selected-project law as `tools/call`, and no silent default-writes for multi-org users
- **v2.3.0b3** — WebMCP outer gate: OAuth-authenticated remote MCP with **per-org multi-tenant execution isolation**, org-based identity/entitlement (CodeNexus), `org_select` tenant binding, and the desktop dashboard's cloud (WebMCP) scope
- **v2.3.0b2** — broader destructive/exfil coverage in the judge, stricter provider-token handling, a single supervised shell chokepoint, smarter release test selection
- **v2.3.0b1** — enforcement parity across all hosts, full raw-tool gating and lane tool enforcement, OpenCode plugin rewrite
- **v2.2.0b** — one-command install (`pip` / `install.sh` / `install.ps1`), `aidocs setup` wizard, host auto-detect + configure, `aidocs doctor`
- **v2.1.0b** — agent-agnostic orchestration, heuristic judge, output guard, edit history with rollback, RBAC, MCP registry browser, skill scanner, conductor tools, dashboard pages
- **v2.0.x** — unified gate cascade, managed-mode enforcement, Tauri dashboard, per-file discovery, language descriptors

---

## Frontier

The frontier is organized by what each item *strengthens*, not by version number — the first public release will fold many of these in together. Items are honest about the current floor: where today's enforcement is heuristic or partial, the item names that.

### Install & onboarding reach

**Goal: zero-to-working in minutes, on any OS, with a first session that teaches.**

- [x] `pip install` + `aidocs setup` + `install.sh` / `install.ps1` (shipped)
- [x] `aidocs doctor` (Python / MCP / hooks / hosts / project)
- [ ] Publish `aidocs-mcp` to PyPI so `pip install` resolves the latest beta
- [ ] `npx aidocs-setup` (Node users) · `brew install aidocs` (macOS/Linux)
- [ ] `aidocs doctor --fix` auto-remediation; `doctor` extended to verify sandbox + runtime-trust posture
- [ ] Dashboard first-run wizard + interactive tutorial session (init → search → edit → journal)
- [ ] Sample project with a pre-built `/.MEMORY/` so users see what "good" looks like
- [ ] Every gate refusal names what happened, why, and the exact tool/command to use instead
- [ ] User-facing docs site (not developer READMEs) + per-host quickstarts + troubleshooting

### Physical enforcement & real sandboxing

**Goal: security posture backed by OS-level isolation, not pattern matching. The current judge + chokepoint are real but heuristic; isolation must become physical before untrusted/public dynamic execution is allowed.**

- [ ] Process/OS-level sandbox for tool execution — evaluate **Landlock** (Linux baseline), a **rootless container** backend, and **gVisor** for cloud/VPS isolation profiles
- [ ] `sandbox status` + runtime probes that *verify* claims — refuse to label `no_network` / `loopback_only` / "sandboxed" unless physically enforced and probed (outbound blocked, metadata blocked, no Docker socket / host home / SSH-agent / credentials present)
- [ ] Filesystem-namespace + network-policy isolation: agents reach only the project directory; outbound blocked unless explicitly allowed
- [ ] Record sandbox config + image digest + probe evidence; named fail-closed reason on any sandbox failure
- [ ] Public/untrusted dynamic execution refused without real containment

### Judge & policy upgrade

**Goal: one decision engine, structured parsing, and grammar coverage beyond regex.**

- [ ] Single canonical decision engine shared by inner gate (hooks/plugin) and outer gate (MCP/web) — no second judge that can drift
- [ ] Structured parsing (e.g. tree-sitter) to complement regex — catch rephrased/obfuscated destructive commands and script indirection
- [ ] Multi-shell grammar completeness (PowerShell-native + CMD-native surfaces alongside POSIX)
- [ ] Optional local-model intent validation for high-risk operations (no external API)
- [ ] Cross-call exfiltration awareness (beyond single-call judgment) + complete network-egress allowlist segmentation
- [ ] Evaluate a dedicated policy engine (OPA / Cedar) as a future authoritative layer — a pilot, not the authority, until proven

### Audit, failure stewardship & memory trust

**Goal: nothing fails silently, nothing is orphaned, and untrusted content never becomes law.**

- [ ] Structured, durable failure-stewardship ledger — every failure carries a signature, causal origin, and current duty; orphaned "not my bug" reports rejected
- [ ] Structured audit log (JSON lines, exportable) + session replay (step-by-step agent actions)
- [ ] Signed / tamper-evident edit history
- [ ] Memory-promotion gate: source-classify every input (operator / trusted / agent / file / web / untrusted); only operator/trusted paths promote to doctrine, with review + rollback + audit

### Performance & cold-start

**Goal: faster first paint without weakening any law.**

- [ ] Reduce repeated fresh-process startup cost on the hook path
- [ ] Persistent / reused MCP server instead of per-fallback rebuilds; cheaper cold boot
- [ ] Cached managed-mode resolution (avoid repeated SQLite/config cascades) with freshness semantics
- [ ] Operator-visible dashboard open/refresh timings; incremental index refresh where safe

### Rust core

**Goal: migrate the enforcement kernel and core outward to Rust behind stable seam contracts — deterministic cleanup (no GC pauses, no half-loaded-runtime states), real parallelism, and a signed, uneditable enforcement kernel.** Grown from the existing Rust foothold, not a big-bang rewrite of the Python surface; each unit becomes a clean input→output seam a Rust implementation can slot behind without touching its callers.

- [ ] Rust enforcement kernel + identity/token stamping as the first signed, run-not-edit citizens
- [ ] Driver-parametrized contract tests as the port's acceptance suite (the Python driver proves the contract today; a Rust driver lights up as modules land)
- [ ] Dependency tiering (Rust-native peer / sidecar / rebuildable-backed) named before porting each seam

### Conductor v2 — A2A protocol

**Goal: real bidirectional orchestration with task states, cancellation, and crash recovery.** Today the conductor dispatches and exchanges structured messages with lanes; v2 replaces subprocess dispatch with the A2A (Agent-to-Agent) protocol so the conductor can drive task lifecycle, stream progress, and recover crashed lanes.

- [ ] A2A task states (submitted → working → input-required → completed/failed) + streaming progress
- [ ] Graceful cancellation, pause/resume with checkpoint
- [ ] Heartbeat-based crash detection + lane restart with checkpoint context (not amnesia); bounded retries
- [ ] Conductor-to-conductor delegation with identity + scope boundaries
- [ ] Lane wrappers (`claude-a2a`, `codex-a2a`, generic stdin/stdout) + dashboard lane timeline with live activity and operator message injection
- [ ] Tested adapters across Claude Code, OpenCode, Cursor, Windsurf, Continue.dev, OpenAI Agents SDK

### Web, multi-user & cross-machine

**Goal: remote and multi-user without thinning the law.**

- [x] **Remote control plane** — the WebMCP outer gate: OAuth-authenticated remote MCP that shares the canonical enforcement path (no fake local confirmations for remote clients), so a hosted client is gated by the same cascade as a local one
- [x] **Per-tenant (per-org) isolation** — separate project registry, config, and selection per org; cross-tenant access refused, proven by live e2e + isolation tests
- [ ] In-browser web dashboard served from the gate (the desktop dashboard already consumes the gate's WebMCP scope)
- [ ] Dashboard v2 (the "mothership") — a unified local-vs-web presentation, process-lifecycle fixes, and OAuth-flow hardening, on the way to a Rust-backed shell
- [ ] **AIDOCS Remote** — remote control of a *local* AIDOCS instance where execution stays local and the cloud gate is a relay only (distinct from cloud execution); currently in design phase
- [ ] Multi-user dashboard with explicit session ownership + shared sessions + human handoff
- [ ] Doctrine and granted skills that follow your identity across machines — laws sync to your account, local files demote to fallback
- [ ] Conflict detection when two agents edit the same file
- [ ] `/.MEMORY/` sync across machines (git- or cloud-based), encrypted or clearly local-only; optional scoped cloud backup
- [ ] Public web/API surfaces: rate limits + abuse detection

### Supply-chain provenance (beyond the gate)

**Goal: the artifacts users install are as accountable as the bytes the gate serves.**

- [x] Tested-artifact integrity: the bytes tested are the bytes served, re-verified before promotion (shipped — see above)
- [x] CVE classifier distinguishes clean / no-fix-warn / fix-available-block; scanner failure is never treated as clean
- [ ] SBOM / provenance for public release artifacts; installer artifacts signed or hash-verified
- [ ] Sandbox/model images pinned by digest (no `latest`); documented update channel

### Enterprise & scale

**Goal: same law, managed operation.**

- [ ] SSO/SAML, team/project RBAC, multi-user dashboard
- [ ] Central policy management (push tool policies org-wide) + policy versioning/migration
- [ ] Token budgets per user/team/project; private MCP registry; org host/model catalog
- [ ] On-prem deployment + regulated-industry guide; incident-response / freeze controls; SLA-backed support
- [ ] Multi-project conductor + federated memory across related projects

---

## Quality gates

Every release must pass the private deploy gate before shipping. A green deploy means every blocking check passed and the published signals were regenerated; a failing check aborts before the release ships. See [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md) for what the published signals mean and how to read them. Before a public launch, additionally:

1. **Install test** — fresh Windows + macOS + Linux, zero prerequisites, install and `/aidocs` works
2. **External user test** — at least one non-author completes a real task
3. **Regression suite** — all tests pass, no new warnings
4. **Dashboard test** — every page loads, every control works or is truthfully disabled
5. **Security test** — output guard catches fixtures, judge blocks known-dangerous patterns, and no public doc/artifact leaks private internals

---

## How to read this roadmap

- A checked box is wired and exercised today.
- An unchecked box is a frontier, not a claim — where the current floor is heuristic or partial, the item says so.
- The order is roughly: **reach** (install/onboarding) → **real enforcement** (sandbox/judge) → **accountability** (audit/failure/memory) → **orchestration** (conductor v2) → **scale** (web/multi-user/enterprise). Foundation first; the differentiators only after the floor is physical.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to contribute and [`SECURITY.md`](SECURITY.md) for the current security model and its honest limits.
