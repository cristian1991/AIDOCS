from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent_memory_epoch import derive_agent_context_id, derive_aidocs_session_id
from .execution_index_store import ExecutionIndexStore


def _pid_alive(pid: int | None) -> bool:
    """Best-effort cross-platform liveness for a recorded worker PID.

    Returns True only when the process is provably alive. Unknown/None pid →
    False would falsely reap, so callers must gate on `pid is set` first; this
    returns True conservatively only on real evidence of life.
    """
    try:
        if not pid or int(pid) <= 0:
            return False
        pid = int(pid)
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        # POSIX: signal 0 probes existence without delivering a signal.
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False
    except Exception:
        return False


class SessionLaneAgentsStore:
    """Registry of per-lane sub-agent workers for the session dispatcher.

    Rows live in the shared execution index sqlite so other readers
    (dashboard, reaper, conductor) share one source of truth.
    """

    def __init__(self) -> None:
        self._execution_index = ExecutionIndexStore()

    def _timestamp(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def connect(self, project_root: Path) -> sqlite3.Connection:
        return self._execution_index.connect(project_root)

    def init_db(self, project_root: Path) -> None:
        self._execution_index.init_db(project_root)

    def register_worker(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        backend: str,
        allowed_files: list[str] | None = None,
        pid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self.init_db(project_root)
        worker_id = uuid4().hex
        now = self._timestamp()
        allowed_files_json = json.dumps(list(allowed_files or []), sort_keys=True, default=str)
        metadata_json = json.dumps(metadata or {}, sort_keys=True, default=str)
        with self.connect(project_root) as conn:
            conn.execute(
                """
                INSERT INTO session_lane_agents (
                    worker_id, session_id, lane_id, backend, state,
                    allowed_files, pid, started_at, updated_at,
                    completed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker_id,
                    session_id,
                    lane_id,
                    backend,
                    "running",
                    allowed_files_json,
                    pid,
                    now,
                    now,
                    None,
                    metadata_json,
                ),
            )
        return worker_id

    def set_host_session_id(
        self,
        project_root: Path,
        worker_id: str,
        host_session_id: str,
    ) -> bool:
        """Stamp host identity and its canonical derived worker identities.

        The host session may rotate on compaction/restart, so the three values
        are updated atomically from the same source row.  A repeated stamp is a
        no-op only when both derived identities are already present and match.
        """
        if not worker_id or not host_session_id:
            return False
        self.init_db(project_root)
        now = self._timestamp()
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT host_session_id, backend, session_id, "
                "agent_context_id, aidocs_session_id "
                "FROM session_lane_agents WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                return False
            backend = str(row["backend"] or "unknown")
            session_id = str(row["session_id"] or "")
            agent_context_id = derive_agent_context_id(
                host_kind=backend,
                project_root=project_root,
                host_session_id=host_session_id,
            )
            aidocs_session_id = derive_aidocs_session_id(
                host_kind=backend,
                project_root=project_root,
                host_session_id=host_session_id,
                session_uuid=session_id,
            )
            if (
                str(row["host_session_id"] or "") == host_session_id
                and str(row["agent_context_id"] or "") == agent_context_id
                and str(row["aidocs_session_id"] or "") == aidocs_session_id
            ):
                return False
            conn.execute(
                "UPDATE session_lane_agents SET host_session_id = ?, "
                "agent_context_id = ?, aidocs_session_id = ?, updated_at = ? "
                "WHERE worker_id = ?",
                (
                    host_session_id,
                    agent_context_id,
                    aidocs_session_id,
                    now,
                    worker_id,
                ),
            )
        return True


    def stamp_agent_context_id(
        self,
        project_root: Path,
        worker_id: str,
        agent_context_id: str,
    ) -> bool:
        """Stamp a worker row's canonical agent_context_id directly.

        Used by the #457 lane auto-bind path, where the caller already
        HOLDS its canonical id (resolved from the authenticated host
        chain) and no separate host_session_id is available to derive it
        from. Never overwrites a non-empty stamp — first bind wins.
        """
        if not worker_id or not agent_context_id:
            return False
        self.init_db(project_root)
        now = self._timestamp()
        with self.connect(project_root) as conn:
            cur = conn.execute(
                "UPDATE session_lane_agents SET agent_context_id = ?, updated_at = ? "
                "WHERE worker_id = ? "
                "AND (agent_context_id IS NULL OR agent_context_id = '')",
                (agent_context_id, now, worker_id),
            )
        return cur.rowcount > 0

    def find_latest_by_agent_context_id(
        self,
        project_root: Path,
        agent_context_id: str,
        *,
        session_id: str | None = None,
        lane_id: str | None = None,
        state_filter: str | None = None,
    ) -> dict[str, Any] | None:
        """Most recent worker row stamped with this canonical actor id.

        The #457/#463 identity seam: lets a governed call resolve the
        caller's lane binding from the spawn registry (#217) instead of
        env vars alone. Optional session/lane/state narrowing.
        """
        if not agent_context_id:
            return None
        self.init_db(project_root)
        sql = (
            "SELECT worker_id, session_id, lane_id, backend, state, "
            "allowed_files, pid, host_session_id, agent_context_id, aidocs_session_id, "
            "started_at, updated_at, completed_at, metadata_json "
            "FROM session_lane_agents WHERE agent_context_id = ?"
        )
        params: list[Any] = [agent_context_id]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if lane_id:
            sql += " AND lane_id = ?"
            params.append(lane_id)
        if state_filter:
            sql += " AND state = ?"
            params.append(state_filter)
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        with self.connect(project_root) as conn:
            row = conn.execute(sql, params).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def update_worker_state(
        self,
        project_root: Path,
        worker_id: str,
        state: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        self.init_db(project_root)
        now = self._timestamp()
        completed_at = now if state in {"done", "failed", "crashed"} else None
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT metadata_json, completed_at FROM session_lane_agents WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                return False
            merged: dict[str, Any] = {}
            existing = row["metadata_json"] if isinstance(row, sqlite3.Row) else row[0]
            if existing:
                try:
                    loaded = json.loads(existing)
                    if isinstance(loaded, dict):
                        merged.update(loaded)
                except (TypeError, ValueError):
                    pass
            if metadata:
                merged.update(metadata)
            merged_json = json.dumps(merged, sort_keys=True, default=str)
            existing_completed = row["completed_at"] if isinstance(row, sqlite3.Row) else row[1]
            final_completed = completed_at or existing_completed
            conn.execute(
                """
                UPDATE session_lane_agents
                SET state = ?, updated_at = ?, completed_at = ?, metadata_json = ?
                WHERE worker_id = ?
                """,
                (state, now, final_completed, merged_json, worker_id),
            )
        return True

    def latest_lane_host_session_id(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> str:
        """The lane's most recently stamped host_session_id, or "".

        #110 Expert resumption: on re-dispatch to the SAME lane, the
        dispatcher passes `<host> --resume <this id>` so the worker
        resumes its prior conversation (context, decisions, loaded
        state) — the worker→Expert promotion. Scoped to (session_id,
        lane_id); unstamped rows are skipped so a crashed spawn that
        never reported an id doesn't shadow an older resumable one.
        """
        if not session_id or not lane_id:
            return ""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT host_session_id FROM session_lane_agents "
                "WHERE session_id = ? AND lane_id = ? "
                "AND host_session_id IS NOT NULL AND host_session_id != '' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (session_id, lane_id),
            ).fetchone()
        if row is None:
            return ""
        value = row["host_session_id"] if isinstance(row, sqlite3.Row) else row[0]
        return str(value or "")

    def get_lane_agents(
        self,
        project_root: Path,
        session_id: str,
        state_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        self.init_db(project_root)
        sql = (
            "SELECT worker_id, session_id, lane_id, backend, state, "
            "allowed_files, pid, host_session_id, agent_context_id, aidocs_session_id, "
            "started_at, updated_at, completed_at, metadata_json "
            "FROM session_lane_agents WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if state_filter:
            sql += " AND state = ?"
            params.append(state_filter)
        sql += " ORDER BY started_at ASC, worker_id ASC"
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_all_lane_agents(
        self,
        project_root: Path,
        state_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cross-session roster: every lane agent in the project, ALL chats.

        Same shape as ``get_lane_agents`` but WITHOUT the session_id filter,
        so a caller can see agents launched from other chats/sessions (clause
        3 cross-agent coordination). Ordered oldest-first for stable output.
        """
        self.init_db(project_root)
        sql = (
            "SELECT worker_id, session_id, lane_id, backend, state, "
            "allowed_files, pid, host_session_id, agent_context_id, aidocs_session_id, "
            "started_at, updated_at, completed_at, metadata_json "
            "FROM session_lane_agents"
        )
        params: list[Any] = []
        if state_filter:
            sql += " WHERE state = ?"
            params.append(state_filter)
        sql += " ORDER BY started_at ASC, worker_id ASC"
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def reap_crashed(
        self,
        project_root: Path,
        session_id: str,
        stale_after_seconds: int = 300,
    ) -> int:
        self.init_db(project_root)
        now_dt = datetime.now(UTC).replace(microsecond=0)
        cutoff_epoch = now_dt.timestamp() - stale_after_seconds
        now_iso = now_dt.isoformat().replace("+00:00", "Z")
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT worker_id, updated_at, pid FROM session_lane_agents "
                "WHERE session_id = ? AND state = 'running'",
                (session_id,),
            ).fetchall()
            stale_ids: list[str] = []
            for row in rows:
                pid = row["pid"]
                # A recorded PID that is provably dead = crashed NOW, regardless
                # of heartbeat recency (the §968 false-'running' bug). A row with
                # no recorded PID can't be proven dead, so it falls back to the
                # heartbeat-staleness cutoff.
                pid_dead = pid is not None and int(pid) > 0 and not _pid_alive(pid)
                heartbeat_stale = self._iso_to_epoch(row["updated_at"]) < cutoff_epoch
                if pid_dead or heartbeat_stale:
                    stale_ids.append(row["worker_id"])
            if not stale_ids:
                return 0
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"UPDATE session_lane_agents "
                f"SET state = 'crashed', updated_at = ?, completed_at = ? "
                f"WHERE worker_id IN ({placeholders})",
                (now_iso, now_iso, *stale_ids),
            )
        return len(stale_ids)

    @staticmethod
    def _iso_to_epoch(ts: str | None) -> float:
        if not ts:
            return 0.0
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        allowed_files_raw = row["allowed_files"]
        try:
            allowed_files = json.loads(allowed_files_raw) if allowed_files_raw else []
        except (TypeError, ValueError):
            allowed_files = []
        metadata_raw = row["metadata_json"]
        try:
            metadata = json.loads(metadata_raw) if metadata_raw else {}
        except (TypeError, ValueError):
            metadata = {}
        return {
            "worker_id": row["worker_id"],
            "session_id": row["session_id"],
            "lane_id": row["lane_id"],
            "backend": row["backend"],
            "state": row["state"],
            "allowed_files": allowed_files,
            "pid": row["pid"],
            "host_session_id": row["host_session_id"],
            "agent_context_id": row["agent_context_id"],
            "aidocs_session_id": row["aidocs_session_id"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "metadata": metadata,
        }
