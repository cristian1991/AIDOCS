"""SQL store for lane completion reviews — emperor-doctrine §VIII.

The doctrine: *"Lane calls `lane_request_completion_review` BEFORE
`task_complete`. Tool blocks until conductor verdict. APPROVE →
completes. DENY+rationale → recycles. No lane self-declares done."*

This module is the storage primitive. The MCP tools (lane request +
conductor verdict) live in mcp_server.py and use this store for
state. The blocking-wait helper is request_review_blocking which
polls the row until verdict or timeout.

Phoenix, 2026-05-07. Backlog #emperor-§VIII-implementation.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def init_db(project_root: Path) -> None:
    """Create the lane_completion_reviews table if missing.
    Idempotent; safe on every call.
    """
    path = _db_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lane_completion_reviews (
                review_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                work_summary TEXT NOT NULL,
                evidence_paths TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
                conductor_message TEXT NOT NULL DEFAULT '',
                conductor_session_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS
                idx_lane_completion_reviews_session_pending
                ON lane_completion_reviews(session_id, status);
            CREATE INDEX IF NOT EXISTS
                idx_lane_completion_reviews_lane
                ON lane_completion_reviews(lane_id, status);
        """)

        # Phoenix 2026-05-08: idempotent migration for host-resume cols.
        for ddl in (
            "ALTER TABLE lane_completion_reviews ADD COLUMN host_session_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE lane_completion_reviews ADD COLUMN backend TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE lane_completion_reviews ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass


def request_review(
    project_root: Path,
    *,
    lane_id: str,
    session_id: str,
    work_summary: str,
    evidence_paths: list[str] | None = None,
    host_session_id: str = "",
    backend: str = "",
    worker_id: str = "",
) -> str:
    """Insert a new pending review. Returns the review_id.

    Phoenix 2026-05-08: host_session_id + backend + worker_id stamped
    so conductor_review_lane_completion can spawn the host's resume
    CLI on deny. Empty values stay empty for conductor-side callers
    without a worker.
    """
    init_db(project_root)
    review_id = str(uuid.uuid4())[:12]
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.execute(
            """INSERT INTO lane_completion_reviews (
                review_id, lane_id, session_id, work_summary,
                evidence_paths, status, created_at,
                host_session_id, backend, worker_id
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                review_id,
                lane_id,
                session_id,
                work_summary,
                json.dumps(list(evidence_paths or [])),
                ts,
                host_session_id,
                backend,
                worker_id,
            ),
        )
        conn.commit()
    return review_id


def get_review(project_root: Path, review_id: str) -> dict[str, Any] | None:
    """Return the row as a dict, or None if missing."""
    init_db(project_root)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM lane_completion_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["evidence_paths"] = json.loads(out.get("evidence_paths") or "[]")
    except Exception:
        out["evidence_paths"] = []
    return out


def submit_verdict(
    project_root: Path,
    *,
    review_id: str,
    verdict: str,
    message: str = "",
    conductor_session_id: str = "",
) -> dict[str, Any]:
    """Record the conductor's verdict. Verdict ∈ {'approved', 'denied'}.
    Returns the updated row, or {'error': ...} when missing/invalid.
    """
    if verdict not in ("approved", "denied"):
        return {"error": f"verdict must be 'approved' or 'denied', got {verdict!r}"}
    init_db(project_root)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        cur = conn.execute(
            """UPDATE lane_completion_reviews
               SET status = ?, conductor_message = ?,
                   conductor_session_id = ?, decided_at = ?
               WHERE review_id = ? AND status = 'pending'""",
            (verdict, message, conductor_session_id, ts, review_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            existing = get_review(project_root, review_id)
            if existing is None:
                return {"error": f"review {review_id} not found"}
            return {
                "error": (
                    f"review {review_id} already decided "
                    f"(status={existing['status']}, decided_at={existing['decided_at']})"
                ),
                "current": existing,
            }
    return get_review(project_root, review_id) or {"error": "post-update fetch failed"}


def wait_for_verdict(
    project_root: Path,
    *,
    review_id: str,
    timeout_seconds: int = 1800,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Block until the review's status changes from 'pending', or
    until timeout. Returns the final row. On timeout, sets the row
    to 'expired' and returns it.
    """
    deadline = time.time() + max(1, int(timeout_seconds))
    while time.time() < deadline:
        row = get_review(project_root, review_id)
        if row is None:
            return {"error": f"review {review_id} disappeared"}
        if row["status"] != "pending":
            return row
        time.sleep(poll_interval)
    # Timeout: mark expired so the conductor's next drain doesn't
    # surface a stale request.
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.execute(
            "UPDATE lane_completion_reviews SET status='expired', "
            "decided_at=? WHERE review_id=? AND status='pending'",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), review_id),
        )
        conn.commit()
    return get_review(project_root, review_id) or {
        "review_id": review_id,
        "status": "expired",
        "error": "timed out waiting for conductor verdict",
    }


def pending_for_session(
    project_root: Path,
    *,
    session_id: str = "",
    host_session_id: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return pending reviews owned by this conductor — what the
    conductor needs to surface in their next tool call envelope.
    Sorted oldest-first so the conductor sees the longest-waiting
    lane review first.

    OR-match on session_id / host_session_id (2026-05-13): a review
    follows the conductor process (host_session_id) AND/OR the
    AIDOCS session it was stamped with. Swapping sessions mid-run
    still surfaces the review; orphan reviews missing one field but
    matching the other still reach their owner. Empty filters are
    skipped so we never match the empty-string column accidentally.
    """
    sid = (session_id or "").strip()
    hsid = (host_session_id or "").strip()
    if not sid and not hsid:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if sid:
        clauses.append("session_id = ?")
        params.append(sid)
    if hsid:
        clauses.append("host_session_id = ?")
        params.append(hsid)
    where = " OR ".join(clauses)
    params.append(int(limit))
    init_db(project_root)
    with sqlite3.connect(str(_db_path(project_root))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT * FROM lane_completion_reviews
               WHERE ({where}) AND status = 'pending'
               ORDER BY created_at ASC
               LIMIT ?""",
            tuple(params),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence_paths"] = json.loads(d.get("evidence_paths") or "[]")
        except Exception:
            d["evidence_paths"] = []
        out.append(d)
    return out


def format_pending_block(reviews: list[dict[str, Any]]) -> str:
    """Format pending reviews as a 📋 block for the conductor's
    tool result envelope. Mirrors run_notifications.format_block.
    """
    if not reviews:
        return ""
    lines = ["📋 LANE COMPLETION REVIEWS PENDING (conductor decides):"]
    for r in reviews:
        rid = r.get("review_id", "?")
        lane = r.get("lane_id", "?")
        summary = (r.get("work_summary") or "").replace("\n", " ")
        if len(summary) > 200:
            summary = summary[:197] + "..."
        evidence = r.get("evidence_paths") or []
        ev_note = f" [{len(evidence)} artifacts]" if evidence else ""
        lines.append(f"  • {rid} (lane={lane}){ev_note}: {summary}")
    lines.append(
        "  → Decide with: ai_review(review_id=..., verdict='approved'|'denied', message='...')",
    )
    return "\n".join(lines)
