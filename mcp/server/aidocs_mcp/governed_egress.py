"""Governed in-process egress — the chokepoint §6 was missing (backlog #195).

§6 swears: "every shell path routes through ShellEgressService; no second shell path." But IN-PROCESS
Python network (urllib/requests/httpx) was an ungated SECOND egress lane — agent/operator-reachable
code (a workflow `webhook("url")` verify) could urlopen ANY host with no allowlist and no audit.
This module is that missing chokepoint: assert_egress_allowed(url) FAILS CLOSED on a non-allowlisted
host and audits EVERY decision (allow + refuse). It carries no secret — only host/purpose/decision.

The trusted-infra in-process callers (LLM provider, MCP registry, github auth, runtime bootstrap)
reach FIXED hosts and are recorded in IN_PROCESS_EGRESS_FINGERPRINTS with their host-class + rationale.
The inventory-guard test asserts every in-process network call-site is EITHER fingerprinted here OR
routed through assert_egress_allowed — so a NEW ungated site can never appear unnoticed (§0).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from urllib.parse import urlparse


class EgressRefused(Exception):
    """Fail-closed refusal: an in-process network call to a non-allowlisted host."""


# Registered legitimate in-process egress call-sites (the "fingerprint" registry). Each entry: the
# purpose, the host-class it reaches, and why it is trusted. Keyed by the source file basename. The
# inventory guard (test_in_process_egress_inventory) asserts every in-process network call-site is
# either here, governed, or a known non-egress false positive.
IN_PROCESS_EGRESS_FINGERPRINTS: dict[str, dict[str, str]] = {
    "aidocs_service.py": {
        "purpose": "release_update_check",
        "host_class": "fixed release channel (GitHub releases API of the public mirror; AIDOCS_UPDATE_CHANNEL_URL operator override)",
        "rationale": "check-only version comparison against the release channel (Empire directive 2026-07-06); never fetches artifacts or installs — installs stay on signed/verified operator paths (aidocs-doctrine §XXIV); fail-soft on any network error",
    },
    "backend_models.py": {
        "purpose": "llm_provider_api",
        "host_class": "operator-configured LLM provider endpoints",
        "rationale": "model inference to provider hosts the operator configured (noqa S310 trusted hosts)",
    },
    "mcp_registry.py": {
        "purpose": "mcp_registry_fetch",
        "host_class": "the configured MCP registry host",
        "rationale": "fetch MCP server definitions from the configured registry endpoint",
    },
    "backlog_hub_client.py": {
        "purpose": "backlog_authoritative_sync",
        "host_class": "fixed codenexus internal host from operator env (AIDOCS_CODENEXUS_INTERNAL_URL)",
        "rationale": "server-authoritative backlog (2026-07-21 ruling): GET the org/project backlog and POST queued write intents over the SAME authenticated internal S2S path as the github credential — fixed operator-configured host, bearer secret, no agent-supplied URL (noqa S310 fixed host). BOUND projects only; unbound/local-only projects never make the call. Fail-soft: any network error leaves local behaviour byte-identical",
    },
    "cli.py": {
        "purpose": "oauth_gate_token_verify",
        "host_class": "fixed codenexus gate host from operator env (AIDOCS gate URL, --gate-url; default https://mcp.codenexus.cloud)",
        "rationale": "dashboard-login-oauth verifies that a CodeNexus-attested bearer is LIVE at the gate (a real authenticated project_list call) BEFORE minting a local operator token — #207 §3: a codenexus-authenticated session is a valid principal, but #404 forbids minting without a verified one, so the bearer must be checked against the authority. Fixed operator-configured gate host, bearer in header, no agent-supplied URL (noqa S310); fail-closed — any non-200 or network error refuses the mint",
    },
    "outer_gate_transport.py": {
        "purpose": "backlog_hub_forward",
        "host_class": "fixed codenexus internal host from operator env (AIDOCS_CODENEXUS_INTERNAL_URL, loopback in production)",
        "rationale": "the gate is the PUBLIC authenticated edge for the server-authoritative backlog: the internal S2S route is loopback-only, so an operator's local AIDOCS calls the gate (sync-scoped token) and the gate forwards, adding the S2S credential. Tenancy is RE-CHECKED before forwarding and the orgId sent onward is the GATE-RESOLVED one, never caller-supplied. Fixed operator-configured host, no agent-supplied URL (noqa S310); hub trouble returns 502, never a 500",
    },
    "outer_gate_github_credential.py": {
        "purpose": "github_auth",
        "host_class": "fixed github/host from operator env",
        "rationale": "github credential/token resolution to a fixed internal host (noqa S310 fixed host)",
    },
    "stdio_shim.py": {
        "purpose": "host_stdio_to_daemon_forward",
        "host_class": "the local AIDOCS daemon (default http://127.0.0.1:8748/mcp); operator-overridable endpoint via AIDOCS_MCP_ENDPOINT",
        "rationale": "#758 per-window shim: the HOST spawns it over stdio, so the spawn environment carries the host session id that stateless HTTP (#435) has no other way to learn; the shim forwards each JSON-RPC message to the daemon with X-Aidocs-Host-Session/-Host-Kind. Deliberately NOT classified KNOWN_NON_EGRESS: unlike hook_broker_client.py it does not REFUSE a non-loopback host — AIDOCS_MCP_ENDPOINT may legitimately point at a remote gate — so it is fingerprinted as an operator-configured endpoint instead. The URL comes from the operator environment only, never from an agent or from message content (noqa S310)",
    },
    "runtime_provisioner.py": {
        "purpose": "runtime_bootstrap_download",
        "host_class": "pinned runtime + wheel sources",
        "rationale": "deploy/provision-time downloads of the pinned runtime; not agent-reachable at runtime",
    },
}

# Network patterns that are NOT egress (the inventory guard excludes these, with the reason).
KNOWN_NON_EGRESS: dict[str, str] = {
    "governed_shell_broker.py": "socket.AF_UNIX — local IPC, not network egress",
    # #335 P3 hook thin-client: same-machine, same-user loopback IPC (127.0.0.1),
    # NOT network egress. HookBroker REFUSES a non-loopback host at construction
    # (raises ValueError) and the client only ever dials 127.0.0.1 — its discovery
    # state file carries a port + token, never a host. Token-gated (compare_digest)
    # before any evaluation. Pinned by tests/host/test_hook_broker.py.
    "hook_broker.py": "127.0.0.1 loopback IPC (non-loopback bind refused at construction) — local, not network egress",
    "hook_broker_client.py": "127.0.0.1 loopback IPC only (dials the local broker; no host is configurable) — local, not network egress",
    "heuristic_judge.py": "egress-DETECTION regex/comment, not a call",
    "preflight_prompt_judge.py": "egress-DETECTION regex, not a call",
    # #686 ai_slop(mode='spaghetti'): a pure-AST N+1 finder. Its cost tables
    # (_EXPENSIVE / _DOTTED_EXPENSIVE) hold the STRING LITERALS "urlopen",
    # "requests.get", "requests.post", "httpx.get", "httpx.post" as callee
    # NAMES to recognise in the code it analyses. Verified 2026-08-01: the
    # module's only imports are `ast` and `typing` — it has no network
    # capability at all, in-process or otherwise. Same self-reference class
    # as heuristic_judge.py / preflight_prompt_judge.py above.
    "slop_spaghetti.py": "expensive-callee NAME table (AST analysis vocabulary), not a call",
    "governed_egress.py": "the chokepoint module itself",
}


def host_of(url: str) -> str | None:
    """Best-effort hostname (lowercased) from a URL; None when unparseable (→ fail closed)."""
    try:
        h = urlparse(str(url or "")).hostname
        return h.lower() if h else None
    except Exception:
        return None


def host_allowed(host: str | None, allow_hosts: Iterable[str]) -> bool:
    """True iff host exactly equals, or is a subdomain of, an allowlist entry. Empty host → False."""
    if not host:
        return False
    h = host.lower()
    for raw in allow_hosts:
        a = str(raw or "").strip().lower()
        if not a:
            continue
        if h == a or h.endswith("." + a):
            return True
    return False


def assert_egress_allowed(
    url: str,
    *,
    purpose: str,
    allow_hosts: Iterable[str],
    audit: Callable[[dict], None] | None = None,
) -> str:
    """Fail-closed in-process egress gate. Returns the host when allowed; raises EgressRefused when
    the URL's host is not in allow_hosts (or the URL is malformed). Audits EVERY decision (allow +
    refuse) via the audit hook — the audit record carries only host/purpose/decision, never a secret
    or the full URL (which may embed credentials)."""
    host = host_of(url)
    allowed = host_allowed(host, allow_hosts)
    if audit is not None:
        try:
            audit(
                {
                    "event": "in_process_egress",
                    "purpose": str(purpose),
                    "host": host or "<unparseable>",
                    "decision": "allow" if allowed else "refuse",
                }
            )
        except Exception:
            pass
    if not allowed:
        raise EgressRefused(
            f"in-process egress refused: host {host!r} is not in the {purpose!r} allowlist "
            f"(add it to the egress allowlist if this destination is trusted)"
        )
    return host  # type: ignore[return-value]
