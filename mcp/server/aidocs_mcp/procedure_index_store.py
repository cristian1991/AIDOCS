from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProcedureIndexStore:
    """Derived SQLite index for workflow and procedure definitions."""

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
                CREATE TABLE IF NOT EXISTS procedure_definitions (
                    procedure_id TEXT PRIMARY KEY,
                    definition_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    section_name TEXT,
                    title TEXT,
                    body_text TEXT NOT NULL,
                    trigger TEXT,
                    action_kind TEXT,
                    action_payload_json TEXT,
                    source_rule TEXT,
                    source_segment TEXT,
                    checksum TEXT NOT NULL,
                    discovered_at TEXT NOT NULL
                );
                """
            )

    def sync_procedures(self, project_root: Path, compiled_workflow: dict[str, object] | None = None) -> int:
        self.init_db(project_root)
        rows: list[tuple[str, str, str, str, str | None, str | None, str, str | None, str | None, str | None, str | None, str | None, str, str]] = []
        workflow_path = project_root / ".MEMORY" / "rules" / "workflow.md"
        discovered_at = self._timestamp()

        if workflow_path.is_file():
            text = workflow_path.read_text(encoding="utf-8")
            for section_name, rule_index, rule_text in self._extract_rule_bullets(text):
                procedure_id = f"workflow-rule:{section_name}:{rule_index}"
                checksum = self._checksum([procedure_id, section_name, rule_text])
                rows.append(
                    (
                        procedure_id,
                        "workflow_rule",
                        "memory_rule_file",
                        ".MEMORY/rules/workflow.md",
                        section_name,
                        None,
                        rule_text,
                        None,
                        None,
                        None,
                        rule_text,
                        None,
                        checksum,
                        discovered_at,
                    )
                )

        compiled = compiled_workflow or {}
        for action in compiled.get("actions", []) or []:
            if not isinstance(action, dict):
                continue
            procedure_id = str(action.get("id") or "").strip()
            if not procedure_id:
                continue
            trigger = self._string_or_none(action.get("trigger"))
            action_kind = self._string_or_none(action.get("kind"))
            source_rule = self._string_or_none(action.get("source_rule"))
            source_segment = self._string_or_none(action.get("source_segment"))
            action_payload = {
                key: value
                for key, value in action.items()
                if key not in {"id", "trigger", "kind", "source_rule", "source_segment"}
            }
            checksum = self._checksum([
                procedure_id,
                trigger or "",
                action_kind or "",
                source_rule or "",
                source_segment or "",
                json.dumps(action_payload, sort_keys=True, default=str),
            ])
            rows.append(
                (
                    procedure_id,
                    "workflow_action",
                    "compiled_workflow_action",
                    ".MEMORY/config/workflow-actions.json",
                    "Automation Rules",
                    None,
                    source_rule or source_segment or procedure_id,
                    trigger,
                    action_kind,
                    json.dumps(action_payload, sort_keys=True, default=str),
                    source_rule,
                    source_segment,
                    checksum,
                    discovered_at,
                )
            )

        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM procedure_definitions")
            conn.executemany(
                """
                INSERT INTO procedure_definitions (
                    procedure_id,
                    definition_kind,
                    source_kind,
                    source_path,
                    section_name,
                    title,
                    body_text,
                    trigger,
                    action_kind,
                    action_payload_json,
                    source_rule,
                    source_segment,
                    checksum,
                    discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def procedure_status(self, project_root: Path) -> dict[str, int | str | dict[str, int]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            count = conn.execute("SELECT COUNT(*) FROM procedure_definitions").fetchone()[0]
            kind_rows = conn.execute(
                "SELECT definition_kind, COUNT(*) AS count FROM procedure_definitions GROUP BY definition_kind ORDER BY count DESC, definition_kind ASC"
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_kind, COUNT(*) AS count FROM procedure_definitions GROUP BY source_kind ORDER BY count DESC, source_kind ASC"
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "procedure_definitions": int(count),
            "by_kind": {row["definition_kind"]: int(row["count"]) for row in kind_rows},
            "by_source": {row["source_kind"]: int(row["count"]) for row in source_rows},
        }

    def find_procedures(self, project_root: Path, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT procedure_id, definition_kind, source_kind, source_path, section_name, title, body_text, trigger, action_kind, action_payload_json, source_rule, source_segment, discovered_at "
            "FROM procedure_definitions"
        )
        params: list[Any] = []
        if query and query.strip():
            sql += " WHERE procedure_id LIKE ? OR body_text LIKE ? OR COALESCE(source_rule, '') LIKE ?"
            needle = f"%{query.strip()}%"
            params.extend([needle, needle, needle])
        sql += " ORDER BY definition_kind ASC, procedure_id ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_procedure(self, project_root: Path, procedure_id: str) -> dict[str, Any] | None:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT procedure_id, definition_kind, source_kind, source_path, section_name, title, body_text, trigger, action_kind, action_payload_json, source_rule, source_segment, discovered_at FROM procedure_definitions WHERE procedure_id = ? LIMIT 1",
                (procedure_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "procedure_id": row["procedure_id"],
            "definition_kind": row["definition_kind"],
            "source_kind": row["source_kind"],
            "source_path": row["source_path"],
            "section_name": row["section_name"],
            "title": row["title"],
            "body_text": row["body_text"],
            "trigger": row["trigger"],
            "action_kind": row["action_kind"],
            "action_payload": json.loads(row["action_payload_json"]) if row["action_payload_json"] else None,
            "source_rule": row["source_rule"],
            "source_segment": row["source_segment"],
            "discovered_at": row["discovered_at"],
        }

    def _extract_rule_bullets(self, text: str) -> list[tuple[str, int, str]]:
        sections = self._parse_sections(text)
        output: list[tuple[str, int, str]] = []
        for section_name, lines in sections.items():
            rule_index = 0
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("-"):
                    continue
                body = stripped[1:].strip()
                if not body or body == "-":
                    continue
                rule_index += 1
                output.append((section_name, rule_index, body))
        return output

    def _parse_sections(self, text: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current = "General"
        sections[current] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
                continue
            if current is not None:
                sections[current].append(line)
        return sections

    def _checksum(self, parts: list[str]) -> str:
        joined = "\n".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _string_or_none(self, value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
