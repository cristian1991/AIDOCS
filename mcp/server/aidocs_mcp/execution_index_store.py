from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class ExecutionIndexStore:
    """Derived SQLite index for execution runs and event evidence."""

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
                CREATE TABLE IF NOT EXISTS execution_runs (
                    run_id TEXT PRIMARY KEY,
                    run_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    status TEXT NOT NULL,
                    ad_hoc INTEGER NOT NULL DEFAULT 1,
                    target_entity TEXT,
                    metadata_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    event_kind TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    session_id TEXT,
                    procedure_id TEXT,
                    capability_name TEXT,
                    action_kind TEXT,
                    target_entity TEXT,
                    status TEXT,
                    payload_json TEXT,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
                );
                """
            )

    def record_run(
        self,
        project_root: Path,
        run_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        status: str = "started",
        ad_hoc: bool = True,
        target_entity: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        completed_at: str | None = None,
    ) -> str:
        self.init_db(project_root)
        run_id = run_id or f"run-{uuid4()}"
        started_at = self._timestamp()
        with self.connect(project_root) as conn:
            conn.execute(
                """
                INSERT INTO execution_runs (
                    run_id, run_kind, source_kind, session_id, procedure_id, capability_name,
                    status, ad_hoc, target_entity, metadata_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    run_kind=excluded.run_kind,
                    source_kind=excluded.source_kind,
                    session_id=excluded.session_id,
                    procedure_id=excluded.procedure_id,
                    capability_name=excluded.capability_name,
                    status=excluded.status,
                    ad_hoc=excluded.ad_hoc,
                    target_entity=excluded.target_entity,
                    metadata_json=excluded.metadata_json,
                    completed_at=COALESCE(excluded.completed_at, execution_runs.completed_at)
                """,
                (
                    run_id,
                    run_kind,
                    source_kind,
                    session_id,
                    procedure_id,
                    capability_name,
                    status,
                    1 if ad_hoc else 0,
                    target_entity,
                    json.dumps(metadata or {}, sort_keys=True, default=str),
                    started_at,
                    completed_at,
                ),
            )
        return run_id

    def record_event(
        self,
        project_root: Path,
        event_kind: str,
        source_kind: str,
        session_id: str | None = None,
        procedure_id: str | None = None,
        capability_name: str | None = None,
        action_kind: str | None = None,
        target_entity: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        observed_at: str | None = None,
    ) -> str:
        self.init_db(project_root)
        event_id = event_id or f"event-{uuid4()}"
        with self.connect(project_root) as conn:
            conn.execute(
                """
                INSERT INTO execution_events (
                    event_id, run_id, event_kind, source_kind, session_id, procedure_id,
                    capability_name, action_kind, target_entity, status, payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    event_kind,
                    source_kind,
                    session_id,
                    procedure_id,
                    capability_name,
                    action_kind,
                    target_entity,
                    status,
                    json.dumps(payload or {}, sort_keys=True, default=str),
                    observed_at or self._timestamp(),
                ),
            )
        return event_id

    def execution_status(self, project_root: Path) -> dict[str, Any]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            run_count = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
            run_kind_rows = conn.execute(
                "SELECT run_kind, COUNT(*) AS count FROM execution_runs GROUP BY run_kind ORDER BY count DESC, run_kind ASC"
            ).fetchall()
            event_kind_rows = conn.execute(
                "SELECT event_kind, COUNT(*) AS count FROM execution_events GROUP BY event_kind ORDER BY count DESC, event_kind ASC"
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_kind, COUNT(*) AS count FROM execution_events GROUP BY source_kind ORDER BY count DESC, source_kind ASC"
            ).fetchall()
        return {
            "db_path": str(self.db_path(project_root)),
            "execution_runs": int(run_count),
            "execution_events": int(event_count),
            "run_kinds": {row["run_kind"]: int(row["count"]) for row in run_kind_rows},
            "event_kinds": {row["event_kind"]: int(row["count"]) for row in event_kind_rows},
            "by_source": {row["source_kind"]: int(row["count"]) for row in source_rows},
        }

    def list_runs(self, project_root: Path, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = "SELECT run_id, run_kind, source_kind, session_id, procedure_id, capability_name, status, ad_hoc, target_entity, metadata_json, started_at, completed_at FROM execution_runs"
        params: list[Any] = []
        if session_id and session_id.strip():
            sql += " WHERE session_id = ?"
            params.append(session_id.strip())
        sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_row_to_dict(row) for row in rows]

    def list_events(
        self,
        project_root: Path,
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT event_id, run_id, event_kind, source_kind, session_id, procedure_id, capability_name, action_kind, target_entity, status, payload_json, observed_at "
            "FROM execution_events"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if session_id and session_id.strip():
            clauses.append("session_id = ?")
            params.append(session_id.strip())
        if query and query.strip():
            needle = f"%{query.strip()}%"
            clauses.append("(event_kind LIKE ? OR COALESCE(capability_name, '') LIKE ? OR COALESCE(action_kind, '') LIKE ? OR COALESCE(payload_json, '') LIKE ?)")
            params.extend([needle, needle, needle, needle])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, event_id DESC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_row_to_dict(row) for row in rows]

    def _run_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "run_kind": row["run_kind"],
            "source_kind": row["source_kind"],
            "session_id": row["session_id"],
            "procedure_id": row["procedure_id"],
            "capability_name": row["capability_name"],
            "status": row["status"],
            "ad_hoc": bool(row["ad_hoc"]),
            "target_entity": row["target_entity"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def _event_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "event_kind": row["event_kind"],
            "source_kind": row["source_kind"],
            "session_id": row["session_id"],
            "procedure_id": row["procedure_id"],
            "capability_name": row["capability_name"],
            "action_kind": row["action_kind"],
            "target_entity": row["target_entity"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
            "observed_at": row["observed_at"],
        }

    def query_last_execution(
        self,
        project_root: Path,
        action_kind: str | None = None,
        capability_name: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Query: 'What actually ran last time?' — returns recent execution events matching filters."""
        self.init_db(project_root)
        clauses: list[str] = []
        params: list[Any] = []
        if action_kind and action_kind.strip():
            clauses.append("action_kind = ?")
            params.append(action_kind.strip())
        if capability_name and capability_name.strip():
            clauses.append("capability_name = ?")
            params.append(capability_name.strip())
        if session_id and session_id.strip():
            clauses.append("session_id = ?")
            params.append(session_id.strip())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT event_id, run_id, event_kind, source_kind, session_id, procedure_id, "
            "capability_name, action_kind, target_entity, status, payload_json, observed_at "
            f"FROM execution_events{where} ORDER BY observed_at DESC LIMIT ?"
        )
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_row_to_dict(row) for row in rows]

    def query_execution_summary(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Query: 'What happened in this session?' — returns aggregate execution summary."""
        self.init_db(project_root)
        where = ""
        params: list[Any] = []
        if session_id and session_id.strip():
            where = " WHERE session_id = ?"
            params.append(session_id.strip())
        with self.connect(project_root) as conn:
            total_events = conn.execute(f"SELECT COUNT(*) FROM execution_events{where}", params).fetchone()[0]
            action_kinds = conn.execute(
                f"SELECT action_kind, COUNT(*) AS count FROM execution_events{where} GROUP BY action_kind ORDER BY count DESC",
                params,
            ).fetchall()
            event_kinds = conn.execute(
                f"SELECT event_kind, COUNT(*) AS count FROM execution_events{where} GROUP BY event_kind ORDER BY count DESC",
                params,
            ).fetchall()
            sources = conn.execute(
                f"SELECT source_kind, COUNT(*) AS count FROM execution_events{where} GROUP BY source_kind ORDER BY count DESC",
                params,
            ).fetchall()
            # Ad-hoc vs procedure-linked
            adhoc_count = conn.execute(
                f"SELECT COUNT(*) FROM execution_events{where} {'AND' if where else 'WHERE'} procedure_id IS NULL",
                params,
            ).fetchone()[0]
            procedure_count = conn.execute(
                f"SELECT COUNT(*) FROM execution_events{where} {'AND' if where else 'WHERE'} procedure_id IS NOT NULL",
                params,
            ).fetchone()[0]
        return {
            "session_id": session_id,
            "total_events": int(total_events),
            "by_action_kind": {row["action_kind"]: int(row["count"]) for row in action_kinds if row["action_kind"]},
            "by_event_kind": {row["event_kind"]: int(row["count"]) for row in event_kinds},
            "by_source": {row["source_kind"]: int(row["count"]) for row in sources},
            "ad_hoc_events": int(adhoc_count),
            "procedure_linked_events": int(procedure_count),
        }

    def query_procedure_compliance(
        self,
        project_root: Path,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query: 'Did execution follow the intended procedure?' — compares runs against procedures."""
        self.init_db(project_root)
        where = ""
        params: list[Any] = []
        if session_id and session_id.strip():
            where = " WHERE session_id = ?"
            params.append(session_id.strip())
        with self.connect(project_root) as conn:
            # Runs with procedures
            procedured = conn.execute(
                f"SELECT run_id, run_kind, procedure_id, capability_name, status, ad_hoc, started_at, completed_at "
                f"FROM execution_runs{where} {'AND' if where else 'WHERE'} procedure_id IS NOT NULL "
                f"ORDER BY started_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            # Ad-hoc runs (no procedure)
            adhoc = conn.execute(
                f"SELECT run_id, run_kind, capability_name, status, started_at, completed_at "
                f"FROM execution_runs{where} {'AND' if where else 'WHERE'} procedure_id IS NULL AND ad_hoc = 1 "
                f"ORDER BY started_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return {
            "session_id": session_id,
            "procedure_linked_runs": [
                {
                    "run_id": row["run_id"],
                    "run_kind": row["run_kind"],
                    "procedure_id": row["procedure_id"],
                    "capability_name": row["capability_name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }
                for row in procedured
            ],
            "ad_hoc_runs": [
                {
                    "run_id": row["run_id"],
                    "run_kind": row["run_kind"],
                    "capability_name": row["capability_name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }
                for row in adhoc
            ],
            "compliance_ratio": f"{len(procedured)}/{len(procedured) + len(adhoc)}" if (procedured or adhoc) else "no data",
        }

    def prune_old_events(
        self,
        project_root: Path,
        max_age_days: int = 30,
        max_events: int = 10000,
    ) -> dict[str, int]:
        """Prune execution events older than max_age_days or exceeding max_events count."""
        self.init_db(project_root)
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=max_age_days)).isoformat().replace("+00:00", "Z")
        with self.connect(project_root) as conn:
            # Delete by age
            age_result = conn.execute(
                "DELETE FROM execution_events WHERE observed_at < ?", (cutoff,)
            )
            pruned_by_age = age_result.rowcount

            # Delete excess (keep most recent max_events)
            total = conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
            pruned_by_count = 0
            if total > max_events:
                excess = total - max_events
                conn.execute(
                    "DELETE FROM execution_events WHERE event_id IN "
                    "(SELECT event_id FROM execution_events ORDER BY observed_at ASC LIMIT ?)",
                    (excess,),
                )
                pruned_by_count = excess

            # Also prune old runs
            run_age_result = conn.execute(
                "DELETE FROM execution_runs WHERE started_at < ? AND run_id NOT IN "
                "(SELECT DISTINCT run_id FROM execution_events WHERE run_id IS NOT NULL)",
                (cutoff,),
            )
            pruned_runs = run_age_result.rowcount

        return {
            "pruned_events_by_age": pruned_by_age,
            "pruned_events_by_count": pruned_by_count,
            "pruned_orphan_runs": pruned_runs,
            "max_age_days": max_age_days,
            "max_events": max_events,
        }

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
