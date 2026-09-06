"""Server-side persistence for the #442 sync hub (`/sync/events`).

The transport layer owns HTTP + authorization; THIS module owns durable state.
Both halves reuse the EXISTING machinery (§XXII extend-don't-fork):

* accepted events land in the project's normal ``GitEventTransport`` outbox
  (write-once ⇒ re-submitting an accepted event is idempotent), and
* authority is recorded through the EXISTING ``sync_store.record_receipt`` —
  ONE authority ledger, no fork. A server-accepted event is therefore
  authoritative to the normal receipted-only fold, with no fold changes.

The only NEW state is canonical ORDER: the server assigns a monotonic ``seq``
per project so ``GET /sync/events?since=<cursor>`` is a stable, replay-safe
walk. ``cursor`` is that opaque seq. A forged client HLC never affects it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Both sites below were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and neither set a
# single pragma, so this store ran with foreign_keys OFF (its FKs inert),
# no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: AUDIT, i.e. the FULL this file already had. sync_server_order
# IS the canonical order: the seq is what makes a re-submitted event
# idempotent and what every client cursor points into. Nothing re-derives
# it, and losing the last rows would re-issue seqs that clients have
# already walked past -- the ledger disagreeing with itself. It is written
# once per accepted event, at network speed.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect
from .sync_store import GitEventTransport, SyncEvent, record_receipt


def _db_path(project_root: Path) -> Path:
    # SAME per-project store DB the receipts live in — no new database.
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _ensure_order_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_server_order ("
        " event_id TEXT PRIMARY KEY,"
        " project_id TEXT NOT NULL,"
        " stream TEXT NOT NULL,"
        " server_hlc TEXT NOT NULL,"
        " seq INTEGER NOT NULL,"
        " payload TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_server_order_seq "
        "ON sync_server_order(project_id, seq)"
    )


def record_accepted(
    project_root: Path, event: SyncEvent, *, server_hlc: str, project_id: str
) -> int:
    """Persist ONE server-accepted event and return its canonical ``seq``.

    Idempotent on event_id: re-submitting an already-accepted event returns the
    SAME seq and never duplicates (the endpoint contract promises this).
    """
    root = Path(project_root)
    db = _db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        _ensure_order_table(conn)
        row = conn.execute(
            "SELECT seq FROM sync_server_order WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if row is not None:
            return int(row[0])  # idempotent replay
        nxt = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM sync_server_order WHERE project_id = ?",
            (str(project_id),),
        ).fetchone()
        seq = int(nxt[0]) if nxt else 1
        conn.execute(
            "INSERT INTO sync_server_order "
            "(event_id, project_id, stream, server_hlc, seq, payload) VALUES (?,?,?,?,?,?)",
            (
                event.event_id,
                str(project_id),
                event.stream,
                str(server_hlc),
                seq,
                json.dumps(_event_as_dict(event), sort_keys=True, ensure_ascii=False),
            ),
        )
        conn.commit()

    # Durable event body in the normal outbox (write-once) + THE authority
    # ledger entry, so the untouched receipted-only fold will apply it.
    GitEventTransport(root).append(event)
    record_receipt(root, event.stream, event.event_id, event.entity_id)
    return seq


def read_since(project_root: Path, project_id: str, cursor: str = "") -> tuple[list[dict], str]:
    """Server-ordered events for ONE project after ``cursor``.

    TENANCY FLOOR: rows are filtered by ``project_id`` in the QUERY — a caller
    can never read another tenant's stream even by guessing a cursor.
    """
    try:
        start = int(str(cursor or "0") or 0)
    except (TypeError, ValueError):
        start = 0
    db = _db_path(Path(project_root))
    if not db.is_file():
        return [], str(start)
    with _canonical_connect(
        db, durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        _ensure_order_table(conn)
        rows = conn.execute(
            "SELECT seq, payload FROM sync_server_order "
            "WHERE project_id = ? AND seq > ? ORDER BY seq ASC LIMIT 500",
            (str(project_id), start),
        ).fetchall()
    out: list[dict] = []
    last = start
    for seq, payload in rows:
        try:
            out.append(json.loads(payload))
        except Exception:  # noqa: BLE001 — a corrupt row must not break the pull
            continue
        last = int(seq)
    return out, str(last)


def _event_as_dict(event: SyncEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "stream": event.stream,
        "entity_id": event.entity_id,
        "op": event.op,
        "actor": event.actor,
        "hlc": event.hlc,
        "ts": event.ts,
        "fields": event.fields,
        "session_id": event.session_id,
        "project_id": event.project_id,
    }
