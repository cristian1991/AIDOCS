from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class ResolvedConfigStore(SQLiteIndexStoreBase):
    """Project-local resolved-config snapshot — sqlite-backed replacement for
    ``.MEMORY/config/resolved-config.json``.

    Single row per project (id=1) holding the fully-merged effective
    config + the active-layer metadata that OpenCode/Claude plugins need
    to decide whether directive injection is on, etc. Ingests the legacy
    JSON on first init_db() and hard-deletes it so the project never
    carries two sources of truth.

    Why sqlite and not a flat JSON: the plugin already opens
    aidocs.sqlite3 for managed-mode + query-gate reads, so one file
    opened once per event covers everything. Config in a separate JSON
    file created a drift risk — edits to gate_messages/default.toml or
    dashboard overrides had to round-trip through a cache rewrite.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resolved_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    resolved_json TEXT NOT NULL,
                    layers_json TEXT NOT NULL,
                    active_layers_json TEXT NOT NULL,
                    last_updated TEXT
                );
                """,
            )
        self._ingest_legacy_json(project_root)

    def _legacy_json_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "resolved-config.json"

    def _ingest_legacy_json(self, project_root: Path) -> None:
        path = self._legacy_json_path(project_root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        resolved = raw.get("resolved") or {}
        layers = raw.get("layers") or {}
        active = raw.get("active_layers") or []
        with self.session(project_root) as conn:
            existing = conn.execute("SELECT 1 FROM resolved_config WHERE id = 1").fetchone()
            if existing is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
                return
            conn.execute(
                """
                INSERT INTO resolved_config
                    (id, resolved_json, layers_json, active_layers_json, last_updated)
                VALUES (1, ?, ?, ?, ?)
                """,
                (
                    json.dumps(resolved, default=str),
                    json.dumps(layers, default=str),
                    json.dumps(active, default=str),
                    None,
                ),
            )
        try:
            path.unlink()
        except OSError:
            pass

    def set(
        self,
        project_root: Path,
        *,
        resolved: dict[str, Any],
        layers: dict[str, Any],
        active_layers: list[str],
        last_updated: str | None = None,
    ) -> None:
        self.init_db(project_root)
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO resolved_config
                    (id, resolved_json, layers_json, active_layers_json, last_updated)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    resolved_json = excluded.resolved_json,
                    layers_json = excluded.layers_json,
                    active_layers_json = excluded.active_layers_json,
                    last_updated = excluded.last_updated
                """,
                (
                    json.dumps(resolved, default=str),
                    json.dumps(layers, default=str),
                    json.dumps(active_layers, default=str),
                    last_updated,
                ),
            )

    def get(self, project_root: Path) -> dict[str, Any] | None:
        try:
            with self.session(project_root) as conn:
                row = conn.execute(
                    "SELECT resolved_json, layers_json, active_layers_json, last_updated "
                    "FROM resolved_config WHERE id = 1",
                ).fetchone()
        except Exception:
            return None
        if row is None:
            return None

        def _parse(s: str, default: Any) -> Any:
            try:
                return json.loads(s)
            except Exception:
                return default

        return {
            "resolved": _parse(row["resolved_json"], {}),
            "layers": _parse(row["layers_json"], {}),
            "active_layers": _parse(row["active_layers_json"], []),
            "last_updated": row["last_updated"],
        }
