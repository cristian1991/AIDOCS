"""Outer Gate HTTP transport — loopback-first, deny-by-default.

This is the TRANSPORT authority layer for the Web/Outer-Gate war. It is a thin
HTTP adapter over the existing in-process `OuterGate` admission boundary. It adds
NO admission authority of its own — it only PARSES HTTP, resolves a principal
from a bearer token, builds a `GateRequest`, and hands EVERY decision to
`OuterGate.discover()/invoke()`. The transport can only make the surface MORE
restrictive (route-class deny-by-default), never more permissive than the gate.

## The deed (what cannot be bypassed)

  - Every tool/catalog/status request flows through `OuterGate`. There is no code
    path from the transport to an executor except `gate.invoke()`, which enforces
    tier (Tier-R-only), trust (package_untrusted ⇒ nothing), loopback bind,
    authenticated principal, project-binding, and mandatory audit.
  - The transport is DENY-BY-DEFAULT: only route classes explicitly mapped to a
    gate call are handled; mutation verbs, control-plane paths, operator-intent
    paths, and unknown paths are refused with a documented reason BEFORE any
    GateRequest is built. Adding a new exposed route is a deliberate edit to
    ROUTE_TABLE + REFUSAL_MATRIX — it cannot happen implicitly.
  - BIND POLICY: loopback-only is the first sealed deployment mode. A non-loopback
    (public) bind is refused at serve time AND re-checked in dispatch
    (defense-in-depth) — the gate independently refuses non-loopback too.
  - TRUST: package_untrusted / unverified install ⇒ the gate's precondition
    refuses every request; the transport never serves a tool from untrusted code.
  - DISABLED BY DEFAULT: nothing binds a socket unless `outer_gate.enabled` is
    true AND the bind is loopback AND the package is trusted. MCP/local stdio
    behavior is entirely unaffected — this module is never imported by the stdio
    server path.

## Growth without bypass

Future phases (Tier-M behind authN+confirm, Tier-A operator-auth, public bind via
a hardened reverse proxy) add ROUTE_TABLE entries and gate phases — but each new
capability still passes through `gate.invoke`, so the deed holds: the transport
can expose only what the gate admits.

## AUDIT-ORDERING DOCTRINE (revisit before Tier-M/Tier-A execution)

Today the transport records its audit event AFTER `gate.invoke` returns. This is
correct ONLY because no mutation executes: Tier-M is refused (`tier_m_disabled`),
Tier-A is discoverable-but-not-invokable (`tier_not_invokable`), and the single
invokable tier (Tier-R) is read-only and side-effect-free. So a post-decision
audit cannot miss a state change — there is none.

When Tier-M/Tier-A EXECUTION ships, post-decision audit becomes INSUFFICIENT and
this pattern MUST change to the three-phase discipline already used by
`operator_intent_resolver`:
  1. INTENT audit recorded BEFORE the mutation — if it can't record, refuse and
     do NOT execute (fail closed, nothing mutated).
  2. The gate's own mandatory audit (audit_or_refuse) still gates execution.
  3. RESULT audit recorded AFTER — a result-audit failure is reported as
     `audit_degraded` (the mutation stands + is intent-audited), never lost.
The guard test `test_tier_m_a_do_not_execute_so_post_audit_is_safe_today` pins the
precondition (no execution today) so wiring execution without this discipline
fails CI.
"""

from __future__ import annotations

import functools as _functools
import html as _html
import json
import re
import urllib.parse as _urlparse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .outer_gate import _LOOPBACK, GateRequest, OuterGate

API = "v1"

# ── Route classes ──────────────────────────────────────────────────────────
RC_HEALTH = "health"  # liveness; no tool surface, no auth leak
RC_CATALOG = "catalog"  # discover the eligible surface (gate.discover)
RC_INVOKE = "invoke"  # invoke a tool (gate.invoke — Tier-R only)
RC_REFUSED_MUTATION = "refused_mutation"  # unsafe HTTP verb on tools
RC_REFUSED_CONTROL_PLANE = "refused_control_plane"  # admin/config/operator paths
RC_REFUSED_OPERATOR_INTENT = "refused_operator_intent"
RC_MCP = "mcp"  # Streamable-HTTP MCP facade (JSON-RPC over the gate)
RC_OPENAPI = "openapi"  # OpenAPI 3.1 schema doc for Custom GPT Actions
# OAuth 2.1 bridge for ChatGPT's native MCP connector. Metadata routes are public
# discovery (no secret); authorize/token are the auth flow (audited authority).
RC_OAUTH_AS_META = "oauth_as_meta"  # /.well-known/oauth-authorization-server
RC_OAUTH_PR_META = "oauth_pr_meta"  # /.well-known/oauth-protected-resource
RC_OAUTH_AUTHORIZE = "oauth_authorize"  # /oauth/authorize (GET login, POST consent)
RC_OAUTH_TOKEN = "oauth_token"  # /oauth/token (POST code→token)
RC_UNKNOWN = "unknown"  # anything else → 404


# ── Refusal matrix — the documented disposition of every route class ────────
# disposition: "open"   = served by the transport itself (health only),
#              "gate"   = delegated to OuterGate (the gate decides admit/refuse),
#              "refuse" = refused by the transport with the named reason, BEFORE
#                         any GateRequest is built (the surface is never reachable).
@dataclass(frozen=True)
class RefusalEntry:
    disposition: str
    http: int
    reason: str


REFUSAL_MATRIX: dict[str, RefusalEntry] = {
    RC_HEALTH: RefusalEntry("open", 200, "liveness"),
    RC_CATALOG: RefusalEntry("gate", 0, "delegated_to_gate_discover"),
    RC_INVOKE: RefusalEntry("gate", 0, "delegated_to_gate_invoke"),
    RC_REFUSED_MUTATION: RefusalEntry("refuse", 405, "mutation_verb_not_exposed"),
    RC_REFUSED_CONTROL_PLANE: RefusalEntry(
        "refuse",
        403,
        "control_plane_not_exposed_over_transport",
    ),
    RC_REFUSED_OPERATOR_INTENT: RefusalEntry("refuse", 403, "operator_intent_is_host_path_only"),
    RC_MCP: RefusalEntry("gate", 0, "delegated_to_gate_via_mcp_jsonrpc"),
    RC_OPENAPI: RefusalEntry("open", 200, "schema_doc"),
    RC_OAUTH_AS_META: RefusalEntry("open", 200, "oauth_as_metadata"),
    RC_OAUTH_PR_META: RefusalEntry("open", 200, "oauth_pr_metadata"),
    RC_OAUTH_AUTHORIZE: RefusalEntry("open", 0, "oauth_authorize_flow"),
    RC_OAUTH_TOKEN: RefusalEntry("open", 0, "oauth_token_flow"),
    RC_UNKNOWN: RefusalEntry("refuse", 404, "no_such_route"),
}

# ── Route table — (method, compiled path) → (route_class, captures) ─────────
# DENY-BY-DEFAULT: only these patterns are recognized; everything else → UNKNOWN.
_INVOKE_RE = re.compile(rf"^/{API}/tools/(?P<kind>[a-z_]+)/(?P<name>[A-Za-z0-9_.\-]+):invoke$")
_CONTROL_PLANE_PREFIXES = (
    f"/{API}/admin",
    f"/{API}/config",
    f"/{API}/rbac",
    f"/{API}/operator",
    f"/{API}/dashboard",
    f"/{API}/freeze",
    f"/{API}/escalation",
)


def classify(method: str, path: str) -> tuple[str, dict[str, str]]:
    """Map an HTTP (method, path) to a route class + captured params. Pure,
    dependency-free, deny-by-default.
    """
    method = (method or "").upper()
    path = (path or "").split("?", 1)[0].rstrip("/") or "/"

    if path == f"/{API}/health":
        return (RC_HEALTH, {}) if method == "GET" else (RC_REFUSED_MUTATION, {})

    if path == f"/{API}/openapi.json":
        return (RC_OPENAPI, {}) if method == "GET" else (RC_REFUSED_MUTATION, {})

    # OAuth 2.1 bridge (ChatGPT native MCP connector). Metadata = GET discovery;
    # authorize = GET (login) + POST (consent); token = POST only.
    if path == "/.well-known/oauth-authorization-server":
        return (RC_OAUTH_AS_META, {}) if method == "GET" else (RC_REFUSED_MUTATION, {})
    if path == "/.well-known/oauth-protected-resource":
        return (RC_OAUTH_PR_META, {}) if method == "GET" else (RC_REFUSED_MUTATION, {})
    if path == "/oauth/authorize":
        return (RC_OAUTH_AUTHORIZE, {}) if method in ("GET", "POST") else (RC_REFUSED_MUTATION, {})
    if path == "/oauth/token":
        return (RC_OAUTH_TOKEN, {}) if method == "POST" else (RC_REFUSED_MUTATION, {})

    # Action-friendly body-form invoke: POST /v1/invoke with {kind,name,arguments}.
    # Same gate.invoke / Tier-R-only / scope as the path form — just a JSON body
    # ingress (GPT Actions dislike the `:invoke` path suffix). kind/name from body.
    if path == f"/{API}/invoke":
        return (RC_INVOKE, {}) if method == "POST" else (RC_REFUSED_MUTATION, {})

    if path == f"/{API}/tools":
        return (RC_CATALOG, {}) if method == "GET" else (RC_REFUSED_MUTATION, {})

    # Streamable-HTTP MCP facade: JSON-RPC via POST. GET (the optional server→
    # client SSE stream) is not offered by this minimal facade → 405; clients
    # fall back to POST request/response. Method semantics route through the gate.
    if path == f"/{API}/mcp":
        return (RC_MCP, {}) if method == "POST" else (RC_REFUSED_MUTATION, {})

    # control-plane / operator-intent paths are NEVER exposed over transport,
    # regardless of verb — refused before any gate/tool lookup.
    for pref in _CONTROL_PLANE_PREFIXES:
        if path == pref or path.startswith(pref + "/"):
            if pref == f"/{API}/operator":
                return RC_REFUSED_OPERATOR_INTENT, {}
            return RC_REFUSED_CONTROL_PLANE, {}

    m = _INVOKE_RE.match(path)
    if m:
        if method != "POST":
            return RC_REFUSED_MUTATION, {}
        return RC_INVOKE, {"kind": m.group("kind"), "name": m.group("name")}

    return RC_UNKNOWN, {}


# ── Bind policy ─────────────────────────────────────────────────────────────
def is_loopback_bind(host: str | None) -> bool:
    """True only for a loopback bind address. Public binds are refused in the
    first sealed deployment mode.
    """
    return str(host or "").strip().lower() in _LOOPBACK


# ── Token / auth policy ─────────────────────────────────────────────────────
# resolver(headers, project_root) -> principal dict | None. Default validates a
# bearer token against the canonical IdentityStore; None ⇒ unauthenticated, which
# the gate refuses fail-closed. We never invent identity.
PrincipalResolver = Callable[[dict, Path | None], dict | None]


def _bearer_token(headers: dict) -> str:
    raw = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "authorization":
            raw = str(v or "")
            break
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def default_principal_resolver(
    headers: dict,
    project_root: Path | None,
) -> dict | None:
    """Canonical token → principal. Fail-closed: no/invalid/expired token ⇒ None.
    A resolved principal is marked authenticated=True so the gate's
    super_admin-requires-authentication rule is satisfiable for real tokens.
    """
    token = _bearer_token(headers)
    if not token or project_root is None:
        return None
    try:
        from .identity_store import IdentityStore

        user = IdentityStore().validate_token(Path(project_root), token)
    except Exception:
        return None
    if user is None:
        return None
    return {
        "user_id": user.user_id,
        "effective_role": user.role,
        "authenticated": True,
        "source": "outer_gate_transport_token",
    }


# ── Scoped/audience-bound auth (the public deployment policy) ────────────────
@dataclass
class AuthOutcome:
    """Result of claim-checking auth. principal is None on failure, with `reason`
    naming exactly why (fail-closed); scope carries the token's granted scopes.
    """

    principal: dict | None = None
    reason: str = ""
    scope: frozenset = field(default_factory=frozenset)


# Per-route required scope. A route with a requirement admits ONLY a token whose
# scope covers it; health requires none (non-authority liveness).
def _required_scope(route_class: str) -> frozenset:
    from .outer_gate_token_store import (
        SCOPE_CATALOG,
        SCOPE_TIER_R_INVOKE,
    )

    return {
        RC_CATALOG: frozenset({SCOPE_CATALOG}),
        RC_INVOKE: frozenset({SCOPE_TIER_R_INVOKE}),
    }.get(route_class, frozenset())


def make_scoped_auth(
    project_root: Path,
    *,
    audience: str | None = None,
) -> Callable[[dict, Path | None], AuthOutcome]:
    """Build the deployment auth resolver: validates a bearer token against the
    OuterGateTokenStore, checking AUDIENCE + expiry + revocation, and returns the
    token's SCOPE. Replaces identity-only bearer validation. Fail-closed: any
    failure → principal=None + a specific reason.
    """
    from .outer_gate_token_store import AUDIENCE_CODENEXUS, OuterGateTokenStore

    aud = audience or AUDIENCE_CODENEXUS
    store = OuterGateTokenStore()

    def _resolve(headers: dict, _proj: Path | None) -> AuthOutcome:
        token = _bearer_token(headers)
        v = store.validate(project_root, token, required_audience=aud)
        if not v.ok:
            return AuthOutcome(None, v.reason or "unauthenticated")
        return AuthOutcome(
            principal={
                "user_id": v.user_id,
                "effective_role": v.role,
                "authenticated": True,
                "source": "outer_gate_scoped_token",
                "scope": sorted(v.scope),
                "audience": v.audience,
                "token_id": v.token_id,
                "resource": v.resource,
                # Execution/data isolation key (codenexus org). "" ⇒ local/legacy
                # token (no tenant binding) → shared gate-root home, current behavior.
                "tenant_id": v.tenant_id,
            },
            reason="",
            scope=v.scope,
        )

    return _resolve


# ── Response shape ──────────────────────────────────────────────────────────
@dataclass
class TransportResponse:
    status: int
    body: dict = field(default_factory=dict)
    route_class: str = ""
    blocked_by: str = ""
    headers: dict = field(default_factory=dict)  # extra response headers (MCP session id)
    # OAuth needs non-JSON responses (HTML login, 302 redirects). When raw_body is
    # set, serve() writes it verbatim with content_type instead of JSON(body).
    raw_body: str | None = None
    content_type: str = "application/json"


# blocked_by → HTTP status. Unknown reasons fail closed to 403.
_BLOCKED_HTTP: dict[str, int] = {
    "unauthenticated": 401,
    "no_token": 401,
    "unknown_token": 401,
    "expired": 401,
    "revoked": 401,
    "wrong_audience": 401,
    "minter_disabled_or_gone": 401,
    "corrupt_scope": 401,
    "insufficient_scope": 403,
    "gateway_disabled": 403,
    "non_loopback_bind": 403,
    "package_untrusted": 403,
    "not_remote_eligible": 403,
    "tier_m_disabled": 403,
    "tier_not_invokable": 403,
    "audit_not_configured": 503,
    "project_binding_required": 400,
    "ambiguous_entry": 409,
    "unknown_tool": 404,
    "audit_failed": 500,
    "execution_error": 500,
}


# ── Audit-as-law ────────────────────────────────────────────────────────────
# Route classes that are NON-AUTHORITY liveness — the ONLY decisions doctrine
# permits to complete without a durable audit. Everything else is an authority
# decision: it MUST be durably audited or fail closed (audit_unrecorded).
# Discovery metadata is public + secretless (like the OpenAPI doc), so it is
# liveness-exempt. The OAuth authorize/token flows are NOT exempt — they are
# authority (they hand out credentials) and must be durably audited.
_AUTHORITY_EXEMPT: frozenset[str] = frozenset(
    {RC_HEALTH, RC_OPENAPI, RC_OAUTH_AS_META, RC_OAUTH_PR_META},
)

# Best-effort tier attribution for the audit event, derived from the gate's
# refusal reason (the gate does not return the tier directly).
_TIER_FROM_BLOCK: dict[str, str] = {
    "tier_m_disabled": "M",
    "tier_not_invokable": "A",
    "not_remote_eligible": "?",
}


def default_transport_audit(project_root: Path) -> Callable[[dict], None]:
    """The DURABLE default transport audit sink — writes a structured
    outer_gate_transport event to the canonical ExecutionIndexStore (the same
    seam operator-intent uses). transport_audit=None therefore does NOT mean
    'unaudited authority'; it means 'use this durable sink'. A caller may inject
    a different sink, but never a no-op for authority decisions.
    """

    def _sink(event: dict) -> None:
        from .execution_index_store import ExecutionIndexStore

        auth = event.get("auth") or {}
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="outer_gate_transport",
            source_kind="outer_gate_transport",
            capability_name=str(event.get("tool") or event.get("route_class") or ""),
            action_kind=str(event.get("verdict") or ""),
            target_entity=str(event.get("path") or ""),
            status=str(event.get("status") or ""),
            user_id=(auth.get("user") or None) if isinstance(auth, dict) else None,
            effective_role=(auth.get("role") or None) if isinstance(auth, dict) else None,
            payload=event,
        )

    return _sink


# ── OpenAPI 3.1 schema for Custom GPT Actions (generated from the deed) ──────
DEFAULT_PUBLIC_BASE = "https://mcp.codenexus.cloud"


def build_openapi(public_base: str = DEFAULT_PUBLIC_BASE) -> dict:
    """A minimal OpenAPI 3.1 schema for ChatGPT Custom GPT Actions, GENERATED from
    the route table + REFUSAL_MATRIX so the doc cannot drift from the deed. Only
    the three exposed operations are described; the refused classes are listed (so
    a reader sees what is NOT reachable) but never become callable operations.

    The schema is DOCUMENTATION, NOT AUTHORITY — the gate enforces tier/trust/
    scope/audit on every request regardless of what this schema claims.
    """
    refused = sorted(rc for rc, e in REFUSAL_MATRIX.items() if e.disposition == "refuse")
    refused_note = (
        "REFUSED (404/403/405) and never reachable through this Action: "
        + ", ".join(refused)
        + "; plus Tier-M and Tier-A tools, operator-intent, config, dashboard, "
        "freeze, escalation, and all control-plane. Only catalog/status and "
        "read-proven Tier-R invoke are exposed."
    )
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "AIDOCS Outer Gate",
            "version": API,
            "description": (
                "Read-only access to the AIDOCS Outer Gate. "
                + refused_note
                + " This OpenAPI schema is documentation, not authority: the gate "
                "independently enforces tier, package trust, token scope/audience/"
                "expiry/revocation, and durable audit on every request."
            ),
        },
        "servers": [{"url": public_base}],
        "components": {
            "securitySchemes": {
                "OuterGateToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Scoped Outer Gate token (audience=codenexus-outer-gate; "
                        "scope catalog/tier_r_invoke). Minted by an authenticated "
                        "admin via the outer_gate_server CLI; short-lived + "
                        "revocable."
                    ),
                },
            },
        },
        "security": [{"OuterGateToken": []}],
        "paths": {
            f"/{API}/health": {
                "get": {
                    "operationId": "health",
                    "summary": "Liveness probe",
                    "security": [],
                    "responses": {"200": {"description": "ok"}},
                },
            },
            f"/{API}/tools": {
                "get": {
                    "operationId": "listTools",
                    "summary": "List the tools invokable now (read-proven Tier-R)",
                    "responses": {
                        "200": {"description": "eligible/invokable tool list"},
                        "401": {"description": "missing/bad/expired/revoked/wrong-audience token"},
                        "403": {"description": "refused (gateway/trust/scope)"},
                    },
                },
            },
            f"/{API}/invoke": {
                "post": {
                    "operationId": "invokeTool",
                    "summary": "Invoke a read-proven Tier-R tool (Tier-M/Tier-A refused)",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {
                                            "type": "string",
                                            "description": "tool name from listTools",
                                        },
                                        "kind": {"type": "string", "default": "mcp_tool"},
                                        "arguments": {
                                            "type": "object",
                                            "description": "tool arguments",
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "responses": {
                        "200": {"description": "tool result"},
                        "401": {"description": "token failure (fail closed)"},
                        "403": {
                            "description": "refused: tier_not_invokable / "
                            "tier_m_disabled / insufficient_scope / "
                            "package_untrusted",
                        },
                    },
                },
            },
        },
    }


# ── MCP JSON-RPC helpers (Streamable-HTTP facade) ───────────────────────────
def _jsonrpc_result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _jsonrpc_error(mid, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def _tool_call_result(value) -> dict:
    """Build a tools/call result that carries BOTH the text content (the gate's
    json-encoded payload) AND structuredContent. Every advertised tool declares
    an outputSchema (_OUTPUT_OBJ, a permissive object), and the MCP SDK client
    ENFORCES that a tool with an output schema returns structured content — so a
    text-only result makes a real MCP client (ChatGPT) fail-closed with
    'has an output schema but did not return structured content'. structuredContent
    must be a JSON object; a non-object payload (e.g. ai_run's list) is wrapped as
    {"result": value} so the schema is always satisfied.
    """
    structured = value if isinstance(value, dict) else {"result": value}
    return {
        "content": [{"type": "text", "text": json.dumps(value)}],
        "structuredContent": structured,
        "isError": False,
    }


# ── Security headers (2026-05-27) ───────────────────────────────────────────
# Applied at the response-finalization layer (handler._run) on every
# response, regardless of route or status code. The login page is the
# most consequential beneficiary — without X-Frame-Options it could be
# iframed by a phishing page to steal operator credentials.
#
# CSP strategy: strict for text/html (the login page is the only HTML
# we serve), permissive default-src 'self' for JSON API responses (no
# script execution to constrain there, but HSTS + nosniff still apply).
#
# Future work: move to a per-route policy registry if/when we add more
# HTML surfaces. For now the bipolar HTML/JSON split is sufficient.

# Inline base64 data URLs are used for the login bg image; the strict
# CSP must allow them. img-src 'self' data: covers that. Inline <style>
# blocks live inside login.html — style-src 'self' 'unsafe-inline'.
# One inline <script> in the template: the rate-limit countdown UI (purely
# progressive enhancement — the form is a plain HTML POST and works without it).
# It is allowed by its sha256 hash, NOT 'unsafe-inline'. If the countdown script
# in templates/login.html changes, regenerate this hash (the browser prints the
# expected 'sha256-…' in the CSP console error) or the countdown silently stops.
_LOGIN_COUNTDOWN_SCRIPT_SHA256 = "sha256-spyY4+m1qeWHSLd9b5ZmItvSIr8YUJZpo8NrPWsyuYU="
_CSP_HTML = (
    "default-src 'none'; "
    f"script-src '{_LOGIN_COUNTDOWN_SCRIPT_SHA256}'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    # form-action covers BOTH the initial POST destination AND every
    # redirect hop the browser would follow afterwards (CSP3 / Chrome /
    # Firefox impl). 'self' alone is wrong for an OAuth authorization
    # server: a successful /oauth/authorize POST 302s to the registered
    # client redirect_uri (e.g. https://chatgpt.com/..., or
    # http://localhost:<port>/callback for desktop MCP clients), and
    # the browser refuses to follow that redirect if it isn't named
    # here. The real redirect_uri validation happens server-side in
    # the OAuth code (registered-client allow-list); CSP is the wrong
    # layer for that check. We permit https: globally (prod OAuth
    # clients) and localhost / 127.0.0.1 (dev / desktop MCP clients).
    # Loopback sources MUST be port-wildcarded: a desktop MCP client's redirect
    # is http://127.0.0.1:<ephemeral-or-fixed-port>/callback, and a bare
    # 'http://127.0.0.1' source matches ONLY port 80 — so the browser blocked the
    # post-auth 302 to the loopback (found via a live browser OAuth run). ':*'
    # admits any loopback port; https: still covers prod (e.g. ChatGPT) clients.
    "form-action 'self' https: http://localhost:* http://127.0.0.1:*; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "connect-src 'self'"
)
_CSP_JSON = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def _security_headers(content_type: str) -> dict[str, str]:
    """Return the security-headers baseline for a given content-type.

    Always-on (every response, regardless of body type):
      X-Frame-Options: DENY                 — no iframing anywhere
      X-Content-Type-Options: nosniff       — MIME confusion defense
      Strict-Transport-Security: max-age=…  — HSTS (Caddy probably
                                              sets this too; redundant
                                              defense is fine)
      Referrer-Policy: strict-origin-…      — limit referer leakage
      Permissions-Policy: …                 — disable unused APIs
      Cross-Origin-Opener-Policy: same-origin   — isolate browsing context
      Cross-Origin-Resource-Policy: same-origin

    CSP varies: strict HTML policy for `text/html`; permissive JSON
    policy otherwise.
    """
    headers = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        ),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if content_type.startswith("text/html"):
        headers["Content-Security-Policy"] = _CSP_HTML
    else:
        headers["Content-Security-Policy"] = _CSP_JSON
    return headers


# ── OAuth helpers (query/form parsing, redirect, login page) ────────────────
_AUTHZ_CARRY = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "state",
    "resource",
)


def _parse_query(path: str) -> dict:
    q = path.split("?", 1)[1] if "?" in path else ""
    return {k: v[-1] for k, v in _urlparse.parse_qs(q, keep_blank_values=True).items()}


def _parse_form(body: bytes | str | None) -> dict:
    raw = body.decode("utf-8") if isinstance(body, bytes) else (body or "")
    return {k: v[-1] for k, v in _urlparse.parse_qs(raw, keep_blank_values=True).items()}


def _redirect_url(base: str, params: dict) -> str:
    sep = "&" if "?" in base else "?"
    q = _urlparse.urlencode({k: v for k, v in params.items() if v})
    return f"{base}{sep}{q}" if q else base


def _login_html(params: dict, *, error: str = "", retry_after: int = 0) -> str:
    """Render the OAuth login page from the on-disk template.

    Template lives at ``mcp/server/aidocs_mcp/templates/login.html`` and uses
    ``string.Template`` substitution (``$name`` placeholders) so CSS braces
    don't need escaping. The template is read once per request — cost is
    negligible (~25KB read) and avoids any reload-on-edit dance during dev.

    DOCTRINE (2026-05-26, reaffirmed 2026-05-27): Project + session data
    are NEVER rendered on this page — they are post-auth state. Help /
    GitHub / homepage URLs mirror mcp/pyproject.toml [project.urls] so
    this page stays in sync with the publish surface.
    """
    # Canonical URLs — single source of truth in mcp/pyproject.toml.
    GH_URL = "https://github.com/cristian1991/AIDOCS"  # noqa: F841 — kept for parity / future link surface
    DOCS_URL = "https://github.com/cristian1991/AIDOCS#readme"
    HOME_URL = "https://codenexus.cloud"

    hidden_fields = "".join(
        f'<input type="hidden" name="{_html.escape(k)}" '
        f'value="{_html.escape(str(params.get(k) or ""))}">'
        for k in _AUTHZ_CARRY
    )

    # The countdown attribute is only set when the rate-limiter is what
    # produced this error. The template ships a tiny inline script that
    # finds [data-rl-countdown], updates the visible "Wait X seconds"
    # text every second, and disables the submit button until N hits 0.
    countdown_attr = f' data-rl-countdown="{int(retry_after)}"' if retry_after > 0 else ""
    error_block = (
        (
            f'<div class="err-banner" role="alert"{countdown_attr}>'
            '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<circle cx="12" cy="12" r="10"/>'
            '<path d="M12 8v4"/><path d="M12 16h.01"/></svg>'
            f"<span>{_html.escape(error)}</span></div>"
        )
        if error
        else ""
    )

    # "Create an account" preserves OAuth carry params so the authorize
    # state survives a back-and-forth (no separate /signup endpoint
    # yet — the link lands back on this page; when a real signup ships
    # this becomes its target).
    carry_qs = _urlparse.urlencode({k: params.get(k) or "" for k in _AUTHZ_CARRY if params.get(k)})
    use_other = "/oauth/authorize" + (("?" + carry_qs) if carry_qs else "")

    # Derive who initiated this OAuth flow from the redirect_uri so the
    # "Back to <who>" link points at the actual caller instead of a
    # generic dashboard. Falls back to codenexus.cloud when the
    # redirect_uri is missing / unparseable.
    _INITIATOR_LABELS = {
        "chatgpt.com": "ChatGPT",
        "chat.openai.com": "ChatGPT",
        "claude.ai": "Claude",
        "anthropic.com": "Claude",
        "codenexus.cloud": "CodeNexus",
        "mcp.codenexus.cloud": "CodeNexus",
        "app.codenexus.cloud": "CodeNexus",
    }
    back_label = "CodeNexus"
    back_url = HOME_URL
    try:
        _ru = params.get("redirect_uri") or ""
        if _ru:
            _parsed = _urlparse.urlparse(_ru)
            _host = (_parsed.hostname or "").lower()
            if _host and _parsed.scheme:
                back_url = f"{_parsed.scheme}://{_parsed.netloc}"
                back_label = _INITIATOR_LABELS.get(
                    _host,
                    (_host.split(".")[-2].capitalize() if "." in _host else _host),
                )
    except Exception:
        pass

    # Scope advertisement (2026-05-27): tell the operator exactly what
    # scopes the initiator is requesting. OAuth `scope` param is
    # space-separated. Map known scope strings to human-readable labels;
    # unknown scopes render raw so a misconfigured client surfaces
    # immediately rather than masquerading as a known capability.
    _SCOPE_LABELS = {
        "mcp.tool.invoke": "Invoke MCP tools (read-only catalog) in the selected project",
        "mcp.tool.edit": "Two-phase file edits (propose + confirm) in the selected project",
        "mcp.tool.run": "Run shell commands (judge-gated, refusal-eligible) in the selected project",
        "mcp.audit.read": "Read your own session's audit log",
        "mcp.project.list": "List projects you've authorized",
        "mcp.project.select": "Select a project for the session (requires host confirmation)",
        "mcp.session.list": "List your sessions in the selected project",
        "mcp.session.select": "Select a session (requires host confirmation)",
        "openid": "Identity (email, account ID)",
        "profile": "Profile metadata (name, locale)",
        "email": "Email address",
        "offline_access": "Refresh tokens (long-lived re-auth without re-prompt)",
    }
    requested_scopes = (params.get("scope") or "").split()
    if requested_scopes:
        items_html = "".join(
            f"<li><code>{_html.escape(s)}</code>"
            + (
                f" — {_html.escape(_SCOPE_LABELS[s])}"
                if s in _SCOPE_LABELS
                else " — <em>(unrecognized scope)</em>"
            )
            + "</li>"
            for s in requested_scopes
        )
        scope_block = (
            '<div class="scope-request" role="region" aria-label="Requested scopes">'
            '<div class="scope-request-head">'
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<rect x="4" y="10" width="16" height="10" rx="2"/>'
            '<path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>'
            f"<span>{_html.escape(back_label)} is requesting:</span>"
            "</div>"
            f"<ul>{items_html}</ul>"
            "</div>"
        )
    else:
        scope_block = ""

    # Forgot-password link. Until a real /reset endpoint exists, the
    # link is a mailto to the operator-admin with a pre-filled subject.
    # Tracked in BACKLOG.md §C.2 — when /reset ships, swap to that URL.
    forgot_url = (
        "mailto:gate-admin@codenexus.cloud"
        "?subject=AIDOCS%20password%20reset"
        "&body=Please%20reset%20my%20AIDOCS%20Gate%20password."
    )

    # Late import to avoid a cycle at module-load.
    from . import __version__ as _aidocs_version

    template = _load_template()
    bg_data_url = _load_bg_data_url()

    return template.safe_substitute(
        post_action="/oauth/authorize",
        hidden_fields=hidden_fields,
        error_block=error_block,
        use_other=_html.escape(use_other),
        home_url=_html.escape(HOME_URL),
        docs_url=_html.escape(DOCS_URL),
        mcp_version=_html.escape(_aidocs_version),
        bg_data_url=bg_data_url,
        back_label=_html.escape(back_label),
        back_url=_html.escape(back_url),
        scope_block=scope_block,
        forgot_url=_html.escape(forgot_url),
    )


@_functools.lru_cache(maxsize=1)
def _load_template() -> _StringTemplate:
    """Read + parse the login template once per process.

    Cold-path page (~1 OAuth flow per token grant) but parsing the same
    ~30KB template on every request was wasteful. lru_cache(maxsize=1)
    pins it to memory after first render. A process restart picks up
    template edits during deploy; pm2 restart is the cache-bust.
    """
    from string import Template

    p = Path(__file__).resolve().parent / "templates" / "login.html"
    return Template(p.read_text(encoding="utf-8"))


@_functools.lru_cache(maxsize=1)
def _load_bg_data_url() -> str:
    """Read + base64-encode the brand-panel background once per process.

    Same rationale as _load_template — 88KB WebP, re-encoded as base64
    on every render is wasted CPU. Cached for the process lifetime;
    pm2 restart picks up new images.
    """
    import base64

    p = Path(__file__).resolve().parent / "templates" / "assets" / "login-bg.webp"
    return "data:image/webp;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


# Type-only alias to avoid importing Template at module load just for the
# return-type annotation of the cached loader above.
#
# Doctrine: the canonical "type-only import" idiom is `if TYPE_CHECKING:`
# (typing.TYPE_CHECKING is False at runtime, True under type checkers).
# Using `if False:` works too but vulture flags it as an unsatisfiable
# condition. TYPE_CHECKING is the conventional spelling and is
# recognized by every static analyzer in the toolchain.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from string import Template as _StringTemplate


# ── project/session selector + catalog tools — schemas/sets owned by the
#    single canonical catalog resolver (no drift). ─────────────────────────
from .outer_gate_catalog import (
    PROJECT_EDIT_TOOLS as _PROJECT_EDIT_TOOLS,
)
from .outer_gate_catalog import (
    PROJECT_READ_TOOLS as _PROJECT_READ_TOOLS,
)
from .outer_gate_catalog import (  # noqa: E402
    PROJECT_TOOLS,
)


def _resolve_session_templates_root() -> Path | None:
    """Best-effort locate the session templates dir (must contain context.md).

    The canonical mcp_server._resolve_templates_root uses repo-layout path math
    that misses on the deployed gate layout, so we walk UP from this package
    looking for the templates dir instead. Returns a Path, or None — None tells
    the caller to use a minimal inline scaffold (so a session is still
    creatable wherever the gate is deployed).
    """
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        for cand in (
            base / "core" / ".MEMORY" / ".aidocs" / "templates",
            base / ".MEMORY" / ".aidocs" / "templates",
            base / "data",
        ):
            try:
                if (cand / "context.md").is_file():
                    return cand
            except OSError:
                continue
    return None


def handle_project_tool(
    name,
    args,
    *,
    gate,
    home,
    default_exec_root,
    token_id,
    principal,
    has,
) -> dict:
    """Handle a selector/catalog/GitHub tool. Returns a JSON result dict, or
    {"_error": reason} for a fail-closed refusal. Reads require `catalog`;
    GitHub register/sync require `tier_m_edit` (operator).
    """
    from . import outer_gate_projects as P

    st = P.GateProjectStore()
    if default_exec_root:
        try:
            st.ensure_default(home, name="AutoDeployBase", root=Path(default_exec_root))
        except Exception:
            pass
    if name in _PROJECT_READ_TOOLS and not has("catalog"):
        return {"_error": "insufficient_scope", "_detail": "grant_required=catalog"}
    # Pillar 3: a multi-org user who hasn't explicitly org_select'd is READ-defaulted to
    # their OWN org; a WRITE (project import/register) must NOT silently land there — make
    # them choose the target org first. Reads/selectors above are unaffected.
    if name in _PROJECT_EDIT_TOOLS and isinstance(principal, dict) and principal.get("org_unselected_multi"):
        return {
            "_error": "org_selection_required",
            "_detail": "you belong to multiple orgs; call org_select to choose which org this write targets (writes are never silently defaulted)",
        }
    # Project import/sync is gated by the DISTINCT project_import grant — NOT
    # tier_m_edit (importing a repo is not editing source).
    if name in _PROJECT_EDIT_TOOLS and not has("project_import"):
        return {"_error": "insufficient_scope", "_detail": "grant_required=project_import"}

    def _pstatus(root):
        return P.project_status(Path(root))

    try:
        if name == "project_list":
            cur = st.current(home, token_id)
            cid = cur["project_id"] if cur else None
            return {
                "projects": [
                    {
                        "project_id": x["project_id"],
                        "name": x["name"],
                        "source": x["source"],
                        "is_default": bool(x["is_default"]),
                        "current": x["project_id"] == cid,
                    }
                    for x in st.list(home)
                ],
            }
        if name == "project_current":
            cur = st.current(home, token_id)
            return {
                "project": None
                if not cur
                else {
                    "project_id": cur["project_id"],
                    "name": cur["name"],
                    "root": cur["root"],
                    "session_id": cur.get("session_id", ""),
                    "status": _pstatus(cur["root"]),
                },
            }
        if name == "dashboard_snapshot":
            # Full dashboard snapshot for the gate's project — powers the desktop
            # dashboard's WebMCP (cloud) scope (read-only; catalog scope). Targets
            # the selected project if bound, else the gate's exec-project-root.
            from .cli import _dashboard_runtime

            cur = st.current(home, token_id)
            root = (cur["root"] if cur and cur.get("root") else "") or default_exec_root
            if not root:
                return {"_error": "no_project", "_detail": "no selected or default project"}
            _, runtime = _dashboard_runtime()
            return {"snapshot": runtime.dashboard_snapshot(Path(root))}
        if name == "project_select":
            # Two-phase confirm so the assistant CANNOT silently retarget the
            # token's project on its own — the user must see + agree to the
            # exact confirmation phrase before the bind happens. Mirrors the
            # `profile_confirm_token` doctrine in operator_surface.py:
            # deterministic, stateless, no popup-protocol dependency. ChatGPT
            # only surfaces a native "destructive" card when its host policy
            # decides to; this gate-level confirm guarantees a confirmation
            # surface regardless of host UI capability.
            pid = str(args.get("project_id") or "")
            tok = str(args.get("confirm_token") or "")
            expected = f"confirm-project-select {pid}"
            if tok != expected:
                cur_sel = st.current(home, token_id)
                # Resolve the proposed target's display name (best-effort —
                # if the id is unknown we still emit the confirm shape, and
                # the second call will surface the unknown-project error).
                try:
                    target = st.get(home, pid)
                except Exception:
                    target = None
                return {
                    "_error": "confirm_required",
                    "_detail": (
                        "project_select changes the bound project for "
                        "this token; ask the user before re-invoking "
                        "with confirm_token"
                    ),
                    "action": "project_select",
                    "project_id": pid,
                    "target_name": (target or {}).get("name", ""),
                    "previous_project_id": (cur_sel["project_id"] if cur_sel else ""),
                    "previous_name": (cur_sel["name"] if cur_sel else ""),
                    "confirm_token": expected,
                    "summary": (
                        f"About to switch this token's bound project to "
                        f"{(target or {}).get('name', pid)!r} "
                        f"(project_id={pid!r})."
                        + (
                            f" Previously bound to "
                            f"{cur_sel['name']!r} (project_id="
                            f"{cur_sel['project_id']!r})."
                            if cur_sel
                            else " No project was previously bound."
                        )
                        + " The user must confirm before this change."
                    ),
                }
            proj = st.select(home, token_id, pid)
            cur_sel = st.current(home, token_id)
            return {
                "selected": proj["project_id"],
                "name": proj["name"],
                "root": proj["root"],
                "session_id": (cur_sel.get("session_id") if cur_sel else ""),
                "status": _pstatus(proj["root"]),
                "confirmed": True,
            }
        if name in ("project_status", "project_index_status"):
            pid = str(args.get("project_id") or "")
            proj = st.get(home, pid) if pid else st.current(home, token_id)
            if proj is None:
                return {"_error": "unknown_project"}
            return {
                "project_id": proj["project_id"],
                "name": proj["name"],
                "status": _pstatus(proj["root"]),
            }
        if name == "session_current":
            cur = st.current(home, token_id)
            # Surface BOTH the SELECTED session (the bound workspace — now
            # guaranteed to exist, never a phantom) AND the per-token EXECUTION
            # session that remote tool runs actually use (ogr_<token>, synthesized
            # at the gate for token isolation — see outer_gate.py). Reporting both
            # means session_current can never quietly diverge from where work runs.
            return {
                "session_id": (cur.get("session_id") if cur else "") or "",
                "execution_session_id": "ogr_" + str(token_id or "anon"),
                "project_id": cur["project_id"] if cur else "",
                "project_name": cur["name"] if cur else "",
                "project_root": cur["root"] if cur else "",
            }
        if name == "session_select":
            # Two-phase confirm — see project_select above for the doctrine.
            # session_select also requires a prior project bind; that check
            # is enforced inside st.select_session (raises ProjectError with
            # blocked_by="no_project_selected"), which still fires on the
            # second-phase call so we don't need to duplicate it here.
            sid = str(args.get("session_id") or "")
            tok = str(args.get("confirm_token") or "")
            expected = f"confirm-session-select {sid}"
            if tok != expected:
                cur = st.current(home, token_id)
                return {
                    "_error": "confirm_required",
                    "_detail": (
                        "session_select changes the bound session for "
                        "this token; ask the user before re-invoking "
                        "with confirm_token"
                    ),
                    "action": "session_select",
                    "session_id": sid,
                    "project_id": cur["project_id"] if cur else "",
                    "project_name": cur["name"] if cur else "",
                    "previous_session_id": (cur or {}).get("session_id", ""),
                    "confirm_token": expected,
                    "summary": (
                        f"About to bind this token to session {sid!r}"
                        + (
                            f" within project {cur['name']!r}."
                            if cur
                            else " (no project selected yet — this will fail with "
                            "no_project_selected after confirmation)."
                        )
                        + " The user must confirm before this change."
                    ),
                }
            st.select_session(home, token_id, sid)
            cur = st.current(home, token_id)
            return {
                "session_id": sid,
                "project_id": cur["project_id"] if cur else "",
                "project_name": cur["name"] if cur else "",
                "project_root": cur["root"] if cur else "",
                "confirmed": True,
            }
        if name == "session_list":
            cur = st.current(home, token_id)
            sroot = (Path(cur["root"]) / ".MEMORY" / "sessions") if cur else None
            ss = (
                sorted(d.name for d in sroot.iterdir() if d.is_dir())
                if sroot and sroot.is_dir()
                else []
            )
            return {"sessions": ss}
        if name == "session_create":
            # Create a new session WORKSPACE (.MEMORY/sessions/<id>/) in the
            # selected project and bind this token to it. Closes the gap where
            # session_select could only bind an id that already existed —
            # there was no way to MAKE a session over the connector.
            import re as _re

            from .session_store import SessionStore

            cur = st.current(home, token_id)
            if not cur:
                return {
                    "_error": "no_project_selected",
                    "_detail": "call project_select first; a session belongs to a project",
                }
            sid = str(args.get("session_id") or "").strip()
            if not sid:
                sid = "session-" + uuid.uuid4().hex[:12]
            if not _re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid):
                return {
                    "_error": "bad_session_id",
                    "_detail": "session_id must match [A-Za-z0-9._-]{1,128}",
                }
            root = Path(cur["root"])
            sdir = root / ".MEMORY" / "sessions" / sid
            existed = sdir.is_dir()
            goal = str(args.get("goal") or "").strip() or "-"
            owner = (principal.get("user") if isinstance(principal, dict) else "") or "connector"
            scaffold = "templated"
            if not existed:
                # Prefer the canonical templated scaffold; fall back to a
                # self-contained inline one when the templates dir can't be
                # resolved (the deployed gate layout differs from the repo, so
                # _resolve_templates_root's path math may miss — a session must
                # still be creatable there).
                troot = _resolve_session_templates_root()
                if troot is not None:
                    try:
                        SessionStore(templates_root=troot).create_session(
                            root, sid, title=sid, owner=owner, goal=goal,
                        )
                    except Exception:
                        troot = None
                if troot is None:
                    scaffold = "minimal"
                    for sub in ("plans", "agents", "artifacts"):
                        (sdir / sub).mkdir(parents=True, exist_ok=True)
                    (sdir / "SESSION.md").write_text(
                        f"# Session: {sid}\n\n"
                        f"- **Status:** active\n"
                        f"- **Owner:** {owner}\n"
                        f"- **Goal:** {goal}\n"
                        f"- **Created via:** connector session_create\n\n"
                        "## State\n-\n\n## Upcoming\n-\n\n## Blockers\n-\n",
                        encoding="utf-8",
                    )
                    (sdir / "context.md").write_text(f"# Context — {sid}\n\n-\n", encoding="utf-8")
            # Bind the token to the new (or pre-existing) session.
            st.select_session(home, token_id, sid)
            cur2 = st.current(home, token_id)
            return {
                "session_id": sid,
                "created": (not existed),
                "scaffold": (scaffold if not existed else "existing"),
                "bound": True,
                "project_id": cur2["project_id"] if cur2 else "",
                "project_name": cur2["name"] if cur2 else "",
                "project_root": cur2["root"] if cur2 else "",
            }
        if name == "tool_catalog":
            # SINGLE SOURCE OF TRUTH: the canonical catalog resolver — every
            # remote-eligible tool with honest class/tier/executable_now/
            # blocked_by. Internal-only (dashboard) tools are excluded.
            from . import outer_gate_catalog as _cat

            cur = st.current(home, token_id)
            _disc = gate.discover(principal)
            _ginv = (
                frozenset(e["name"] for e in _disc.result if e.get("invokable_now"))
                if _disc.ok
                else frozenset()
            )
            return {
                "tools": [
                    {
                        "name": r["name"],
                        "class": r["class"],
                        "tier": r["tier"],
                        "executable_now": r["executable_now"],
                        "blocked_by": r["blocked_by"],
                        "grant_required": r["grant_required"],
                    }
                    for r in _cat.catalog(principal=principal, project=cur, gate_invokable=_ginv)
                ],
            }
        if name == "tool_capabilities":
            from . import outer_gate_catalog as _cat

            tn = str(args.get("tool") or "")
            cur = st.current(home, token_id)
            r = _cat.resolve(
                tn,
                principal=principal,
                project=cur,
                manifest_entry=_cat._manifest_index().get(tn),
            )
            if r["class"] == _cat.CLASS_INTERNAL and tn not in _cat._manifest_index():
                return {"_error": "unknown_tool"}
            return {
                "tool": tn,
                "class": r["class"],
                "tier": r["tier"],
                "schema": r["inputSchema"],
                "executable_now": r["executable_now"],
                "grant_reason": r["blocked_by"],
                "grant_required": r["grant_required"],
                "visible": r["visible"],
            }
        # Registry-driven dispatch (single source of truth). Every entry
        # in `tool_interface.REGISTRY` with surface="both" whose cls is
        # SELECTOR or IMPORT is routed through this branch — enforce
        # the spec's `confirm` mode, then call the late-bound impl. The
        # legacy explicit branches below (project_select etc.) stay for
        # tools that haven't been migrated to the registry yet.
        from . import tool_interface as _ti

        _spec = _ti.get(name)
        if _spec is not None and _spec.surface == "both":
            if _spec.confirm == "two_phase":
                expected = _ti.build_confirm_phrase(_spec, args)
                if str(args.get("confirm_token") or "") != expected:
                    return {
                        "_error": "confirm_required",
                        "_detail": (
                            f"{name} is a confirmation-gated action; "
                            f"ask the user before re-invoking with "
                            f"confirm_token"
                        ),
                        "action": name,
                        "confirm_token": expected,
                        "summary": (
                            f"About to invoke {name} with "
                            f"args={args!r}. The user must confirm "
                            f"before this change."
                        ),
                    }
            # Dispatch via the canonical local MCP server's call_tool.
            # Many tool impls are nested inside `create_server` closures
            # (so they can capture the server object for register-time
            # configuration) and therefore aren't directly importable;
            # going through call_tool is the only universally-correct
            # path. The registry's `impl` pointer is documentation +
            # future-direct-call optimization, not load-bearing here.
            import asyncio as _asyncio

            from .mcp_server import create_server as _create_server

            # Strip confirm_token from impl args — registry-level
            # contract, not an arg the underlying handler understands.
            impl_args = {k: v for k, v in args.items() if k != "confirm_token"}
            try:
                srv = _create_server(tools_profile="full")
                tool_result = _asyncio.run(srv.call_tool(name, impl_args))
            except Exception as e:  # noqa: BLE001
                return {"_error": "registry_impl_failed", "_detail": f"{name}: {e!r}"}
            # call_tool returns a list[TextContent]; surface the JSON
            # payload of the first content block when it parses as
            # JSON, else the raw text.
            from .outer_gate_executor import _result_payload as _pl

            payload = _pl(tool_result)
            return payload if isinstance(payload, dict) else {"result": payload}

        if name == "project_register_from_github_url":
            # Resolve the BOUND org's GitHub credential just-in-time. When a tenant is
            # bound, disable the shared-platform env fallback so a tenant with no
            # connected credential can never clone with the platform token (isolation).
            _tid = principal.get("tenant_id") if isinstance(principal, dict) else None
            _cred = None
            _env_fallback = True
            if _tid:
                from .outer_gate_github_credential import resolve_org_github_token

                _cred = resolve_org_github_token(_tid)
                _env_fallback = False
            proj = P.register_from_github_url(
                st,
                home,
                url=str(args.get("url") or ""),
                ref=args.get("ref"),
                projects_base=Path(home) / "projects",
                credential=_cred,
                allow_env_fallback=_env_fallback,
            )
            return {
                "project_id": proj["project_id"],
                "name": proj["name"],
                "status": _pstatus(proj["root"]),
            }
        if name == "project_sync":
            return P.sync_project(st, home, str(args.get("project_id") or ""))
    except P.ProjectError as e:
        return {"_error": e.blocked_by, "_detail": e.reason}
    return {"_error": "unknown_tool"}


# ── webmcp service-mode entitlement gate (DoD #2) ───────────────────────────
# Env var holding the codenexus read-only DSN. Set ONLY on the live codenexus/
# webmcp install; unset everywhere else (local/dev/test) so the entitlement gate
# is a no-op and the OAuth login behaves exactly as before. Never hardcoded.
_CODENEXUS_DSN_ENV = "AIDOCS_CODENEXUS_DSN"


def _webmcp_authz_refusal(email: str) -> str | None:
    """Authorize an authenticated login against codenexus identity for webmcp
    service mode (DoD #2). Returns a refusal reason, or None to admit.

    No DSN configured ⇒ this is not the live webmcp install ⇒ admit (no-op).
    DSN configured ⇒ resolve the codenexus principal (role + seat, incl. seats
    drawn from an org owner) and apply the pure ``authorize_webmcp`` policy.
    Fail-closed: when the gate IS configured for codenexus, any resolver error
    or unresolved user denies rather than silently admitting."""
    import os

    dsn = os.environ.get(_CODENEXUS_DSN_ENV, "").strip()
    if not dsn:
        return None  # not the codenexus-bound install — entitlement gate off
    try:
        from .codenexus_identity import CodenexusPostgresResolver
        from .webmcp_authz import authorize_webmcp

        principal = CodenexusPostgresResolver(dsn=dsn).resolve(email)
        decision = authorize_webmcp(principal)
        return None if decision.allowed else (decision.reason or "webmcp_not_authorized")
    except Exception:
        return "webmcp_authz_unavailable"


# Injectable seam for tests (monkeypatch this name).
_WEBMCP_AUTHZ = _webmcp_authz_refusal


def _codenexus_authenticate(email: str, password: str):
    """Authenticate (email, password) against codenexus identity (DoD #2 — the gate
    authenticates against codenexus, never its own store). Returns ``(principal,
    exists)`` (see CodenexusPostgresResolver.authenticate).

    No DSN configured ⇒ ``(None, False)`` so the caller uses local auth (dev/local
    install, unchanged). Configured ⇒ codenexus is the authority; a known codenexus
    user with a bad password refuses (exists=True) instead of falling through."""
    import os

    dsn = os.environ.get(_CODENEXUS_DSN_ENV, "").strip()
    if not dsn:
        return (None, False)
    try:
        from .codenexus_identity import CodenexusPostgresResolver

        return CodenexusPostgresResolver(dsn=dsn).authenticate(email, password)
    except Exception:
        return (None, False)


# Injectable seam for tests (monkeypatch this name).
_CODENEXUS_AUTHN = _codenexus_authenticate


def _codenexus_list_user_orgs(user_id: str) -> list[dict]:
    """The orgs a codenexus user belongs to (Membership), with role + entitlement.
    No DSN ⇒ [] (local gate). Fail-closed. Authority for org_select + the per-request
    membership re-check."""
    import os

    dsn = os.environ.get(_CODENEXUS_DSN_ENV, "").strip()
    uid = str(user_id or "").strip()
    if not dsn or not uid:
        return []
    try:
        from .codenexus_identity import CodenexusPostgresResolver

        return CodenexusPostgresResolver(dsn=dsn).list_user_orgs(uid)
    except Exception:
        return []


# Injectable seam for tests (monkeypatch this name).
_CODENEXUS_LIST_USER_ORGS = _codenexus_list_user_orgs

# Org-binding tools operate at the SHARED auth home (which tenant a token acts as),
# ABOVE the per-tenant project layer — so they are handled distinctly from the
# tenant-scoped PROJECT_TOOLS (which bind to the already-selected tenant home).
_ORG_TOOLS = frozenset({"org_list", "org_select"})


def handle_org_tool(name, args, *, home, principal, token_id, has) -> dict:
    """org_list / org_select — pick which of YOUR orgs (tenant) this token acts as.
    Reads memberships from codenexus (the authority) and records the per-token choice
    in the SHARED auth-home selection store after verifying membership. `home` MUST be
    the shared auth home (project_root), NOT a tenant home. Catalog scope.
    """
    from .outer_gate_tenancy import GateOrgSelectionStore

    if not has("catalog"):
        return {"_error": "insufficient_scope", "_detail": "grant_required=catalog"}
    uid = (principal.get("user_id") if isinstance(principal, dict) else "") or ""
    orgs = _CODENEXUS_LIST_USER_ORGS(uid)
    store = GateOrgSelectionStore()
    selected = store.get(home, token_id) if (home and token_id) else ""

    def _view(o: dict) -> dict:
        return {
            "org_id": o["org_id"],
            "role": o["org_role"],
            "entitled": bool(o["entitled"]),
            "name": o.get("org_name") or o.get("org_slug") or o["org_id"],
            "current": o["org_id"] == selected,
        }

    if name == "org_list":
        return {"orgs": [_view(o) for o in orgs], "selected_org_id": selected}
    # org_select — two-phase confirm (mirrors project_select/session_select).
    oid = str(args.get("org_id") or "")
    tok = str(args.get("confirm_token") or "")
    match = next((o for o in orgs if o["org_id"] == oid), None)
    expected = f"confirm-org-select {oid}"
    if tok != expected:
        return {
            "_error": "confirm_required",
            "_detail": (
                "org_select changes which org (tenant) this token acts as; ask the "
                "user before re-invoking with confirm_token"
            ),
            "action": "org_select",
            "org_id": oid,
            "target_name": (match or {}).get("org_name") or (match or {}).get("org_slug") or "",
            "previous_org_id": selected,
            "confirm_token": expected,
            "summary": (
                f"About to bind this token to org {oid!r}"
                + (f" ({_view(match)['name']!r}, your role: {match['org_role']})." if match
                   else " — but you are NOT a member of that org; this will be refused.")
            ),
        }
    if match is None:
        return {"_error": "not_a_member", "_detail": f"you are not a member of org {oid!r}"}
    store.set(home, token_id, oid)
    return {
        "selected": oid,
        "name": _view(match)["name"],
        "role": match["org_role"],
        "entitled": bool(match["entitled"]),
        "confirmed": True,
    }


# ── The pure dispatcher ─────────────────────────────────────────────────────
def _with_tenant_config_reset(fn):
    """Wrap dispatch so the per-tenant global-config contextvar is ALWAYS restored to
    its pre-request value when the call returns OR raises (Pillar 1, 2026-06-15).

    The 3a binding inside dispatch is set-at-entry (clear to "", then bind the tenant
    DB) — it relies on every path re-binding before any config read. Without a finally
    reset, a same-thread dispatch that early-returns or raises BEFORE re-binding would
    leave the previous tenant's global-config DB bound and bleed it into the next
    request. config_store already exposes the token/reset primitives; this guarantees
    the reset fires on every exit path without re-indenting the ~900-line handler body.
    """
    import functools

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        from . import config_store as _cs

        _tok = _cs.set_tenant_global_db(None)
        try:
            return fn(*args, **kwargs)
        finally:
            _cs.reset_tenant_global_db(_tok)

    return _wrapper


@_with_tenant_config_reset
def dispatch(
    method: str,
    path: str,
    headers: dict | None = None,
    body: bytes | str | None = None,
    *,
    gate: OuterGate,
    project_root: Path | None = None,
    bind_host: str = "127.0.0.1",
    principal_resolver: PrincipalResolver = default_principal_resolver,
    auth_resolver: Callable[[dict, Path | None], AuthOutcome] | None = None,
    transport_audit: Callable[[dict], None] | None = None,
) -> TransportResponse:
    """Framework-agnostic request handler. Pure: no socket. Every admit decision
    is OuterGate's; the transport only classifies, authenticates, refuses — and
    AUDITS. Every authority decision is durably audited or fails closed
    (audit_unrecorded); only non-authority liveness (health) may go unaudited.

    When `auth_resolver` is supplied (the public deployment policy) it does
    claim-checking auth (audience + scope + expiry + revocation) and the route's
    required scope is enforced BEFORE OuterGate admission. Without it, identity-
    only `principal_resolver` is used (local/test mode, no scope gate).
    """
    headers = headers or {}
    request_id = uuid.uuid4().hex[:16]
    route_class, params = classify(method, path)

    # Proxy/client metadata for AUDIT ONLY — captured from Caddy's forwarding
    # headers. NEVER used for bind/loopback/auth decisions (those use the literal
    # socket bind + the validated token); a spoofed header changes the audit
    # record's client_ip but can never grant access or fake a loopback origin.
    def _hget(name: str) -> str:
        for k, v in headers.items():
            if str(k).lower() == name:
                return str(v or "")
        return ""

    _xff = _hget("x-forwarded-for")
    client_ip = _xff.split(",")[0].strip() if _xff else _hget("x-real-ip")
    proxy_proto = _hget("x-forwarded-proto")
    proxy_host = _hget("x-forwarded-host") or _hget("host")
    proxy_path = (path or "").split("?", 1)[0]

    # transport_audit=None ⇒ the DURABLE canonical sink (never a no-op) whenever a
    # project is bound. With no sink AND no project_root, an authority decision is
    # unauditable → it will fail closed in _finalize.
    sink = transport_audit
    if sink is None and project_root is not None:
        sink = default_transport_audit(project_root)

    def _finalize(
        status: int,
        body: dict,
        blocked_by: str = "",
        *,
        verdict: str,
        tool: str = "",
        kind: str = "",
        tier: str = "",
        principal: dict | None = None,
        force_authority: bool = False,
        headers: dict | None = None,
        inject_request_id: bool = True,
        exec_project_root: str = "",
        exec_project_id: str = "",
        raw_body: str | None = None,
        content_type: str = "application/json",
        scope_diag: dict | None = None,
    ) -> TransportResponse:
        auth = (
            {
                "user": principal.get("user_id"),
                "role": principal.get("effective_role"),
                "authenticated": bool(principal.get("authenticated")),
                "scope": principal.get("scope"),
                "audience": principal.get("audience"),
                "token_id": principal.get("token_id"),
                "tenant_id": principal.get("tenant_id") or "",
            }
            if isinstance(principal, dict)
            else {"authenticated": False}
        )
        event = {
            "event": "outer_gate_transport_request",
            "request_id": request_id,
            "method": (method or "").upper(),
            "path": proxy_path,
            "route_class": route_class,
            "tool": tool,
            "kind": kind,
            "tier": tier or _TIER_FROM_BLOCK.get(blocked_by, ""),
            "auth": auth,
            "operator": auth.get("user") or "",
            "token_id": auth.get("token_id") or "",
            # Tenant identity on EVERY authority event (admit + refuse) — no tenant
            # data lands in a shared anonymous ledger without its owning org.
            "tenant_id": auth.get("tenant_id") or "",
            "source": "outer_gate_transport",
            "verdict": verdict,  # admit | refuse | liveness
            "reason": blocked_by or str(body.get("error") or ""),
            "status": status,
            "bind_host": bind_host,
            # Attribution: auth/trust home (tokens + signed trust) vs the REAL
            # execution project the call read against — ALWAYS recorded separately
            # so a read is never mis-attributed to the auth home.
            "auth_home": str(project_root) if project_root else "",
            "exec_project_root": exec_project_root,
            "exec_project_id": exec_project_id,
            # proxy/client metadata — AUDIT ONLY (never an auth/bind input)
            "client_ip": client_ip,
            "proxy_proto": proxy_proto,
            "proxy_host": proxy_host,
            "proxy_path": proxy_path,
        }
        # Non-secret OAuth scope decision (requested vs client-registered vs
        # issued) — diagnostics for the connector-grant flow. Never carries a
        # client secret, authorization code, or access token.
        if scope_diag:
            event["scope_decision"] = scope_diag
        recorded = False
        if sink is not None:
            try:
                sink(event)
                recorded = True
            except Exception:
                recorded = False
        # A non-loopback bind refusal is always an authority-relevant event even
        # on the health route (a public-exposure attempt), so it never rides the
        # liveness exemption.
        is_authority = (
            route_class not in _AUTHORITY_EXEMPT
            or force_authority
            or blocked_by == "non_loopback_bind"
        )
        if is_authority and not recorded:
            # FAIL CLOSED, HONESTLY: an authority decision that cannot be durably
            # audited is refused with the audit-failure reason — never returned as
            # its original verdict (no swallowing into success/refusal).
            return TransportResponse(
                500,
                {
                    "error": "audit_unrecorded",
                    "request_id": request_id,
                    "route_class": route_class,
                    "detail": (
                        f"transport decision ({verdict}:"
                        f"{blocked_by or status}) could not be durably "
                        f"audited; failing closed"
                    ),
                },
                route_class,
                "audit_unrecorded",
            )
        out = dict(body)
        if inject_request_id:
            out.setdefault("request_id", request_id)
        return TransportResponse(
            status,
            out,
            route_class,
            blocked_by,
            headers=dict(headers or {}),
            raw_body=raw_body,
            content_type=content_type,
        )

    # 0. Bind policy (defense-in-depth; the gate also refuses non-loopback).
    if not is_loopback_bind(bind_host):
        return _finalize(
            403,
            {"error": "non_loopback_bind", "detail": "public bind is not permitted in this mode"},
            "non_loopback_bind",
            verdict="refuse",
            force_authority=True,
        )

    entry = REFUSAL_MATRIX[route_class]

    # 1. Transport-level refusals — surface never reachable, no GateRequest built.
    if entry.disposition == "refuse":
        return _finalize(
            entry.http,
            {"error": entry.reason, "route_class": route_class},
            entry.reason,
            verdict="refuse",
        )

    # 2. Health — transport-served NON-AUTHORITY liveness. Audited best-effort
    #    (never fails closed); no tool surface, no identity echo.
    if route_class == RC_HEALTH:
        return _finalize(
            200,
            {"status": "ok", "api": API, "enabled": bool(gate.is_enabled())},
            verdict="liveness",
        )

    # 2b. OpenAPI schema — NON-AUTHORITY documentation doc (generated from the
    #     route table + refusal matrix; reveals only the 3 exposed operations).
    #     Server URL reflects the actual public edge (proxy host) when present.
    if route_class == RC_OPENAPI:
        base = f"https://{proxy_host}" if proxy_host else DEFAULT_PUBLIC_BASE
        return _finalize(200, build_openapi(base), verdict="schema", inject_request_id=False)

    # 2c. OAuth 2.1 bridge for ChatGPT's native MCP connector. These endpoints are
    #     UNAUTHENTICATED at the bearer level (they ARE the auth flow); metadata is
    #     public discovery, authorize/token are audited authority. The issued
    #     access token is a real scoped Outer Gate token (no gate bypass).
    _oauth_base = f"https://{proxy_host}" if proxy_host else DEFAULT_PUBLIC_BASE
    _mcp_resource = f"{_oauth_base}/{API}/mcp"
    if route_class == RC_OAUTH_AS_META:
        from .outer_gate_oauth import as_metadata

        return _finalize(200, as_metadata(_oauth_base), verdict="schema", inject_request_id=False)
    if route_class == RC_OAUTH_PR_META:
        from .outer_gate_oauth import protected_resource_metadata

        return _finalize(
            200,
            protected_resource_metadata(_oauth_base, _mcp_resource),
            verdict="schema",
            inject_request_id=False,
        )
    if route_class == RC_OAUTH_AUTHORIZE:
        from .outer_gate_oauth import AuthorizeError, OAuthStore, validate_authorize

        store = OAuthStore()
        params = _parse_query(path) if method == "GET" else _parse_form(body)
        vr = validate_authorize(store, project_root, params, mcp_resource=_mcp_resource)
        if isinstance(vr, AuthorizeError):
            if vr.redirect_ok:
                loc = _redirect_url(
                    vr.redirect_uri,
                    {
                        "error": vr.error,
                        "error_description": vr.error_description,
                        "state": vr.state,
                    },
                )
                return _finalize(
                    302,
                    {},
                    vr.error,
                    verdict="refuse",
                    inject_request_id=False,
                    raw_body="",
                    headers={"Location": loc},
                )
            # Untrusted client/redirect — render a plain error, NEVER redirect.
            return _finalize(
                400,
                {"error": vr.error, "error_description": vr.error_description},
                vr.error,
                verdict="refuse",
            )
        if method == "GET":
            return _finalize(
                200,
                {},
                verdict="liveness",
                inject_request_id=False,
                raw_body=_login_html(params),
                content_type="text/html",
            )
        # Rate-limit the login form: per-IP AND per-account, 1 attempt
        # per 10s (king directive 2026-05-28). See _auth_rate_limit.py
        # for the doctrine. Reject with 429 + Retry-After if either key
        # is throttled; the countdown UI on the login page reads the
        # data-rl-countdown attribute injected into the error banner.
        from . import _auth_rate_limit as _rl

        _email_norm = (params.get("email") or "").strip().lower()
        for _rl_key in (f"ip:{client_ip}", f"email:{_email_norm}"):
            _ok, _retry = _rl.try_consume(_rl_key)
            if not _ok:
                return _finalize(
                    429,
                    {},
                    "oauth_login_rate_limited",
                    verdict="refuse",
                    inject_request_id=False,
                    raw_body=_login_html(
                        params,
                        error=f"Too many attempts. Please wait {_retry} seconds before trying again.",
                        retry_after=_retry,
                    ),
                    content_type="text/html",
                    headers={"Retry-After": str(_retry)},
                )

        # POST: authenticate the operator, then issue a single-use code + redirect.
        # webmcp service mode (DoD #2): AUTHENTICATE against codenexus identity
        # FIRST (the authority — the gate never trusts its own store for codenexus
        # users); fall back to the local identity store ONLY for emails that are not
        # codenexus users (dev/legacy operators). DSN unset ⇒ codenexus authn is a
        # no-op and local auth is used, exactly as before.
        _email = params.get("email", "")
        _password = params.get("password", "")
        _cnx_principal, _cnx_exists = _CODENEXUS_AUTHN(_email, _password)
        if _cnx_principal is not None:
            _uid, _role, _uemail = (
                _cnx_principal.user_id,
                _cnx_principal.role,
                _cnx_principal.email,
            )
        elif _cnx_exists:
            # Known codenexus account, wrong/absent password — refuse, never fall
            # back to a (possibly differently-passworded) local account.
            return _finalize(
                401,
                {},
                "oauth_login_failed",
                verdict="refuse",
                raw_body=_login_html(params, error="Invalid credentials"),
                content_type="text/html",
            )
        else:
            from .identity_store import IdentityStore

            user = IdentityStore().authenticate(project_root, email=_email, password=_password)
            if user is None:
                return _finalize(
                    401,
                    {},
                    "oauth_login_failed",
                    verdict="refuse",
                    raw_body=_login_html(params, error="Invalid credentials"),
                    content_type="text/html",
                )
            _uid, _role, _uemail = user.user_id, user.role, user.email
        # Authorize the authenticated identity against codenexus (role + active
        # seat) BEFORE minting a code. No-op unless the codenexus DSN is configured.
        _wm_refusal = _WEBMCP_AUTHZ(_uemail)
        if _wm_refusal:
            return _finalize(
                403,
                {},
                _wm_refusal,
                verdict="refuse",
                raw_body=_login_html(
                    params, error="This account is not authorized for WebMCP access."
                ),
                content_type="text/html",
            )
        # Multi-org: the token is NOT bound to a single org at mint — a user may
        # belong to several orgs and picks the active one post-connect via org_select
        # (gate transport). So no tenant is stamped on the auth code here.
        code = store.issue_code(project_root, vr, user_id=_uid, role=_role)
        loc = _redirect_url(vr.redirect_uri, {"code": code, "state": vr.state})
        return _finalize(
            302,
            {},
            "",
            verdict="admit",
            inject_request_id=False,
            raw_body="",
            headers={"Location": loc},
            principal={"user_id": _uid, "effective_role": _role, "authenticated": True},
            scope_diag=vr.scope_diag(),
        )
    if route_class == RC_OAUTH_TOKEN:
        from .outer_gate_oauth import OAuthStore, exchange_token

        res = exchange_token(
            OAuthStore(),
            project_root,
            _parse_form(body),
            mcp_resource=_mcp_resource,
        )
        return _finalize(
            (200 if res.ok else res.status),
            res.body,
            ("" if res.ok else str(res.body.get("error") or "oauth_error")),
            verdict=("admit" if res.ok else "refuse"),
            inject_request_id=False,
        )

    # 3. Authenticate. With a claim-checking auth_resolver (public deployment):
    #    validate audience+expiry+revocation, then enforce the route's required
    #    SCOPE before any gate admission. Wrong-audience/expired/revoked/unknown →
    #    fail closed with the specific reason; missing scope → insufficient_scope.
    #    Without an auth_resolver: identity-only principal (local/test), no scope.
    if auth_resolver is not None:
        outcome = auth_resolver(headers, project_root)
        principal = outcome.principal
        if principal is None:
            reason = outcome.reason or "unauthenticated"
            # MCP discovery: a 401 on /v1/mcp advertises the protected-resource
            # metadata so ChatGPT's connector can find the OAuth server (RFC 9728
            # / MCP auth). Header only — never an auth input.
            extra = {}
            if route_class == RC_MCP:
                extra["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{_oauth_base}/.well-known/oauth-protected-resource"'
                )
            return _finalize(
                _BLOCKED_HTTP.get(reason, 401),
                {"error": reason},
                reason,
                verdict="refuse",
                headers=extra,
            )
        need = _required_scope(route_class)
        if need and not need <= outcome.scope:
            return _finalize(
                403,
                {
                    "error": "insufficient_scope",
                    "detail": (
                        f"token scope {sorted(outcome.scope)} does not cover "
                        f"required {sorted(need)} for {route_class}"
                    ),
                },
                "insufficient_scope",
                verdict="refuse",
                principal=principal,
            )
        # RFC 8707 resource binding: a token bound to a resource (OAuth-issued)
        # may be used ONLY at that resource. /v1/mcp requires the MCP resource; a
        # resource-bound token presented on any other route is rejected. Unbound
        # tokens (directly-minted CLI/Action tokens) are unaffected.
        tok_resource = str(principal.get("resource") or "")
        route_resource = _mcp_resource if route_class == RC_MCP else ""
        if tok_resource and tok_resource != route_resource:
            return _finalize(
                403,
                {
                    "error": "resource_mismatch",
                    "detail": (
                        f"token is bound to resource {tok_resource!r}, not valid for this route"
                    ),
                },
                "resource_mismatch",
                verdict="refuse",
                principal=principal,
            )
    else:
        principal = principal_resolver(headers, project_root)

    # 3a. Multi-tenant execution binding (webmcp isolation). The AUTHORITATIVE active
    #     tenant is the SELECTED org resolved below (the org_select selection re-checked
    #     against live membership; else the user's OWN org as a read default) — NOT the
    #     token's mint-time tenant_id. The token tenant_id (principal["tenant_id"]) is
    #     LEGACY / direct-mint compatibility ONLY: the fallback used when no Organization
    #     memberships resolve (pre-migration / no-DSN-local), preserving single-tenant /
    #     local behaviour. Authority is always the validated principal + SERVER-SIDE
    #     membership — never the request body/query or the token's mint-time value. Bound
    #     here (post-auth, pre-handler) so EVERY execution route — MCP, invoke, catalog —
    #     resolves the project registry + "global" config under this tenant. The per-tenant global-config contextvar is set unconditionally
    #     (tenant path or cleared to "") so a prior request on this worker can never
    #     bleed its tenant's global config into the next.
    # tenant_id resolution (multi-org): 1) the org the token SELECTED via org_select
    # (must still be a current membership); 2) else the user's OWN org (memberships are
    # returned own-first) as the sensible default; 3) else the M0-derived principal
    # tenant — the pre-migration / no-DSN-local fallback (Organization table absent ⇒
    # list_user_orgs is empty ⇒ single-org behaviour, unchanged). Authority is the
    # validated principal + SERVER-SIDE membership; never the request body/query.
    _user_id_b = str(principal.get("user_id") or "") if isinstance(principal, dict) else ""
    _tok_id_b = str(principal.get("token_id") or "") if isinstance(principal, dict) else ""
    _legacy_tenant = str(principal.get("tenant_id") or "") if isinstance(principal, dict) else ""
    _role_u = str(principal.get("effective_role") or "").strip().upper() if isinstance(principal, dict) else ""
    _tenant_id = _legacy_tenant
    _org_role = ""
    _org_unselected_multi = False
    from . import config_store as _config_store

    if _user_id_b and project_root is not None:
        _user_orgs = _CODENEXUS_LIST_USER_ORGS(_user_id_b)
        if _user_orgs:
            from .outer_gate_tenancy import GateOrgSelectionStore as _OrgSel

            _sel = ""
            try:
                _sel = _OrgSel().get(project_root, _tok_id_b) if _tok_id_b else ""
            except Exception:
                _sel = ""
            _match = None
            if _sel:
                _match = next((o for o in _user_orgs if o["org_id"] == _sel), None)
                if _match is None:
                    # the SELECTED org is no longer one of the user's memberships
                    # (removed / moved) — refuse rather than silently re-defaulting.
                    _config_store.set_tenant_global_db(None)
                    return _finalize(
                        403,
                        {"error": "tenant_membership_revoked",
                         "detail": "your selected org is no longer one of your memberships; call org_select"},
                        "tenant_membership_revoked",
                        verdict="refuse",
                        principal=principal,
                    )
            if _match is None:
                _match = _user_orgs[0]  # default = the user's own org (returned first)
            # entitlement re-check (SUPER_ADMIN bypasses; the org's OWNER holds the seat)
            if not _match["entitled"] and _role_u != "SUPER_ADMIN":
                _config_store.set_tenant_global_db(None)
                return _finalize(
                    403,
                    {"error": "webmcp_entitlement_revoked",
                     "detail": "this org has no active webmcp entitlement"},
                    "webmcp_entitlement_revoked",
                    verdict="refuse",
                    principal=principal,
                )
            _tenant_id = _match["org_id"]
            _org_role = _match["org_role"]
            # Pillar 3: a user in >1 org who did NOT explicitly org_select is bound to
            # their OWN org as a READ default — but a WRITE must never silently land in
            # a defaulted org. Flag it so the write surfaces refuse org_selection_required
            # (the OAuth-consent / org_select binding is the user's way to resolve it).
            _org_unselected_multi = len(_user_orgs) > 1 and not _sel
        # else: no memberships resolved (pre-migration / local) → keep _legacy_tenant
    if isinstance(principal, dict):
        principal["tenant_id"] = _tenant_id
        principal["org_role"] = _org_role
        principal["org_unselected_multi"] = _org_unselected_multi
    _eff_home = project_root
    # No tenant gets a built-in default project. The gate's own exec-project-root
    # (the deploy repo, e.g. AutoDeployBase) is NEVER a tenant's project — every
    # tenant starts EMPTY and registers/imports its own projects. (Reads/edits/runs
    # with no selected project are refused below, never run against the deploy repo.)
    _eff_default_exec = ""

    if _tenant_id and project_root is not None:
        from . import outer_gate_tenancy as _tenancy

        try:
            _eff_home = _tenancy.tenant_home(project_root, _tenant_id)
            _config_store.set_tenant_global_db(
                _tenancy.tenant_global_config_db(project_root, _tenant_id)
            )
        except _tenancy.TenantError as _te:
            _config_store.set_tenant_global_db(None)
            return _finalize(
                403,
                {"error": _te.reason, "detail": _te.detail},
                _te.reason,
                verdict="refuse",
                principal=principal,
            )
    else:
        _config_store.set_tenant_global_db(None)

    # 3b. MCP Streamable-HTTP facade — JSON-RPC OVER the gate (not around it).
    #     tools/list → gate.discover, tools/call → gate.invoke. Method-level
    #     results/errors ride HTTP 200 per the Streamable-HTTP spec; transport
    #     auth (above) already gated the token. Per-method scope enforced here.
    if route_class == RC_MCP:
        raw = body.decode("utf-8") if isinstance(body, bytes) else (body or "")
        try:
            msg = json.loads(raw) if raw.strip() else {}
        except Exception:
            return _finalize(
                400,
                _jsonrpc_error(None, -32700, "parse error"),
                "mcp_parse_error",
                verdict="refuse",
                principal=principal,
                inject_request_id=False,
            )
        rpc_method = str(msg.get("method") or "")
        mid = msg.get("id")
        _scope = principal.get("scope") if isinstance(principal, dict) else None

        def _has(s: str) -> bool:  # identity-only principals (no scope) pass
            return _scope is None or s in _scope

        if rpc_method == "initialize":
            result = {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "aidocs-outer-gate", "version": API},
            }
            return _finalize(
                200,
                _jsonrpc_result(mid, result),
                verdict="admit",
                tool="initialize",
                principal=principal,
                headers={"Mcp-Session-Id": uuid.uuid4().hex},
                inject_request_id=False,
            )
        if rpc_method.startswith("notifications/"):
            return _finalize(
                202,
                {},
                verdict="liveness",
                principal=principal,
                inject_request_id=False,
            )
        if rpc_method == "ping":
            return _finalize(
                200,
                _jsonrpc_result(mid, {}),
                verdict="liveness",
                principal=principal,
                inject_request_id=False,
            )
        # resources/* and prompts/* — this gate is a TOOLS-only MCP server (it
        # advertises only `{"tools": {...}}` in initialize). Hosts (ChatGPT)
        # still probe these during handshake; answering with a JSON-RPC
        # method-not-found (-32601) reads as a broken server to strict clients.
        # Return an empty result instead — honest "I support the method, I have
        # none" — so discovery completes cleanly. No scope needed (empty either
        # way; the tools surface remains the scope/trust authority).
        if rpc_method in ("resources/list", "resources/templates/list"):
            return _finalize(
                200,
                _jsonrpc_result(mid, {"resources": []}),
                verdict="liveness",
                principal=principal,
                inject_request_id=False,
            )
        if rpc_method == "prompts/list":
            return _finalize(
                200,
                _jsonrpc_result(mid, {"prompts": []}),
                verdict="liveness",
                principal=principal,
                inject_request_id=False,
            )
        if rpc_method == "tools/list":
            if not _has("catalog"):
                return _finalize(
                    200,
                    _jsonrpc_error(mid, -32001, "insufficient_scope"),
                    "insufficient_scope",
                    verdict="refuse",
                    principal=principal,
                    inject_request_id=False,
                )
            # Preconditions still apply (signed trust etc.) before advertising.
            disc = gate.discover(principal)
            if not disc.ok:
                return _finalize(
                    200,
                    _jsonrpc_error(mid, -32002, disc.blocked_by, disc.reason),
                    disc.blocked_by,
                    verdict="refuse",
                    principal=principal,
                    inject_request_id=False,
                )
            # SINGLE SOURCE OF TRUTH: the canonical catalog resolver decides what
            # is advertised — advertised-invokable == actually-callable (no drift
            # with the executor allowlist / edit / selector / import handlers).
            from . import outer_gate_catalog as _cat

            # Advertised reads == THIS gate's invokable_now ∩ executor allowlist
            # (no drift). Visibility otherwise scope/class-based.
            gate_inv = frozenset(e["name"] for e in disc.result if e.get("invokable_now"))
            tools = [
                {
                    "name": r["name"],
                    "description": r["description"],
                    "inputSchema": r["inputSchema"],
                    "outputSchema": r.get("outputSchema", {}),
                    "annotations": r.get("annotations", {}),
                }
                for r in _cat.advertised(principal=principal, project=None, gate_invokable=gate_inv)
            ]
            return _finalize(
                200,
                _jsonrpc_result(mid, {"tools": tools}),
                verdict="admit",
                tool="tools/list",
                principal=principal,
                inject_request_id=False,
            )
        if rpc_method == "tools/call":
            p = msg.get("params") or {}
            name = str(p.get("name") or "")
            args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
            _tok_id = principal.get("token_id") if isinstance(principal, dict) else ""
            # Org-binding tools (which tenant this token acts as) — handled at the
            # SHARED auth home (project_root), ABOVE the per-tenant project layer.
            if name in _ORG_TOOLS:
                out = handle_org_tool(
                    name,
                    args,
                    home=project_root,
                    principal=principal,
                    token_id=_tok_id or "",
                    has=_has,
                )
                if "_error" in out:
                    return _finalize(
                        200,
                        _jsonrpc_error(mid, -32004, out["_error"], out.get("_detail")),
                        out["_error"],
                        verdict="refuse",
                        tool=name,
                        principal=principal,
                        inject_request_id=False,
                    )
                return _finalize(
                    200,
                    _jsonrpc_result(mid, _tool_call_result(out)),
                    verdict="admit",
                    tool=name,
                    principal=principal,
                    inject_request_id=False,
                )
            # Selector / catalog / GitHub control-plane tools (not gate.invoke).
            if name in PROJECT_TOOLS:
                out = handle_project_tool(
                    name,
                    args,
                    gate=gate,
                    home=_eff_home,
                    default_exec_root=_eff_default_exec,
                    token_id=_tok_id or "",
                    principal=principal,
                    has=_has,
                )
                if "_error" in out:
                    return _finalize(
                        200,
                        _jsonrpc_error(mid, -32004, out["_error"], out.get("_detail")),
                        out["_error"],
                        verdict="refuse",
                        tool=name,
                        principal=principal,
                        inject_request_id=False,
                    )
                return _finalize(
                    200,
                    _jsonrpc_result(mid, _tool_call_result(out)),
                    verdict="admit",
                    tool=name,
                    principal=principal,
                    inject_request_id=False,
                )
            # Resolve the SERVER-SIDE selected exec project for this token. Reads/
            # edits bind to THIS root (registry-selected), never CWD / project_root
            # / a raw path. None ⇒ the gate's configured default.
            _sel_exec = None
            try:
                from . import outer_gate_projects as _P

                _stp = _P.GateProjectStore()
                # No built-in default project: a tenant only has the projects it
                # registered/imported (empty until then).
                _cur = _stp.current(_eff_home, _tok_id or "")
                _sel_exec = _cur["root"] if _cur else None
                # Exec-root defense: the selected root MUST be registered under THIS
                # tenant's registry. By construction _stp.current(_eff_home) only
                # returns this tenant's selection, but verify explicitly so a stale/
                # foreign root can never reach execute().
                if _sel_exec and not any(
                    Path(p.get("root") or "") == Path(_sel_exec) for p in _stp.list(_eff_home)
                ):
                    _sel_exec = None
            except Exception:
                _sel_exec = None
            # A codenexus TENANT with no selected project ⇒ REFUSE — read/edit/run must
            # never fall back to the gate's own exec-project-root (the deploy repo);
            # every tenant starts empty and must project_select / register one first.
            # (Local/legacy gates — no tenant binding — keep their configured exec root;
            # selector + org tools are handled above and need no project.)
            if not _sel_exec and _tenant_id:
                return _finalize(
                    200,
                    _jsonrpc_error(
                        mid,
                        -32004,
                        "no_project_selected",
                        "select a project (project_select) or register one first",
                    ),
                    "no_project_selected",
                    verdict="refuse",
                    tool=name,
                    kind="mcp_tool",
                    principal=principal,
                    inject_request_id=False,
                )
            # execute() path: single canonical admission surface.
            cv = gate.execute(
                GateRequest(
                    tool_name=name,
                    kind="mcp_tool",
                    principal=principal,
                    project_root=str(project_root) if project_root else None,
                    tool_input=args,
                    exec_root=_sel_exec,
                ),
            )
            if cv.verdict == "pass":
                return _finalize(
                    200,
                    _jsonrpc_result(mid, _tool_call_result(cv.result)),
                    verdict="admit",
                    tool=name,
                    kind="mcp_tool",
                    principal=principal,
                    inject_request_id=False,
                    exec_project_root=cv.exec_project_root,
                    exec_project_id=cv.exec_project_id,
                )
            if cv.verdict == "confirmable_freeze":
                body = _jsonrpc_error(mid, -32003, cv.blocked_by, cv.reason)
                if cv.pending_action:
                    body["pending_action"] = cv.pending_action
                if cv.freeze_id:
                    body["freeze_id"] = cv.freeze_id
                return _finalize(
                    200,
                    body,
                    cv.blocked_by,
                    verdict="confirmable",
                    tool=name,
                    kind="mcp_tool",
                    principal=principal,
                    inject_request_id=False,
                    exec_project_root=cv.exec_project_root,
                    exec_project_id=cv.exec_project_id,
                )
            return _finalize(
                200,
                _jsonrpc_error(
                    mid,
                    -32001 if cv.blocked_by == "insufficient_scope" else -32003,
                    cv.blocked_by,
                    cv.reason,
                ),
                cv.blocked_by,
                verdict="refuse",
                tool=name,
                kind="mcp_tool",
                principal=principal,
                inject_request_id=False,
                exec_project_root=cv.exec_project_root,
                exec_project_id=cv.exec_project_id,
            )
        return _finalize(
            200,
            _jsonrpc_error(mid, -32601, "method not found", rpc_method),
            "mcp_method_not_found",
            verdict="refuse",
            principal=principal,
            inject_request_id=False,
        )

    # 4. Catalog — delegate to gate.discover.
    if route_class == RC_CATALOG:
        res = gate.discover(principal)
        if not res.ok:
            return _finalize(
                _BLOCKED_HTTP.get(res.blocked_by, 403),
                {"error": res.blocked_by, "detail": res.reason},
                res.blocked_by,
                verdict="refuse",
                principal=principal,
            )
        return _finalize(200, {"tools": res.result}, verdict="admit", principal=principal)

    # 5. Invoke — hand the WHOLE decision to gate.invoke (Tier-R only; trust,
    #    auth, project-binding, mandatory gate audit all enforced there).
    #    AUDIT-ORDERING: the transport audit below runs AFTER gate.invoke. Safe
    #    TODAY because only read-only Tier-R executes (Tier-M/Tier-A are refused,
    #    nothing mutates). Before wiring Tier-M/Tier-A EXECUTION, switch to the
    #    intent-before / gate-audit / result-after discipline (see module
    #    docstring "AUDIT-ORDERING DOCTRINE") so a mutation can never run unaudited.
    if route_class == RC_INVOKE:
        # Tenant parity (Pillar 2, 2026-06-15): /v1/invoke (the GPT-Action body ingress)
        # does NOT carry the per-tenant selected-project law that /v1/mcp tools/call
        # enforces (tenant home _eff_home, registered selected exec root _sel_exec, and
        # the no_project_selected refusal). Rather than duplicate that law on a second
        # execution path where it could silently drift out of parity, a TENANT-BOUND
        # request is refused here and must use /v1/mcp — the canonical, fully-gated,
        # project-scoped tenant surface. Local/legacy (no tenant_id) is unaffected.
        if _tenant_id:
            return _finalize(
                403,
                {
                    "error": "tenant_invoke_use_mcp",
                    "detail": "tenant requests must use the /v1/mcp tools/call surface "
                    "(the project-scoped, fully-gated path); /v1/invoke is not tenant-scoped",
                },
                "tenant_invoke_use_mcp",
                verdict="refuse",
                principal=principal,
            )
        # Parse the JSON body once (used by both forms).
        parsed: dict = {}
        if body:
            raw = body.decode("utf-8") if isinstance(body, bytes) else body
            try:
                _p = json.loads(raw) if raw.strip() else {}
                parsed = _p if isinstance(_p, dict) else {}
            except Exception:
                return _finalize(
                    400,
                    {"error": "invalid_json_body"},
                    "invalid_json_body",
                    verdict="refuse",
                    principal=principal,
                )
        if params.get("name"):
            # Path form: /v1/tools/{kind}/{name}:invoke — body is the arguments.
            tool, kind = params["name"], params["kind"]
            tool_input = parsed
        else:
            # Body form: /v1/invoke — {kind?, name, arguments}. (GPT-Action ingress)
            tool = str(parsed.get("name") or "")
            kind = str(parsed.get("kind") or "mcp_tool")
            _args = parsed.get("arguments")
            tool_input = _args if isinstance(_args, dict) else {}
            if not tool:
                return _finalize(
                    400,
                    {"error": "missing_tool_name"},
                    "missing_tool_name",
                    verdict="refuse",
                    kind=kind,
                    principal=principal,
                )
        req = GateRequest(
            tool_name=tool,
            kind=kind,
            principal=principal,
            project_root=str(project_root) if project_root else None,
            tool_input=tool_input,
        )
        res = gate.invoke(req)
        if not res.ok:
            return _finalize(
                _BLOCKED_HTTP.get(res.blocked_by, 403),
                {"error": res.blocked_by, "detail": res.reason},
                res.blocked_by,
                verdict="refuse",
                tool=tool,
                kind=kind,
                principal=principal,
                exec_project_root=res.exec_project_root,
                exec_project_id=res.exec_project_id,
            )
        return _finalize(
            200,
            {"ok": True, "result": res.result},
            verdict="admit",
            tool=tool,
            kind=kind,
            principal=principal,
            exec_project_root=res.exec_project_root,
            exec_project_id=res.exec_project_id,
        )

    # Unreachable (classify only returns known classes), but fail closed.
    return _finalize(
        403,
        {"error": "unhandled_route_class"},
        "unhandled",
        verdict="refuse",
        force_authority=True,
    )


# ── HTTP server (loopback-only, disabled by default) ────────────────────────
def _assert_serve_trust_or_refuse() -> None:
    """SHA-match start guard (2026-05-31): a SIGNED gate must not serve code
    whose fingerprint != its signed manifest. A half-deploy / stale rsync /
    tamper makes ``verify_release`` fail with the trust artifacts PRESENT —
    we refuse to start (fail closed) rather than boot and crash-loop on
    mismatched code (the ``OuterGate.execute`` stale-skew incident). An
    UNSIGNED floor (artifacts absent → dev / source install) is allowed:
    that's the legitimate dev-iteration mode, not a tampered release.
    """
    from .release_trust import verify_release

    rt = verify_release()
    if not rt.ok and not rt.reason.startswith("unsigned"):
        raise PermissionError(
            "outer_gate_transport: refusing to serve — signed-release "
            f"verification FAILED: {rt.reason}. The gate's code does not match "
            "its signed manifest (half-deploy / stale sync / tamper); failing "
            "closed instead of serving mismatched code. Redeploy a coherent "
            "signed release.",
        )


def serve(
    gate: OuterGate,
    *,
    project_root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    auth_resolver: Callable[[dict, Path | None], AuthOutcome] | None = None,
    transport_audit: Callable[[dict], None] | None = None,
):
    """Start a loopback HTTP server bound to `host`. REFUSES a non-loopback bind
    and REFUSES to start unless the gate is enabled. Returns the running server
    (call .shutdown() to stop). Disabled-by-default: a caller must opt in, and a
    public host raises before any socket is opened.

    This is a thin binding; all policy lives in `dispatch`. Not started anywhere
    automatically — MCP/local stdio is untouched.
    """
    if not is_loopback_bind(host):
        raise PermissionError(
            f"outer_gate_transport: refusing non-loopback bind {host!r} "
            f"(loopback-only is the first sealed deployment mode)",
        )
    if not gate.is_enabled():
        raise PermissionError(
            "outer_gate_transport: refusing to serve — outer_gate.enabled is "
            "False (the gateway is disabled by default)",
        )
    # SHA-match start guard: a signed gate whose code != its manifest fails
    # closed here instead of booting and crash-looping on mismatched code.
    _assert_serve_trust_or_refuse()

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default stderr logging
            return

        def _run(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            resp = dispatch(
                method,
                self.path,
                dict(self.headers),
                raw,
                gate=gate,
                project_root=project_root,
                bind_host=host,
                auth_resolver=auth_resolver,
                transport_audit=transport_audit,
            )
            if resp.raw_body is not None:
                payload = resp.raw_body.encode("utf-8")
                ctype = resp.content_type
            elif resp.status == 202 and not resp.body:
                payload, ctype = b"", "application/json"
            else:
                payload = json.dumps(resp.body).encode("utf-8")
                ctype = "application/json"
            self.send_response(resp.status)
            if payload:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            # Security headers baseline (2026-05-27).
            # Every response carries these — defends the login page
            # against iframe phishing, MIME confusion, mixed-content
            # downgrades, and referer leakage. Per-response headers
            # below can append (e.g. Cache-Control) but should not
            # override these baseline protections.
            for sec_hk, sec_hv in _security_headers(ctype if payload else "").items():
                self.send_header(sec_hk, sec_hv)
            for hk, hv in (resp.headers or {}).items():
                self.send_header(hk, str(hv))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self):
            self._run("GET")

        def do_POST(self):
            self._run("POST")

        def do_PUT(self):
            self._run("PUT")

        def do_DELETE(self):
            self._run("DELETE")

    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
