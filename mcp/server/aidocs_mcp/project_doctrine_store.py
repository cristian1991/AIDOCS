"""Private project-law rows: never installed into the global Empire catalog."""
from __future__ import annotations

import json
from pathlib import Path

from ._sqlite_connect import connect
from ._sqlite_index_store_base import SQLiteIndexStoreBase


class ProjectDoctrineStore(SQLiteIndexStoreBase):
    """One project's SQL authority, provisioned on lifecycle or explicit seal."""

    def read(self, project_root: Path, skill_id: str) -> dict | None:
        db = self.db_path(project_root)
        if not db.is_file():
            return None
        with connect(db, read_only=True) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_doctrine'"
            ).fetchone() is None:
                return None
            row = conn.execute(
                "SELECT payload, source FROM project_doctrine WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["source"] = row["source"]
        return payload

    def write(self, project_root: Path, payload: dict, *, seed: bool = False) -> None:
        source = "project_seed" if seed else "manual"
        with self.session(project_root) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS project_doctrine ("
                "skill_id TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL)"
            )
            # A lifecycle refresh never overwrites operator-sealed law.
            conn.execute(
                "INSERT INTO project_doctrine(skill_id, payload, source) VALUES (?, ?, ?) "
                "ON CONFLICT(skill_id) DO UPDATE SET payload=excluded.payload, "
                "source=excluded.source WHERE ? = 0 OR project_doctrine.source='project_seed'",
                (payload["skill_id"], json.dumps(payload), source, int(seed)),
            )
