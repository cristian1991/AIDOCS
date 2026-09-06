"""Per-session response grounding ledger — War AZ #474.

THE CORE FIX for the stateless tool surface: every tool response used to
render from scratch with no memory of what this session was already told.
This module is the small, boring, sqlite-backed memory of AIDOCS's own
conversation with the agent, per (project, session):

1. NOTIFY-ON-CHANGE (``dedupe_state_notice`` / ``mark_state``): state-keyed
   system notices (hook-bootstrap banner, gate-health) emit their FULL text
   on first occurrence per session and again ONLY when the underlying state
   changes (healed, degraded further). Suppressed repeats are NOTHING — not
   even a stub. Fail-open: any ledger failure emits the notice (a real
   alert must never be lost to bookkeeping).
2. LIFECYCLE SNAPSHOT (``record_lifecycle`` / ``get_lifecycle``): the
   session's last-known active task id + status, so "no active task"
   refusals can say WHICH task disappeared and when.
3. SURFACED-FILE SET (``record_surfaced_files`` / ``surfaced_files``):
   discovery continuity per SESSION (not per task).
4. BUDGET HONESTY helpers (``budget_label`` / ``apply_listing_budget``):
   the reusable in-band "showing N of M — page with ..." envelope for
   listing surfaces (no-silent-caps law).

Storage: three tables in the canonical kingdom sqlite via
``SQLiteIndexStoreBase`` — census-classified in ``canonical_taxonomy``
as runtime conversation-state, not doctrine rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

__all__ = [
    "SessionResponseLedger",
    "apply_listing_budget",
    "budget_label",
    "dedupe_state_notice",
    "get_lifecycle",
    "mark_state",
    "record_lifecycle",
    "record_surfaced_files",
    "surfaced_files",
]

# Default in-band character budget for listing surfaces. Anything larger
# than this must be trimmed AND labeled — never silently dumped.
DEFAULT_LISTING_CHAR_BUDGET = 20_000


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8", errors="replace")).hexdigest()[:24]


class SessionResponseLedger(SQLiteIndexStoreBase):
    """Sqlite rows keyed by (session_id, key) — tiny, indexed, boring."""

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_response_ledger (
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                state_hash TEXT NOT NULL DEFAULT '',
                emit_count INTEGER NOT NULL DEFAULT 0,
                first_emitted_at TEXT NOT NULL DEFAULT '',
                last_emitted_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (session_id, key)
            )
            """,
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_lifecycle_snapshot (
                session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_surfaced_files (
                session_id TEXT NOT NULL,
                path TEXT NOT NULL,
                first_surfaced_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (session_id, path)
            )
            """,
        )

    # ── notify-on-change ────────────────────────────────────────────

    def should_emit(self, project_root: Path, session_id: str, key: str, state: str) -> bool:
        """True when this (session, key) has never emitted OR the state
        changed since the last emit. Updates the row on True; a same-state
        repeat is a pure read (no write amplification per tool call).
        """
        digest = _state_hash(state)
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT state_hash FROM session_response_ledger "
                "WHERE session_id = ? AND key = ?",
                (session_id, key),
            ).fetchone()
            if row is not None and str(row["state_hash"]) == digest:
                return False
            now = self._timestamp()
            conn.execute(
                "INSERT INTO session_response_ledger "
                "(session_id, key, state_hash, emit_count, first_emitted_at, last_emitted_at) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(session_id, key) DO UPDATE SET "
                "state_hash = excluded.state_hash, "
                "emit_count = session_response_ledger.emit_count + 1, "
                "last_emitted_at = excluded.last_emitted_at",
                (session_id, key, digest, now, now),
            )
            return True

    # ── lifecycle snapshot ──────────────────────────────────────────

    def set_lifecycle(self, project_root: Path, session_id: str, task_id: str, status: str) -> None:
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            conn.execute(
                "INSERT INTO session_lifecycle_snapshot (session_id, task_id, status, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "task_id = excluded.task_id, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (session_id, task_id, status, self._timestamp()),
            )

    def lifecycle(self, project_root: Path, session_id: str) -> dict[str, Any] | None:
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT task_id, status, updated_at FROM session_lifecycle_snapshot "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    # ── surfaced-file set ───────────────────────────────────────────

    def add_surfaced_files(self, project_root: Path, session_id: str, paths: list[str]) -> None:
        cleaned = [str(p).strip() for p in paths if str(p).strip()]
        if not cleaned:
            return
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            now = self._timestamp()
            conn.executemany(
                "INSERT OR IGNORE INTO session_surfaced_files "
                "(session_id, path, first_surfaced_at) VALUES (?, ?, ?)",
                [(session_id, p, now) for p in cleaned],
            )

    def surfaced_file_set(self, project_root: Path, session_id: str) -> set[str]:
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT path FROM session_surfaced_files WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return {str(r["path"]) for r in rows}


_LEDGER = SessionResponseLedger()


# ── module-level fail-open API (what callers use) ────────────────────


def dedupe_state_notice(
    project_root: Path,
    session_id: str,
    key: str,
    notice: str,
) -> str | None:
    """Return ``notice`` when it should be emitted for this session
    (first occurrence, or state changed), else None (suppressed —
    nothing, not even a stub).

    FAIL-OPEN CONTRACT: any ledger failure — and an empty session id,
    which makes dedupe meaningless — returns the notice. Current
    (repeating) behavior is the fallback; a real alert is never lost.
    """
    if not notice:
        return None
    if not session_id:
        return notice
    try:
        emit = _LEDGER.should_emit(project_root, session_id, key, notice)
    except Exception:
        return notice
    return notice if emit else None


def mark_state(project_root: Path, session_id: str, key: str, state: str) -> None:
    """Record a state transition WITHOUT emitting anything (e.g. the
    'healthy' interlude between two identical degradation banners, so a
    re-degradation re-emits). Best-effort, fail-quiet.
    """
    if not session_id:
        return
    try:
        _LEDGER.should_emit(project_root, session_id, key, state)
    except Exception:
        pass


def record_lifecycle(project_root: Path, session_id: str, *, task_id: str, status: str) -> None:
    """Best-effort lifecycle snapshot write (task begin/complete)."""
    if not session_id:
        return
    try:
        _LEDGER.set_lifecycle(project_root, session_id, task_id, status)
    except Exception:
        pass


def get_lifecycle(project_root: Path, session_id: str) -> dict[str, Any] | None:
    """Last-known {task_id, status, updated_at} for the session, or None."""
    if not session_id:
        return None
    try:
        return _LEDGER.lifecycle(project_root, session_id)
    except Exception:
        return None


def record_surfaced_files(project_root: Path, session_id: str, paths: list[str]) -> None:
    """Accumulate surfaced files per SESSION (discovery continuity)."""
    if not session_id:
        return
    try:
        _LEDGER.add_surfaced_files(project_root, session_id, paths)
    except Exception:
        pass


def surfaced_files(project_root: Path, session_id: str) -> set[str]:
    if not session_id:
        return set()
    try:
        return _LEDGER.surfaced_file_set(project_root, session_id)
    except Exception:
        return set()


# ── budget honesty (no-silent-caps law) ──────────────────────────────


def budget_label(shown: int, total: int, page_hint: str) -> str:
    """The one canonical in-band truncation label."""
    return f"showing {shown} of {total} — {page_hint}"


def apply_listing_budget(
    items: list[dict[str, Any]],
    *,
    total: int,
    page_hint: str,
    char_budget: int = DEFAULT_LISTING_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Shape-preserving budget envelope for list-of-dict tool results.

    - Under budget AND complete (len(items) == total): passthrough,
      zero label noise.
    - Over ``char_budget`` (approximate serialized size): trim items
      until under budget.
    - Whenever items shown < total (server-side cap or trim), append a
      final in-band ``{"_budget": "showing N of M — <page_hint>"}``
      sentinel item so the truncation is visible IN the payload.
    """
    import json

    kept = list(items)
    size = 0
    for i, item in enumerate(kept):
        try:
            size += len(json.dumps(item, default=str)) + 2
        except Exception:
            size += 64
        if size > char_budget:
            kept = kept[:i] if i > 0 else kept[:1]
            break
    if len(kept) >= total and len(kept) == len(items):
        return kept
    return [*kept, {"_budget": budget_label(len(kept), total, page_hint)}]
