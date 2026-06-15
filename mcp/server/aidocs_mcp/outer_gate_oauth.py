"""OAuth 2.1 + PKCE authorization server for the ChatGPT custom MCP connector.

This is the MINIMAL authorization layer ChatGPT's native MCP connector requires.
It does NOT replace or weaken the Outer Gate: an OAuth access token IS an ordinary
scoped Outer Gate token (minted via OuterGateTokenStore.mint_for_operator), so all
existing law still applies on every /v1/mcp call — signed package trust, scope,
audience, durable audit, Tier-R-only executor, and Tier-M/A/control refusals. The
OAuth flow only decides WHO may be handed such a token, after a human login.

Sealed properties (all fail-closed):
  * Authorization Code + PKCE (S256) ONLY. No implicit, no password grant, no
    plain code_challenge_method.
  * Manual client registration only (no DCR here). A client has an exact
    redirect_uri allowlist; a non-matching redirect_uri is rejected and NEVER
    redirected to.
  * resource (RFC 8707) and audience are bound to the MCP resource; the issued
    token carries the resource so it only works at /v1/mcp.
  * scopes are a subset of VALID_SCOPES AND of the operator-registered client
    scope (requested ∩ client.scope); anything else rejected. tier_m_edit is
    grantable only to a client the operator explicitly registered for it.
  * codes are single-use, short-lived (60s), hashed at rest, bound to client +
    redirect_uri + code_challenge + scope + resource + the authenticated user.
  * the token endpoint re-checks client identity (secret for confidential
    clients; PKCE for public), redirect_uri match, code_verifier, and resource.

Discovery: as_metadata() (RFC 8414) + protected_resource_metadata() (RFC 9728).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .outer_gate_token_store import (
    AUDIENCE_CODENEXUS,
    SCOPE_CATALOG,
    SCOPE_PROJECT_IMPORT,
    SCOPE_PROJECT_RUN,
    SCOPE_STATUS,
    SCOPE_TIER_M_EDIT,
    SCOPE_TIER_R_INVOKE,
)

_CODE_TTL_SECONDS = 60
_DEFAULT_SCOPES = (SCOPE_CATALOG, SCOPE_STATUS, SCOPE_TIER_R_INVOKE)
# OAuth (ChatGPT connector) grantable scopes. OAuth is the TRANSPORT, AIDOCS law
# is the AUTHORITY: a scope being OAuth-grantable does NOT weaken any gate law —
# signed trust, selected-project binding, protected-path refusals, two-phase
# confirm, audit, and post-edit reindex all still run on every /v1/mcp call.
#
# tier_m_edit (real source edits) IS grantable here, but ONLY by intersection
# with the OPERATOR-registered client scope (see validate_authorize: requested ∩
# client.scope). It is therefore NOT handed to unknown/unauthorized clients and
# NOT granted by default — the operator must explicitly register tier_m_edit on
# the specific connector client. It remains a DISTINCT grant: it does not imply
# project_import (clone+index) or project_run (detached shell), and neither of
# those imply it.
VALID_SCOPES = frozenset(
    {
        SCOPE_CATALOG,
        SCOPE_STATUS,
        SCOPE_TIER_R_INVOKE,
        SCOPE_TIER_M_EDIT,
        SCOPE_PROJECT_IMPORT,
        SCOPE_PROJECT_RUN,
    },
)
AUTH_METHOD_NONE = "none"  # public client (PKCE only)
AUTH_METHOD_SECRET_POST = "client_secret_post"
_VALID_AUTH_METHODS = frozenset({AUTH_METHOD_NONE, AUTH_METHOD_SECRET_POST})


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_now() -> str:
    return _iso(_now())


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_s256_verify(code_verifier: str, code_challenge: str) -> bool:
    """RFC 7636 S256: challenge == base64url(sha256(verifier)), no padding.
    Fail-closed on any missing/short input (verifier must be 43–128 chars).
    """
    if not code_verifier or not code_challenge:
        return False
    if not (43 <= len(code_verifier) <= 128):
        return False
    expected = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return secrets.compare_digest(expected, code_challenge)


def normalize_scopes(requested: str | None) -> tuple[frozenset[str], str | None]:
    """Map a space/comma-separated OAuth scope string to the gate vocabulary.
    Returns (scopes, error). Unknown scope → error (fail-closed). Empty → default
    read-only set. 'openid'/'profile'/'email' are tolerated and dropped (we are
    not an OIDC provider) so a connector that always asks for them still works.
    """
    if not requested or not requested.strip():
        return frozenset(_DEFAULT_SCOPES), None
    raw = [s for s in requested.replace(",", " ").split() if s]
    out: set[str] = set()
    for s in raw:
        if s in ("openid", "profile", "email"):
            continue
        if s not in VALID_SCOPES:
            return frozenset(), f"invalid_scope:{s}"
        out.add(s)
    if not out:
        out = set(_DEFAULT_SCOPES)
    return frozenset(out), None


@dataclass(frozen=True)
class AuthorizeRequest:
    """A validated /authorize request, ready to show a login + issue a code."""

    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    scope: frozenset
    resource: str
    # Diagnostics (non-secret): what the client asked for vs what it is
    # registered for vs what will be issued. Surfaced for audit; never includes
    # client secrets / codes / tokens.
    requested_scope: frozenset = frozenset()
    client_scope: frozenset = frozenset()

    def scope_diag(self) -> dict:
        """Safe scope-decision summary for audit logging (no secrets)."""
        return {
            "requested_scope": sorted(self.requested_scope),
            "client_scope": sorted(self.client_scope),
            "issued_scope": sorted(self.scope),
        }


@dataclass(frozen=True)
class AuthorizeError:
    """A rejected /authorize request. redirect_ok=True ⇒ the error MAY be sent
    back to the (validated) redirect_uri as ?error=...; False ⇒ the client/
    redirect itself is untrusted, so render a plain error, NEVER redirect.
    """

    error: str
    error_description: str
    redirect_ok: bool
    redirect_uri: str = ""
    state: str = ""


@dataclass(frozen=True)
class TokenResult:
    ok: bool
    body: dict = field(default_factory=dict)  # JSON body (success or error)
    status: int = 200


class OAuthStore:
    """Manual OAuth client + authorization-code store, co-located with identity."""

    def db_path(self, project_root: Path) -> Path:
        from .identity_store import IdentityStore

        return IdentityStore().db_path(project_root)

    def init_db(self, project_root: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret_hash TEXT NOT NULL DEFAULT '',
                    redirect_uris_json TEXT NOT NULL,
                    auth_method TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    force_full_scope INTEGER NOT NULL DEFAULT 0
                )
                """,
            )
            # Migration: add force_full_scope to a pre-existing table (idempotent).
            try:
                conn.execute(
                    "ALTER TABLE oauth_clients "
                    "ADD COLUMN force_full_scope INTEGER NOT NULL DEFAULT 0",
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    tenant_id TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            # Migration: bind the codenexus tenant (org) onto the auth code so the
            # minted access/refresh tokens inherit it (multi-tenant isolation).
            try:
                conn.execute(
                    "ALTER TABLE oauth_codes ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''",
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()

    # ── manual client registration ─────────────────────────────────────────
    def register_client(
        self,
        project_root: Path,
        *,
        redirect_uris: list[str],
        auth_method: str = AUTH_METHOD_NONE,
        scope: list[str] | None = None,
        client_secret: str | None = None,
    ) -> dict:
        """Register a client MANUALLY (no DCR). Returns {client_id,
        client_secret?}. A public client (auth_method 'none') gets no secret and
        MUST use PKCE. A confidential client gets a generated secret unless one is
        supplied. redirect_uris is an EXACT allowlist.
        """
        if auth_method not in _VALID_AUTH_METHODS:
            raise ValueError(f"invalid auth_method {auth_method!r}")
        uris = [u.strip() for u in (redirect_uris or []) if u.strip()]
        if not uris:
            raise ValueError("at least one redirect_uri is required")
        for u in uris:
            if not (
                u.startswith("https://")
                or u.startswith("http://127.0.0.1")
                or u.startswith("http://localhost")
            ):
                raise ValueError(f"redirect_uri must be https (or loopback): {u!r}")
        scope_set = sorted(set(scope) & VALID_SCOPES) if scope else sorted(_DEFAULT_SCOPES)
        if not scope_set:
            raise ValueError("scope must be a non-empty subset of the gate vocabulary")
        secret_plain = None
        secret_hash = ""
        if auth_method == AUTH_METHOD_SECRET_POST:
            secret_plain = client_secret or ("ogc_" + secrets.token_urlsafe(32))
            secret_hash = _sha(secret_plain)
        client_id = "ogcid_" + secrets.token_urlsafe(16)
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, client_secret_hash, "
                "redirect_uris_json, auth_method, scope_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    client_id,
                    secret_hash,
                    json.dumps(uris),
                    auth_method,
                    json.dumps(scope_set),
                    _iso_now(),
                ),
            )
            conn.commit()
        out = {
            "client_id": client_id,
            "auth_method": auth_method,
            "redirect_uris": uris,
            "scope": scope_set,
        }
        if secret_plain is not None:
            out["client_secret"] = secret_plain
        return out

    def update_client_scope(
        self,
        project_root: Path,
        client_id: str,
        *,
        scope: list[str],
    ) -> dict:
        """Operator-only: replace a registered client's scope allowlist (e.g. add
        tier_m_edit to the ChatGPT connector) WITHOUT minting a new client_id, so
        the already-configured connector keeps working. The new scope is bounded
        to VALID_SCOPES (an unknown scope is dropped, never silently widened past
        the gate vocabulary) and must be non-empty. Fail-closed: unknown client
        raises.
        """
        scope_set = sorted(set(scope) & VALID_SCOPES) if scope else []
        if not scope_set:
            raise ValueError("scope must be a non-empty subset of the gate vocabulary")
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE oauth_clients SET scope_json=? WHERE client_id=? AND disabled=0",
                (json.dumps(scope_set), client_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"unknown or disabled client_id {client_id!r}")
        return {"client_id": client_id, "scope": scope_set}

    def set_force_full_scope(
        self,
        project_root: Path,
        client_id: str,
        *,
        enabled: bool,
    ) -> dict:
        """Operator-only first-party override. When enabled, an /authorize request
        from THIS client is issued the client's FULL registered scope even if the
        request explicitly asks for a narrower set — for connectors (ChatGPT's
        native MCP client) that hardcode a read-only scope request and give the
        operator no way to widen it. Still bounded by the client's registered
        scope ∩ VALID_SCOPES (never grants beyond registration); operator-set
        only; per-client (never global). Fail-closed: unknown client raises.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE oauth_clients SET force_full_scope=? WHERE client_id=? AND disabled=0",
                (1 if enabled else 0, client_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"unknown or disabled client_id {client_id!r}")
        return {"client_id": client_id, "force_full_scope": bool(enabled)}

    def list_clients(self, project_root: Path) -> list[dict]:
        """Operator inventory: active clients with their id, auth method, redirect
        allowlist, scope, and the first-party force_full_scope override. (Secrets
        are never returned — only the hash exists at rest and it is not surfaced.)
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT client_id, auth_method, redirect_uris_json, scope_json, "
                "force_full_scope FROM oauth_clients WHERE disabled=0 "
                "ORDER BY created_at",
            ).fetchall()
        return [
            {
                "client_id": r["client_id"],
                "auth_method": r["auth_method"],
                "redirect_uris": json.loads(r["redirect_uris_json"]),
                "scope": json.loads(r["scope_json"]),
                "force_full_scope": bool(r["force_full_scope"]),
            }
            for r in rows
        ]

    def get_client(self, project_root: Path, client_id: str) -> dict | None:
        if not client_id:
            return None
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        if row is None or int(row["disabled"]):
            return None
        keys = row.keys()
        return {
            "client_id": row["client_id"],
            "client_secret_hash": row["client_secret_hash"],
            "redirect_uris": json.loads(row["redirect_uris_json"]),
            "auth_method": row["auth_method"],
            "scope": json.loads(row["scope_json"]),
            "force_full_scope": bool(row["force_full_scope"])
            if "force_full_scope" in keys
            else False,
        }

    # ── authorization codes (single-use, short-lived) ──────────────────────
    def issue_code(
        self,
        project_root: Path,
        req: AuthorizeRequest,
        *,
        user_id: str,
        role: str,
        tenant_id: str = "",
    ) -> str:
        code = "ogac_" + secrets.token_urlsafe(32)
        issued = _now()
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, "
                "code_challenge, scope_json, resource, user_id, role, issued_at, "
                "expires_at, tenant_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _sha(code),
                    req.client_id,
                    req.redirect_uri,
                    req.code_challenge,
                    json.dumps(sorted(req.scope)),
                    req.resource,
                    user_id,
                    role,
                    _iso(issued),
                    _iso(issued + _CODE_TTL_SECONDS),
                    str(tenant_id or ""),
                ),
            )
            conn.commit()
        return code

    def consume_code(self, project_root: Path, code: str) -> dict | None:
        """Atomically mark a code consumed and return its row, or None if it is
        unknown/expired/already-consumed (fail-closed, single-use).
        """
        if not code:
            return None
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM oauth_codes WHERE code_hash=?",
                (_sha(code),),
            ).fetchone()
            if row is None or int(row["consumed"]):
                return None
            if str(row["expires_at"]) <= _iso_now():
                return None
            conn.execute("UPDATE oauth_codes SET consumed=1 WHERE code_hash=?", (_sha(code),))
            conn.commit()
            return dict(row)


# ── discovery metadata (RFC 8414 / RFC 9728) ────────────────────────────────
def as_metadata(issuer: str) -> dict:
    issuer = issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [AUTH_METHOD_NONE, AUTH_METHOD_SECRET_POST],
        "scopes_supported": sorted(VALID_SCOPES),
        "response_modes_supported": ["query"],
    }


def protected_resource_metadata(issuer: str, resource: str) -> dict:
    issuer = issuer.rstrip("/")
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": sorted(VALID_SCOPES),
        "bearer_methods_supported": ["header"],
    }


# ── /authorize validation ───────────────────────────────────────────────────
def validate_authorize(
    store: OAuthStore,
    project_root: Path,
    params: dict,
    *,
    mcp_resource: str,
) -> AuthorizeRequest | AuthorizeError:
    """Validate an /authorize query. Unknown client OR unregistered redirect_uri
    ⇒ AuthorizeError(redirect_ok=False) (NEVER redirect to an untrusted URI).
    Other errors ⇒ redirect_ok=True (send error to the validated redirect).
    """
    client_id = str(params.get("client_id") or "")
    redirect_uri = str(params.get("redirect_uri") or "")
    client = store.get_client(project_root, client_id)
    if client is None:
        return AuthorizeError("invalid_client", "unknown client_id", False)
    if redirect_uri not in client["redirect_uris"]:
        return AuthorizeError(
            "invalid_request",
            "redirect_uri not registered for this client",
            False,
        )
    # From here, errors may be redirected to the (now-trusted) redirect_uri.
    state = str(params.get("state") or "")

    def _err(code: str, desc: str) -> AuthorizeError:
        return AuthorizeError(code, desc, True, redirect_uri=redirect_uri, state=state)

    if str(params.get("response_type") or "") != "code":
        return _err("unsupported_response_type", "only response_type=code")
    if str(params.get("code_challenge_method") or "") != "S256":
        return _err("invalid_request", "code_challenge_method must be S256")
    code_challenge = str(params.get("code_challenge") or "")
    if not (43 <= len(code_challenge) <= 128):
        return _err("invalid_request", "missing/invalid code_challenge")
    # The OPERATOR-registered client scope bounds everything the client may ever
    # be issued (intersected with the gate vocabulary defensively).
    client_scope = frozenset(client.get("scope") or []) & VALID_SCOPES
    raw_scope = params.get("scope")
    if not raw_scope or not str(raw_scope).strip():
        # OMITTED/empty scope (a connector that sends none): default the requested
        # scope to the FULL operator-registered client scope — NOT the hardcoded
        # read-only _DEFAULT_SCOPES. Bug fix 2026-05-25: the old path ran
        # normalize_scopes(None) → read-only default → ∩ client, so an
        # edit-registered connector that omitted scope silently collapsed to
        # read-only and ai_str_replace stayed hidden.
        requested = client_scope
    else:
        requested, scope_err = normalize_scopes(raw_scope)
        if scope_err:
            return _err("invalid_scope", scope_err)
        requested = frozenset(requested)
    # First-party override (operator-set, per-client): a connector that HARDCODES
    # a narrower (read-only) scope request and gives the operator no way to widen
    # it is upgraded to its full registered scope. Still bounded by client_scope ∩
    # VALID_SCOPES — never grants beyond what the operator registered. Only this
    # explicitly-flagged client is affected; all others keep requested ∩ client.
    if client.get("force_full_scope"):
        scope = client_scope
    else:
        # Issued = requested ∩ client-registered ∩ VALID_SCOPES (defense in depth).
        scope = requested & client_scope
    if not scope:
        return _err("invalid_scope", "no requested scope is registered for this client")
    # Resource indicator: if supplied it MUST be the MCP resource; if omitted we
    # bind to the MCP resource anyway (single-resource server).
    resource = str(params.get("resource") or "") or mcp_resource
    if resource != mcp_resource:
        return _err("invalid_target", "resource must be the MCP resource")
    return AuthorizeRequest(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        scope=scope,
        resource=resource,
        requested_scope=requested,
        client_scope=client_scope,
    )


# ── /token exchange ─────────────────────────────────────────────────────────
def _refresh_grant(
    store: OAuthStore,
    project_root: Path,
    params: dict,
    *,
    mcp_resource: str,
    ttl_seconds: int,
) -> TokenResult:
    """OAuth refresh_token grant: validate the client, ROTATE the single-use
    refresh token, and mint a fresh access + refresh pair with the SAME scope and
    resource. Lets a connector keep a live session past the 1h access TTL without
    re-authorizing. Fail-closed on every check."""

    def _err(code: str, desc: str, status: int = 400) -> TokenResult:
        return TokenResult(False, {"error": code, "error_description": desc}, status)

    client_id = str(params.get("client_id") or "")
    client = store.get_client(project_root, client_id)
    if client is None:
        return _err("invalid_client", "unknown client_id", 401)
    if client["auth_method"] == AUTH_METHOD_SECRET_POST:
        presented = str(params.get("client_secret") or "")
        if not presented or _sha(presented) != client["client_secret_hash"]:
            return _err("invalid_client", "bad client_secret", 401)

    from .outer_gate_token_store import OuterGateTokenStore

    ts = OuterGateTokenStore()
    claims = ts.rotate_refresh(project_root, str(params.get("refresh_token") or ""))
    if claims is None:
        return _err("invalid_grant", "refresh token unknown/expired/used")
    scope = claims["scope"] & VALID_SCOPES
    if not scope:
        return _err("invalid_scope", "no valid scope on refresh token")
    # Same authority split as the code grant: in webmcp service mode (codenexus
    # DSN) the original authorize enforced login + entitlement, so re-issue via the
    # grant mint (an entitled USER is legitimate); a local/dev gate keeps the
    # operator mint. The bound tenant_id rides through the rotation unchanged.
    import os as _os

    _webmcp_mode = bool(_os.environ.get("AIDOCS_CODENEXUS_DSN", "").strip())
    _mint = ts.mint_for_grant if _webmcp_mode else ts.mint_for_operator
    _claim_tenant = str(claims.get("tenant_id") or "")
    try:
        minted = _mint(
            project_root,
            user_id=claims["user_id"],
            role=claims["role"],
            audience=AUDIENCE_CODENEXUS,
            scope=sorted(scope),
            ttl_seconds=ttl_seconds,
            resource=mcp_resource,
            tenant_id=_claim_tenant,
        )
    except (PermissionError, ValueError) as exc:
        return _err("invalid_grant", f"cannot re-issue from refresh: {exc}")
    new_refresh = ts.mint_refresh(
        project_root,
        user_id=claims["user_id"],
        role=claims["role"],
        scope=sorted(scope),
        audience=AUDIENCE_CODENEXUS,
        resource=mcp_resource,
        tenant_id=_claim_tenant,
    )
    return TokenResult(
        True,
        {
            "access_token": minted.token,
            "token_type": "Bearer",
            "expires_in": int(ttl_seconds),
            "scope": " ".join(sorted(scope)),
            "refresh_token": new_refresh,
        },
    )


def exchange_token(
    store: OAuthStore,
    project_root: Path,
    params: dict,
    *,
    mcp_resource: str,
    ttl_seconds: int = 3600,
) -> TokenResult:
    """Authorization-code + PKCE token exchange. Mints a scoped Outer Gate token
    bound to the MCP resource. Every check fail-closed with an OAuth error.
    """

    def _err(code: str, desc: str, status: int = 400) -> TokenResult:
        return TokenResult(False, {"error": code, "error_description": desc}, status)

    grant = str(params.get("grant_type") or "")
    if grant == "refresh_token":
        return _refresh_grant(
            store, project_root, params, mcp_resource=mcp_resource, ttl_seconds=ttl_seconds
        )
    if grant != "authorization_code":
        return _err("unsupported_grant_type", "only authorization_code or refresh_token")
    client_id = str(params.get("client_id") or "")
    client = store.get_client(project_root, client_id)
    if client is None:
        return _err("invalid_client", "unknown client_id", 401)
    # Client authentication per its registered method.
    if client["auth_method"] == AUTH_METHOD_SECRET_POST:
        presented = str(params.get("client_secret") or "")
        if not presented or _sha(presented) != client["client_secret_hash"]:
            return _err("invalid_client", "bad client_secret", 401)

    code = str(params.get("code") or "")
    row = store.consume_code(project_root, code)
    if row is None:
        return _err("invalid_grant", "code unknown/expired/used")
    if str(row["client_id"]) != client_id:
        return _err("invalid_grant", "code was issued to a different client")
    if str(row["redirect_uri"]) != str(params.get("redirect_uri") or ""):
        return _err("invalid_grant", "redirect_uri mismatch")
    # PKCE: the verifier must match the challenge bound to the code.
    if not pkce_s256_verify(str(params.get("code_verifier") or ""), str(row["code_challenge"])):
        return _err("invalid_grant", "PKCE verification failed")
    # Resource: a supplied resource must match; otherwise inherit the code's.
    req_resource = str(params.get("resource") or "") or str(row["resource"])
    if req_resource != mcp_resource or str(row["resource"]) != mcp_resource:
        return _err("invalid_target", "resource mismatch")
    scope = frozenset(json.loads(row["scope_json"])) & VALID_SCOPES
    if not scope:
        return _err("invalid_scope", "no valid scope on code")

    # Mint a real scoped Outer Gate token (existing law: audience + scope +
    # expiry + revocation + sha256-at-rest), resource-bound to the MCP endpoint.
    from .outer_gate_token_store import OuterGateTokenStore

    ts = OuterGateTokenStore()
    # Authority for the minted token: in webmcp service mode (codenexus DSN
    # configured) the authorize step already enforced codenexus login + webmcp
    # entitlement, so an entitled org owner/operator (platform role USER) is
    # legitimately granted — use the grant mint (no platform-admin gate). On a
    # local/dev gate (no DSN) there is no entitlement gate, so keep the operator
    # mint's admin/super_admin requirement (unchanged).
    import os as _os

    _webmcp_mode = bool(_os.environ.get("AIDOCS_CODENEXUS_DSN", "").strip())
    _mint = ts.mint_for_grant if _webmcp_mode else ts.mint_for_operator
    _code_tenant = str(row["tenant_id"] or "") if "tenant_id" in row.keys() else ""
    minted = _mint(
        project_root,
        user_id=str(row["user_id"]),
        role=str(row["role"]),
        audience=AUDIENCE_CODENEXUS,
        scope=sorted(scope),
        ttl_seconds=ttl_seconds,
        resource=mcp_resource,
        tenant_id=_code_tenant,
    )
    refresh = ts.mint_refresh(
        project_root,
        user_id=str(row["user_id"]),
        role=str(row["role"]),
        scope=sorted(scope),
        audience=AUDIENCE_CODENEXUS,
        resource=mcp_resource,
        tenant_id=_code_tenant,
    )
    return TokenResult(
        True,
        {
            "access_token": minted.token,
            "token_type": "Bearer",
            "expires_in": int(ttl_seconds),
            "scope": " ".join(sorted(scope)),
            "refresh_token": refresh,
        },
    )
