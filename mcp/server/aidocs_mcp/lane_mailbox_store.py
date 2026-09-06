"""Lane mailbox: conductor → worker prompt delivery via SQLite.

Architecture
------------
Lane workers complete a unit of work and exit. To hand a worker a new
instruction the conductor writes here (ai_lane_send) and resumes the
worker's host session (lane_resume_dispatcher: `<host> --resume`/`-s`/
`resume <session-id>`); the pending prompt for `worker_id` is injected
into the worker's next-turn input by PromptMutator.worker_lane_intercept
(host-agnostic — the UPS pipeline every host calls, no host hook required).
Legacy ScheduleWakeup park-and-wake is retired (Empire doctrine #103).

Schema
------
    session_lane_mailbox(
        mailbox_id INTEGER PK AUTOINCREMENT,
        worker_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        prompt TEXT NOT NULL,
        author_session_id TEXT,           -- conductor session that wrote
        author_task_id TEXT,              -- active task on conductor at write time
        written_at TEXT NOT NULL,         -- ISO8601 UTC
        consumed_at TEXT,                 -- null until worker reads
        status TEXT NOT NULL              -- 'pending' | 'consumed' | 'expired'
    )

Audit
-----
Every put/take/expire calls ExecutionIndexStore.record_event with
action_kind in ('lane_mailbox_write', 'lane_mailbox_consume',
'lane_mailbox_expire') so the Merkle chain tracks every message.

TTL
---
Default 15 min. Stale rows flip status='expired' (TTL exhausted); an
expired mailbox yields no instruction when the worker is resumed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: AUDIT, i.e. the FULL this file already had. take/drain write
# a CONSUMPTION MARK (status='consumed'), and a consumption mark a power
# cut un-does re-delivers a conductor instruction that was already acted
# on. The prompt itself is also the only copy -- nothing re-derives a
# conductor's message. Rows are written at conductor speed and every
# put/take/expire is Merkle-chained, so FULL costs nothing here.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_lane_mailbox (
            mailbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            author_session_id TEXT,
            author_task_id TEXT,
            written_at TEXT NOT NULL,
            consumed_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """,
    )
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(session_lane_mailbox)").fetchall()
    }
    additive = {
        "protocol": "TEXT NOT NULL DEFAULT ''",
        "message_id": "TEXT NOT NULL DEFAULT ''",
        "correlation_id": "TEXT NOT NULL DEFAULT ''",
        "sender_actor_id": "TEXT NOT NULL DEFAULT ''",
        "target_actor_id": "TEXT NOT NULL DEFAULT ''",
        "lane_id": "TEXT NOT NULL DEFAULT ''",
        "message_kind": "TEXT NOT NULL DEFAULT ''",
        "severity": "TEXT NOT NULL DEFAULT ''",
    }
    for column, ddl in additive.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE session_lane_mailbox ADD COLUMN {column} {ddl}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_mailbox_worker "
        "ON session_lane_mailbox (worker_id, status, written_at)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_mailbox_route "
        "ON session_lane_mailbox (session_id, target_actor_id, lane_id, status, written_at)",
    )
    conn.commit()



def _now() -> str:
    return datetime.now(UTC).isoformat()


class LaneMailboxStore:
    """Per-worker prompt queue. FIFO per worker_id."""

    DEFAULT_TTL_SECONDS = 15 * 60

    def put(
        self,
        project_root: Path,
        *,
        worker_id: str,
        session_id: str,
        prompt: str,
        author_session_id: str | None = None,
        author_task_id: str | None = None,
        protocol: str = "",
        message_id: str = "",
        correlation_id: str = "",
        sender_actor_id: str = "",
        target_actor_id: str = "",
        lane_id: str = "",
        message_kind: str = "",
        severity: str = "",
    ) -> int:
        """Write a prompt addressed to `worker_id`. Returns mailbox_id.

        Caller usually is the conductor-agent via `lane_send_prompt`
        MCP tool. Emits a lane_mailbox_write audit event.
        """
        db = _db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            cur = conn.execute(
                "INSERT INTO session_lane_mailbox "
                "(worker_id, session_id, prompt, author_session_id, "
                "author_task_id, written_at, status, protocol, message_id, "
                "correlation_id, sender_actor_id, target_actor_id, lane_id, "
                "message_kind, severity) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    worker_id,
                    session_id,
                    prompt,
                    author_session_id,
                    author_task_id,
                    _now(),
                    str(protocol or ""),
                    str(message_id or ""),
                    str(correlation_id or ""),
                    str(sender_actor_id or ""),
                    str(target_actor_id or worker_id),
                    str(lane_id or ""),
                    str(message_kind or ""),
                    str(severity or ""),
                ),
            )
            conn.commit()
            mailbox_id = int(cur.lastrowid or 0)
        _audit(
            project_root,
            session_id,
            action_kind="lane_mailbox_write",
            target_entity=worker_id,
            payload={
                "mailbox_id": mailbox_id,
                "prompt_preview": prompt[:200],
                "author_session_id": author_session_id,
                "author_task_id": author_task_id,
                "protocol": str(protocol or ""),
                "message_id": str(message_id or ""),
                "correlation_id": str(correlation_id or ""),
                "sender_actor_id": str(sender_actor_id or ""),
                "target_actor_id": str(target_actor_id or worker_id),
                "lane_id": str(lane_id or ""),
                "message_kind": str(message_kind or ""),
                "severity": str(severity or ""),
            },
        )
        return mailbox_id

    def take(
        self,
        project_root: Path,
        *,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """Pop the oldest pending prompt for worker_id. Marks consumed.

        Called by claude_hook at lane-worker wake time. Emits
        lane_mailbox_consume audit event. Returns None when empty.
        """
        db = _db_path(project_root)
        if not db.is_file():
            return None
        now = _now()
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT mailbox_id, session_id, prompt, author_session_id, "
                "author_task_id, written_at, protocol, message_id, correlation_id, "
                "sender_actor_id, target_actor_id, lane_id, message_kind, severity "
                "FROM session_lane_mailbox "
                "WHERE worker_id = ? AND status = 'pending' "
                "ORDER BY written_at ASC LIMIT 1",
                (worker_id,),
            ).fetchone()
            if row is None:
                return None
            mailbox_id = int(row[0])
            conn.execute(
                "UPDATE session_lane_mailbox SET consumed_at = ?, "
                "status = 'consumed' WHERE mailbox_id = ?",
                (now, mailbox_id),
            )
            conn.commit()
        result = {
            "mailbox_id": mailbox_id,
            "session_id": row[1],
            "prompt": row[2],
            "author_session_id": row[3],
            "author_task_id": row[4],
            "written_at": row[5],
            "consumed_at": now,
            "protocol": row[6],
            "message_id": row[7],
            "correlation_id": row[8],
            "sender_actor_id": row[9],
            "target_actor_id": row[10],
            "lane_id": row[11],
            "message_kind": row[12],
            "severity": row[13],
        }
        _audit(
            project_root,
            str(row[1]),
            action_kind="lane_mailbox_consume",
            target_entity=worker_id,
            payload={
                "mailbox_id": mailbox_id,
                "age_seconds": _age_seconds(row[5], now),
                "protocol": row[6],
                "message_id": row[7],
                "correlation_id": row[8],
                "sender_actor_id": row[9],
                "target_actor_id": row[10],
                "lane_id": row[11],
                "message_kind": row[12],
                "severity": row[13],
            },
        )
        return result

    def consume_pending(
        self,
        project_root: Path,
        *,
        worker_id: str,
    ) -> int:
        """Mark ALL pending prompts for worker_id consumed. Returns count.

        WAR D (#452/#217): the worker's own inbox read is the drain that
        clears the unread-message block; without consumption the block
        would persist across every subsequent tool call. Emits ONE
        lane_mailbox_consume audit event with the drained count.
        """
        db = _db_path(project_root)
        if not db.is_file():
            return 0
        now = _now()
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            rows = conn.execute(
                "SELECT mailbox_id, session_id FROM session_lane_mailbox "
                "WHERE worker_id = ? AND status = 'pending'",
                (worker_id,),
            ).fetchall()
            if not rows:
                return 0
            conn.executemany(
                "UPDATE session_lane_mailbox SET consumed_at = ?, "
                "status = 'consumed' WHERE mailbox_id = ?",
                [(now, int(r[0])) for r in rows],
            )
            conn.commit()
        _audit(
            project_root,
            str(rows[0][1] or ""),
            action_kind="lane_mailbox_consume",
            target_entity=worker_id,
            payload={
                "consumed_count": len(rows),
                "mailbox_ids": [int(r[0]) for r in rows],
                "via": "ai_lane_inbox",
            },
        )
        return len(rows)

    def peek(
        self,
        project_root: Path,
        *,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """Read oldest pending without consuming. For dashboards/tests."""
        db = _db_path(project_root)
        if not db.is_file():
            return None
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT mailbox_id, prompt, written_at, session_id, protocol, "
                "message_id, correlation_id, sender_actor_id, target_actor_id, "
                "lane_id, message_kind, severity FROM session_lane_mailbox "
                "WHERE worker_id = ? AND status = 'pending' "
                "ORDER BY written_at ASC LIMIT 1",
                (worker_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "mailbox_id": int(row[0]),
            "prompt": row[1],
            "written_at": row[2],
            "session_id": row[3],
            "protocol": row[4],
            "message_id": row[5],
            "correlation_id": row[6],
            "sender_actor_id": row[7],
            "target_actor_id": row[8],
            "lane_id": row[9],
            "message_kind": row[10],
            "severity": row[11],
        }

    def expire_stale(
        self,
        project_root: Path,
        *,
        ttl_seconds: int | None = None,
    ) -> int:
        """Mark mailbox rows older than ttl as expired. Returns count.

        Called opportunistically by the hook on wake, and by a
        scheduled sweep. Emits one lane_mailbox_expire event per
        worker_id with the expired count, not per row (less audit
        noise).
        """
        ttl = ttl_seconds if ttl_seconds is not None else self.DEFAULT_TTL_SECONDS
        db = _db_path(project_root)
        if not db.is_file():
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=ttl)).isoformat()
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            rows = conn.execute(
                "SELECT mailbox_id, worker_id, session_id FROM "
                "session_lane_mailbox WHERE status = 'pending' "
                "AND written_at < ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                return 0
            conn.executemany(
                "UPDATE session_lane_mailbox SET status = 'expired' WHERE mailbox_id = ?",
                [(r[0],) for r in rows],
            )
            conn.commit()
        per_worker: dict[str, tuple[str, int]] = {}
        for _mid, w, s in rows:
            prev = per_worker.get(w, (s, 0))
            per_worker[w] = (prev[0], prev[1] + 1)
        for worker_id, (session_id, count) in per_worker.items():
            _audit(
                project_root,
                session_id,
                action_kind="lane_mailbox_expire",
                target_entity=worker_id,
                payload={"expired_count": count, "ttl_seconds": ttl},
            )
        return len(rows)

    def list_for_worker(
        self,
        project_root: Path,
        *,
        worker_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Full history (pending + consumed + expired) for a worker,
        newest first. Diagnostic surface for dashboards.
        """
        db = _db_path(project_root)
        if not db.is_file():
            return []
        with _canonical_connect(
            db, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            _init(conn)
            rows = conn.execute(
                "SELECT mailbox_id, prompt, written_at, consumed_at, "
                "status, author_session_id, author_task_id, session_id, protocol, "
                "message_id, correlation_id, sender_actor_id, target_actor_id, "
                "lane_id, message_kind, severity FROM session_lane_mailbox "
                "WHERE worker_id = ? "
                "ORDER BY written_at DESC LIMIT ?",
                (worker_id, int(limit)),
            ).fetchall()
        return [
            {
                "mailbox_id": int(r[0]),
                "prompt": r[1],
                "written_at": r[2],
                "consumed_at": r[3],
                "status": r[4],
                "author_session_id": r[5],
                "author_task_id": r[6],
                "session_id": r[7],
                "protocol": r[8],
                "message_id": r[9],
                "correlation_id": r[10],
                "sender_actor_id": r[11],
                "target_actor_id": r[12],
                "lane_id": r[13],
                "message_kind": r[14],
                "severity": r[15],
            }
            for r in rows
        ]


def _age_seconds(written_at: str, now: str) -> int:
    try:
        w = datetime.fromisoformat(written_at)
        n = datetime.fromisoformat(now)
        return int((n - w).total_seconds())
    except Exception:
        return 0


def _audit(
    project_root: Path,
    session_id: str,
    *,
    action_kind: str,
    target_entity: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort write to execution_events. Never raises."""
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="lane_mailbox",
            source_kind="mcp",
            session_id=session_id,
            action_kind=action_kind,
            target_entity=target_entity,
            status="ok",
            payload=payload,
        )
    except Exception:
        pass
