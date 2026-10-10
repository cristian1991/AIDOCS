"""#1134: the authoritative CodeNexus organization -> AIDOCS tenant mapping.

Ownership (SPEC-1134, r0b2 correction 6): CodeNexus signs and owns the
``organization_id``; AIDOCS owns the mapping to its own tenant id. The AIDOCS
tenant id is AIDOCS-issued (random, never derived from the organization id),
written ONCE and immutable afterwards, UNIQUE on both sides. CodeNexus keeps the
tenant id only as opaque evidence from the bind receipt; it is never a selector
on the wire.

Exactly two production write paths exist:

* :meth:`SqliteOrganizationTenantMap.issue` -- the authenticated S18 ``bind``
  of an unmapped organization (provenance ``issued_by_bind``: an allocation
  reservation, kept and reused even if that bind then fails; never swept);
* :meth:`SqliteOrganizationTenantMap.seed_existing_tenant_mapping` -- the
  AUDITED operator adoption of a pre-existing tenant (provenance ``seeded``).
  The mapping row and an immutable ``org_tenant_adoption_audit`` row (operator,
  closed reason, event id, the legacy grant / binding / connector / resource
  identity, timestamp) commit in ONE transaction of the SAME file; a ``seeded``
  mapping row cannot exist without its audit event (CHECK + FK). It is run by
  the local operator command :mod:`.tenant_mapping_cli`.

There is no metadata-free seed on the durable map. The in-memory map keeps a
plain ``seed`` purely as a test helper.

Contract used by the platform-control verifier:

* ``resolve(organization_id)`` -> the tenant id, or ``None`` when unmapped;
* ``issue(organization_id, now=)`` -> the existing tenant id, or a freshly
  minted one recorded atomically (concurrent issues converge on one row);
* any storage inability RAISES (``OrganizationMappingRefused("mapping_unavailable")``
  or the underlying error): it is never reported as "unmapped".

:func:`require_tenant_mapping` is the deploy pre-flip check: it reads the pair
back through the same resolver and refuses when it is absent or different.

The durable store is one install-wide append-only SQLite file
(``<gate_root>/app_org_tenant_map.sqlite3``), modelled on the managed pack store.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .._sqlite_connect import Durability
from .._sqlite_connect import connect as _canonical_connect

__all__ = [
    "REASON_LEGACY_APP_INSTALL_ADOPTION",
    "MemoryOrganizationTenantMap",
    "OrganizationMappingRefused",
    "OrganizationTenantMap",
    "SqliteOrganizationTenantMap",
    "TenantAdoptionReceipt",
    "TenantAdoptionRecord",
    "org_tenant_map_db_path",
    "require_tenant_mapping",
]

_DB_NAME = "app_org_tenant_map.sqlite3"
_ORG_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OPERATOR_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,128}$")
_RESOURCE_RE = re.compile(r"^[!-~]{1,512}$")
# An authenticated tenant-ALLOCATION reservation made by an S18 bind of an
# unmapped organization. It is NOT proof that the bind completed: the bind
# response (``authority_mapping``) is the completion receipt. Allocations are
# kept and pinned (never swept or recycled); a retry bind reuses them.
PROVENANCE_ISSUED = "issued_by_bind"
PROVENANCE_SEEDED = "seeded"
REASON_LEGACY_APP_INSTALL_ADOPTION = "legacy_app_install_adoption"
_REASONS = frozenset({REASON_LEGACY_APP_INSTALL_ADOPTION})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_tenant_adoption_audit (
    event_id         TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL UNIQUE,
    tenant_id        TEXT NOT NULL UNIQUE,
    operator         TEXT NOT NULL,
    reason           TEXT NOT NULL CHECK (reason IN ('legacy_app_install_adoption')),
    legacy_grant_id  TEXT,
    binding_id       TEXT,
    connector_id     TEXT,
    resource         TEXT,
    record_sha256    TEXT NOT NULL,
    seeded_at        INTEGER NOT NULL CHECK (seeded_at >= 0)
);

CREATE TABLE IF NOT EXISTS org_tenant_map (
    organization_id  TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL UNIQUE,
    provenance       TEXT NOT NULL CHECK (provenance IN ('issued_by_bind', 'seeded')),
    created_at       INTEGER NOT NULL CHECK (created_at >= 0),
    seed_event_id    TEXT REFERENCES org_tenant_adoption_audit (event_id),
    CHECK ((provenance = 'seeded') = (seed_event_id IS NOT NULL))
);

CREATE TRIGGER IF NOT EXISTS org_tenant_map_no_update
BEFORE UPDATE ON org_tenant_map
BEGIN
    SELECT RAISE(ABORT, 'org_tenant_map is write-once');
END;

CREATE TRIGGER IF NOT EXISTS org_tenant_map_no_delete
BEFORE DELETE ON org_tenant_map
BEGIN
    SELECT RAISE(ABORT, 'org_tenant_map is write-once');
END;

CREATE TRIGGER IF NOT EXISTS org_tenant_adoption_audit_no_update
BEFORE UPDATE ON org_tenant_adoption_audit
BEGIN
    SELECT RAISE(ABORT, 'org_tenant_adoption_audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS org_tenant_adoption_audit_no_delete
BEFORE DELETE ON org_tenant_adoption_audit
BEGIN
    SELECT RAISE(ABORT, 'org_tenant_adoption_audit is append-only');
END;
"""

_AUDIT_COLUMNS = (
    "organization_id",
    "tenant_id",
    "operator",
    "event_id",
    "reason",
    "legacy_grant_id",
    "binding_id",
    "connector_id",
    "resource",
    "seeded_at",
)


class OrganizationMappingRefused(ValueError):
    """``code``: ``organization_invalid`` / ``tenant_invalid`` / ``mapping_conflict`` /
    ``mapping_unavailable`` / ``mapping_absent`` / ``mapping_mismatch`` / ``seed_event_conflict`` /
    ``reason_invalid`` / ``operator_invalid`` / ``event_invalid`` / ``provenance_invalid``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OrganizationTenantMap(Protocol):
    def resolve(self, organization_id: str) -> Optional[str]: ...

    def issue(self, organization_id: str, *, now: int) -> str: ...


@dataclass(frozen=True)
class TenantAdoptionRecord:
    """One audited adoption of an EXISTING AIDOCS tenant by a CodeNexus organization."""

    organization_id: str
    aidocs_tenant_id: str
    operator: str  # the operator / migration authority that ran the adoption
    event_id: str  # correlation id; the idempotency key
    reason: str = REASON_LEGACY_APP_INSTALL_ADOPTION
    legacy_grant_id: Optional[str] = None  # the AppToolspaceGrant being retired
    binding_id: Optional[str] = None  # the live binding
    connector_id: Optional[str] = None
    resource: Optional[str] = None  # the connector resource identity


@dataclass(frozen=True)
class TenantAdoptionReceipt:
    organization_id: str
    aidocs_tenant_id: str
    event_id: str
    seeded_at: int
    created: bool


def org_tenant_map_db_path(gate_root: Any) -> Path:
    return Path(gate_root) / _DB_NAME


def _mint_tenant_id() -> str:
    """AIDOCS-issued: random, never derived from the organization id."""
    return "tnt_" + secrets.token_hex(16)


def _org(value: Any) -> str:
    if type(value) is not str or not _ORG_RE.fullmatch(value):
        raise OrganizationMappingRefused("organization_invalid")
    return value


def _tenant(value: Any) -> str:
    if type(value) is not str or not _TENANT_RE.fullmatch(value):
        raise OrganizationMappingRefused("tenant_invalid")
    return value


def _now(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise OrganizationMappingRefused("mapping_unavailable")
    return value


def _optional(value: Any, pattern: re.Pattern[str]) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not str or not pattern.fullmatch(value):
        raise OrganizationMappingRefused("provenance_invalid")
    return value


def _validated(record: Any) -> TenantAdoptionRecord:
    if type(record) is not TenantAdoptionRecord:
        raise OrganizationMappingRefused("provenance_invalid")
    if record.reason not in _REASONS:
        raise OrganizationMappingRefused("reason_invalid")
    if type(record.operator) is not str or not _OPERATOR_RE.fullmatch(record.operator):
        raise OrganizationMappingRefused("operator_invalid")
    if type(record.event_id) is not str or not _ORG_RE.fullmatch(record.event_id):
        raise OrganizationMappingRefused("event_invalid")
    _org(record.organization_id)
    _tenant(record.aidocs_tenant_id)
    _optional(record.legacy_grant_id, _ORG_RE)
    _optional(record.binding_id, _ORG_RE)
    _optional(record.connector_id, _ORG_RE)
    _optional(record.resource, _RESOURCE_RE)
    return record


def _record_sha256(record: TenantAdoptionRecord) -> str:
    raw = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


class MemoryOrganizationTenantMap:
    """In-process map for tests and non-production composition (same resolve/issue contract).

    ``seed`` here is a metadata-free TEST helper; production adoption is
    :meth:`SqliteOrganizationTenantMap.seed_existing_tenant_mapping` only.
    """

    def __init__(self, *, mint: Optional[Callable[[], str]] = None) -> None:
        self._mint = mint or _mint_tenant_id
        self._by_org: dict[str, str] = {}
        self._by_tenant: dict[str, str] = {}
        self._lock = threading.Lock()

    def resolve(self, organization_id: str) -> Optional[str]:
        org = _org(organization_id)
        with self._lock:
            return self._by_org.get(org)

    def _insert(self, org: str, tenant: str) -> str:
        existing = self._by_org.get(org)
        if existing is not None:
            if existing != tenant:
                raise OrganizationMappingRefused("mapping_conflict")
            return existing
        if tenant in self._by_tenant:
            raise OrganizationMappingRefused("mapping_conflict")
        self._by_org[org] = tenant
        self._by_tenant[tenant] = org
        return tenant

    def issue(self, organization_id: str, *, now: int) -> str:
        org = _org(organization_id)
        _now(now)
        with self._lock:
            existing = self._by_org.get(org)
            if existing is not None:
                return existing
            return self._insert(org, _tenant(self._mint()))

    def organization_for(self, tenant_id: str) -> Optional[str]:
        """READ-ONLY reverse lookup: the CodeNexus organization of an AIDOCS tenant (or None)."""
        tenant = _tenant(tenant_id)
        with self._lock:
            return self._by_tenant.get(tenant)

    def seed(self, organization_id: str, tenant_id: str, *, now: int) -> None:
        org, tenant = _org(organization_id), _tenant(tenant_id)
        _now(now)
        with self._lock:
            self._insert(org, tenant)


class SqliteOrganizationTenantMap:
    """The durable install-wide map. Opened lazily; every fault raises ``mapping_unavailable``."""

    def __init__(self, db_path: Any, *, mint: Optional[Callable[[], str]] = None) -> None:
        self.db_path = Path(db_path)
        self._mint = mint or _mint_tenant_id
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if not self.db_path.parent.is_dir():
            raise OrganizationMappingRefused("mapping_unavailable")
        try:
            conn = _canonical_connect(
                self.db_path,
                durability=Durability.AUDIT,
                timeout=5.0,
                busy_timeout_ms=5000,
                row_factory=False,
                check_same_thread=False,
            )
            conn.isolation_level = None  # explicit transactions only
            conn.executescript(_SCHEMA)
        except sqlite3.Error:
            raise OrganizationMappingRefused("mapping_unavailable") from None
        self._conn = conn
        return conn

    def resolve(self, organization_id: str) -> Optional[str]:
        org = _org(organization_id)
        with self._lock:
            try:
                row = self._connection().execute(
                    "SELECT tenant_id FROM org_tenant_map WHERE organization_id = ?", (org,)
                ).fetchone()
            except sqlite3.Error:
                raise OrganizationMappingRefused("mapping_unavailable") from None
        if row is None:
            return None
        return _tenant_or_corrupt(row[0])

    def organization_for(self, tenant_id: str) -> Optional[str]:
        """READ-ONLY reverse lookup: the CodeNexus organization of an AIDOCS tenant (or None).

        The tenant side is UNIQUE, so at most one row answers; a stored value
        outside the organization alphabet fails closed (``mapping_unavailable``).
        """
        tenant = _tenant(tenant_id)
        with self._lock:
            try:
                row = self._connection().execute(
                    "SELECT organization_id FROM org_tenant_map WHERE tenant_id = ?", (tenant,)
                ).fetchone()
            except sqlite3.Error:
                raise OrganizationMappingRefused("mapping_unavailable") from None
        if row is None:
            return None
        if type(row[0]) is not str or not _ORG_RE.fullmatch(row[0]):
            raise OrganizationMappingRefused("mapping_unavailable")
        return row[0]

    def _transaction(self, body: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    out = body(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return out
            except OrganizationMappingRefused:
                raise
            except sqlite3.IntegrityError:
                # A UNIQUE race lost to a concurrent writer (already rolled back).
                raise OrganizationMappingRefused("mapping_conflict") from None
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise OrganizationMappingRefused("mapping_unavailable") from None

    def issue(self, organization_id: str, *, now: int) -> str:
        org, ts = _org(organization_id), _now(now)

        def body(conn: sqlite3.Connection) -> str:
            row = conn.execute("SELECT tenant_id FROM org_tenant_map WHERE organization_id = ?", (org,)).fetchone()
            if row is not None:
                return _tenant_or_corrupt(row[0])
            new = _tenant(self._mint())
            if conn.execute("SELECT 1 FROM org_tenant_map WHERE tenant_id = ?", (new,)).fetchone() is not None:
                raise OrganizationMappingRefused("mapping_conflict")
            conn.execute(
                "INSERT INTO org_tenant_map (organization_id, tenant_id, provenance, created_at, seed_event_id)"
                " VALUES (?, ?, ?, ?, NULL)",
                (org, new, PROVENANCE_ISSUED, ts),
            )
            return new

        return self._transaction(body)

    def seed_existing_tenant_mapping(self, record: TenantAdoptionRecord, *, now: int) -> TenantAdoptionReceipt:
        """Audited adoption of an EXISTING tenant: mapping + provenance in ONE transaction.

        Exact retry (same event id and identical record) returns the original
        receipt with ``created=False``. The same event with any other content is
        ``seed_event_conflict``; an organization or tenant already mapped (by any
        other event, or by ``issue``) is ``mapping_conflict``.
        """
        rec, ts = _validated(record), _now(now)
        digest = _record_sha256(rec)

        def body(conn: sqlite3.Connection) -> TenantAdoptionReceipt:
            prior = conn.execute(
                "SELECT record_sha256, seeded_at FROM org_tenant_adoption_audit WHERE event_id = ?",
                (rec.event_id,),
            ).fetchone()
            if prior is not None:
                mapped = conn.execute(
                    "SELECT tenant_id, seed_event_id FROM org_tenant_map WHERE organization_id = ?",
                    (rec.organization_id,),
                ).fetchone()
                if prior[0] != digest or mapped != (rec.aidocs_tenant_id, rec.event_id):
                    raise OrganizationMappingRefused("seed_event_conflict")
                return TenantAdoptionReceipt(rec.organization_id, rec.aidocs_tenant_id, rec.event_id, prior[1], False)
            if conn.execute(
                "SELECT 1 FROM org_tenant_map WHERE organization_id = ? OR tenant_id = ?",
                (rec.organization_id, rec.aidocs_tenant_id),
            ).fetchone() is not None:
                raise OrganizationMappingRefused("mapping_conflict")
            conn.execute(
                "INSERT INTO org_tenant_adoption_audit (event_id, organization_id, tenant_id, operator, reason,"
                " legacy_grant_id, binding_id, connector_id, resource, record_sha256, seeded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.event_id, rec.organization_id, rec.aidocs_tenant_id, rec.operator, rec.reason,
                 rec.legacy_grant_id, rec.binding_id, rec.connector_id, rec.resource, digest, ts),
            )
            conn.execute(
                "INSERT INTO org_tenant_map (organization_id, tenant_id, provenance, created_at, seed_event_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (rec.organization_id, rec.aidocs_tenant_id, PROVENANCE_SEEDED, ts, rec.event_id),
            )
            return TenantAdoptionReceipt(rec.organization_id, rec.aidocs_tenant_id, rec.event_id, ts, True)

        return self._transaction(body)

    def adoption_record(self, organization_id: str) -> Optional[dict[str, Any]]:
        """The immutable provenance of a seeded mapping (``None`` when it was not adopted)."""
        org = _org(organization_id)
        with self._lock:
            try:
                row = self._connection().execute(
                    "SELECT " + ", ".join(_AUDIT_COLUMNS) + " FROM org_tenant_adoption_audit"
                    " WHERE organization_id = ?",
                    (org,),
                ).fetchone()
            except sqlite3.Error:
                raise OrganizationMappingRefused("mapping_unavailable") from None
        if row is None:
            return None
        out = dict(zip(_AUDIT_COLUMNS, row))
        out["aidocs_tenant_id"] = out.pop("tenant_id")
        return out


def _tenant_or_corrupt(value: Any) -> str:
    if type(value) is not str or not _TENANT_RE.fullmatch(value):
        raise OrganizationMappingRefused("mapping_unavailable")
    return value


def require_tenant_mapping(organizations: Any, organization_id: str, expected_tenant_id: str) -> None:
    """Deploy pre-flip check: the pair must read back EXACTLY through the production resolver.

    Raises ``mapping_absent`` / ``mapping_mismatch`` (or ``mapping_unavailable``
    on a store fault). S18 v2 must not serve the organization until this passes.
    """
    org, expected = _org(organization_id), _tenant(expected_tenant_id)
    actual = organizations.resolve(org)
    if actual is None:
        raise OrganizationMappingRefused("mapping_absent")
    if actual != expected:
        raise OrganizationMappingRefused("mapping_mismatch")
