from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class WorkflowActionsStore(SQLiteIndexStoreBase):
    """Project-scoped compiled workflow actions — sqlite-backed
    replacement for ``.MEMORY/config/workflow-actions.json``.

    Single-row table; the compiled payload (rules, action_definitions,
    actions, unsupported_rules, source_paths, compiled_at) lives as a
    JSON-encoded blob because the writer emits it whole and every
    reader consumes it whole — no field-level queries.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_actions (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT
                );
                """,
            )
        self._ingest_legacy_json(project_root)

    def _legacy_json_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "workflow-actions.json"

    def _ingest_legacy_json(self, project_root: Path) -> None:
        path = self._legacy_json_path(project_root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt JSON stays on disk for operator triage; the store
            # falls back to "no compiled actions" so the next
            # compile_project_rules call rebuilds from source.
            return
        if not isinstance(raw, dict):
            return
        with self.session(project_root) as conn:
            existing = conn.execute("SELECT 1 FROM workflow_actions WHERE id = 1").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO workflow_actions (id, payload, updated_at) VALUES (1, ?, ?)",
                    (json.dumps(raw), self._timestamp()),
                )
        path.unlink()

    def get(self, project_root: Path) -> dict[str, Any] | None:
        with self.session(project_root) as conn:
            row = conn.execute("SELECT payload FROM workflow_actions WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["payload"] or "{}")
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set(self, project_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._timestamp()
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO workflow_actions (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(payload), now),
            )
        return dict(payload)
