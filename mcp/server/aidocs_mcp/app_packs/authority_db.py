"""The durable per-org APPLICATION AUTHORITY database -- RFC 0003 v4.6.1 §3.6, §5, §16.1, §22.1.

The gate restarts; losing a pairing, a seat pin, a freeze, a pending write or
the org key directory is not allowed. This module is the ONE durable home of
that non-secret authority state (r0b2 rulings, build plan S3 / S6 / S7 / S11 /
S14):

* **Location.** One SQLite file per org under the tenant home:
  ``tenant_home(<gate root>, tenant_id)/app_authority.sqlite3``
  (:func:`app_authority_db_path`), never under a project root. It is opened
  ONLY through the canonical connector (``_sqlite_connect.connect`` at
  ``Durability.AUDIT``), the same one the S12 ledger and the OAuth store use.
* **Contents.** Bindings, pairing offers / reviews (verifiers only), seat pins,
  AppScope freezes, pending mutation records, the org request-signing key
  DIRECTORY (public material only) -- see :mod:`.authority_stores` -- and the
  ``control_decisions`` table: the DECISION audit rows of those transitions.
* **One transaction.** :meth:`AppAuthorityDB.transaction` is ONE
  ``BEGIN IMMEDIATE`` transaction; a nested call joins it through a SAVEPOINT.
  The decision sink (:class:`AuthorityDecisionSink`) writes a transition row on
  the ambient transaction's connection, so a state change and its DECISION row
  commit together or not at all: there is no audit-before-state or
  state-before-audit crash window. The ambient transaction is tracked per
  thread and per resolved FILE, so two store objects over the same file share
  it.
* **Refusal rows survive the rollback.** A row whose status is ``refused`` that
  is emitted inside a transaction is held and written AFTER that transaction
  ends (commit or rollback), in its own transaction: the rollback of a refused
  transition never erases the record of the refusal. Refusal rows stay best
  effort, as before.
* **Tamper evidence.** ``control_decisions`` is append-only (triggers refuse
  every UPDATE and DELETE) and hash-chained per tenant (``row_hash =
  SHA-256(JCS(row))`` over the previous row's hash). :meth:`verify_decisions`
  detects corruption, gaps and forks inside the file; like the S12 ledger it
  makes no freshness claim (a whole-file rollback still verifies).

Why the DECISION rows live here and not in the S12 ``app_audit.sqlite3``
ledger: that ledger's chain key is exactly the AppScope ``(tenant_id, sub,
toolspace_id)`` and control-plane acts have no ``sub``; two SQLite files cannot
share one transaction. Keeping the control-plane DECISION rows in the SAME file
as the state they describe is what makes "one transaction" literal.

Secrets are never stored here: no private key, no pairing secret, no review
handle in clear, no raw host subject. Audit metadata is the already-validated
§16.2 allowlist of the event types.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from .. import outer_gate_tenancy
from .._sqlite_connect import Durability
from .._sqlite_connect import connect as _canonical_connect
from .jcs import jcs_sha256_hex

__all__ = [
    "AUTHORITY_FILENAME",
    "DECISION_HASH_VERSION",
    "AppAuthorityDB",
    "AuthorityDecisionSink",
    "AuthorityPathError",
    "AuthorityUnavailable",
    "app_authority_db_path",
    "authority_transaction",
]

AUTHORITY_FILENAME = "app_authority.sqlite3"
DECISION_HASH_VERSION = "app-authority-decision-v1"
_GENESIS = "0" * 64
_STATUS_REFUSED = "refused"


class AuthorityPathError(ValueError):
    """The authority DB location is not an org-authority location."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class AuthorityUnavailable(RuntimeError):
    """The authority DB could not be opened or written. Generic; fail closed."""


def _refuse_under_project_root(path: Path, project_roots: Iterable[str | Path]) -> None:
    target = Path(path).resolve()
    for root in project_roots:
        r = Path(root).resolve()
        if target == r or r in target.parents:
            raise AuthorityPathError("under_project_root", f"the app authority DB may not live under a project root ({r})")


def app_authority_db_path(
    gate_root: str | Path,
    tenant_id: str,
    *,
    project_roots: Iterable[str | Path] = (),
    create: bool = True,
) -> Path:
    """``tenant_home(gate_root, tenant_id)/app_authority.sqlite3`` -- the ONE location.

    Same rules as the S12 ledger path: the gate root is never a project root;
    a real project directory containing the file is refused; every check runs
    before the home is created.
    """
    roots = tuple(project_roots)
    gate = Path(gate_root)
    base = outer_gate_tenancy.tenants_base(gate).resolve()
    for root in roots:
        r = Path(root).resolve()
        if r == base or r in base.parents:
            raise AuthorityPathError("gate_root_not_a_project_root", f"{r} is the gate root; never a project root")
    try:
        home = outer_gate_tenancy.tenant_home(gate, tenant_id, create=False)
    except outer_gate_tenancy.TenantError as exc:
        raise AuthorityPathError("bad_tenant", f"no tenant home for this tenant_id: {exc.reason}") from exc
    path = home / AUTHORITY_FILENAME
    _refuse_under_project_root(path, roots)
    if create:
        outer_gate_tenancy.tenant_home(gate, tenant_id, create=True)
    return path


# -- schema -----------------------------------------------------------------------------------------

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS control_decisions (
        row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id       TEXT NOT NULL,
        chain_seq       INTEGER NOT NULL CHECK (chain_seq >= 1),
        event_id        TEXT NOT NULL UNIQUE,
        kind            TEXT NOT NULL,
        status          TEXT NOT NULL,
        actor_user_id   TEXT NOT NULL,
        binding_id      TEXT,
        observed_at     TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        prev_hash       TEXT NOT NULL,
        row_hash        TEXT NOT NULL,
        hash_version    TEXT NOT NULL,
        UNIQUE (tenant_id, chain_seq),
        UNIQUE (tenant_id, prev_hash)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS control_decisions_no_update
    BEFORE UPDATE ON control_decisions
    BEGIN SELECT RAISE(ABORT, 'control decisions are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS control_decisions_no_delete
    BEFORE DELETE ON control_decisions
    BEGIN SELECT RAISE(ABORT, 'control decisions are never deleted'); END
    """,
    # -- bindings (S7) --
    """
    CREATE TABLE IF NOT EXISTS bindings (
        binding_id      TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        toolspace_id    TEXT NOT NULL,
        state           TEXT NOT NULL,
        binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
        record_json     TEXT NOT NULL
    )
    """,
    # §3.4: exactly one ACTIVE binding per (tenant, toolspace) -- enforced by the file.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_bindings_one_active
        ON bindings (tenant_id, toolspace_id) WHERE state = 'active'
    """,
    # -- pairing (S7): verifiers only --
    """
    CREATE TABLE IF NOT EXISTS pairing_offers (
        secret_sha256   TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        binding_id      TEXT NOT NULL,
        expires_at      INTEGER NOT NULL,
        spent           INTEGER NOT NULL DEFAULT 0 CHECK (spent IN (0, 1)),
        offer_json      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pairing_reviews (
        review_sha256   TEXT PRIMARY KEY,
        secret_sha256   TEXT NOT NULL,
        tenant_id       TEXT NOT NULL,
        expires_at      INTEGER NOT NULL,
        trust_json      TEXT NOT NULL,
        review_json     TEXT NOT NULL
    )
    """,
    # -- seat pins (S6): keyed digest only --
    """
    CREATE TABLE IF NOT EXISTS seat_pins (
        tenant_id       TEXT NOT NULL,
        sub             TEXT NOT NULL,
        digest          TEXT NOT NULL,
        pinned_at       INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, sub),
        UNIQUE (tenant_id, digest)
    )
    """,
    # -- AppScope freezes (S11) --
    """
    CREATE TABLE IF NOT EXISTS freezes (
        target_key      TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        toolspace_id    TEXT,
        sub             TEXT,
        frozen_at       INTEGER NOT NULL,
        reason          TEXT NOT NULL,
        binding_id      TEXT,
        connector_id    TEXT
    )
    """,
    # -- pending mutation records (S14) --
    """
    CREATE TABLE IF NOT EXISTS pending_mutations (
        mutation_ref    TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        tenant_id       TEXT NOT NULL,
        sub             TEXT NOT NULL,
        toolspace_id    TEXT NOT NULL,
        state           TEXT NOT NULL,
        state_since     INTEGER NOT NULL,
        record_json     TEXT NOT NULL,
        body            BLOB NOT NULL
    )
    """,
    # -- the org request-signing key DIRECTORY (S3): public material only --
    """
    CREATE TABLE IF NOT EXISTS signing_keysets (
        tenant_id       TEXT NOT NULL,
        purpose         TEXT NOT NULL,
        keyset_epoch    INTEGER NOT NULL CHECK (keyset_epoch >= 1),
        PRIMARY KEY (tenant_id, purpose)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signing_keys (
        tenant_id       TEXT NOT NULL,
        purpose         TEXT NOT NULL,
        kid             TEXT NOT NULL,
        public_jwk      TEXT NOT NULL,
        thumbprint      TEXT NOT NULL,
        state           TEXT NOT NULL CHECK (state IN ('next', 'current', 'retiring', 'retired', 'revoked')),
        added_epoch     INTEGER NOT NULL,
        state_epoch     INTEGER NOT NULL,
        retire_after    INTEGER,
        PRIMARY KEY (tenant_id, purpose, kid),
        UNIQUE (tenant_id, purpose, thumbprint)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signing_keys_one_current
        ON signing_keys (tenant_id, purpose) WHERE state = 'current'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signing_keys_one_next
        ON signing_keys (tenant_id, purpose) WHERE state = 'next'
    """,
    # -- S8 human links: AIDOCS's side (tenant_id, binding_id, sub) of RFC §8.3 --
    """
    CREATE TABLE IF NOT EXISTS app_links (
        tenant_id       TEXT NOT NULL,
        binding_id      TEXT NOT NULL,
        sub             TEXT NOT NULL,
        linked_at       INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, binding_id, sub)
    )
    """,
)


# -- the ambient transaction ---------------------------------------------------------------------------


class _Tx:
    __slots__ = ("conn", "deferred", "depth")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.deferred: list[Any] = []
        self.depth = 0


_LOCAL = threading.local()


def _ambient() -> dict[str, _Tx]:
    txs = getattr(_LOCAL, "txs", None)
    if txs is None:
        txs = {}
        _LOCAL.txs = txs
    return txs


def _utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def authority_transaction(obj: Any) -> contextlib.AbstractContextManager:
    """``obj.transaction()`` when ``obj`` is a durable authority store, else a no-op.

    Lets a service wrap "decide + audit + write" in the store's transaction
    without knowing whether the store is durable.
    """
    tx = getattr(obj, "transaction", None)
    return tx() if callable(tx) else contextlib.nullcontext()


class AppAuthorityDB:
    """One org's durable application authority file (see module doc)."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        project_roots: Iterable[str | Path] = (),
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        _refuse_under_project_root(Path(db_path), tuple(project_roots))
        self.db_path = Path(db_path)
        self._key = str(self.db_path.resolve())
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or _utc_iso
        try:
            with self.transaction() as conn:
                for stmt in _SCHEMA:
                    conn.execute(stmt)
        except sqlite3.Error as exc:
            raise AuthorityUnavailable("the app authority DB cannot be opened") from exc

    def __repr__(self) -> str:
        return f"<AppAuthorityDB {self.db_path.name}>"

    @property
    def identity(self) -> str:
        """The resolved file this object writes (two objects on one file are one authority)."""
        return self._key

    # -- connections ------------------------------------------------------------------

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

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """ONE ``BEGIN IMMEDIATE`` transaction; a nested call is a SAVEPOINT on it."""
        txs = _ambient()
        tx = txs.get(self._key)
        if tx is not None:
            tx.depth += 1
            name = f"sp_{tx.depth}"
            tx.conn.execute(f"SAVEPOINT {name}")
            try:
                yield tx.conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    tx.conn.execute(f"ROLLBACK TO {name}")
                    tx.conn.execute(f"RELEASE {name}")
                raise
            else:
                tx.conn.execute(f"RELEASE {name}")
            finally:
                tx.depth -= 1
            return
        conn = self._connect()
        tx = _Tx(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            txs[self._key] = tx
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        finally:
            txs.pop(self._key, None)
            conn.close()
            self._flush_deferred(tx.deferred)

    @contextlib.contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The ambient transaction's connection, or a fresh autocommit one."""
        tx = _ambient().get(self._key)
        if tx is not None:
            yield tx.conn
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def in_transaction(self) -> bool:
        return self._key in _ambient()

    # -- DECISION rows ------------------------------------------------------------------

    def decision_sink(self) -> "AuthorityDecisionSink":
        return AuthorityDecisionSink(self)

    def emit_decision(self, event: Any) -> None:
        """Write ``event`` as a DECISION row. Inside a transaction a transition row
        joins it; a refusal row is deferred until the transaction ends."""
        tx = _ambient().get(self._key)
        if tx is not None and getattr(event, "status", None) == _STATUS_REFUSED:
            tx.deferred.append(event)
            return
        with self.transaction() as conn:
            self._insert_decision(conn, event)

    def _flush_deferred(self, events: list[Any]) -> None:
        for event in events:
            try:
                with self.transaction() as conn:
                    self._insert_decision(conn, event)
            except Exception:  # noqa: BLE001 -- refusal rows are best effort; the refusal stands
                pass

    def _insert_decision(self, conn: sqlite3.Connection, event: Any) -> None:
        kind = getattr(event, "kind", None)
        tenant = getattr(event, "tenant_id", None)
        status = getattr(event, "status", None)
        actor = getattr(event, "actor_user_id", None)
        binding_id = getattr(event, "binding_id", None)
        retention = getattr(event, "retention", None)
        metadata = dict(getattr(event, "metadata", {}) or {})
        if retention != "DECISION":
            raise ValueError("only DECISION-class control events are written here")
        for name, value in (("kind", kind), ("tenant_id", tenant), ("status", status), ("actor_user_id", actor)):
            if type(value) is not str or not value:
                raise ValueError(f"control event {name} must be a non-empty string")
        if binding_id is not None and type(binding_id) is not str:
            raise ValueError("control event binding_id must be a string")
        head = conn.execute(
            "SELECT chain_seq, row_hash FROM control_decisions WHERE tenant_id = ? ORDER BY chain_seq DESC LIMIT 1",
            (tenant,),
        ).fetchone()
        seq = int(head[0]) + 1 if head else 1
        prev = str(head[1]) if head else _GENESIS
        event_id = uuid.uuid4().hex
        observed_at = self._clock()
        row = {
            "v": DECISION_HASH_VERSION,
            "tenant_id": tenant,
            "seq": seq,
            "prev_hash": prev,
            "event_id": event_id,
            "kind": kind,
            "status": status,
            "actor_user_id": actor,
            "binding_id": binding_id,
            "observed_at": observed_at,
            "metadata": metadata,
        }
        row_hash = jcs_sha256_hex(row)
        conn.execute(
            "INSERT INTO control_decisions (tenant_id, chain_seq, event_id, kind, status, actor_user_id,"
            " binding_id, observed_at, metadata_json, prev_hash, row_hash, hash_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant, seq, event_id, kind, status, actor, binding_id, observed_at,
             json.dumps(metadata, sort_keys=True, separators=(",", ":")), prev, row_hash, DECISION_HASH_VERSION),
        )

    def decisions(self, tenant_id: str) -> list[dict]:
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT chain_seq, event_id, kind, status, actor_user_id, binding_id, observed_at, metadata_json,"
                " prev_hash, row_hash, hash_version FROM control_decisions WHERE tenant_id = ?"
                " ORDER BY chain_seq, row_id",
                (tenant_id,),
            ).fetchall()
        return [
            {
                "seq": int(r[0]), "event_id": r[1], "kind": r[2], "status": r[3], "actor_user_id": r[4],
                "binding_id": r[5], "observed_at": r[6], "metadata": json.loads(r[7]), "prev_hash": r[8],
                "row_hash": r[9], "hash_version": r[10],
            }
            for r in rows
        ]

    def verify_decisions(self, tenant_id: str) -> tuple[bool, tuple[str, ...]]:
        """Corruption, gaps and forks inside the file. No freshness claim."""
        problems: list[str] = []
        expected_seq, expected_prev = 1, _GENESIS
        for row in self.decisions(tenant_id):
            where = f"seq {row['seq']}"
            if row["seq"] != expected_seq:
                problems.append(f"{where}: expected seq {expected_seq}")
            if row["prev_hash"] != expected_prev:
                problems.append(f"{where}: prev_hash does not link")
            recomputed = jcs_sha256_hex(
                {
                    "v": row["hash_version"],
                    "tenant_id": tenant_id,
                    "seq": row["seq"],
                    "prev_hash": row["prev_hash"],
                    "event_id": row["event_id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "actor_user_id": row["actor_user_id"],
                    "binding_id": row["binding_id"],
                    "observed_at": row["observed_at"],
                    "metadata": row["metadata"],
                }
            )
            if recomputed != row["row_hash"]:
                problems.append(f"{where}: row_hash does not match the row")
            expected_seq, expected_prev = row["seq"] + 1, row["row_hash"]
        return (not problems, tuple(problems))

    # -- stores over this file ---------------------------------------------------------------

    def bindings(self):
        from .authority_stores import SqliteBindingBackend

        return SqliteBindingBackend(self)

    def pairing_state(self):
        from .authority_stores import SqlitePairingState

        return SqlitePairingState(self)

    def seat_pins(self):
        from .authority_stores import SqliteSeatPinStore

        return SqliteSeatPinStore(self)

    def freezes(self):
        from .authority_stores import SqliteFreezeStore

        return SqliteFreezeStore(self)

    def pending(self):
        from .authority_stores import SqlitePendingStore

        return SqlitePendingStore(self)

    def key_directory(self):
        from .authority_stores import SqliteOrgKeyDirectory

        return SqliteOrgKeyDirectory(self)

    def links(self):
        from .authority_stores import SqliteLinkStore

        return SqliteLinkStore(self)


class AuthorityDecisionSink:
    """The ``ControlPlaneAuditSink`` over :class:`AppAuthorityDB` (same-transaction writes)."""

    __slots__ = ("db",)

    def __init__(self, db: AppAuthorityDB) -> None:
        self.db = db

    def emit(self, event: Any) -> None:
        self.db.emit_decision(event)

    def __repr__(self) -> str:
        return f"<AuthorityDecisionSink {self.db!r}>"


def require_same_authority(store_db: AppAuthorityDB, sink: Any) -> None:
    """A durable store's transitions MUST be audited on its own file (ruling 2):
    any other sink would reopen the audit/state crash window."""
    if not isinstance(sink, AuthorityDecisionSink) or sink.db.identity != store_db.identity:
        raise ValueError("a durable authority store requires the decision sink of the SAME authority DB")

