"""palace_bridge_tx — two-ledger transaction journal (RFC 003 §7).

A small SQLite-backed journal that records every cross-store palace
operation (palace's chroma + AIDOCS's index DB) so partial failures
are recoverable and auditable.

Not a full inter-process outbox (v0.1's bridge_outbox). In-process
two-store coordination doesn't need a retry queue, async reconciliation,
or delivery-status state machines — it needs a receipt for what
happened to each ledger.

This module lives in mempalace's tree so it can be installed alongside
the rest of MemPalace as a single pip dep. AIDOCS imports
``BridgeTxJournal`` from here and wires it into
``AidocsServiceHub.bridge_tx``.

Standalone use: the journal also works without AIDOCS present, stored
alongside the palace at ``<palace>/bridge_tx.sqlite3``. Standalone
PalaceService calls still receive BridgeTxHandle objects from this
journal.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

from .conjoined_types import BridgeTxHandle, PartialBridgeFailureEvent


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection pragmas (#746)
# ---------------------------------------------------------------------------
#
# THE ONE PLACE THIS PACKAGE OPENS A DATABASE, and the reason the pragmas are
# spelled out here instead of imported: AIDOCS owns the canonical connect
# (``aidocs_mcp._sqlite_connect.connect``, #755 / empire-doctrine XXII), but
# mempalace is installable and usable WITHOUT AIDOCS present (see the module
# docstring's "Standalone use"), so importing it would invert the dependency and
# break every standalone install. The pragma SET is therefore duplicated
# deliberately; it is not a second opinion. Keep it identical to the canonical
# one.
#
# WHAT WENT WRONG (#746, measured on the operator's box 2026-08-02). This
# journal ran on sqlite's DEFAULT rollback journal, where a writer takes an
# EXCLUSIVE lock over the WHOLE database and a concurrent reader cannot overlap
# it at all -- SQLITE_BUSY, surfaced as a bare "database is locked". The
# prompt-submit path is exactly that shape (this is the "tx" store its degraded
# banner named: ``prompt_submit_transaction_degraded[transaction_lock]``), and
# every degraded prompt ran with NLP, memory, doctrine and palace surfacing
# SKIPPED. WAL lets readers and the single writer proceed concurrently.
#
#   journal_mode = WAL   readers no longer block on the writer. PERSISTED IN THE
#                        FILE HEADER, so it is established once per file per
#                        process -- the memo below, not a micro-optimisation:
#                        re-issuing it per connection was measured as pure waste.
#   synchronous = NORMAL 8-10x cheaper commits. Under WAL this CANNOT corrupt
#                        the database: process death / taskkill / SIGKILL lose
#                        NOTHING (committed data lives in the WAL and the OS page
#                        cache, which outlives the process). Only a kernel panic
#                        or power cut can lose commits since the last checkpoint.
#                        Do not "harden" this back to FULL on the belief that it
#                        is a corruption trade -- it is not.
#   busy_timeout         a bounded wait that refuses beats an unbounded one that
#                        hangs.
#   foreign_keys = ON    NOT performance -- CORRECTNESS. SQLite defaults it OFF
#                        PER CONNECTION, which leaves every FK constraint this
#                        schema declares INERT. Faster refusal is good; faster
#                        bypass is treason.

_wal_established: set[str] = set()


def _connect(db_path: Path, *, timeout: float = 5.0, busy_timeout_ms: int = 5000):
    """Open ``db_path`` with the runtime pragma set applied."""
    key = str(db_path)
    conn = sqlite3.connect(key, timeout=timeout)
    try:
        if key not in _wal_established:
            conn.execute("PRAGMA journal_mode = WAL")
            _wal_established.add(key)
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    except sqlite3.DatabaseError:
        # A locked/again pragma must NEVER break the connection (fail-open on
        # PERFORMANCE only). Drop the memo so the next connect RETRIES
        # establishing WAL rather than assuming a failed attempt succeeded.
        _wal_established.discard(key)
    # DELIBERATELY OUTSIDE THE except: foreign_keys is correctness, not
    # performance. If it cannot be set, the caller gets the DatabaseError rather
    # than a connection that silently enforces no constraints.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_cache_clear() -> None:
    """Forget the per-process WAL memo (tests, or a caller that moved files).

    Correctness never depends on this: a dropped memo costs one extra pragma.
    """
    _wal_established.clear()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS palace_bridge_tx (
    tx_id              TEXT PRIMARY KEY,
    operation          TEXT NOT NULL,
    audit_event_id     TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    committed_at       TEXT,
    palace_write_status   TEXT NOT NULL CHECK (palace_write_status IN
                          ('pending', 'committed', 'failed', 'rolled_back')),
    palace_write_ref      TEXT,
    aidocs_write_status   TEXT NOT NULL CHECK (aidocs_write_status IN
                          ('pending', 'committed', 'failed', 'rolled_back', 'not_required')),
    aidocs_write_ref      TEXT,
    rollback_status       TEXT CHECK (rollback_status IN
                          ('not_attempted', 'palace_rolled_back',
                           'aidocs_rolled_back', 'both_rolled_back', 'partial_failure')),
    rollback_reason       TEXT,
    operator_resolution   TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridge_tx_session
    ON palace_bridge_tx(session_id);

CREATE INDEX IF NOT EXISTS idx_bridge_tx_unresolved
    ON palace_bridge_tx(committed_at, rollback_status);
"""


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


class SQLiteBridgeTxHandle(BridgeTxHandle):
    """Concrete BridgeTxHandle backed by a SQLite row."""

    def __init__(
        self,
        *,
        journal: "BridgeTxJournal",
        tx_id: str,
        operation: str,
        audit_event_id: str,
        session_id: str,
    ):
        super().__init__(tx_id=tx_id, operation=operation)
        self._journal = journal
        self._audit_event_id = audit_event_id
        self._session_id = session_id
        self._palace_status: str = "pending"
        self._aidocs_status: str = "pending"
        self._palace_ref: Optional[str] = None
        self._aidocs_ref: Optional[str] = None
        self._closed = False

    def record_palace_write(
        self,
        *,
        ref: Optional[str] = None,
        status: Literal["pending", "committed", "failed", "rolled_back"],
    ) -> None:
        self._palace_status = status
        if ref is not None:
            self._palace_ref = ref
        self._journal._update(
            self.tx_id,
            palace_write_status=status,
            palace_write_ref=ref,
        )

    def record_aidocs_write(
        self,
        *,
        ref: Optional[str] = None,
        status: Literal["pending", "committed", "failed", "rolled_back", "not_required"],
    ) -> None:
        self._aidocs_status = status
        if ref is not None:
            self._aidocs_ref = ref
        self._journal._update(
            self.tx_id,
            aidocs_write_status=status,
            aidocs_write_ref=ref,
        )

    def commit(self) -> None:
        if self._closed:
            return
        # Backfill statuses if neither side ever recorded — caller
        # forgot to call record_*_write. Default both to committed.
        if self._palace_status == "pending":
            self.record_palace_write(status="committed")
        if self._aidocs_status == "pending":
            self.record_aidocs_write(status="not_required")
        self._journal._update(self.tx_id, committed_at=_now())
        self._closed = True

    def rollback(self, reason: str) -> None:
        if self._closed:
            return
        # Determine rollback_status from current state.
        palace_committed = self._palace_status == "committed"
        aidocs_committed = self._aidocs_status == "committed"
        if palace_committed and not aidocs_committed:
            rb = "palace_rolled_back"
        elif aidocs_committed and not palace_committed:
            rb = "aidocs_rolled_back"
        elif palace_committed and aidocs_committed:
            rb = "both_rolled_back"
        else:
            rb = "not_attempted"
        self._journal._update(
            self.tx_id,
            rollback_status=rb,
            rollback_reason=str(reason)[:500],
        )
        self._closed = True

    def mark_partial_failure(self, reason: str) -> PartialBridgeFailureEvent:
        """Promote this transaction to partial_failure state.

        Called by PalaceService when a compensating rollback ALSO
        fails — operator must reconcile.
        """
        self._journal._update(
            self.tx_id,
            rollback_status="partial_failure",
            rollback_reason=str(reason)[:500],
        )
        self._closed = True
        return PartialBridgeFailureEvent(
            event_id=f"pbf-{uuid.uuid4().hex[:16]}",
            tx_id=self.tx_id,
            palace_write_ref=self._palace_ref or "",
            aidocs_failure_reason=str(reason),
            palace_rollback_failure_reason=str(reason),
            surfaced_at=_now(),
        )


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class BridgeTxJournal:
    """SQLite-backed palace_bridge_tx journal.

    One journal instance per project (one DB per
    ``<project>/.MEMORY/.index/palace_bridge_tx.sqlite3``). Constructed
    by AIDOCS during AidocsServiceHub init and shared across the
    process; thread-safe through a lock around writes.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        # Establishes journal_mode=WAL on the FILE (#746) as a side effect of
        # being the first open, which is why creation goes through _connect too.
        conn = _connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # 10s stays this journal's own busy budget rather than dropping to the
        # helper default: WAL is meant to REMOVE the waiting, not to be an excuse
        # to shorten the one deliberate wait that was already here.
        conn = _connect(self.db_path, timeout=10.0, busy_timeout_ms=10_000)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def begin(
        self,
        *,
        operation: str,
        audit_event_id: str,
        session_id: str,
    ) -> SQLiteBridgeTxHandle:
        """Open a new transaction. Returns a handle the caller drives
        through ``record_palace_write`` / ``record_aidocs_write`` /
        ``commit`` / ``rollback``.
        """
        tx_id = f"tx-{uuid.uuid4().hex[:24]}"
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO palace_bridge_tx (
                    tx_id, operation, audit_event_id, session_id,
                    started_at, palace_write_status, aidocs_write_status,
                    rollback_status
                ) VALUES (?, ?, ?, ?, ?, 'pending', 'pending', 'not_attempted')""",
                (tx_id, operation, audit_event_id, session_id, _now()),
            )
        return SQLiteBridgeTxHandle(
            journal=self,
            tx_id=tx_id,
            operation=operation,
            audit_event_id=audit_event_id,
            session_id=session_id,
        )

    def _update(self, tx_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [tx_id]
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE palace_bridge_tx SET {cols} WHERE tx_id = ?",
                vals,
            )

    def unresolved(self) -> list[dict]:
        """Return all transactions that are either still in-flight or
        ended in partial_failure state.

        Called at AIDOCS server boot to surface partial failures that
        need operator reconciliation.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT tx_id, operation, palace_write_status,
                          aidocs_write_status, rollback_status,
                          started_at, audit_event_id, session_id
                   FROM palace_bridge_tx
                   WHERE committed_at IS NULL
                      OR rollback_status = 'partial_failure'
                   ORDER BY started_at""",
            ).fetchall()
        return [
            {
                "tx_id": r[0],
                "operation": r[1],
                "palace_write_status": r[2],
                "aidocs_write_status": r[3],
                "rollback_status": r[4],
                "started_at": r[5],
                "audit_event_id": r[6],
                "session_id": r[7],
            }
            for r in rows
        ]

    def resolve(
        self,
        tx_id: str,
        *,
        operator_resolution: Literal[
            "palace_wins", "aidocs_wins", "merge", "discard",
        ],
    ) -> None:
        """Mark an unresolved transaction as resolved by operator."""
        self._update(
            tx_id,
            operator_resolution=operator_resolution,
            committed_at=_now(),
        )
