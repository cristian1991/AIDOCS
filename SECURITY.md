# AIDOCS Security Model

This page describes the public-safe security model: the gates that AI coding agents pass through when they run inside an AIDOCS-managed project, what those gates actually enforce, what they do not, and the authority chain that ships the code.

If you are reporting a vulnerability, see "Reporting" at the bottom.

## Threat model in one paragraph

Agents are powerful but not trusted. They can produce destructive shell, leak secrets through tool output, edit the wrong files, escalate from a narrow task to a broad refactor. AIDOCS sits between the agent and the host's raw tools so a bad call surfaces as a refusal with a doctrine line, not as a destroyed working tree or a leaked credential.

## Gate cascade

The runtime enforces a multi-stage cascade. Each stage refuses or asks for confirmation before the next; nothing reaches the host's raw tools without passing every active gate.

### 1. Lifecycle + task gate

Tools that touch shared state (shell execution, edits, broad reads, conductor dispatch) require an active session + task. The gate is fail-closed for shell egress: an unwired or broken lifecycle service refuses the call rather than letting it through. Read-only audit tools fail-open with a structured warning instead, so a routing tool that needs to inspect state can still run during bootstrap.

The active task is the unit of authority — it carries the scope, the owned files, and the audit trail. A refused call records the refusal reason so the agent can correct course rather than retry blindly.

### 2. Taxonomy-based heuristic judge

The judge is a structured rule library, not a regex grab-bag. Every rule emits a typed verdict (class + severity); the judge combines the verdict set into a single decision — allow, ask-for-confirmation, or block.

Rule classes cover destructive shell (`rm -rf`, fork-bomb, recursive `chmod`), credential exfiltration (provider tokens, container/host escape, hypervisor abuse), inline-runtime meta-bypass (`bash <(curl …)`, `eval $(curl …)`, language extraction via deno/lua/php/pwsh/awk/osascript), and protected-path indirection (symlink/mv classifier-aware splits, `cp /dev/null` redirection). Provider-token rules are strict-by-default; legitimate downgrade routes through user-intent grants captured as audited operator messages.

### 3. Output guard

Every tool that returns text passes through the output guard before the agent sees it. The guard fail-closes on a credential pattern hit — the agent gets a sanitized placeholder, not the raw match. Auto-redaction patterns cover the standard provider-token shapes plus injection markers; rule additions go through the same doctrine review as judge rules.

### 4. Shell-egress chokepoint

All shell execution routes through one supervised chokepoint. It:

- Consults the lifecycle gate
- Consults the judge (legitimate destructive intent is an explicit operator grant, not a heuristic guess)
- Runs the call
- Runs the output through the output guard
- Lands an audit record

There is no second path. If a future feature needs a shell, it routes here or it is refused by a static doctrine check.

### 5. Tool policies + RBAC

Admin allow/deny glob patterns scope what tools a user/role can call. The default user has the workflow grants needed to run the standard managed-mode flow; admin-only tools (clearing freezes, force-completing tasks, registry edits) are gated separately.

### 6. PR quarantine for fork PRs

Public pull-request workflow runs base-repo rules against PR code in an isolated sandbox. The trigger is `on: pull_request` (not `pull_request_target`) so base secrets never reach PR-controlled code paths. Tests in the quarantine validate without importing PR sources — PR files are inspected as data only, never as Python imports.

## Known limits

These are honest, public-facing limits — work is in flight, but today the system does not provide:

- **No real network sandbox.** The judge classifies network-shaped commands and refuses or asks-confirm based on patterns, but there is no namespaced network isolation around the shell yet. A determined exfil through DNS / well-known clean URLs is not yet detectable as such; the judge catches the obvious shapes (`curl … | sh`, IWR upload patterns, raw TCP via `nc`).
- **No process-level sandboxing.** Shell runs in the host's normal process space. Filesystem namespace isolation and fork/exec limiting are roadmap items, not shipped today.
- **Heuristic judge, not semantic.** Rule-based detection catches patterns; it does not understand intent the way an LLM would. A rephrased destructive command can slip past if no rule matches its shape. The roadmap includes optional LLM-backed intent validation for high-risk verdicts using a local model.
- **Output-guard regex coverage.** Auto-redaction is pattern-based. Novel credential shapes (e.g. a custom provider that hasn't shipped a public format guide) are not auto-redacted until the pattern is added.
- **Lane tool enforcement on generic MCP hosts.** Hosts that don't route their raw tools through AIDOCS aren't lane-gated for those tools — see [`HOSTS.md`](HOSTS.md).

These are the limits AIDOCS would acknowledge to a security reviewer, not aspirational goals.

## Deploy-gate authority

The runtime in production is whatever a private deploy gate has shipped. The gate — not public CI — is the release authority: it runs static analysis, security scanning, and the full test suite across blocking and report-first lanes, and signs and ships only on success. A failing blocking check aborts before anything is published.

A successful deploy publishes generated, sanitized status artifacts that the README badges read. GitHub Actions here is a peer-audit lane on public contributions, not the authority — by design it cannot reach the private gate, and the private gate cannot reach fork PRs.

See [`QUALITY_AND_RELEASE_TRUTH.md`](QUALITY_AND_RELEASE_TRUTH.md) for what the published signals mean and how to read them.

## Reporting

For a suspected vulnerability, **do not open a public issue**. Email the maintainer through the GitHub profile contact path, or coordinate via the security contact in this repo's `SECURITY` advisory channel if one is configured. We will acknowledge and route appropriately.
