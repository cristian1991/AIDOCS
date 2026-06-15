"""Lane mailbox: conductor → parked worker prompt delivery via SQLite.

Architecture
------------
Lane workers that finish a unit of work park themselves on a short
ScheduleWakeup (60–120s) and exit the LLM loop until the next wake.
When the wake fires, the claude_hook checks this mailbox for a pending
prompt addressed to `worker_id`. If found, the hook swaps the wake's
"check mailbox" prompt with the conductor's real message so the agent
acts on new instructions immediately.

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
Default 15 min. Stale rows flip status='expired' and the worker's
next wake fires with an EXIT directive (TTL exhausted, no more work).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_mailbox_worker "
        "ON session_lane_mailbox (worker_id, status, written_at)",
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
    ) -> int:
        """Write a prompt addressed to `worker_id`. Returns mailbox_id.

        Caller usually is the conductor-agent via `lane_send_prompt`
        MCP tool. Emits a lane_mailbox_write audit event.
        """
        db = _db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            cur = conn.execute(
                "INSERT INTO session_lane_mailbox "
                "(worker_id, session_id, prompt, author_session_id, "
                "author_task_id, written_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (
                    worker_id,
                    session_id,
                    prompt,
                    author_session_id,
                    author_task_id,
                    _now(),
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
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT mailbox_id, session_id, prompt, author_session_id, "
                "author_task_id, written_at "
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
        }
        _audit(
            project_root,
            str(row[1]),
            action_kind="lane_mailbox_consume",
            target_entity=worker_id,
            payload={
                "mailbox_id": mailbox_id,
                "age_seconds": _age_seconds(row[5], now),
            },
        )
        return result

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
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT mailbox_id, prompt, written_at "
                "FROM session_lane_mailbox "
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
        with sqlite3.connect(str(db)) as conn:
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
        with sqlite3.connect(str(db)) as conn:
            _init(conn)
            rows = conn.execute(
                "SELECT mailbox_id, prompt, written_at, consumed_at, "
                "status, author_session_id, author_task_id "
                "FROM session_lane_mailbox "
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
