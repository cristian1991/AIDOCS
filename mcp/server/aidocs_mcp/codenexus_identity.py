"""CodenexusPostgresResolver — resolve a codenexus identity for webmcp authz by
reading the codenexus.cloud Postgres (the default, Postgres-read variant of
``CodenexusIdentityResolver``; gate + codenexus are co-located on one VPS).

It returns a ``WebmcpPrincipal`` (role + whether the user holds an ACTIVE webmcp
entitlement) which the pure ``authorize_webmcp`` policy then judges. The DSN is
gate config (the codenexus read-only DB URL) wired by the operator — NEVER
hardcoded; ``connect`` is injectable for tests.

Entitlement (WebMCP M1, multi-org) = an active, non-expired License of type WEBMCP
(or one flagged ``includesAll``) held by the OWNER of ANY org the user belongs to.
The org model is ``Organization`` + ``TeamMember`` (the canonical teams-plugin shape):
a user can belong to several orgs with a ``TeamRole`` in each; seats live on the org
OWNER's License. Entitlement resolves via ``TeamMember → Organization → License`` on
the owner — the coarse "may connect" gate; the precise per-SELECTED-org check happens
in the transport from ``list_user_orgs`` after ``org_select``.

SQL is prod-safe: ``type::text = 'WEBMCP'`` compares the enum's TEXT so it returns
empty (no error) on any DB where the LicenseType.WEBMCP value is absent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .webmcp_authz import WebmcpPrincipal

_ROLE_SQL = 'SELECT id, email, role FROM "User" WHERE lower(email) = lower(%s) LIMIT 1'
# Entitlement (WebMCP M1, multi-org): a user is entitled iff ANY org they belong to
# (Organization + TeamMember) has an active WEBMCP / includesAll license on its OWNER.
# Replaces the legacy single-org seat check (userId IN [self, orgOwnerId]). This is the
# coarse "may connect at all" gate (authorize + validate); the precise per-SELECTED-org
# entitlement is re-checked in the transport from list_user_orgs.
_ENTITLED_ANY_SQL = (
    'SELECT 1 FROM "TeamMember" tm '
    'JOIN "Organization" o ON o.id = tm."organizationId" '
    'JOIN "License" l ON l."userId" = o."ownerId" '
    'WHERE tm."userId" = %s AND l."isActive" '
    "AND (l.type::text = 'WEBMCP' OR l.\"includesAll\") "
    'AND (l."expiresAt" IS NULL OR l."expiresAt" > now()) LIMIT 1'
)
# Authentication (DoD #2 "authenticates against codenexus"): the bcrypt password
# hash is read alongside identity so the gate verifies credentials against
# codenexus itself — never its own store. The read-only role has SELECT on
# "User" (which includes the password column); no write is needed.
_AUTHN_SQL = (
    'SELECT id, email, role, password FROM "User" '
    'WHERE lower(email) = lower(%s) LIMIT 1'
)
# Re-resolve a token's minter by codenexus user id (cuid). Token validation
# re-resolves the minter's CURRENT role + entitlement on every call so a demoted /
# de-licensed user's token stops working; for codenexus-authenticated tokens the
# authority is codenexus, not the gate's local identity store.
_BY_ID_SQL = 'SELECT id, role FROM "User" WHERE id = %s LIMIT 1'
# Multi-org membership (WebMCP M1): every org a user belongs to, with their TeamRole
# in each and whether that org is webmcp-entitled (an active license held by the org
# OWNER). Source of truth = Organization + TeamMember (the canonical org-identity model
# adopted from the ADB teams-plugin shape). org_id = Organization.id. Own org first.
# Powers the `org_select` tool + the transport's per-request tenant binding + the
# membership re-check.
_USER_ORGS_SQL = (
    'SELECT tm."organizationId", tm."role", '
    "EXISTS(SELECT 1 FROM \"License\" l WHERE l.\"userId\" = o.\"ownerId\" "
    'AND l."isActive" AND (l.type::text = \'WEBMCP\' OR l."includesAll") '
    'AND (l."expiresAt" IS NULL OR l."expiresAt" > now())) AS entitled, '
    'o.name AS org_name, o.slug AS org_slug '
    'FROM "TeamMember" tm JOIN "Organization" o ON o.id = tm."organizationId" '
    'WHERE tm."userId" = %s '
    'ORDER BY (o."ownerId" = tm."userId") DESC, lower(o.name)'
)
# Seat-cap (WebMCP seats): a member is within the org's plan iff their join-order rank
# (joinedAt) is <= the org owner's active WEBMCP/includesAll License.maxActivations, OR they
# ARE the owner. Read-only soft gate, counted by IDENTITY (TeamMember) ranked stably so the
# same members keep their seats; max_seats=0 (no cap resolved) fails OPEN. See seat_ok().
_SEAT_OK_SQL = (
    'WITH ranked AS ('
    ' SELECT tm."userId" AS uid, row_number() OVER (ORDER BY tm."joinedAt", tm.id) AS rnk'
    ' FROM "TeamMember" tm WHERE tm."organizationId" = %s'
    '), cap AS ('
    ' SELECT COALESCE(MAX(l."maxActivations"), 0) AS max_seats,'
    ' bool_or(o."ownerId" = %s) AS is_owner'
    ' FROM "Organization" o'
    ' LEFT JOIN "License" l ON l."userId" = o."ownerId" AND l."isActive"'
    "  AND (l.type::text = 'WEBMCP' OR l.\"includesAll\")"
    ' AND (l."expiresAt" IS NULL OR l."expiresAt" > now())'
    ' WHERE o.id = %s'
    ') '
    'SELECT (cap.is_owner OR cap.max_seats = 0 OR EXISTS('
    ' SELECT 1 FROM ranked r WHERE r.uid = %s AND r.rnk <= cap.max_seats'
    ')) AS seat_ok FROM cap'
)


class CodenexusPostgresResolver:
    """Resolve a login email/subject to a codenexus WebmcpPrincipal via Postgres."""

    def __init__(self, dsn: str = "", connect: Callable[[], Any] | None = None) -> None:
        self._dsn = dsn
        self._connect = connect  # () -> DBAPI connection; default psycopg2

    def _conn(self) -> Any:
        if self._connect is not None:
            return self._connect()
        import psycopg2  # imported lazily; only needed for the real binding

        return psycopg2.connect(self._dsn)

    def authenticate(self, email: str, password: str) -> tuple[WebmcpPrincipal | None, bool]:
        """Verify (email, password) against codenexus. Returns ``(principal, exists)``:

        - principal is a fully-resolved ``WebmcpPrincipal`` (role + entitlement)
          ONLY when the email is a codenexus user AND the bcrypt password matches;
        - exists is True iff the email is a codenexus user — so the caller refuses
          (rather than falling back to a local store) on a wrong/absent password
          for a known codenexus account.

        Fail-closed: any DB/driver error → ``(None, False)`` (the caller then tries
        local auth, which fails for codenexus-only users → deny). bcrypt is required
        (codenexus hashes with bcrypt); its absence yields a failed match, never an
        open admit."""
        addr = str(email or "").strip()
        if not addr or not password:
            return (None, False)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_AUTHN_SQL, (addr,))
            row = cur.fetchone()
            if row is None:
                return (None, False)  # not a codenexus user → allow local fallback
            user_id, em, role, pw_hash = str(row[0]), str(row[1]), str(row[2]), row[3]
            if not pw_hash:
                return (None, True)  # exists but credential-less (OAuth-only) → refuse
            import bcrypt

            try:
                ok = bcrypt.checkpw(password.encode("utf-8"), str(pw_hash).encode("utf-8"))
            except Exception:
                ok = False
            if not ok:
                return (None, True)  # known user, wrong password → refuse, no fallback
            cur.execute(_ENTITLED_ANY_SQL, (user_id,))
            entitled = cur.fetchone() is not None
            return (
                WebmcpPrincipal(user_id=user_id, email=em, role=role, webmcp_entitled=entitled),
                True,
            )
        except Exception:
            return (None, False)  # fail-closed: treat as unknown (local fallback denies cnx users)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def resolve_minter_identity(self, user_id: str) -> dict | None:
        """Re-resolve a token's minter to its LIVE role + entitlement, the authority
        for validate-time re-checks. Returns ``{user_id, role, entitled}`` or None
        (unknown id / error → fail-closed). entitled = has ANY webmcp-entitled org
        (Organization + TeamMember). No single tenant_id — multi-org tenancy is bound
        per-request via org_select, not derived from a single owner."""
        uid = str(user_id or "").strip()
        if not uid:
            return None
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_BY_ID_SQL, (uid,))
            row = cur.fetchone()
            if row is None:
                return None
            rid, role = str(row[0]), str(row[1])
            cur.execute(_ENTITLED_ANY_SQL, (rid,))
            entitled = cur.fetchone() is not None
            return {"user_id": rid, "role": role, "entitled": entitled}
        except Exception:
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def list_user_orgs(self, user_id: str) -> list[dict]:
        """Every org a user belongs to (Organization+TeamMember), with their TeamRole +
        that org's live webmcp entitlement. Returns ``[{org_id, org_role, entitled,
        org_slug, org_name}]`` (own org first; org_id = Organization.id). [] on unknown
        user / error / table absent (fail-closed — the caller then has no selectable
        orgs). Authority for `org_select` choices AND the per-request membership re-check."""
        uid = str(user_id or "").strip()
        if not uid:
            return []
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_USER_ORGS_SQL, (uid,))
            rows = cur.fetchall()
            out: list[dict] = []
            for r in rows or []:
                out.append(
                    {
                        "org_id": str(r[0]),
                        "org_role": str(r[1]),
                        "entitled": bool(r[2]),
                        "org_name": str(r[3] or ""),
                        "org_slug": str(r[4] or ""),
                    },
                )
            return out
        except Exception:
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def resolve(self, credential: str) -> WebmcpPrincipal | None:
        """Resolve a login email (or subject) → WebmcpPrincipal, or None when the
        user is unknown. Fail-closed: any DB error surfaces as None (deny)."""
        email = str(credential or "").strip()
        if not email:
            return None
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_ROLE_SQL, (email,))
            row = cur.fetchone()
            if row is None:
                return None
            user_id, em, role = str(row[0]), str(row[1]), str(row[2])
            # Entitled iff ANY org the user belongs to holds an active webmcp license.
            cur.execute(_ENTITLED_ANY_SQL, (user_id,))
            entitled = cur.fetchone() is not None
            return WebmcpPrincipal(user_id=user_id, email=em, role=role, webmcp_entitled=entitled)
        except Exception:
            return None  # fail-closed: unresolved → deny
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def seat_ok(self, user_id: str, org_id: str) -> bool:
        """Seat-cap soft gate (read-only): is this user within ``org_id``'s WebMCP seat
        plan (the org owner's active WEBMCP/includesAll License.maxActivations)? Admits
        the owner, any member within the cap ranked by joinedAt, OR when the cap can't be
        resolved — fail-OPEN so a transient/unknown state NEVER locks out a paying user.
        Returns False ONLY for a member ranked beyond the cap (the caller soft-gates them
        to /upgrade — existing seats are never evicted, since the rank is stable)."""
        uid = str(user_id or "").strip()
        oid = str(org_id or "").strip()
        if not uid or not oid:
            return True
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_SEAT_OK_SQL, (oid, uid, oid, uid))
            row = cur.fetchone()
            return bool(row[0]) if row and row[0] is not None else True
        except Exception:
            return True  # fail-open: never lock out a paying user on a transient error
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
