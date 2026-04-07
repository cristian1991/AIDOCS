"""SQLite-backed configuration store — replaces TOML file chain for settings.

Single source of truth for all AIDOCS settings. Stored in the project's
.MEMORY/.index/aidocs.sqlite3 alongside other indexes.

Features:
- Atomic reads/writes (SQLite transactions)
- No merge order bugs (one table, one truth)
- Hot reload (every read hits DB, no stale caches)
- Scope support: global, user, project, session
- Import from TOML (migration path from existing aidocs.toml files)
- Audit trail (updated_at timestamp on every write)

Table schema:
    config_settings (
        setting_path TEXT,      -- e.g. "gate.enforce"
        scope TEXT,             -- "global", "user", "project", "session"
        scope_key TEXT,         -- session_id for session scope, "" otherwise
        value TEXT,             -- JSON-encoded value
        updated_at TEXT,        -- ISO timestamp
        PRIMARY KEY (setting_path, scope, scope_key)
    )
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS config_settings (
    setting_path TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    scope_key TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (setting_path, scope, scope_key)
)
"""

_SCOPE_PRIORITY = {"global": 0, "user": 1, "project": 2, "session": 3}


class ConfigStore:
    """SQLite-backed configuration with scoped resolution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)
        return conn

    def get(
        self,
        project_root: Path,
        setting_path: str,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> Any | None:
        """Get a single setting value. Returns None if not set."""
        with self._lock:
            conn = self._connect(project_root)
            try:
                row = conn.execute(
                    "SELECT value FROM config_settings WHERE setting_path = ? AND scope = ? AND scope_key = ?",
                    (setting_path, scope, scope_key),
                ).fetchone()
                if row is None:
                    return None
                return json.loads(row["value"])
            finally:
                conn.close()

    def get_effective(
        self,
        project_root: Path,
        setting_path: str,
        *,
        session_id: str | None = None,
        default: Any = None,
    ) -> Any:
        """Get the effective value for a setting, resolving scope cascade.

        Priority: session > project > user > global > default
        """
        with self._lock:
            conn = self._connect(project_root)
            try:
                # Query all scopes for this setting
                rows = conn.execute(
                    "SELECT scope, scope_key, value FROM config_settings WHERE setting_path = ? ORDER BY scope",
                    (setting_path,),
                ).fetchall()

                if not rows:
                    return default

                # Build scope map
                values: dict[str, Any] = {}
                for row in rows:
                    scope = row["scope"]
                    scope_key = row["scope_key"]
                    # Session scope: only match the active session
                    if scope == "session" and session_id and scope_key == session_id:
                        values["session"] = json.loads(row["value"])
                    elif scope != "session":
                        values[scope] = json.loads(row["value"])

                # Cascade: session > project > user > global
                for scope in ("session", "project", "user", "global"):
                    if scope in values:
                        return values[scope]

                return default
            finally:
                conn.close()

    def set(
        self,
        project_root: Path,
        setting_path: str,
        value: Any,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> None:
        """Set a setting value."""
        now = datetime.now(timezone.utc).isoformat()
        json_value = json.dumps(value)
        with self._lock:
            conn = self._connect(project_root)
            try:
                conn.execute(
                    """INSERT INTO config_settings (setting_path, scope, scope_key, value, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (setting_path, scope, scope_key)
                       DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                    (setting_path, scope, scope_key, json_value, now),
                )
                conn.commit()
            finally:
                conn.close()

    def delete(
        self,
        project_root: Path,
        setting_path: str,
        *,
        scope: str = "project",
        scope_key: str = "",
    ) -> bool:
        """Delete a setting. Returns True if it existed."""
        with self._lock:
            conn = self._connect(project_root)
            try:
                cursor = conn.execute(
                    "DELETE FROM config_settings WHERE setting_path = ? AND scope = ? AND scope_key = ?",
                    (setting_path, scope, scope_key),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def get_all(
        self,
        project_root: Path,
        *,
        scope: str | None = None,
        scope_key: str = "",
    ) -> dict[str, Any]:
        """Get all settings, optionally filtered by scope."""
        with self._lock:
            conn = self._connect(project_root)
            try:
                if scope:
                    rows = conn.execute(
                        "SELECT setting_path, value FROM config_settings WHERE scope = ? AND scope_key = ?",
                        (scope, scope_key),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT setting_path, scope, scope_key, value, updated_at FROM config_settings",
                    ).fetchall()
                return {row["setting_path"]: json.loads(row["value"]) for row in rows}
            finally:
                conn.close()

    def get_all_with_metadata(
        self,
        project_root: Path,
    ) -> list[dict[str, Any]]:
        """Get all settings with full metadata (scope, updated_at)."""
        with self._lock:
            conn = self._connect(project_root)
            try:
                rows = conn.execute(
                    "SELECT setting_path, scope, scope_key, value, updated_at FROM config_settings ORDER BY setting_path, scope",
                ).fetchall()
                return [
                    {
                        "setting_path": row["setting_path"],
                        "scope": row["scope"],
                        "scope_key": row["scope_key"],
                        "value": json.loads(row["value"]),
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
            finally:
                conn.close()

    def effective_config(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
        defaults: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Build the full effective config dict, matching the old TOML format.

        Starts from defaults, then layers DB values by scope cascade.
        Returns nested dict like {"gate": {"enforce": True}, "agent": {"host_mode": "enforced"}, ...}
        """
        from copy import deepcopy
        result: dict[str, Any] = deepcopy(defaults) if defaults else {}

        with self._lock:
            conn = self._connect(project_root)
            try:
                rows = conn.execute(
                    "SELECT setting_path, scope, scope_key, value FROM config_settings ORDER BY setting_path",
                ).fetchall()
            finally:
                conn.close()

        # Group by setting_path, resolve scope cascade
        from collections import defaultdict
        by_path: dict[str, dict[str, Any]] = defaultdict(dict)
        for row in rows:
            scope = row["scope"]
            scope_key = row["scope_key"]
            if scope == "session" and session_id and scope_key != session_id:
                continue
            by_path[row["setting_path"]][scope] = json.loads(row["value"])

        for setting_path, scope_values in by_path.items():
            # Cascade: session > project > user > global
            value = None
            for scope in ("session", "project", "user", "global"):
                if scope in scope_values:
                    value = scope_values[scope]
                    break

            if value is None:
                continue

            # Write into nested dict: "gate.enforce" → result["gate"]["enforce"]
            parts = setting_path.split(".")
            current = result
            for part in parts[:-1]:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value

        return result

    def import_from_toml(
        self,
        project_root: Path,
        toml_path: Path,
        *,
        scope: str = "project",
        scope_key: str = "",
        overwrite: bool = False,
    ) -> int:
        """Import settings from a TOML file into the DB.

        Args:
            project_root: Project root for DB location.
            toml_path: Path to the TOML file to import.
            scope: Scope to assign to imported settings.
            scope_key: Scope key (e.g. session_id).
            overwrite: If True, overwrite existing DB values. If False, skip existing.

        Returns:
            Number of settings imported.
        """
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return 0

        if not toml_path.is_file():
            return 0

        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            return 0

        count = 0
        flat = _flatten_dict(data)
        for setting_path, value in flat.items():
            # Skip non-setting keys (interaction.*, etc.)
            if setting_path.startswith("interaction.") or setting_path.startswith("policies."):
                continue
            if not overwrite:
                existing = self.get(project_root, setting_path, scope=scope, scope_key=scope_key)
                if existing is not None:
                    continue
            self.set(project_root, setting_path, value, scope=scope, scope_key=scope_key)
            count += 1

        return count


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict: {"gate": {"enforce": True}} → {"gate.enforce": True}."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            result.update(_flatten_dict(value, full_key))
        else:
            result[full_key] = value
    return result
