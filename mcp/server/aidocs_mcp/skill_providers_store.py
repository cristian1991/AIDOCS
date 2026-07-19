from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class SkillProvidersStore(SQLiteIndexStoreBase):
    """Project-scoped external-skill-provider registry — sqlite-backed
    replacement for ``.MEMORY/config/skill-providers.json`` (and the
    older ``.MEMORY/skill-providers.json``).

    The provider list is stored as a JSON-encoded blob in a single-row
    table because callers always read/write the whole list and never
    query into individual provider fields. Both legacy paths are swept
    on init; the canonical config/ path wins when both exist.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_providers (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    providers TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT
                );
                """,
            )
        self._ingest_legacy_json(project_root)

    def _canonical_legacy_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "skill-providers.json"

    def _older_legacy_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "skill-providers.json"

    def _ingest_legacy_json(self, project_root: Path) -> None:
        # Canonical wins because that's where the live writer was
        # writing right before Beat 3 landed; the older path only holds
        # historical state from projects that predate the config/
        # subdir migration.
        canonical = self._canonical_legacy_path(project_root)
        older = self._older_legacy_path(project_root)
        source: Path | None = None
        if canonical.is_file():
            source = canonical
        elif older.is_file():
            source = older

        if source is None:
            return

        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt JSON stays on disk for operator triage; the store
            # falls back to the empty defaults so the rest of the
            # project keeps working.
            return
        if not isinstance(raw, dict):
            return
        providers_raw = raw.get("providers")
        normalized: list[dict[str, Any]] = []
        if isinstance(providers_raw, list):
            normalized = [item for item in providers_raw if isinstance(item, dict)]

        with self.session(project_root) as conn:
            existing = conn.execute("SELECT 1 FROM skill_providers WHERE id = 1").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO skill_providers (id, providers, updated_at) VALUES (1, ?, ?)",
                    (json.dumps(normalized), self._timestamp()),
                )

        # Delete BOTH legacy files even if only one supplied the data.
        # If both exist on disk, the older one is stale and would
        # confuse future operators looking at the project tree.
        for legacy in (canonical, older):
            try:
                legacy.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, project_root: Path) -> list[dict[str, Any]]:
        with self.session(project_root) as conn:
            row = conn.execute("SELECT providers FROM skill_providers WHERE id = 1").fetchone()
        if row is None:
            return []
        try:
            parsed = json.loads(row["providers"] or "[]")
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def set(self, project_root: Path, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [item for item in providers if isinstance(item, dict)]
        now = self._timestamp()
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO skill_providers (id, providers, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    providers = excluded.providers,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(normalized), now),
            )
        return normalized
