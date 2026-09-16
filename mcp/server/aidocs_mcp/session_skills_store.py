from __future__ import annotations

import json
from pathlib import Path

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class SessionSkillsStore(SQLiteIndexStoreBase):
    """Per-session selected-skills list — sqlite-backed replacement for
    ``.MEMORY/sessions/{id}/skills.json``.

    One row per session_id; selected skill IDs live as a JSON-encoded
    TEXT column. Whole-list read/write semantics match the legacy JSON
    shape so the service layer's validation + self-healing rewrite
    remain unchanged.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_skills (
                    session_id TEXT PRIMARY KEY,
                    selected_skills TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT
                );
                """,
            )
        self._ingest_all_legacy_json(project_root)

    def _legacy_json_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "skills.json"

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
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # Leave corrupt JSON on disk for operator triage; empty row
            # is the safe fallback so the rest of the project keeps
            # working.
            return
        if not isinstance(raw, dict):
            return
        selected = raw.get("selected_skills")
        normalized: list[str]
        if isinstance(selected, list):
            normalized = [str(s) for s in selected if isinstance(s, str) and s]
        else:
            normalized = []
        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT 1 FROM session_skills WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                path.unlink()
                return
            conn.execute(
                "INSERT INTO session_skills (session_id, selected_skills, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, json.dumps(normalized), self._timestamp()),
            )
        path.unlink()

    def get(self, project_root: Path, session_id: str) -> list[str]:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT selected_skills FROM session_skills WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return []
        try:
            parsed = json.loads(row["selected_skills"] or "[]")
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(s) for s in parsed if isinstance(s, str)]

    def set(self, project_root: Path, session_id: str, selected_skills: list[str]) -> list[str]:
        normalized = [str(s) for s in selected_skills if isinstance(s, str) and s]
        now = self._timestamp()
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO session_skills (session_id, selected_skills, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    selected_skills = excluded.selected_skills,
                    updated_at = excluded.updated_at
                """,
                (session_id, json.dumps(normalized), now),
            )
        return normalized
