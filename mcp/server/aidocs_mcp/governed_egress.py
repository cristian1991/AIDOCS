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
    "outer_gate_github_credential.py": {
        "purpose": "github_auth",
        "host_class": "fixed github/host from operator env",
        "rationale": "github credential/token resolution to a fixed internal host (noqa S310 fixed host)",
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
    "heuristic_judge.py": "egress-DETECTION regex/comment, not a call",
    "preflight_prompt_judge.py": "egress-DETECTION regex, not a call",
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
