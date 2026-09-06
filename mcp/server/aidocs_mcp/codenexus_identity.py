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
from dataclasses import dataclass
from typing import Any

from .webmcp_authz import WebmcpPrincipal

# Canonical seat statuses (War 2 / #376). ``ok`` (admit) is True ONLY for ACTIVE.
# Every non-admit status is a NAMED, diagnosable reason — a datasource failure is
# its OWN status (never a silent admit), so a transient DB error cannot mint a seat.
SEAT_ACTIVE = "active"  # owner, or member within the cap → seated
SEAT_OVER_CAP = "over_cap"  # member ranked beyond the plan's seats → refuse (upgrade)
SEAT_NO_MEMBERSHIP = "no_membership"  # not a member (removed/revoked) → refuse
SEAT_NO_LICENSE = "no_license"  # owner holds no active WebMCP/includesAll license → refuse
SEAT_DATASOURCE_UNAVAILABLE = "datasource_unavailable"  # query failed → bounded refusal


# Gate read-only role health (War 3 / #376): a fresh provisioning either COMPLETES
# or reports EXACTLY what is missing. Statuses distinguish the failure modes so the
# operator knows whether to run the migration, the password provisioning, or fix the
# DB — never a vague "it's broken".
GATE_RO_OK = "ok"  # role present, required identity tables readable
GATE_RO_MISSING_ROLE = "missing_role"  # login failed → role absent / no password set
GATE_RO_MISSING_GRANT = "missing_grant"  # connected, but a required table is not readable
GATE_RO_SCHEMA_MISMATCH = "schema_mismatch"  # a required table/column does not exist
GATE_RO_UNAVAILABLE_DB = "unavailable_db"  # cannot reach the DB at all


@dataclass(frozen=True)
class GateRoHealth:
    """Diagnosable health of the gate's read-only DB role. ``ok`` gates 'usable';
    ``status`` names the fix; ``detail`` is a short, credential-free hint."""

    ok: bool
    status: str
    detail: str = ""
    missing_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeatResolution:
    """The ONE canonical seat result every consumer reads. ``ok`` gates admission;
    ``status`` is the diagnosable reason; rank/max_seats are diagnostics (no
    connection details). A datasource failure is ``ok=False`` — fail CLOSED."""

    ok: bool
    status: str
    is_owner: bool = False
    member_rank: int | None = None
    max_seats: int | None = None

    def diagnostic(self) -> str:
        """Operator-facing one-liner — useful, never leaks connection details."""
        if self.status == SEAT_ACTIVE:
            return "seated"
        if self.status == SEAT_OVER_CAP:
            return f"over seat cap (rank {self.member_rank} > {self.max_seats} seats)"
        if self.status == SEAT_NO_MEMBERSHIP:
            return "not a member of this org"
        if self.status == SEAT_NO_LICENSE:
            return "the org owner holds no active WebMCP seat plan"
        if self.status == SEAT_DATASOURCE_UNAVAILABLE:
            return "seat status could not be verified (identity datasource unavailable)"
        return self.status

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
# Canonical seat resolution (War 2 / #376): ONE query that returns the raw facts a
# seat decision needs, so BOTH repos (this gate + the ADB team route) derive the SAME
# result from the SAME org state — no independent gate-side ranking. Facts:
#   is_member  — the user is a TeamMember of the org (a removed/revoked member is not)
#   is_owner   — the user OWNS the org (always seated)
#   has_license— the owner holds an ACTIVE WEBMCP/includesAll license (seats exist)
#   max_seats  — that license's maxActivations (0 when none)
#   member_rank— the user's stable join-order rank (joinedAt, id) among members
# The Python side maps these to a STATUS (never a bare bool); a query error is
# NOT a fact — it maps to datasource_unavailable (bounded refusal), never a silent
# admit. Ranking lives ONLY here (canonical), matching the ADB seat helper's order.
_SEAT_RESOLVE_SQL = (
    'WITH ranked AS ('
    ' SELECT tm."userId" AS uid, row_number() OVER (ORDER BY tm."joinedAt", tm.id) AS rnk'
    ' FROM "TeamMember" tm WHERE tm."organizationId" = %s'
    '), cap AS ('
    ' SELECT COALESCE(MAX(l."maxActivations"), 0) AS max_seats,'
    ' bool_or(l."userId" IS NOT NULL) AS has_license,'
    ' bool_or(o."ownerId" = %s) AS is_owner'
    ' FROM "Organization" o'
    ' LEFT JOIN "License" l ON l."userId" = o."ownerId" AND l."isActive"'
    "  AND (l.type::text = 'WEBMCP' OR l.\"includesAll\")"
    ' AND (l."expiresAt" IS NULL OR l."expiresAt" > now())'
    ' WHERE o.id = %s'
    ') '
    'SELECT cap.is_owner, cap.has_license, cap.max_seats,'
    ' (SELECT r.rnk FROM ranked r WHERE r.uid = %s) AS member_rank,'
    ' EXISTS(SELECT 1 FROM ranked r WHERE r.uid = %s) AS is_member'
    ' FROM cap'
)


#: WHY authentication did not produce a principal. Local backlog 989.
#:
#: `authenticate()` answered `(None, False)` for FOUR different situations —
#: empty credentials, "codenexus has no such user", ANY DB/driver error, and (at
#: the transport) no DSN configured. Its own docstring called the error case
#: "fail-closed: treat as unknown (the caller then tries local auth)", and that
#: is not fail-closed: it is FAIL-OVER TO A WEAKER AUTHORITY. A codenexus outage
#: silently promoted the local bcrypt store to the identity authority for anyone
#: holding a local account.
#:
#: Operator ruling 2026-08-31: "users are stored on codenexus.cloud, not logged
#: in no access to aidocs". Retiring the local fallback REQUIRES these four to be
#: distinguishable first — otherwise removing it locks out every deployment with
#: no DSN, a state indistinguishable from "user unknown" today.
AUTHN_OK = "ok"
AUTHN_UNKNOWN_USER = "unknown_user"          # codenexus ANSWERED: no such user
AUTHN_CREDENTIAL_REJECTED = "credential_rejected"  # known user, credential refused
AUTHN_NO_CREDENTIALS = "no_credentials"      # nothing was supplied to check
AUTHN_BACKEND_ERROR = "backend_error"        # we could NOT ASK codenexus


@dataclass(frozen=True)
class AuthnVerdict:
    """A discriminated authentication answer — including "we could not ask".

    `answered` is the load-bearing field: it is True only when codenexus itself
    produced the verdict. A caller deciding whether any fallback is permissible
    must branch on THAT, never on `principal is None`, because the absence of a
    principal is exactly what an outage and a rejection have in common.
    """

    reason: str
    principal: "WebmcpPrincipal | None" = None
    exists: bool = False

    @property
    def answered(self) -> bool:
        """Did codenexus actually decide this? An error is not a decision."""
        return self.reason in (AUTHN_OK, AUTHN_UNKNOWN_USER, AUTHN_CREDENTIAL_REJECTED)

    def as_tuple(self) -> "tuple[WebmcpPrincipal | None, bool]":
        """The legacy ``(principal, exists)`` shape, byte-identical to before."""
        return (self.principal, self.exists)


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
        return self.authenticate_verdict(email, password).as_tuple()

    def authenticate_verdict(self, email: str, password: str) -> AuthnVerdict:
        """Same check, but the answer says WHY (local backlog 989).

        `authenticate()` is now an adapter over this, so behaviour is unchanged
        for every existing caller — what is new is that a caller can ask whether
        codenexus ANSWERED. Until it could, "no such user" and "the database is
        down" were the same value, and the login route's local fallback fired on
        both.
        """
        addr = str(email or "").strip()
        if not addr or not password:
            return AuthnVerdict(AUTHN_NO_CREDENTIALS)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_AUTHN_SQL, (addr,))
            row = cur.fetchone()
            if row is None:
                # codenexus ANSWERED, and the answer is "no such user".
                return AuthnVerdict(AUTHN_UNKNOWN_USER)
            user_id, em, role, pw_hash = str(row[0]), str(row[1]), str(row[2]), row[3]
            if not pw_hash:
                # exists but credential-less (OAuth-only) → refuse, never fall back
                return AuthnVerdict(AUTHN_CREDENTIAL_REJECTED, exists=True)
            import bcrypt

            try:
                ok = bcrypt.checkpw(password.encode("utf-8"), str(pw_hash).encode("utf-8"))
            except Exception:
                ok = False
            if not ok:
                return AuthnVerdict(AUTHN_CREDENTIAL_REJECTED, exists=True)
            cur.execute(_ENTITLED_ANY_SQL, (user_id,))
            entitled = cur.fetchone() is not None
            return AuthnVerdict(
                AUTHN_OK,
                principal=WebmcpPrincipal(
                    user_id=user_id, email=em, role=role, webmcp_entitled=entitled
                ),
                exists=True,
            )
        except Exception:
            # WE COULD NOT ASK. This used to say "fail-closed: treat as unknown
            # (local fallback denies cnx users)", and the parenthesis is the
            # whole problem: it is only a denial for users who exist ONLY in
            # codenexus. For anyone holding a local account, a codenexus outage
            # silently promoted the local bcrypt store to the identity
            # authority. `as_tuple()` keeps the legacy value so nothing changes
            # yet; `answered` is how a caller stops treating this as a verdict.
            return AuthnVerdict(AUTHN_BACKEND_ERROR)
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

    def resolve_seat(self, user_id: str, org_id: str) -> SeatResolution:
        """Canonical seat resolution (War 2 / #376): the ONE result every consumer
        reads, derived from the SAME org state the ADB team route sees.

        Maps the raw facts (is_owner / is_member / has_license / max_seats /
        member_rank, ranked by joinedAt) to a NAMED status:
          * owner OR member ranked <= max_seats → ACTIVE (ok)
          * member ranked  > max_seats          → OVER_CAP
          * not a member (removed/revoked)      → NO_MEMBERSHIP
          * owner holds no active license       → NO_LICENSE
          * the query failed                    → DATASOURCE_UNAVAILABLE

        FAIL CLOSED: a datasource error is its own status with ok=False — a
        transient DB error never silently produces an active seat (the war's core
        fix; the old seat_ok fell OPEN here). Empty ids → ACTIVE (local/no-tenant
        gate; the caller only reaches this with a real tenant)."""
        uid = str(user_id or "").strip()
        oid = str(org_id or "").strip()
        if not uid or not oid:
            return SeatResolution(ok=True, status=SEAT_ACTIVE)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(_SEAT_RESOLVE_SQL, (oid, uid, oid, uid, uid))
            row = cur.fetchone()
            if row is None:
                # No org row at all → the org does not exist for seating purposes.
                return SeatResolution(ok=False, status=SEAT_NO_MEMBERSHIP)
            is_owner = bool(row[0])
            has_license = bool(row[1])
            max_seats = int(row[2] or 0)
            member_rank = int(row[3]) if row[3] is not None else None
            is_member = bool(row[4])
            if is_owner:
                return SeatResolution(
                    ok=True, status=SEAT_ACTIVE, is_owner=True, max_seats=max_seats
                )
            if not is_member:
                return SeatResolution(ok=False, status=SEAT_NO_MEMBERSHIP)
            if not has_license or max_seats <= 0:
                return SeatResolution(
                    ok=False, status=SEAT_NO_LICENSE, member_rank=member_rank, max_seats=max_seats
                )
            if member_rank is not None and member_rank <= max_seats:
                return SeatResolution(
                    ok=True, status=SEAT_ACTIVE, member_rank=member_rank, max_seats=max_seats
                )
            return SeatResolution(
                ok=False, status=SEAT_OVER_CAP, member_rank=member_rank, max_seats=max_seats
            )
        except Exception:
            # FAIL CLOSED (War 2): a datasource error is a bounded refusal with a
            # diagnostic, NEVER a silent admit.
            return SeatResolution(ok=False, status=SEAT_DATASOURCE_UNAVAILABLE)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def gate_ro_health(self) -> GateRoHealth:
        """Health of the gate's read-only role (War 3 / #376): does a fresh
        provisioning give a USABLE resolver, and if not, EXACTLY what is missing?

        Distinguishes the failure modes so the diagnostic is actionable:
          * cannot connect at all                → unavailable_db (DB down / DSN bad)
                                                    OR missing_role (auth failed →
                                                    role absent / password unset)
          * connected, a required table missing  → schema_mismatch (run migrations)
          * connected, a required table not SELECTable → missing_grant (run the
            provisioning migration)
          * all required identity tables readable → ok

        The probe SELECTs 0 rows from each required table (``WHERE false``) so it
        reads NOTHING sensitive and mutates NOTHING. Never leaks the DSN/password."""
        required = ("User", "License", "Organization", "TeamMember", "Invitation")
        conn = None
        try:
            try:
                conn = self._conn()
            except Exception as e:  # noqa: BLE001 — classify connect failures
                msg = str(e).lower()
                # A failed password/auth means the login role is not usable yet
                # (role absent or password not provisioned); a network/DB-down
                # error is a different fix. Classify on the driver's message.
                if any(
                    t in msg
                    for t in ("password", "authentication", "role ", "does not exist")
                ):
                    return GateRoHealth(
                        ok=False,
                        status=GATE_RO_MISSING_ROLE,
                        detail=(
                            "cannot authenticate as the gate role; run the provisioning "
                            "migration then set its password (scripts/provision-gate-ro.sh)"
                        ),
                    )
                return GateRoHealth(
                    ok=False,
                    status=GATE_RO_UNAVAILABLE_DB,
                    detail="cannot reach the identity database",
                )
            cur = conn.cursor()
            missing_grant: list[str] = []
            missing_table: list[str] = []
            for tbl in required:
                try:
                    cur.execute(f'SELECT 1 FROM "{tbl}" WHERE false')
                    cur.fetchall()
                except Exception as e:  # noqa: BLE001 — classify per-table
                    m = str(e).lower()
                    if "permission denied" in m or "must be owner" in m:
                        missing_grant.append(tbl)
                    elif "does not exist" in m or "undefined" in m or "relation" in m:
                        missing_table.append(tbl)
                    else:
                        missing_grant.append(tbl)  # fail toward the actionable grant fix
            if missing_table:
                return GateRoHealth(
                    ok=False,
                    status=GATE_RO_SCHEMA_MISMATCH,
                    detail="required identity table(s) missing — run prisma migrate deploy",
                    missing_tables=tuple(missing_table),
                )
            if missing_grant:
                return GateRoHealth(
                    ok=False,
                    status=GATE_RO_MISSING_GRANT,
                    detail=(
                        "required table(s) not SELECTable by the gate role — apply the "
                        "provision_aidocs_gate_ro migration"
                    ),
                    missing_tables=tuple(missing_grant),
                )
            return GateRoHealth(ok=True, status=GATE_RO_OK, detail="gate role can read identity")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
