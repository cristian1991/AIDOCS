"""Pending ⚠ freeze-strike notice store + rail surfacing.

When a security strike is recorded (SecurityViolationService.record_and_escalate)
— including a self-cancel strike minted when the AGENT clears its own freeze
(clear_freeze_service) — a terse notice lands here and the universal
notification injector (``notification_injector._collect_notification_blocks``)
surfaces it on the agent's next tool calls.

Why a notification rail and NOT the UPS additional-context note it replaces:
the old hook_pipeline strike-note re-fired on EVERY prompt while peak>0 (a
per-prompt tax). This store surfaces a strike a bounded number of times
(``_MAX_SURFACES`` = 3, operator directive 2026-07-16) then auto-drops — the
agent is told, then it stops nagging. The blocked-tool error still renders the
full strike trail at block time (freeze_service._render_strike_trail); this rail
is the BETWEEN-blocks visibility that was previously missing.

Storage (WAR M Phase A, #445 — no-file-layer): rows live in the canonical
kingdom sqlite (``freeze_strike_notices`` table via ``SQLiteIndexStoreBase``),
NOT the legacy ``.MEMORY/.index/freeze_strike_notices.json`` loose file.
Heal-forward bridge mirrors the commission-stamp precedent
(mcp_server_runtime_helpers.stamp_commissioned): on the store's first touch a
still-present legacy JSON is adopted into the DB exactly once (stamped in
``index_meta``), then the DB is canonical — later edits to the file change
nothing, and NO new writes ever land in the file. The legacy file is NOT
deleted here (Phase B owns removal).

Mirrors aidocs_nlp/durable_hint_store surfaced_count/max_surfaces semantics.
Fail-quiet like every notification layer: any error enqueues/surfaces nothing
and breaks no caller.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

__all__ = [
    "enqueue_strike_notice",
    "pending_for_session",
    "surface_pending",
    "format_strike_block",
]

_LEGACY_FILENAME = "freeze_strike_notices.json"
_ADOPTION_STAMP_KEY = "freeze_strike_notices_file_adopted"
_MAX_SURFACES = 3  # operator directive 2026-07-16: 3 surfaces, then auto-drop


def _legacy_path(project_root: Path) -> Path:
    """Legacy loose-file location — read ONCE by the adoption bridge, never
    written. Retained (not deleted) until Phase B removal."""
    return Path(project_root) / ".MEMORY" / ".index" / _LEGACY_FILENAME


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FreezeStrikeNoticeStore(SQLiteIndexStoreBase):
    """Sqlite-backed freeze-strike notice rows (one row per strike)."""

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS freeze_strike_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                agent_context_id TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 0,
                threshold INTEGER NOT NULL DEFAULT 0,
                family TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT '',
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """,
        )

    def _adopt_legacy_file(self, conn: sqlite3.Connection, project_root: Path) -> None:
        """Heal-forward bridge — one-shot, stamped, file left in place.

        On the first DB touch: if the legacy JSON still exists, its pending
        notices are inserted; either way the adoption stamp is written so
        the DB is canonical from here on (a file that appears LATER is never
        folded back in — DB wins after adoption).
        """
        row = conn.execute(
            "SELECT 1 FROM index_meta WHERE key = ?",
            (_ADOPTION_STAMP_KEY,),
        ).fetchone()
        if row is not None:
            return
        legacy = _legacy_path(project_root)
        if legacy.is_file():
            try:
                raw = json.loads(legacy.read_text(encoding="utf-8"))
                pending = raw.get("pending") if isinstance(raw, dict) else None
            except Exception:
                pending = None
            for notice in pending if isinstance(pending, list) else []:
                if not isinstance(notice, dict):
                    continue
                session_id = str(notice.get("session_id") or "")
                if not session_id:
                    continue
                conn.execute(
                    "INSERT INTO freeze_strike_notices "
                    "(session_id, agent_context_id, count, threshold, family, "
                    " origin, surfaced_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        str(notice.get("agent_context_id") or ""),
                        int(notice.get("count") or 0),
                        int(notice.get("threshold") or 0),
                        str(notice.get("family") or ""),
                        str(notice.get("origin") or ""),
                        int(notice.get("surfaced_count") or 0),
                        str(notice.get("created_at") or _now()),
                    ),
                )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (_ADOPTION_STAMP_KEY, _now()),
        )

    def _prepare(self, conn: sqlite3.Connection, project_root: Path) -> None:
        self._ensure_schema(conn)
        self._adopt_legacy_file(conn, project_root)

    @staticmethod
    def _row_to_notice(row: sqlite3.Row) -> dict[str, Any]:
        # Same dict shape the JSON store returned — no DB ``id`` leak, so
        # renderers/consumers see identical payloads.
        return {
            "session_id": row["session_id"],
            "agent_context_id": row["agent_context_id"],
            "count": int(row["count"]),
            "threshold": int(row["threshold"]),
            "family": row["family"],
            "origin": row["origin"],
            "surfaced_count": int(row["surfaced_count"]),
            "created_at": row["created_at"],
        }

    def enqueue(
        self,
        project_root: Path,
        session_id: str,
        *,
        count: int,
        threshold: int,
        family: str = "",
        origin: str = "",
        agent_context_id: str = "",
    ) -> bool:
        if not session_id:
            return False
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            conn.execute(
                "INSERT INTO freeze_strike_notices "
                "(session_id, agent_context_id, count, threshold, family, "
                " origin, surfaced_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    session_id,
                    str(agent_context_id or ""),
                    int(count),
                    int(threshold),
                    str(family or ""),
                    str(origin or ""),
                    _now(),
                ),
            )
        return True

    def pending(self, project_root: Path, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            rows = conn.execute(
                "SELECT * FROM freeze_strike_notices "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [self._row_to_notice(r) for r in rows]

    def surface(
        self,
        project_root: Path,
        *,
        session_id: str,
        agent_context_id: str = "",
        max_surfaces: int = _MAX_SURFACES,
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        surfaced: list[dict[str, Any]] = []
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            rows = conn.execute(
                "SELECT * FROM freeze_strike_notices ORDER BY id",
            ).fetchall()
            for row in rows:
                # Owner = the AGENT (agent_context_id), which FOLLOWS the agent
                # across work sessions — agent_context_id derives from the agent's
                # own host_session_id and EXCLUDES session_uuid (operator model
                # 2026-07-15). Match on agent_context_id when the notice carries
                # one (the session need NOT match, so the notice follows a moved
                # agent); fall back to the session label only for legacy notices
                # that have no agent identity.
                n_ctx = str(row["agent_context_id"] or "")
                if n_ctx:
                    owned = n_ctx == agent_context_id
                else:
                    owned = str(row["session_id"] or "") == session_id
                if not owned:
                    continue
                new_count = int(row["surfaced_count"]) + 1
                notice = self._row_to_notice(row)
                notice["surfaced_count"] = new_count
                surfaced.append(notice)
                if new_count < max_surfaces:
                    conn.execute(
                        "UPDATE freeze_strike_notices SET surfaced_count = ? "
                        "WHERE id = ?",
                        (new_count, row["id"]),
                    )
                else:
                    # at/over cap: dropped from pending
                    conn.execute(
                        "DELETE FROM freeze_strike_notices WHERE id = ?",
                        (row["id"],),
                    )
        return surfaced


_STORE = FreezeStrikeNoticeStore()


def enqueue_strike_notice(
    project_root: Path,
    session_id: str,
    *,
    count: int,
    threshold: int,
    family: str = "",
    origin: str = "",
    agent_context_id: str = "",
) -> bool:
    """Enqueue one pending ⚠ freeze-strike notice for ``session_id``.

    ``count``/``threshold`` drive the rendered ratchet ("N/3"); ``origin``
    ('self_cancel' | '') tailors the lesson text. One notice per strike
    (no dedup — each strike is a distinct event worth surfacing). Returns
    True when enqueued. Fail-quiet: any error returns False.
    """
    try:
        return _STORE.enqueue(
            project_root,
            session_id,
            count=count,
            threshold=threshold,
            family=family,
            origin=origin,
            agent_context_id=agent_context_id,
        )
    except Exception:
        return False


def pending_for_session(project_root: Path, session_id: str) -> list[dict[str, Any]]:
    """Peek (no bump, no drop). Empty session_id returns []. Fail-quiet."""
    try:
        return _STORE.pending(project_root, session_id)
    except Exception:
        return []


def surface_pending(
    project_root: Path,
    *,
    session_id: str,
    agent_context_id: str = "",
    max_surfaces: int = _MAX_SURFACES,
) -> list[dict[str, Any]]:
    """Peek + bump surfaced_count + auto-drop at ``max_surfaces``.

    Mirrors durable_hint_store.surface_pending: each call that RETURNS a
    notice counts as one surface; a notice that reaches the cap is dropped
    from pending. Empty session_id returns []. Fail-quiet.
    """
    try:
        return _STORE.surface(
            project_root,
            session_id=session_id,
            agent_context_id=agent_context_id,
            max_surfaces=max_surfaces,
        )
    except Exception:
        return []


def format_strike_block(notices: list[dict[str, Any]]) -> str:
    """Terse ⚠ rail block. One line per strike notice."""
    lines: list[str] = []
    for notice in notices:
        count = int(notice.get("count") or 0)
        threshold = int(notice.get("threshold") or 0)
        origin = str(notice.get("origin") or "")
        cause = (
            "self-cancelling your own freeze"
            if origin == "self_cancel"
            else "a repeated security denial"
        )
        ratchet = f"{count}/{threshold}" if threshold >= 1 else str(count)
        line = f"⚠ Freeze-strike {ratchet} — recorded for {cause}. "
        if threshold >= 1 and count >= threshold:
            line += (
                "You are AT the freeze ceiling — the next strike (or an active "
                "lock) freezes the session, clearable only by a genuine external "
                "operator. Stop and change approach."
            )
        elif threshold >= 1 and count >= threshold - 1:
            line += (
                "ONE more strike freezes the session (uncancelable — external "
                "operator only). Stop and rethink the approach instead of retrying."
            )
        else:
            line += "Avoid repeating the action that caused it."
        lines.append(line)
    return "\n".join(lines)

