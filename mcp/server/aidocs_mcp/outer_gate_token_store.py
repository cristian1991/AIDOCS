"""Outer Gate scoped tokens — audience + scope + expiry + revocation.

The transport's first cut validated a bare IdentityStore bearer token (identity
only). For public exposure (codenexus.cloud) that is not enough: a token must
carry an AUDIENCE (so an identity token for some other surface can't be replayed
at the gate) and a SCOPE (so a token can be limited to catalog/status/Tier-R read
and NOTHING else), be SHORT-LIVED (expiry), and be REVOCABLE.

Design (canonical, no new crypto infra): a token is an opaque random bearer; only
its sha256 hash is stored, alongside its claims (audience, scope, expiry,
minted-by, revoked). Validation = hash lookup + not-expired + not-revoked +
audience match + scope coverage. Revocation = flip a flag (append-only history is
the identity DB's job; here we keep a simple revoked column + revoked_at).

MINTING REQUIRES AUTHENTICATED OPERATOR/ADMIN AUTHORITY: `mint_for_operator`
takes an authenticated User and refuses unless the role is admin/super_admin. NLP
/ agents / unauthenticated callers can never mint — this is a human-admin act.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The ONE audience the codenexus gate accepts. A token minted for any other
# audience is inert at this transport.
AUDIENCE_CODENEXUS = "codenexus-outer-gate"

# The scope vocabulary. A token's scope is a subset; a route requires a subset.
SCOPE_CATALOG = "catalog"  # GET /v1/tools (discover) + /v1/health-class reads
SCOPE_STATUS = "status"  # read-only status routes
SCOPE_TIER_R_INVOKE = "tier_r_invoke"  # POST invoke of read-proven Tier-R tools
# Tier-M SURGICAL edit (str_replace/edit_lines only) — every edit is two-phase
# confirmation-gated + fully audited; never raw shell / broad writes. Deliberately
# NOT in the OAuth read-only default; minted only for an operator.
SCOPE_TIER_M_EDIT = "tier_m_edit"
# Project import/sync (clone+bootstrap+index a registered repo). A DISTINCT,
# narrower grant than tier_m_edit: importing a project is NOT editing source, so
# a ChatGPT session can be granted project_import WITHOUT any source-edit power.
SCOPE_PROJECT_IMPORT = "project_import"
# Project run (detached shell via the canonical ai_run + its full destructive
# law). A DISTINCT grant: project_run does NOT imply tier_m_edit (source edits) or
# project_import (repo registration). Running diagnostics ≠ editing ≠ importing.
SCOPE_PROJECT_RUN = "project_run"
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

# Roles permitted to MINT a gate token (authenticated human admin authority).
_MINT_ROLES = frozenset({"admin", "super_admin"})

_DEFAULT_TTL_SECONDS = 3600  # short-lived by default (1h)
_DEFAULT_REFRESH_TTL_SECONDS = 30 * 24 * 3600  # refresh tokens live 30 days


def _resolve_codenexus_minter(user_id: str) -> dict | None:
    """Re-resolve a token's minter against codenexus — the authority for codenexus-
    authenticated webmcp tokens, whose ``minted_by_user_id`` is a codenexus user id
    (not a local identity row). Returns ``{"user_id","role","entitled"}`` (LIVE values)
    or None. No DSN configured ⇒ None (this is a local-only gate). Fail-closed. The
    entitled field powers the validate-time ``webmcp_entitlement_revoked`` re-check
    (entitled = has ANY webmcp-entitled org)."""
    import os

    dsn = os.environ.get("AIDOCS_CODENEXUS_DSN", "").strip()
    uid = str(user_id or "").strip()
    if not dsn or not uid:
        return None
    try:
        from .codenexus_identity import CodenexusPostgresResolver

        res = CodenexusPostgresResolver(dsn=dsn).resolve_minter_identity(uid)
        if not res:
            return None
        return {
            "user_id": res["user_id"],
            "role": res["role"],
            "entitled": bool(res.get("entitled")),
        }
    except Exception:
        return None


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_now() -> str:
    return _iso(time.time())


@dataclass(frozen=True)
class MintedToken:
    token: str  # plaintext — returned ONCE, never stored
    token_id: str
    audience: str
    scope: tuple[str, ...]
    minted_by: str
    expires_at: str


@dataclass(frozen=True)
class TokenValidation:
    """Result of validating a presented token. ok=True ⇒ admit-eligible with
    these claims; otherwise reason names exactly why it failed (fail-closed).
    """

    ok: bool
    reason: str = ""  # no_token|unknown_token|expired|revoked|wrong_audience|tenant_changed|webmcp_entitlement_revoked
    user_id: str = ""
    role: str = ""
    scope: frozenset = frozenset()
    audience: str = ""
    token_id: str = ""
    resource: str = ""  # RFC 8707 binding ('' = unbound/legacy)
    tenant_id: str = ""  # codenexus org (orgOwnerId or self) — the isolation key


class OuterGateTokenStore:
    """SQLite-backed scoped-token store, co-located with the identity DB."""

    def __init__(self) -> None:
        pass

    def db_path(self, project_root: Path) -> Path:
        # Co-locate with identity (same canonical control-plane DB location).
        from .identity_store import IdentityStore

        return IdentityStore().db_path(project_root)

    def init_db(self, project_root: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outer_gate_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    audience TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    minted_by_user_id TEXT NOT NULL,
                    minted_by_role TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    revoked_at TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outer_gate_tokens_hash "
                "ON outer_gate_tokens(token_hash)",
            )
            # RFC 8707 resource binding (added for the OAuth bridge). Empty for
            # directly-minted CLI/Action tokens (unbound, legacy behavior); set to
            # the protected-resource URL for OAuth-issued tokens so they only work
            # at that resource. Migrate existing DBs in place.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(outer_gate_tokens)").fetchall()}
            if "resource" not in cols:
                conn.execute(
                    "ALTER TABLE outer_gate_tokens ADD COLUMN resource TEXT NOT NULL DEFAULT ''",
                )
            # tenant_id: the codenexus org (orgOwnerId or self id) BOUND at mint.
            # Empty on legacy rows; validate re-resolves the live tenant and refuses
            # tenant_changed when a bound non-empty tenant no longer matches.
            if "tenant_id" not in cols:
                conn.execute(
                    "ALTER TABLE outer_gate_tokens ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''",
                )
            # Refresh tokens (OAuth refresh grant). Single-use: rotated on use so
            # a connector keeps a live session past the access token's 1h TTL
            # without re-authorizing. sha256-at-rest like access tokens.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outer_gate_refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    resource TEXT NOT NULL DEFAULT '',
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    tenant_id TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            rcols = {
                r[1] for r in conn.execute("PRAGMA table_info(outer_gate_refresh_tokens)").fetchall()
            }
            if "tenant_id" not in rcols:
                conn.execute(
                    "ALTER TABLE outer_gate_refresh_tokens "
                    "ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''",
                )
            conn.commit()

    # ── minting (authenticated admin authority only) ───────────────────────
    def mint_for_operator(
        self,
        project_root: Path,
        *,
        user_id: str,
        role: str,
        audience: str = AUDIENCE_CODENEXUS,
        scope: list[str] | frozenset[str] | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        resource: str = "",
        tenant_id: str = "",
    ) -> MintedToken:
        """Mint a scoped token. REFUSES unless role is admin/super_admin (the
        caller MUST have authenticated the operator). Scope is validated against
        VALID_SCOPES; an empty/invalid scope is refused (no all-powerful token by
        default). Default scope is read-only catalog + Tier-R invoke. ``tenant_id``
        binds the token to a codenexus org (orgOwnerId or self) for execution-layer
        isolation; re-verified live at validate.
        """
        if str(role or "").strip().lower() not in _MINT_ROLES:
            raise PermissionError(
                f"outer_gate token mint refused: role {role!r} is not an "
                f"authenticated admin/super_admin",
            )
        if not str(user_id or "").strip():
            raise PermissionError("outer_gate token mint refused: no operator user_id")
        # scope=None ⇒ default read-only set; an EXPLICIT empty/invalid scope is
        # an error (never silently widen to default).
        scope_set = (
            frozenset(scope)
            if scope is not None
            else frozenset({SCOPE_CATALOG, SCOPE_STATUS, SCOPE_TIER_R_INVOKE})
        )
        bad = scope_set - VALID_SCOPES
        if bad or not scope_set:
            raise ValueError(f"invalid scope(s): {sorted(bad) or 'empty'}")
        self.init_db(project_root)
        token = "ogt_" + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(8)
        issued = time.time()
        expires = issued + max(60, int(ttl_seconds))
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO outer_gate_tokens (token_id, token_hash, audience, "
                "scope_json, minted_by_user_id, minted_by_role, issued_at, "
                "expires_at, resource, tenant_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    _hash_token(token),
                    audience,
                    json.dumps(sorted(scope_set)),
                    user_id,
                    role,
                    _iso(issued),
                    _iso(expires),
                    resource or "",
                    str(tenant_id or ""),
                ),
            )
            conn.commit()
        return MintedToken(
            token=token,
            token_id=token_id,
            audience=audience,
            scope=tuple(sorted(scope_set)),
            minted_by=user_id,
            expires_at=_iso(expires),
        )

    def mint_for_grant(
        self,
        project_root: Path,
        *,
        user_id: str,
        role: str,
        audience: str = AUDIENCE_CODENEXUS,
        scope: list[str] | frozenset[str] | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        resource: str = "",
        tenant_id: str = "",
    ) -> MintedToken:
        """Mint a scoped token for an OAuth authorization-code exchange. Unlike
        ``mint_for_operator`` this does NOT require an admin/super_admin role: a
        code grant already had its authority enforced UPSTREAM at the authorize step
        (codenexus login + webmcp entitlement via ``authorize_webmcp``), so an
        entitled org owner/operator — a platform-role USER — legitimately receives a
        token. Scope/audience/resource validation is identical. This is ONLY for the
        post-authz code exchange; never a direct-mint path."""
        if not str(user_id or "").strip():
            raise PermissionError("outer_gate token mint refused: no user_id")
        scope_set = (
            frozenset(scope)
            if scope is not None
            else frozenset({SCOPE_CATALOG, SCOPE_STATUS, SCOPE_TIER_R_INVOKE})
        )
        bad = scope_set - VALID_SCOPES
        if bad or not scope_set:
            raise ValueError(f"invalid scope(s): {sorted(bad) or 'empty'}")
        self.init_db(project_root)
        token = "ogt_" + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(8)
        issued = time.time()
        expires = issued + max(60, int(ttl_seconds))
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO outer_gate_tokens (token_id, token_hash, audience, "
                "scope_json, minted_by_user_id, minted_by_role, issued_at, "
                "expires_at, resource, tenant_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    _hash_token(token),
                    audience,
                    json.dumps(sorted(scope_set)),
                    user_id,
                    role,
                    _iso(issued),
                    _iso(expires),
                    resource or "",
                    str(tenant_id or ""),
                ),
            )
            conn.commit()
        return MintedToken(
            token=token,
            token_id=token_id,
            audience=audience,
            scope=tuple(sorted(scope_set)),
            minted_by=user_id,
            expires_at=_iso(expires),
        )

    # ── refresh tokens (single-use rotation) ───────────────────────────────
    def mint_refresh(
        self,
        project_root: Path,
        *,
        user_id: str,
        role: str,
        scope: list[str] | frozenset[str],
        audience: str = AUDIENCE_CODENEXUS,
        resource: str = "",
        ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
        tenant_id: str = "",
    ) -> str:
        """Mint a single-use refresh token bound to the same user/role/scope/
        resource/tenant as the access token. Returns the opaque token (sha256 at
        rest)."""
        scope_set = frozenset(scope) & VALID_SCOPES
        if not scope_set:
            raise ValueError("refresh token requires a non-empty valid scope")
        self.init_db(project_root)
        token = "ogr_" + secrets.token_urlsafe(32)
        issued = time.time()
        expires = issued + max(60, int(ttl_seconds))
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO outer_gate_refresh_tokens (token_hash, user_id, role, "
                "audience, scope_json, resource, issued_at, expires_at, tenant_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _hash_token(token),
                    user_id,
                    role,
                    audience,
                    json.dumps(sorted(scope_set)),
                    resource or "",
                    _iso(issued),
                    _iso(expires),
                    str(tenant_id or ""),
                ),
            )
            conn.commit()
        return token

    def rotate_refresh(self, project_root: Path, refresh_token: str) -> dict | None:
        """Validate + CONSUME a refresh token (single-use rotation) and return its
        claims for re-issue. Fail-closed: unknown / consumed / revoked / expired
        ⇒ None. Marking consumed BEFORE re-issue defeats replay."""
        if not refresh_token:
            return None
        self.init_db(project_root)
        h = _hash_token(refresh_token)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM outer_gate_refresh_tokens WHERE token_hash=?",
                (h,),
            ).fetchone()
            if row is None or int(row["consumed"]) or int(row["revoked"]):
                return None
            if str(row["expires_at"]) <= _iso_now():
                return None
            conn.execute(
                "UPDATE outer_gate_refresh_tokens SET consumed=1 WHERE token_hash=?",
                (h,),
            )
            conn.commit()
        try:
            r_tenant = str(row["tenant_id"] or "")
        except (IndexError, KeyError):
            r_tenant = ""
        return {
            "user_id": str(row["user_id"]),
            "role": str(row["role"]),
            "audience": str(row["audience"]),
            "scope": frozenset(json.loads(row["scope_json"])),
            "resource": str(row["resource"]),
            "tenant_id": r_tenant,
        }

    def revoke_refresh_for_user(self, project_root: Path, user_id: str) -> int:
        """Revoke all of a user's refresh tokens (logout / compromise). Returns
        the count revoked."""
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE outer_gate_refresh_tokens SET revoked=1 "
                "WHERE user_id=? AND revoked=0",
                (str(user_id).strip(),),
            )
            conn.commit()
            return cur.rowcount

    # ── validation (fail-closed, claim-checking) ───────────────────────────
    def validate(
        self,
        project_root: Path,
        token: str,
        *,
        required_audience: str = AUDIENCE_CODENEXUS,
    ) -> TokenValidation:
        """Resolve a presented token to its claims. Fail-closed at every step;
        the reason names exactly which check failed.
        """
        if not token:
            return TokenValidation(False, "no_token")
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM outer_gate_tokens WHERE token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None:
            return TokenValidation(False, "unknown_token")
        if int(row["revoked"]):
            return TokenValidation(False, "revoked")
        if str(row["expires_at"]) <= _iso_now():
            return TokenValidation(False, "expired")
        if str(row["audience"]) != required_audience:
            return TokenValidation(False, "wrong_audience")
        try:
            scope = frozenset(json.loads(row["scope_json"]))
        except Exception:
            return TokenValidation(False, "corrupt_scope")
        # Resolve the minter's CURRENT role/disabled state — a token outlives a
        # demotion/disable, so the live identity is the authority, not the stamp.
        from .identity_store import IdentityStore

        user = None
        try:
            with sqlite3.connect(str(self.db_path(project_root))) as conn:
                conn.row_factory = sqlite3.Row
                u = conn.execute(
                    "SELECT user_id, role, disabled FROM identity_users WHERE user_id = ?",
                    (row["minted_by_user_id"],),
                ).fetchone()
            if u is not None and not int(u["disabled"]):
                user = u
        except Exception:
            user = None
        cnx_user = None
        if user is None:
            # Codenexus-authenticated webmcp token: the minter is a codenexus user
            # (not in the LOCAL identity store) — re-resolve against codenexus, the
            # authority, by user id. No DSN ⇒ None (local-only gate, unchanged).
            cnx_user = _resolve_codenexus_minter(row["minted_by_user_id"])
            user = cnx_user
        if user is None:
            return TokenValidation(False, "minter_disabled_or_gone")
        _ = IdentityStore  # keep the canonical seam imported/visible
        try:
            resource = str(row["resource"] or "")
        except (IndexError, KeyError):
            resource = ""
        # Token's bound-tenant column. Multi-org: normally "" — the ACTIVE org is bound
        # per-request via org_select (gate transport), not stamped at mint. Retained for
        # legacy / direct-mint tokens; the transport ignores it once the user resolves
        # to memberships.
        try:
            bound_tenant = str(row["tenant_id"] or "")
        except (IndexError, KeyError):
            bound_tenant = ""
        # Re-verify ENTITLEMENT for codenexus-authenticated tokens against the LIVE
        # identity — a token outlives a license/seat revocation, so live codenexus state
        # is the authority (entitled = has ANY webmcp-entitled org; SUPER_ADMIN bypasses).
        # Local identity-store tokens (cnx_user is None) skip this (full-trust local
        # path). The precise per-SELECTED-org membership + entitlement check is the
        # transport's job (org_select + list_user_orgs).
        if cnx_user is not None:
            role_u = str(cnx_user.get("role") or "").strip().upper()
            if role_u != "SUPER_ADMIN" and not cnx_user.get("entitled"):
                return TokenValidation(False, "webmcp_entitlement_revoked")
        return TokenValidation(
            True,
            "",
            user_id=str(user["user_id"]),
            role=str(user["role"]),
            scope=scope,
            audience=str(row["audience"]),
            token_id=str(row["token_id"]),
            resource=resource,
            tenant_id=bound_tenant,
        )

    def revoke(self, project_root: Path, *, token: str = "", token_id: str = "") -> bool:
        """Revoke by plaintext token or token_id. Returns True if a row flipped."""
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            if token:
                cur = conn.execute(
                    "UPDATE outer_gate_tokens SET revoked=1, revoked_at=? "
                    "WHERE token_hash=? AND revoked=0",
                    (_iso_now(), _hash_token(token)),
                )
            else:
                cur = conn.execute(
                    "UPDATE outer_gate_tokens SET revoked=1, revoked_at=? "
                    "WHERE token_id=? AND revoked=0",
                    (_iso_now(), token_id),
                )
            conn.commit()
            return cur.rowcount > 0

    def purge_expired(self, project_root: Path) -> int:
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute("DELETE FROM outer_gate_tokens WHERE expires_at <= ?", (_iso_now(),))
            conn.commit()
            return cur.rowcount
