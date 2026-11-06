"""Owed remote XAACP attaches -- TRANSPORT OUTBOX state, never an authority.

#1095 step 4 (r0b2 804329fc-66d). On a cloud-bound project the gate owns the
XAACP actor registry. A lifecycle attach (SubagentStart / SessionStart) that
cannot reach the gate is recorded HERE so a later lifecycle event can retry
it. This store is deliberately NOT a second addressability authority:

  * it never holds, answers or implies an actor row -- a pending entry means
    exactly "spawned but not remotely addressable yet";
  * it lives in its own file, so a cloud-bound project never grows a local
    conductor_comms registry (which would be the fork #981 forbids);
  * it is drained only from lifecycle rails (session/subagent start), never
    from an ordinary tool call and never from a read;
  * a later explicit xaacp_attach by the same identity settles it.

Key: the trusted, host-measured identity (host_session_id, host_agent_id).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect


def outbox_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "xaacp_attach_outbox.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS xaacp_attach_owed (
    host_session_id TEXT NOT NULL,
    host_agent_id TEXT NOT NULL DEFAULT '',
    host_kind TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_attempt_at REAL NOT NULL,
    PRIMARY KEY (host_session_id, host_agent_id)
)
"""


def record(
    project_root: Path,
    *,
    host_session_id: str,
    host_agent_id: str,
    host_kind: str,
    actor_kind: str,
    session_id: str,
    project_id: str = "",
    source: str = "",
    reason: str = "",
) -> None:
    """Record (or bump) one owed attach for this exact identity."""
    hsid = str(host_session_id or "").strip()
    if not hsid:
        return
    path = outbox_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _canonical_connect(str(path), row_factory=False) as conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT INTO xaacp_attach_owed (host_session_id, host_agent_id, host_kind, "
            "actor_kind, session_id, project_id, source, reason, attempts, created_at, "
            "last_attempt_at) VALUES (?,?,?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT(host_session_id, host_agent_id) DO UPDATE SET "
            "session_id=excluded.session_id, project_id=excluded.project_id, "
            "reason=excluded.reason, attempts=xaacp_attach_owed.attempts+1, "
            "last_attempt_at=excluded.last_attempt_at",
            (
                hsid,
                str(host_agent_id or "").strip(),
                str(host_kind or "").strip(),
                str(actor_kind or "").strip(),
                str(session_id or "").strip(),
                str(project_id or "").strip(),
                str(source or ""),
                str(reason or ""),
                now,
                now,
            ),
        )
        conn.commit()


def pending(
    project_root: Path, *, limit: int = 20, min_age_seconds: float = 0.0
) -> list[dict]:
    """Owed attaches, oldest attempt first. A pure read: no store, no rows.

    ``min_age_seconds`` skips entries attempted more recently than that, so a
    lifecycle drain right after a failed attempt does not hammer the gate (and
    does not add a second network wait to the same spawn)."""
    path = outbox_path(project_root)
    if not path.is_file():
        return []
    cutoff = time.time() - max(0.0, float(min_age_seconds))
    try:
        with _canonical_connect(str(path), read_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM xaacp_attach_owed WHERE last_attempt_at <= ? "
                "ORDER BY last_attempt_at LIMIT ?",
                (cutoff, max(1, int(limit))),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        # A MISSING TABLE is "nothing owed". Anything else -- locked, corrupt,
        # unreadable -- is NOT an empty queue: it propagates so the drain can
        # report it (r0b2 f1531e47-d73 reliability note).
        if "no such table" in str(exc).lower():
            return []
        raise
    return [dict(r) for r in rows]


def settle(project_root: Path, *, host_session_id: str, host_agent_id: str = "") -> bool:
    """Remove the owed entry for this identity; True iff one was removed."""
    path = outbox_path(project_root)
    if not path.is_file():
        return False
    try:
        with _canonical_connect(str(path), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM xaacp_attach_owed WHERE host_session_id=? AND host_agent_id=?",
                (str(host_session_id or "").strip(), str(host_agent_id or "").strip()),
            )
            conn.commit()
            return int(cur.rowcount or 0) > 0
    except Exception:  # noqa: BLE001
        return False
