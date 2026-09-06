"""RFC-4 Phase D — palace_stale_signals.

Three states only (KISSSY — per RFC-4 §3 Phase D simplification):

    active   — content_hash matches current unit
    stale    — content_hash mismatches; unit still exists
    deleted  — unit_id no longer resolves (refactor / removal)

That's it. No "potentially_stale", no fuzzy matches, no rebase tracking.

Adopt by copying to ``aidocs_mcp/palace_stale_signals.py``.

Lives co-located with the bridge_tx journal at
``<project>/.MEMORY/.index/palace_bridge_tx.sqlite3`` (same DB, separate
table) so unresolved-bridge-tx and palace-staleness can be surveyed
together at boot.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

StalenessState = Literal["active", "stale", "deleted"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS palace_stale_signals (
    drawer_id           TEXT PRIMARY KEY,
    unit_id             TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('active', 'stale', 'deleted')),
    old_content_hash    TEXT NOT NULL DEFAULT '',
    new_content_hash    TEXT NOT NULL DEFAULT '',
    detected_at         TEXT NOT NULL,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_palace_stale_signals_unit
    ON palace_stale_signals(unit_id);

CREATE INDEX IF NOT EXISTS idx_palace_stale_signals_state
    ON palace_stale_signals(state)
    WHERE state IN ('stale', 'deleted');
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class PalaceStaleSignals:
    """SQLite-backed staleness tracker for palace anchors.

    Called by:
      - ai_index_sync (AIDOCS-side, Phase D extension): when a unit's
        content_hash changes, mark anchored drawers `stale`. When a
        unit_id no longer resolves, mark `deleted`.
      - PalaceService.search (read-path filter): consult state during
        result hydration so stale drawers surface with a warning marker
        and deleted drawers are hidden by default.
      - hub.palace.refresh_anchor (operator command): transition
        `stale` → `active` after re-ingesting the drawer with new
        content.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # #746: both connects below ran on sqlite's DEFAULT rollback journal, where
    # a writer holds an EXCLUSIVE lock over the whole file and any concurrent
    # READER gets SQLITE_BUSY -- "database is locked". This store is consulted on
    # the palace READ path (PalaceService.search hides retired drawers through
    # it) while ai_index_sync WRITES it, so reader and writer collide by
    # construction. Routed through the ONE canonical connect (#755,
    # empire-doctrine XXII) instead of re-deciding the pragmas here: it
    # establishes journal_mode=WAL (persisted on the FILE, hence once per file
    # per process), synchronous, busy_timeout and foreign_keys=ON.
    #
    # The mkdir stays in __init__ because the helper deliberately never creates a
    # directory -- opening a store is not an adoption.

    def _init_db(self) -> None:
        with _canonical_connect(self.db_path, durability=_Durability.RUNTIME) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # busy_timeout stays at this store's own 10s rather than the helper's 2s
        # default: the value was chosen for a tracker that the index-sync writer
        # can hold for a while, and WAL is meant to REMOVE the waits, not to be
        # an excuse to shorten the one deliberate wait already here.
        conn = _canonical_connect(
            self.db_path,
            durability=_Durability.RUNTIME,
            timeout=10.0,
            busy_timeout_ms=10_000,
        )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def mark_stale(
        self,
        *,
        drawer_id: str,
        unit_id: str,
        old_content_hash: str,
        new_content_hash: str,
    ) -> None:
        """Mark a drawer as stale (content_hash mismatch detected)."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO palace_stale_signals
                   (drawer_id, unit_id, state, old_content_hash,
                    new_content_hash, detected_at)
                   VALUES (?, ?, 'stale', ?, ?, ?)
                   ON CONFLICT(drawer_id) DO UPDATE SET
                       state = 'stale',
                       new_content_hash = excluded.new_content_hash,
                       detected_at = excluded.detected_at,
                       resolved_at = NULL""",
                (drawer_id, unit_id, old_content_hash, new_content_hash, _now()),
            )

    def mark_deleted(self, *, drawer_id: str, unit_id: str) -> None:
        """Mark a drawer's anchor as deleted (unit_id no longer resolves)."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO palace_stale_signals
                   (drawer_id, unit_id, state, detected_at)
                   VALUES (?, ?, 'deleted', ?)
                   ON CONFLICT(drawer_id) DO UPDATE SET
                       state = 'deleted',
                       detected_at = excluded.detected_at,
                       resolved_at = NULL""",
                (drawer_id, unit_id, _now()),
            )

    def mark_active(self, *, drawer_id: str) -> None:
        """Clear a stale/deleted signal (transition back to active).

        Called by hub.palace.refresh_anchor after re-ingesting the drawer.
        """
        with self._lock, self._conn() as conn:
            conn.execute(
                """UPDATE palace_stale_signals
                   SET state = 'active', resolved_at = ?
                   WHERE drawer_id = ?""",
                (_now(), drawer_id),
            )

    # ------------------------------------------------------------------
    # Read API for the read-path filter
    # ------------------------------------------------------------------

    def get_state(self, drawer_id: str) -> StalenessState:
        """Return the current state for a drawer. Defaults to 'active'
        when no row exists (drawer hasn't been flagged).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state FROM palace_stale_signals WHERE drawer_id = ?",
                (drawer_id,),
            ).fetchone()
            if not row:
                return "active"
            return row[0]  # type: ignore[return-value]

    def get_states(self, drawer_ids: list[str]) -> dict[str, StalenessState]:
        """Batch lookup. Default to 'active' for drawers without a row."""
        if not drawer_ids:
            return {}
        placeholders = ",".join("?" * len(drawer_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT drawer_id, state FROM palace_stale_signals "
                f"WHERE drawer_id IN ({placeholders})",
                drawer_ids,
            ).fetchall()
        out: dict[str, StalenessState] = dict.fromkeys(drawer_ids, "active")
        for row in rows:
            out[row[0]] = row[1]
        return out

    def list_stale_for_unit(self, unit_id: str) -> list[str]:
        """Return drawer_ids currently in stale or deleted state for a unit."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT drawer_id FROM palace_stale_signals
                   WHERE unit_id = ? AND state IN ('stale', 'deleted')""",
                (unit_id,),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Boot-time survey
    # ------------------------------------------------------------------

    def unresolved_count(self) -> dict[str, int]:
        """Return counts of stale + deleted drawers. Used at boot to
        surface to the operator.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT state, COUNT(*) FROM palace_stale_signals
                   WHERE state IN ('stale', 'deleted')
                   GROUP BY state""",
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}
