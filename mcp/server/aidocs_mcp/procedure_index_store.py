from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class ProcedureIndexStore(SQLiteIndexStoreBase):
    """Derived SQLite index for workflow and procedure definitions."""

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
                """,
            )

    def sync_procedures(
        self,
        project_root: Path,
        compiled_workflow: dict[str, object] | None = None,
    ) -> int:
        self.init_db(project_root)
        rows: list[
            tuple[
                str,
                str,
                str,
                str,
                str | None,
                str | None,
                str,
                str | None,
                str | None,
                str | None,
                str | None,
                str | None,
                str,
                str,
            ]
        ] = []
        from . import workflow_definitions_store as _wd

        discovered_at = self._timestamp()

        # Workflow RULES come from the canonical workflow_definitions SQL
        # (Stage 5c, SQL-canonical end to end). Procedure authority is NEVER
        # derived from raw markdown — that would re-introduce a hidden runtime
        # markdown authority path. When the SQL store is empty, we index zero
        # workflow rule rows and emit a visible diagnostic; the audited
        # markdown→SQL migration happens only through
        # WorkflowActionService.compile_project_rules, which the bootstrap
        # sync runs before sync_procedures. (Action DEFINITIONS below already
        # come from the compiled payload, which is SQL-canonical.)
        rule_defs = _wd.active_bodies(project_root, "rule")
        if rule_defs:
            section_name = "Workflow Rules"
            for rule_index, rule_text in enumerate(rule_defs, start=1):
                procedure_id = f"workflow-rule:{section_name}:{rule_index}"
                checksum = self._checksum(
                    [procedure_id, "workflow_definitions", section_name, rule_text],
                )
                rows.append(
                    (
                        procedure_id,
                        "workflow_rule",
                        "memory_rule_file",
                        "workflow_definitions",
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
                    ),
                )
        else:
            workflow_root = project_root / ".MEMORY" / "rules"
            legacy_present = (workflow_root / "workflow-rules.md").is_file() or (
                workflow_root / "workflow-actions.md"
            ).is_file()
            if legacy_present:
                # Legacy markdown exists but SQL is empty: refuse to derive
                # authority from it. Surface a durable diagnostic so the
                # empty-but-md state is visible and a compile migration is
                # clearly required.
                try:
                    from .execution_index_store import ExecutionIndexStore

                    ExecutionIndexStore().record_event(
                        project_root,
                        event_kind="workflow_procedure_markdown_not_authority",
                        source_kind="procedure_index_store",
                        capability_name="sync_procedures",
                        action_kind="skip",
                        target_entity="workflow_definitions",
                        status="migration_required",
                        payload={
                            "legacy_markdown_present": True,
                            "reason": "workflow_definitions empty; run "
                            "compile_project_rules to migrate markdown to SQL "
                            "before procedure authority is derived",
                        },
                    )
                except Exception:
                    # Diagnostic is best-effort; the load-bearing guarantee is
                    # that NO markdown rows are indexed, which holds regardless.
                    pass

        compiled = compiled_workflow or {}
        for action in compiled.get("action_definitions", []) or []:
            if not isinstance(action, dict):
                continue
            procedure_id = str(action.get("id") or "").strip()
            if not procedure_id:
                continue
            action_kind = self._string_or_none(action.get("kind"))
            title = self._string_or_none(action.get("name"))
            source_rule = self._string_or_none(action.get("source_rule"))
            source_segment = self._string_or_none(action.get("source_segment"))
            action_payload = {
                key: value
                for key, value in action.items()
                if key not in {"id", "name", "kind", "source_rule", "source_segment"}
            }
            checksum = self._checksum(
                [
                    procedure_id,
                    title or "",
                    action_kind or "",
                    source_rule or "",
                    source_segment or "",
                    json.dumps(action_payload, sort_keys=True, default=str),
                ],
            )
            rows.append(
                (
                    procedure_id,
                    "workflow_action_definition",
                    "compiled_workflow_action_definition",
                    ".MEMORY/config/workflow-actions.json",
                    "Workflow Actions",
                    title,
                    source_rule or source_segment or procedure_id,
                    None,
                    action_kind,
                    json.dumps(action_payload, sort_keys=True, default=str),
                    source_rule,
                    source_segment,
                    checksum,
                    discovered_at,
                ),
            )

        for rule in compiled.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            procedure_id = str(rule.get("id") or "").strip()
            if not procedure_id:
                continue
            trigger = self._string_or_none(rule.get("trigger"))
            source_rule = self._string_or_none(rule.get("source_rule"))
            steps = [item for item in (rule.get("steps") or []) if isinstance(item, dict)]
            checksum = self._checksum(
                [
                    procedure_id,
                    trigger or "",
                    source_rule or "",
                    json.dumps(steps, sort_keys=True, default=str),
                ],
            )
            rows.append(
                (
                    procedure_id,
                    "workflow_flow",
                    "compiled_workflow_rule",
                    ".MEMORY/config/workflow-actions.json",
                    "Workflow Rules",
                    None,
                    source_rule or procedure_id,
                    trigger,
                    None,
                    json.dumps({"steps": steps}, sort_keys=True, default=str),
                    source_rule,
                    None,
                    checksum,
                    discovered_at,
                ),
            )
            for step in steps:
                idx = int(step.get("index") or 0)
                step_id = f"{procedure_id}::step::{idx:02d}"
                action_kind = self._string_or_none(step.get("kind"))
                source_segment = self._string_or_none(step.get("source_segment"))
                checksum = self._checksum(
                    [
                        step_id,
                        procedure_id,
                        str(idx),
                        action_kind or "",
                        source_segment or "",
                        json.dumps(step, sort_keys=True, default=str),
                    ],
                )
                rows.append(
                    (
                        step_id,
                        "workflow_flow_step",
                        "compiled_workflow_rule_step",
                        ".MEMORY/config/workflow-actions.json",
                        "Workflow Rules",
                        self._string_or_none(step.get("action_ref")) or f"step {idx}",
                        source_segment or step_id,
                        trigger,
                        action_kind,
                        json.dumps(step, sort_keys=True, default=str),
                        source_rule,
                        source_segment,
                        checksum,
                        discovered_at,
                    ),
                )

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
            checksum = self._checksum(
                [
                    procedure_id,
                    trigger or "",
                    action_kind or "",
                    source_rule or "",
                    source_segment or "",
                    json.dumps(action_payload, sort_keys=True, default=str),
                ],
            )
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
                ),
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
                "SELECT definition_kind, COUNT(*) AS count FROM procedure_definitions GROUP BY definition_kind ORDER BY count DESC, definition_kind ASC",
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_kind, COUNT(*) AS count FROM procedure_definitions GROUP BY source_kind ORDER BY count DESC, source_kind ASC",
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "procedure_definitions": int(count),
            "by_kind": {row["definition_kind"]: int(row["count"]) for row in kind_rows},
            "by_source": {row["source_kind"]: int(row["count"]) for row in source_rows},
        }

    def find_procedures(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT procedure_id, definition_kind, source_kind, source_path, section_name, title, body_text, trigger, action_kind, action_payload_json, source_rule, source_segment, discovered_at "
            "FROM procedure_definitions"
        )
        params: list[Any] = []
        if query and query.strip():
            sql += (
                " WHERE procedure_id LIKE ? OR body_text LIKE ? OR COALESCE(source_rule, '') LIKE ?"
            )
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
            "action_payload": json.loads(row["action_payload_json"])
            if row["action_payload_json"]
            else None,
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
