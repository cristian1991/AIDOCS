"""Lane 1 MCP Events: the gate-wide wake budget ledger (#1226, r0b1 9a50a086-8ac).

Each project keeps its wakes in its own conductor_comms store, but OpenAI's
event-task limit is per account across ALL of a user's event tasks. So the budget
cannot live in any one project store: a store-local count gave a principal
subscribed in N projects N times the budget.

This is the one place a first claim is admitted. reserve() counts the key's
reservations in the last hour and day and inserts one only when both are under the
caps, in a single BEGIN IMMEDIATE transaction on this one file, so concurrent
ticks over different projects cannot both take the last slot.

A reservation belongs to ONE claim: its id is that claim's lease token. A wake
parked before it was ever POSTed releases its own claim's reservation and no
other, so a late refund can never delete the slot of a newer claim of the same
event that was already delivered (r0b1 c9f4642c-84b). A crash between reserve and
claim leaks a slot until it ages out of the window: that fails toward fewer
wakes, never more.

The key is budget_key(row): the gate principal (codenexus user_id). Design decision
(2026-10-24): one ChatGPT account is one AIDOCS user, so this key is the per-account
key OpenAI's limit needs. If that mapping ever changes, budget_key is the one seam.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from . import mcp_events_store as store
from ._sqlite_connect import Durability
from ._sqlite_connect import connect as _canonical_connect

FILENAME = "mcp_events_budget.sqlite3"
_DAY = 86400.0
_HOUR = 3600.0

_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS wake_reservations ("
        "reservation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, budget_key TEXT NOT NULL, "
        "reserved_at REAL NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS wake_reservations_by_key ON wake_reservations (budget_key, reserved_at)",
)


def budget_key(row) -> str:
    """Whose quota a wake spends: the gate principal."""
    return str(row["principal_id"])


class GateBudget:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        # The shared chokepoint (#755) applies WAL, busy_timeout and synchronous;
        # AUDIT durability because this is the quota of record. Autocommit, since
        # every write here is its own explicit BEGIN IMMEDIATE ... COMMIT or a
        # single statement (release) that must not sit in an implicit transaction.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(self.path, durability=Durability.AUDIT, timeout=30, busy_timeout_ms=30000)
        conn.isolation_level = None
        for ddl in _SCHEMA:
            conn.execute(ddl)
        return conn

    def reserve(
        self, key: str, reservation_id: str, *, now: float, caps: tuple[int, int], event_id: str = ""
    ) -> bool:
        """Take one slot of `key`'s quota as `reservation_id`, or refuse. A repeat of
        the same reservation id is that same slot."""
        per_hour, per_day = caps
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM wake_reservations WHERE reserved_at <= ?", (now - _DAY,))
                if conn.execute(
                    "SELECT 1 FROM wake_reservations WHERE reservation_id = ?", (reservation_id,)
                ).fetchone():
                    conn.execute("COMMIT")
                    return True

                def since(window: float) -> int:
                    return conn.execute(
                        "SELECT COUNT(*) FROM wake_reservations WHERE budget_key = ? AND reserved_at > ?",
                        (key, now - window),
                    ).fetchone()[0]

                if since(_HOUR) >= per_hour or since(_DAY) >= per_day:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT INTO wake_reservations (reservation_id, event_id, budget_key, reserved_at) "
                    "VALUES (?, ?, ?, ?)",
                    (reservation_id, event_id or reservation_id, key, now),
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def release(self, reservation_id: str) -> None:
        """Give back exactly this claim's slot (it was parked: nothing was sent)."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM wake_reservations WHERE reservation_id = ?", (reservation_id,))

    def admit(self, row, reservation_id: str, *, now: float) -> bool:
        """claim_due's reserve hook: this claim of the row spends its key's quota."""
        return self.reserve(
            budget_key(row), reservation_id, now=now, caps=store.budget_caps(), event_id=str(row["event_id"])
        )
