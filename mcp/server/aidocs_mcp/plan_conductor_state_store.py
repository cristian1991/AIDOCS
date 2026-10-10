from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class PlanConductorStateStore(SQLiteIndexStoreBase):
    """Per-session plan conductor state — sqlite-backed replacement for
    ``.MEMORY/sessions/<id>/artifacts/plan_conductor_state.json``.

    One row per session. Payload is stored as JSON in a TEXT column because
    the shape is nested (paused_lanes dict, lane_signals list-of-dicts,
    lane_states enum map) and the runtime already normalizes it on read.
    Migrating to columns-per-field would fight the existing read logic
    without reducing attack surface further.
    """

    def _legacy_json_path(self, project_root: Path, session_id: str) -> Path:
        return (
            project_root
            / ".MEMORY"
            / "sessions"
            / session_id
            / "artifacts"
            / "plan_conductor_state.json"
        )

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plan_conductor_state (
                    session_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT
                );
                """,
            )
        self._ingest_all_legacy_json(project_root)

    def _ingest_all_legacy_json(self, project_root: Path) -> None:
        sessions_dir = project_root / ".MEMORY" / "sessions"
        if not sessions_dir.is_dir():
            return
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            self._ingest_single_legacy_json(project_root, session_dir.name)

    def _ingest_single_legacy_json(self, project_root: Path, session_id: str) -> None:
        path = self._legacy_json_path(project_root, session_id)
        if not path.is_file():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT 1 FROM plan_conductor_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                path.unlink()
                return
            conn.execute(
                """
                INSERT INTO plan_conductor_state
                    (session_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (session_id, json.dumps(payload), self._timestamp()),
            )
        path.unlink()

    def get(self, project_root: Path, session_id: str) -> dict[str, Any] | None:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT payload_json FROM plan_conductor_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def set(self, project_root: Path, session_id: str, payload: dict[str, Any]) -> None:
        now = self._timestamp()
        serialized = json.dumps(payload, sort_keys=True, default=str)
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO plan_conductor_state (session_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, serialized, now),
            )
