from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class CapabilityIndexStore:
    """Derived SQLite index for MCP-discoverable capability definitions."""

    _EXPLICIT_METADATA: dict[str, dict[str, object]] = {
        "memory_capture": {"aliases": ["write_memory", "remember", "persist_memory"], "family": "memory"},
        "task_begin": {"aliases": ["start_task", "begin_task"], "family": "task_lifecycle"},
        "task_update": {"aliases": ["update_task"], "family": "task_lifecycle"},
        "task_complete": {"aliases": ["finish_task", "complete_task"], "family": "task_lifecycle"},
        "session_start": {"aliases": ["start_session"], "family": "session"},
        "project_bootstrap_or_resume": {"aliases": ["bootstrap_project", "resume_project"], "family": "bootstrap"},
        "workflow_actions_compile": {"aliases": ["compile_workflow", "compile_workflow_actions"], "family": "workflow"},
        "workflow_actions_get": {"aliases": ["read_workflow_actions"], "family": "workflow"},
        "action_surface_compare": {"aliases": ["should_can_did", "compare_action_surface"], "family": "analysis"},
    }

    _PREFIX_FAMILIES: tuple[tuple[str, str], ...] = (
        ("session_", "session"),
        ("task_", "task_lifecycle"),
        ("memory_", "memory"),
        ("workflow_", "workflow"),
        ("execution_", "execution"),
        ("capability_", "capability"),
        ("procedure_", "procedure"),
        ("schema_", "schema"),
        ("code_", "code"),
        ("related_project_", "related_project"),
        ("project_status_", "project_status"),
        ("aidocs_", "orchestration"),
        ("project_", "project"),
    )

    def index_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index"

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self, project_root: Path) -> None:
        with self.connect(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS capability_definitions (
                    name TEXT PRIMARY KEY,
                    capability_kind TEXT NOT NULL,
                    capability_family TEXT,
                    source_kind TEXT NOT NULL,
                    title TEXT,
                    description TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    output_schema_json TEXT NOT NULL,
                    meta_json TEXT,
                    task_mode TEXT,
                    timeout_seconds REAL,
                    checksum TEXT NOT NULL,
                    discovered_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "capability_definitions", "capability_family", "TEXT")
            self._ensure_column(conn, "capability_definitions", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")

    def sync_capabilities(self, project_root: Path, tools: Iterable[Any]) -> int:
        self.init_db(project_root)
        discovered_at = self._timestamp()
        rows = []
        for tool in tools:
            serialized = self._serialize_tool(tool)
            rows.append(
                (
                    serialized["name"],
                    serialized["capability_kind"],
                    serialized["capability_family"],
                    serialized["source_kind"],
                    serialized["title"],
                    serialized["description"],
                    serialized["aliases_json"],
                    serialized["tags_json"],
                    serialized["parameters_json"],
                    serialized["output_schema_json"],
                    serialized["meta_json"],
                    serialized["task_mode"],
                    serialized["timeout_seconds"],
                    serialized["checksum"],
                    discovered_at,
                )
            )

        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM capability_definitions")
            conn.executemany(
                """
                INSERT INTO capability_definitions (
                    name,
                    capability_kind,
                    capability_family,
                    source_kind,
                    title,
                    description,
                    aliases_json,
                    tags_json,
                    parameters_json,
                    output_schema_json,
                    meta_json,
                    task_mode,
                    timeout_seconds,
                    checksum,
                    discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def capability_status(self, project_root: Path) -> dict[str, int | str | dict[str, int]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            count = conn.execute("SELECT COUNT(*) FROM capability_definitions").fetchone()[0]
            kind_rows = conn.execute(
                "SELECT capability_kind, COUNT(*) AS count FROM capability_definitions GROUP BY capability_kind ORDER BY count DESC, capability_kind ASC"
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_kind, COUNT(*) AS count FROM capability_definitions GROUP BY source_kind ORDER BY count DESC, source_kind ASC"
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "capability_definitions": int(count),
            "by_kind": {row["capability_kind"]: int(row["count"]) for row in kind_rows},
            "by_source": {row["source_kind"]: int(row["count"]) for row in source_rows},
        }

    def find_capabilities(self, project_root: Path, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT name, capability_kind, capability_family, source_kind, title, description, aliases_json, tags_json, parameters_json, output_schema_json, meta_json, task_mode, timeout_seconds, discovered_at "
            "FROM capability_definitions"
        )
        params: list[Any] = []
        if query and query.strip():
            sql += " WHERE name LIKE ? OR description LIKE ? OR aliases_json LIKE ?"
            needle = f"%{query.strip()}%"
            params.extend([needle, needle, needle])
        sql += " ORDER BY name ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_capability(self, project_root: Path, name: str) -> dict[str, Any] | None:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT name, capability_kind, capability_family, source_kind, title, description, aliases_json, tags_json, parameters_json, output_schema_json, meta_json, task_mode, timeout_seconds, discovered_at FROM capability_definitions WHERE name = ? LIMIT 1",
                (name,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "capability_kind": row["capability_kind"],
            "capability_family": row["capability_family"],
            "source_kind": row["source_kind"],
            "title": row["title"],
            "description": row["description"],
            "aliases": json.loads(row["aliases_json"]),
            "tags": json.loads(row["tags_json"]),
            "parameters": json.loads(row["parameters_json"]),
            "output_schema": json.loads(row["output_schema_json"]),
            "meta": json.loads(row["meta_json"]) if row["meta_json"] else None,
            "task_mode": row["task_mode"],
            "timeout_seconds": row["timeout_seconds"],
            "discovered_at": row["discovered_at"],
        }

    def _serialize_tool(self, tool: Any) -> dict[str, Any]:
        tags = sorted(str(tag) for tag in (getattr(tool, "tags", None) or []))
        parameters = getattr(tool, "parameters", None) or {}
        output_schema = getattr(tool, "output_schema", None) or {}
        meta = getattr(tool, "meta", None)
        aliases: list[str] = []
        capability_family: str | None = None
        default_meta = self._default_capability_metadata(getattr(tool, "name", ""))
        aliases.extend(default_meta["aliases"])
        capability_family = default_meta["family"]
        if isinstance(meta, dict):
            raw_aliases = meta.get("capability_aliases")
            if isinstance(raw_aliases, list):
                aliases.extend(str(item).strip() for item in raw_aliases if str(item).strip())
            raw_family = meta.get("capability_family")
            if isinstance(raw_family, str) and raw_family.strip():
                capability_family = raw_family.strip()
        aliases = sorted({alias for alias in aliases if alias})
        task_config = getattr(tool, "task_config", None)
        timeout = getattr(tool, "timeout", None)
        timeout_seconds = float(timeout.total_seconds()) if hasattr(timeout, "total_seconds") else None
        payload = {
            "name": getattr(tool, "name", ""),
            "capability_kind": "mcp_tool",
            "capability_family": capability_family,
            "source_kind": "mcp_registry",
            "title": getattr(tool, "title", None),
            "description": getattr(tool, "description", None) or "",
            "aliases": aliases,
            "tags": tags,
            "parameters": parameters,
            "output_schema": output_schema,
            "meta": meta,
            "task_mode": getattr(task_config, "mode", None),
            "timeout_seconds": timeout_seconds,
        }
        checksum_payload = json.dumps(payload, sort_keys=True, default=str)
        return {
            "name": payload["name"],
            "capability_kind": payload["capability_kind"],
            "capability_family": payload["capability_family"],
            "source_kind": payload["source_kind"],
            "title": payload["title"],
            "description": payload["description"],
            "aliases_json": json.dumps(payload["aliases"], sort_keys=True),
            "tags_json": json.dumps(payload["tags"], sort_keys=True),
            "parameters_json": json.dumps(payload["parameters"], sort_keys=True),
            "output_schema_json": json.dumps(payload["output_schema"], sort_keys=True),
            "meta_json": json.dumps(payload["meta"], sort_keys=True, default=str) if payload["meta"] is not None else None,
            "task_mode": payload["task_mode"],
            "timeout_seconds": payload["timeout_seconds"],
            "checksum": checksum_payload,
        }

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _default_capability_metadata(self, name: str) -> dict[str, Any]:
        normalized = str(name or "").strip()
        explicit = self._EXPLICIT_METADATA.get(normalized, {})
        aliases = list(explicit.get("aliases") or [])
        family = explicit.get("family") if isinstance(explicit.get("family"), str) else None
        if family is None:
            for prefix, candidate in self._PREFIX_FAMILIES:
                if normalized.startswith(prefix):
                    family = candidate
                    break
        return {
            "aliases": aliases,
            "family": family,
        }
