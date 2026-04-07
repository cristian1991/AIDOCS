from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProcedureCapabilityLinkStore:
    """Derived SQLite bridge between procedure definitions and capabilities."""

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
                CREATE TABLE IF NOT EXISTS procedure_capability_links (
                    link_id TEXT PRIMARY KEY,
                    procedure_id TEXT NOT NULL,
                    capability_name TEXT,
                    action_kind TEXT,
                    link_kind TEXT NOT NULL,
                    resolution_status TEXT NOT NULL,
                    match_basis TEXT,
                    notes TEXT,
                    discovered_at TEXT NOT NULL
                );
                """
            )

    def sync_links(
        self,
        project_root: Path,
        procedures: list[dict[str, Any]],
        capabilities: list[dict[str, Any]],
    ) -> int:
        self.init_db(project_root)
        discovered_at = self._timestamp()
        capability_by_name = {
            str(item.get("name") or "").strip(): item
            for item in capabilities
            if str(item.get("name") or "").strip()
        }
        capability_by_alias: dict[str, dict[str, Any]] = {}
        for item in capabilities:
            for alias in item.get("aliases") or []:
                normalized = str(alias).strip()
                if normalized:
                    capability_by_alias.setdefault(normalized, item)
        rows: list[tuple[str, str, str | None, str | None, str, str, str | None, str | None, str]] = []
        for procedure in procedures:
            definition_kind = str(procedure.get("definition_kind") or "")
            procedure_id = str(procedure.get("procedure_id") or "").strip()
            if not procedure_id:
                continue
            if definition_kind == "workflow_rule":
                matches = self._match_rule_capabilities(
                    str(procedure.get("body_text") or ""),
                    capability_by_name,
                    capability_by_alias,
                )
                for idx, match in enumerate(matches, start=1):
                    rows.append(
                        (
                            f"{procedure_id}::rule::{idx:02d}",
                            procedure_id,
                            match["capability_name"],
                            None,
                            "procedure_mentions_capability",
                            "resolved",
                            match["match_basis"],
                            None,
                            discovered_at,
                        )
                    )
                continue
            if definition_kind != "workflow_action":
                continue
            action_kind = str(procedure.get("action_kind") or "").strip() or None
            if action_kind and action_kind in capability_by_name:
                rows.append(
                    (
                        f"{procedure_id}::{action_kind}",
                        procedure_id,
                        action_kind,
                        action_kind,
                        "procedure_declares_capability",
                        "resolved",
                        "direct_action_kind_match",
                        None,
                        discovered_at,
                    )
                )
                continue
            if action_kind and action_kind in capability_by_alias:
                capability = capability_by_alias[action_kind]
                capability_name = str(capability.get("name") or "").strip() or None
                rows.append(
                    (
                        f"{procedure_id}::{action_kind}:alias",
                        procedure_id,
                        capability_name,
                        action_kind,
                        "procedure_declares_capability",
                        "resolved",
                        "capability_alias_match",
                        None,
                        discovered_at,
                    )
                )
                continue
            continue

        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM procedure_capability_links")
            conn.executemany(
                """
                INSERT INTO procedure_capability_links (
                    link_id,
                    procedure_id,
                    capability_name,
                    action_kind,
                    link_kind,
                    resolution_status,
                    match_basis,
                    notes,
                    discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def link_status(self, project_root: Path) -> dict[str, Any]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            count = conn.execute("SELECT COUNT(*) FROM procedure_capability_links").fetchone()[0]
            status_rows = conn.execute(
                "SELECT resolution_status, COUNT(*) AS count FROM procedure_capability_links GROUP BY resolution_status ORDER BY count DESC, resolution_status ASC"
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "procedure_capability_links": int(count),
            "by_resolution": {row["resolution_status"]: int(row["count"]) for row in status_rows},
        }

    def list_links(
        self,
        project_root: Path,
        procedure_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT link_id, procedure_id, capability_name, action_kind, link_kind, resolution_status, match_basis, notes, discovered_at "
            "FROM procedure_capability_links"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if procedure_id and procedure_id.strip():
            clauses.append("procedure_id = ?")
            params.append(procedure_id.strip())
        if unresolved_only:
            clauses.append("resolution_status = 'unresolved'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY procedure_id ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _match_rule_capabilities(
        self,
        text: str,
        capability_by_name: dict[str, dict[str, Any]],
        capability_by_alias: dict[str, dict[str, Any]],
    ) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        for token in re.findall(r"`([^`]+)`", text):
            item = capability_by_name.get(token) or capability_by_alias.get(token)
            if not item:
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            found.append({"capability_name": name, "match_basis": "workflow_rule_backtick_match"})

        normalized = f" {text} "
        for name in sorted(capability_by_name.keys(), key=len, reverse=True):
            if name in seen:
                continue
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", normalized):
                continue
            seen.add(name)
            found.append({"capability_name": name, "match_basis": "workflow_rule_text_match"})

        for alias, item in sorted(capability_by_alias.items(), key=lambda pair: len(pair[0]), reverse=True):
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", normalized):
                continue
            seen.add(name)
            found.append({"capability_name": name, "match_basis": "workflow_rule_alias_match"})

        return found

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
