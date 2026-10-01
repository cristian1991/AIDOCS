"""The typed AppScope audit ledger -- RFC 0003 v4.6.1 §3.6 R-d, §16 (build plan S12).

An org-authority audit ledger whose STORAGE and TAMPER-CHAIN scope is the
AppScope ``(tenant_id, sub, toolspace_id)``:

* **Storage.** One SQLite file per org under the tenant home
  (``tenant_home(<gate root>, tenant_id)/app_audit.sqlite3``, see
  :func:`app_audit_ledger_path`), never under a project root and never a fake
  session. ``ExecutionIndexStore`` is not reused or generalized: this is a new
  store with its own schema (build plan §1.1).
* **Chain.** Each AppScope is one partition with its own hash chain. The chain
  key is EXACTLY the three AppScope members; ``binding_id`` and every other
  piece of evidence live in row metadata only, so a rebind, an upgrade or a
  recreated connector never forks the chain (§3.6 R-d). Append is atomic: seq
  allocation, the ``prev_hash`` read and the insert run in ONE
  ``BEGIN IMMEDIATE`` transaction, and ``UNIQUE(scope, chain_seq)`` /
  ``UNIQUE(scope, prev_hash)`` make a duplicate seq or a second child of one
  parent structurally impossible in the live store.
* **Row hash.** ``row_hash = SHA-256(JCS({v, scope, seq, prev_hash, kind,
  retention_class, row_digest}))`` where ``row_digest = SHA-256(JCS(row))``
  over the row content (event id, kind, status, observed_at, retention class,
  metadata). Same shape as the execution-event chain (content hash chained to
  ``prev_hash``, version domain-separated) but over RFC 8785 bytes. Splitting
  out ``row_digest`` is what lets an OPERATIONAL row be pruned to a tombstone
  while the chain stays verifiable end to end.
* **Vocabulary.** ``kind`` is exactly one of the kinds on the closed
  ``boundary_audit.KIND_STATUS`` matrix (the sealed §16.1 kinds, the pre-call
  ``app_call_admitted`` and the S7 control-plane kinds), each with the class the
  one retention registry (``execution_event_retention``) gives it; the module
  refuses to import if the two disagree. ``(kind, status)`` must pass
  ``boundary_audit.kind_status_admissible``. ``metadata`` is REFUSED (not
  filtered) unless every key is on the §16.2 allowlist and every value has that
  key's exact shape (``boundary_audit.AUDIT_METADATA_VALIDATORS``), so a
  credential, body, stack trace or PII never reaches a row. ``connector_id``
  and ``binding_id`` are allowlisted EVIDENCE: row metadata, never chain key.
* **Fail closed.** Anything that stops a row from being committed raises an
  :class:`AppAuditNotRecorded` subclass. The caller MUST treat it as "no
  egress" (§9 pipeline: the pre-call boundary audit precedes the request).
  The errors are storage-generic and carry NO §16.3 platform code: the CALLER
  maps them (a mutation's pre-call row -> ``app_mutation_audit_failed``; a read
  or other non-mutation -> ``app_internal``).
* **Retention.** DECISION rows are never pruned, by age or by count; a trigger
  refuses any update of them and any delete at all. OPERATIONAL rows are
  tombstoned by :meth:`AppAuditLedger.prune_operational` (metadata dropped,
  chain header kept).
* **What verification proves.** :meth:`AppAuditLedger.verify_chain` detects
  corruption, gaps and forks inside the file. It does NOT detect a rollback of
  the whole file to an older valid state and makes no freshness claim; compare
  :meth:`AppAuditLedger.high_water` against a manifest kept elsewhere for that.
* **Backup.** :meth:`AppAuditLedger.backup_to` uses the SQLite online-backup API
  and chain-verifies the copy. Scheduling / shipping backups is #1112.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

from .. import outer_gate_tenancy
from .._sqlite_connect import Durability
from .._sqlite_connect import connect as _canonical_connect
from ..execution_event_retention import EVENT_KIND_RETENTION, RetentionClass
from .boundary_audit import (
    AUDIT_METADATA_STATUSES,
    AUDIT_METADATA_VALIDATORS,
    KIND_STATUS,
    kind_status_admissible,
)
from .jcs import JcsError, canonicalize, jcs_sha256_hex
from .scope import AppScope

__all__ = [
    "APP_AUDIT_EVENT_KINDS",
    "GENESIS_HASH",
    "HASH_VERSION",
    "LEDGER_FILENAME",
    "AppAuditBackupError",
    "AppAuditLedger",
    "AppAuditLedgerError",
    "AppAuditLedgerPathError",
    "AppAuditNotRecorded",
    "AppAuditRefused",
    "AppAuditRow",
    "AppAuditWriteFailed",
    "BackupReport",
    "ChainVerification",
    "HighWater",
    "app_audit_ledger_path",
]

LEDGER_FILENAME = "app_audit.sqlite3"
HASH_VERSION = "app-audit-v1"
GENESIS_HASH = "0" * 64

#: Every ledger kind is DECISION or OPERATIONAL (§16.1); a MECHANICAL or
#: FORENSIC class for one of them is a registry error, refused at import.
_LEDGER_CLASSES = frozenset({RetentionClass.DECISION, RetentionClass.OPERATIONAL})


def _kind_table() -> Mapping[str, RetentionClass]:
    """The admitted kinds: exactly ``KIND_STATUS``, classed by the one registry."""
    table: dict[str, RetentionClass] = {}
    for kind in KIND_STATUS:
        cls = EVENT_KIND_RETENTION.get(kind)
        if cls not in _LEDGER_CLASSES:
            raise RuntimeError(
                f"application audit kind {kind!r} has no DECISION/OPERATIONAL class in the retention registry"
            )
        table[kind] = cls
    return MappingProxyType(table)


APP_AUDIT_EVENT_KINDS: Mapping[str, RetentionClass] = _kind_table()

#: Scope members that may ALSO appear as §16.2 metadata; if present they must
#: equal the row's AppScope (evidence may not contradict the key).
_SCOPE_FIELDS = ("tenant_id", "sub", "toolspace_id")


# ── errors ──────────────────────────────────────────────────────────────


class AppAuditLedgerError(Exception):
    """Base class for every ledger error."""


class AppAuditNotRecorded(AppAuditLedgerError):
    """No row was committed. The caller MUST NOT egress (fail closed, §16 / §9)."""

    egress_permitted = False


class AppAuditRefused(AppAuditNotRecorded, ValueError):
    """The row itself is not admissible: scope type, kind, status or metadata."""


class AppAuditWriteFailed(AppAuditNotRecorded):
    """The store could not commit an admissible row (lock, I/O, corruption).

    Storage-generic: no §16.3 code. The caller maps it (mutation pre-call ->
    ``app_mutation_audit_failed``; read / non-mutation -> ``app_internal``).
    """


class AppAuditLedgerPathError(AppAuditLedgerError, ValueError):
    """The ledger location is not an org-authority location.

    ``reason`` is ``"under_project_root"`` (the ledger would live in a real
    project directory), ``"gate_root_not_a_project_root"`` (a caller passed the
    gate root or the tenants base as a forbidden project root) or
    ``"bad_tenant"``.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class AppAuditBackupError(AppAuditLedgerError):
    """A backup copy was written but does not chain-verify."""

    def __init__(self, report: "BackupReport") -> None:
        super().__init__(f"backup at {report.path} failed chain verification")
        self.report = report


# ── records ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppAuditRow:
    scope: AppScope
    seq: int
    event_id: str
    kind: str
    retention_class: RetentionClass
    status: str
    observed_at: str
    metadata: Optional[dict[str, Any]]
    row_digest: str
    prev_hash: str
    row_hash: str
    hash_version: str
    pruned: bool


@dataclass(frozen=True)
class HighWater:
    """The head of one AppScope chain: a value to keep OUTSIDE the snapshot."""

    seq: int
    row_hash: str


@dataclass(frozen=True)
class ChainVerification:
    scope: AppScope
    ok: bool
    rows: int
    head_seq: int
    head_hash: str
    problems: tuple[str, ...]
    #: Always False: an intact chain says nothing about whether it is the
    #: LATEST chain (a whole-file rollback verifies). See :class:`HighWater`.
    freshness_claimed: bool = False


@dataclass(frozen=True)
class BackupReport:
    path: Path
    ok: bool
    verifications: tuple[ChainVerification, ...]


# ── location ────────────────────────────────────────────────────────────


def _refuse_under_project_root(path: Path, project_roots: Iterable[str | Path]) -> None:
    target = Path(path).resolve()
    for root in project_roots:
        r = Path(root).resolve()
        if target == r or r in target.parents:
            raise AppAuditLedgerPathError(
                "under_project_root",
                f"the application audit ledger may not live under a project root ({r})",
            )


def app_audit_ledger_path(
    gate_root: str | Path,
    tenant_id: str,
    *,
    project_roots: Iterable[str | Path] = (),
    create: bool = True,
) -> Path:
    """``tenant_home(gate_root, tenant_id)/app_audit.sqlite3`` -- the ONE location.

    Derived only from the tenant home (``outer_gate_tenancy.tenant_home``, which
    sanitises ``tenant_id`` and checks containment).

    ``project_roots`` are the forbidden roots and must be ACTUAL project
    directories (a tenant's registered / imported repos, e.g. under
    ``<tenant home>/projects/``). The GATE ROOT IS NOT A PROJECT ROOT, even when
    the gate is itself AIDOCS-managed: ``tenant_home(gate_root, tenant_id)`` is
    exactly the intended home. Callers must NEVER pass the gate root (or the
    tenants base under it) as a forbidden project root; doing so is refused as a
    caller error (``reason="gate_root_not_a_project_root"``), never read as "the
    home is inside a project". A real project directory that would contain the
    ledger is refused (``reason="under_project_root"``). Every check runs
    BEFORE the home is created, so a refused call leaves nothing on disk.
    """
    roots = tuple(project_roots)
    gate = Path(gate_root)
    base = outer_gate_tenancy.tenants_base(gate).resolve()
    for root in roots:
        r = Path(root).resolve()
        if r == base or r in base.parents:
            raise AppAuditLedgerPathError(
                "gate_root_not_a_project_root",
                f"{r} is the gate root or above the tenants base; it is never a project root",
            )
    try:
        home = outer_gate_tenancy.tenant_home(gate, tenant_id, create=False)
    except outer_gate_tenancy.TenantError as exc:
        raise AppAuditLedgerPathError("bad_tenant", f"no tenant home for this tenant_id: {exc.reason}") from exc
    path = home / LEDGER_FILENAME
    _refuse_under_project_root(path, roots)
    if create:
        outer_gate_tenancy.tenant_home(gate, tenant_id, create=True)
    return path


# ── hashing ─────────────────────────────────────────────────────────────


def _scope_obj(scope: AppScope) -> dict[str, str]:
    return {"tenant_id": scope.tenant_id, "sub": scope.sub, "toolspace_id": scope.toolspace_id}


def _row_digest(
    *, event_id: str, kind: str, status: str, observed_at: str, retention_class: str, metadata: dict
) -> str:
    return jcs_sha256_hex(
        {
            "event_id": event_id,
            "kind": kind,
            "status": status,
            "observed_at": observed_at,
            "retention_class": retention_class,
            "metadata": metadata,
        }
    )


def _row_hash(
    *,
    hash_version: str,
    scope: AppScope,
    seq: int,
    prev_hash: str,
    kind: str,
    retention_class: str,
    row_digest: str,
) -> str:
    return jcs_sha256_hex(
        {
            "v": hash_version,
            "scope": _scope_obj(scope),
            "seq": seq,
            "prev_hash": prev_hash,
            "kind": kind,
            "retention_class": retention_class,
            "row_digest": row_digest,
        }
    )


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ── schema ──────────────────────────────────────────────────────────────

_COLUMNS = (
    "row_id",
    "tenant_id",
    "sub",
    "toolspace_id",
    "chain_seq",
    "event_id",
    "event_kind",
    "retention_class",
    "status",
    "observed_at",
    "row_digest",
    "prev_hash",
    "row_hash",
    "hash_version",
)

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_audit_events (
        row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id       TEXT NOT NULL,
        sub             TEXT NOT NULL,
        toolspace_id    TEXT NOT NULL,
        chain_seq       INTEGER NOT NULL CHECK (chain_seq >= 1),
        event_id        TEXT NOT NULL UNIQUE,
        event_kind      TEXT NOT NULL,
        retention_class TEXT NOT NULL CHECK (retention_class IN ('decision', 'operational')),
        status          TEXT NOT NULL,
        observed_at     TEXT NOT NULL,
        metadata_json   TEXT,
        row_digest      TEXT NOT NULL,
        prev_hash       TEXT NOT NULL,
        row_hash        TEXT NOT NULL,
        hash_version    TEXT NOT NULL,
        pruned          INTEGER NOT NULL DEFAULT 0 CHECK (pruned IN (0, 1)),
        UNIQUE (tenant_id, sub, toolspace_id, chain_seq),
        UNIQUE (tenant_id, sub, toolspace_id, prev_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_app_audit_prune
        ON app_audit_events (retention_class, pruned, observed_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS app_audit_events_no_delete
    BEFORE DELETE ON app_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'app audit rows are never deleted');
    END
    """,
    # The ONLY permitted update: tombstoning a live OPERATIONAL row (metadata
    # dropped, pruned set), every chain column unchanged. DECISION rows can
    # never be updated at all.
    """
    CREATE TRIGGER IF NOT EXISTS app_audit_events_tombstone_only
    BEFORE UPDATE ON app_audit_events
    WHEN NOT (
        OLD.retention_class = 'operational' AND OLD.pruned = 0
        AND NEW.pruned = 1 AND NEW.metadata_json IS NULL
        """
    + "".join(f" AND NEW.{c} IS OLD.{c}" for c in _COLUMNS)
    + """
    )
    BEGIN
        SELECT RAISE(ABORT, 'app audit rows are append-only; only an OPERATIONAL row may be tombstoned');
    END
    """,
)

_SELECT_ROWS = (
    "SELECT chain_seq, event_id, event_kind, retention_class, status, observed_at,"
    " metadata_json, row_digest, prev_hash, row_hash, hash_version, pruned"
    " FROM app_audit_events WHERE tenant_id = ? AND sub = ? AND toolspace_id = ?"
    " ORDER BY chain_seq, row_id"
)


# ── the ledger ──────────────────────────────────────────────────────────


class AppAuditLedger:
    """One org's AppScope-partitioned, hash-chained application audit ledger."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        busy_timeout_ms: int = 30_000,
        project_roots: Iterable[str | Path] = (),
    ) -> None:
        self._project_roots = tuple(project_roots)
        _refuse_under_project_root(Path(db_path), self._project_roots)
        self.db_path = Path(db_path)
        self._clock = clock or _utc_now
        self._busy_timeout_ms = int(busy_timeout_ms)
        try:
            self._write(lambda conn: [conn.execute(stmt) for stmt in _SCHEMA])
        except sqlite3.Error as exc:
            raise AppAuditWriteFailed(f"cannot open the application audit ledger: {exc}") from exc

    # -- connections -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = _canonical_connect(
            self.db_path,
            durability=Durability.AUDIT,
            timeout=self._busy_timeout_ms / 1000.0,
            busy_timeout_ms=self._busy_timeout_ms,
            row_factory=False,
        )
        conn.isolation_level = None  # explicit transactions only
        return conn

    def _write(self, body: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run ``body`` inside ONE ``BEGIN IMMEDIATE`` transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = body(conn)
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            return result
        finally:
            conn.close()

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # -- admission of a row ----------------------------------------------

    @staticmethod
    def _admit_kind(kind: Any) -> RetentionClass:
        cls = APP_AUDIT_EVENT_KINDS.get(kind) if type(kind) is str else None
        if cls is None:
            raise AppAuditRefused(f"not an application audit kind (KIND_STATUS): {kind!r}")
        return cls

    @staticmethod
    def _admit_status(kind: str, status: Any) -> str:
        if type(status) is not str or status not in AUDIT_METADATA_STATUSES:
            raise AppAuditRefused(f"not an application audit status: {status!r}")
        # S12-B5: the closed kind <-> status matrix.
        if not kind_status_admissible(kind, status):
            raise AppAuditRefused(f"status {status!r} is not admissible for kind {kind!r}")
        return status

    @staticmethod
    def _admit_metadata(scope: AppScope, metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, Mapping):
            raise AppAuditRefused("metadata must be a mapping")
        out: dict[str, Any] = {}
        for key, value in metadata.items():
            validator = AUDIT_METADATA_VALIDATORS.get(key) if type(key) is str else None
            if validator is None:
                # Deny by default (§16.2): an unknown key is refused, never
                # dropped, so a caller cannot believe a secret was recorded
                # "safely filtered".
                raise AppAuditRefused(f"metadata key not on the §16.2 allowlist: {key!r}")
            if not validator(value):
                raise AppAuditRefused(f"metadata value for {key!r} does not have its allowed shape")
            out[key] = value
        for field in _SCOPE_FIELDS:
            if field in out and out[field] != getattr(scope, field):
                raise AppAuditRefused(f"metadata {field} contradicts the row's AppScope")
        return out

    # -- append ----------------------------------------------------------

    def append(self, scope: AppScope, kind: str, status: str, metadata: Mapping[str, Any]) -> AppAuditRow:
        """Commit one row at the head of ``scope``'s chain, or raise.

        Raises :class:`AppAuditRefused` for an inadmissible row and
        :class:`AppAuditWriteFailed` when the store cannot commit. Both are
        :class:`AppAuditNotRecorded`: no row exists, so no egress.
        """
        if type(scope) is not AppScope:
            raise AppAuditRefused("the chain key is exactly an AppScope")
        retention = self._admit_kind(kind)
        self._admit_status(kind, status)
        meta = self._admit_metadata(scope, metadata)
        try:
            metadata_json = canonicalize(meta).decode("utf-8")
        except JcsError as exc:
            raise AppAuditRefused(f"metadata is not canonical JSON: {exc}") from exc
        event_id = uuid.uuid4().hex
        rclass = retention.value

        def _insert(conn: sqlite3.Connection) -> AppAuditRow:
            head = conn.execute(
                "SELECT chain_seq, row_hash FROM app_audit_events"
                " WHERE tenant_id = ? AND sub = ? AND toolspace_id = ?"
                " ORDER BY chain_seq DESC LIMIT 1",
                (scope.tenant_id, scope.sub, scope.toolspace_id),
            ).fetchone()
            seq = int(head[0]) + 1 if head else 1
            prev_hash = str(head[1]) if head else GENESIS_HASH
            observed_at = _iso(self._clock())
            digest = _row_digest(
                event_id=event_id,
                kind=kind,
                status=status,
                observed_at=observed_at,
                retention_class=rclass,
                metadata=meta,
            )
            row_hash = _row_hash(
                hash_version=HASH_VERSION,
                scope=scope,
                seq=seq,
                prev_hash=prev_hash,
                kind=kind,
                retention_class=rclass,
                row_digest=digest,
            )
            conn.execute(
                "INSERT INTO app_audit_events (tenant_id, sub, toolspace_id, chain_seq, event_id,"
                " event_kind, retention_class, status, observed_at, metadata_json, row_digest,"
                " prev_hash, row_hash, hash_version, pruned)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    scope.tenant_id,
                    scope.sub,
                    scope.toolspace_id,
                    seq,
                    event_id,
                    kind,
                    rclass,
                    status,
                    observed_at,
                    metadata_json,
                    digest,
                    prev_hash,
                    row_hash,
                    HASH_VERSION,
                ),
            )
            return AppAuditRow(
                scope=scope,
                seq=seq,
                event_id=event_id,
                kind=kind,
                retention_class=retention,
                status=status,
                observed_at=observed_at,
                metadata=dict(meta),
                row_digest=digest,
                prev_hash=prev_hash,
                row_hash=row_hash,
                hash_version=HASH_VERSION,
                pruned=False,
            )

        try:
            return self._write(_insert)
        except sqlite3.Error as exc:
            raise AppAuditWriteFailed(f"application audit row not committed: {exc}") from exc

    # -- reads -----------------------------------------------------------

    def _raw_rows(self, scope: AppScope) -> list[tuple]:
        return self._read(_SELECT_ROWS, (scope.tenant_id, scope.sub, scope.toolspace_id))

    def rows(self, scope: AppScope) -> list[AppAuditRow]:
        out: list[AppAuditRow] = []
        for (seq, event_id, kind, rclass, status, observed_at, meta_json, digest, prev, rhash, ver, pruned) in (
            self._raw_rows(scope)
        ):
            metadata: Optional[dict[str, Any]] = None
            if meta_json is not None:
                with contextlib.suppress(ValueError):
                    metadata = json.loads(meta_json)
            # A stored class outside the enum is corruption; rows() reports the
            # raw value and verify_chain() flags it.
            retention: Any = rclass
            with contextlib.suppress(ValueError):
                retention = RetentionClass(rclass)
            out.append(
                AppAuditRow(
                    scope=scope,
                    seq=int(seq),
                    event_id=event_id,
                    kind=kind,
                    retention_class=retention,
                    status=status,
                    observed_at=observed_at,
                    metadata=metadata,
                    row_digest=digest,
                    prev_hash=prev,
                    row_hash=rhash,
                    hash_version=ver,
                    pruned=bool(pruned),
                )
            )
        return out

    def scopes(self) -> list[AppScope]:
        rows = self._read(
            "SELECT DISTINCT tenant_id, sub, toolspace_id FROM app_audit_events"
            " ORDER BY tenant_id, sub, toolspace_id"
        )
        return [AppScope(t, s, ts) for (t, s, ts) in rows]

    def high_water(self, scope: AppScope) -> Optional[HighWater]:
        """The current head ``(seq, row_hash)``. Record it OUTSIDE a snapshot
        and compare on restore if rollback detection is wanted."""
        rows = self._read(
            "SELECT chain_seq, row_hash FROM app_audit_events"
            " WHERE tenant_id = ? AND sub = ? AND toolspace_id = ?"
            " ORDER BY chain_seq DESC LIMIT 1",
            (scope.tenant_id, scope.sub, scope.toolspace_id),
        )
        return HighWater(int(rows[0][0]), str(rows[0][1])) if rows else None

    # -- verification ----------------------------------------------------

    def verify_chain(self, scope: AppScope) -> ChainVerification:
        """Detect corruption, gaps and forks in ``scope``'s chain.

        Proves internal consistency only. A rollback of the whole file to an
        older valid state still verifies; no freshness is claimed.
        """
        problems: list[str] = []
        expected_seq, expected_prev = 1, GENESIS_HASH
        seen: set[int] = set()
        count, head_seq, head_hash = 0, 0, GENESIS_HASH
        for (seq, event_id, kind, rclass, status, observed_at, meta_json, digest, prev, rhash, ver, pruned) in (
            self._raw_rows(scope)
        ):
            count += 1
            seq = int(seq)
            where = f"seq {seq}"
            if seq in seen:
                problems.append(f"{where}: duplicate sequence number (fork)")
                continue
            seen.add(seq)
            if seq != expected_seq:
                problems.append(f"{where}: expected seq {expected_seq} (gap or reorder)")
            if prev != expected_prev:
                problems.append(f"{where}: prev_hash does not link to the previous row")
            if ver != HASH_VERSION:
                problems.append(f"{where}: unknown hash version {ver!r}")
            registered = APP_AUDIT_EVENT_KINDS.get(kind)
            if registered is None or registered.value != rclass:
                problems.append(f"{where}: kind/retention class not admissible ({kind!r}, {rclass!r})")
            if pruned:
                if rclass != RetentionClass.OPERATIONAL.value:
                    problems.append(f"{where}: a {rclass} row is pruned; only OPERATIONAL rows may be")
                if meta_json is not None:
                    problems.append(f"{where}: a pruned row still carries metadata")
            else:
                try:
                    meta = json.loads(meta_json) if meta_json is not None else None
                    if not isinstance(meta, dict):
                        raise ValueError("metadata is not an object")
                    recomputed = _row_digest(
                        event_id=event_id,
                        kind=kind,
                        status=status,
                        observed_at=observed_at,
                        retention_class=rclass,
                        metadata=meta,
                    )
                except (ValueError, TypeError) as exc:
                    problems.append(f"{where}: row content unreadable ({exc})")
                else:
                    if recomputed != digest:
                        problems.append(f"{where}: row content does not match its digest")
            try:
                recomputed_hash = _row_hash(
                    hash_version=str(ver),
                    scope=scope,
                    seq=seq,
                    prev_hash=str(prev),
                    kind=str(kind),
                    retention_class=str(rclass),
                    row_digest=str(digest),
                )
            except (ValueError, TypeError) as exc:
                problems.append(f"{where}: row header unreadable ({exc})")
            else:
                if recomputed_hash != rhash:
                    problems.append(f"{where}: row_hash does not match the row")
            expected_seq, expected_prev = seq + 1, str(rhash)
            head_seq, head_hash = seq, str(rhash)
        return ChainVerification(
            scope=scope,
            ok=not problems,
            rows=count,
            head_seq=head_seq,
            head_hash=head_hash,
            problems=tuple(problems),
        )

    # -- retention -------------------------------------------------------

    def prune_operational(self, before: datetime) -> int:
        """Tombstone OPERATIONAL rows observed before ``before``; return the count.

        DECISION rows are never touched (and the store's trigger refuses it).
        A tombstone keeps its chain header, so every chain still verifies.
        """
        cutoff = _iso(before)

        def _prune(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE app_audit_events SET metadata_json = NULL, pruned = 1"
                " WHERE retention_class = ? AND pruned = 0 AND observed_at < ?",
                (RetentionClass.OPERATIONAL.value, cutoff),
            )
            return int(cur.rowcount)

        try:
            return self._write(_prune)
        except sqlite3.Error as exc:
            raise AppAuditLedgerError(f"prune failed: {exc}") from exc

    # -- backup ----------------------------------------------------------

    def backup_to(self, path: str | Path) -> BackupReport:
        """Copy the ledger with the SQLite online-backup API, then chain-verify
        every AppScope in the copy. Raises :class:`AppAuditBackupError` if the
        copy does not verify."""
        dest = Path(path)
        if dest.resolve() == self.db_path.resolve():
            raise ValueError("a backup may not overwrite its own source")
        _refuse_under_project_root(dest, self._project_roots)
        src = self._connect()
        try:
            dst = _canonical_connect(dest, durability=Durability.AUDIT, row_factory=False)
            try:
                src.backup(dst)
            finally:
                dst.close()
        except sqlite3.Error as exc:
            raise AppAuditLedgerError(f"online backup failed: {exc}") from exc
        finally:
            src.close()
        copy = AppAuditLedger(dest, project_roots=self._project_roots)
        verifications = tuple(copy.verify_chain(scope) for scope in copy.scopes())
        report = BackupReport(path=dest, ok=all(v.ok for v in verifications), verifications=verifications)
        if not report.ok:
            raise AppAuditBackupError(report)
        return report
