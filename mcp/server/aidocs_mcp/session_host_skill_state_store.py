from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class SessionHostSkillStateStore(SQLiteIndexStoreBase):
    """Per-session host-skill-state cache — sqlite-backed replacement for
    ``.MEMORY/.runtime/sessions/{id}/host-skill-state.json`` (and the
    older ``.MEMORY/sessions/{id}/host-skill-state.json``).

    The payload is a computed snapshot (selected_skills, active_skills,
    provider_states, triggered list, etc.) that the runtime writes on
    every refresh. Nothing queries into the fields — consumers read the
    whole blob — so the store keeps it opaque as JSON TEXT.

    Both legacy locations are swept on init. The newer runtime-subdir
    path wins when both exist, because that's what the live runtime has
    been writing to; the older path only contained historical captures
    from sessions that predate the subdir migration.
    """

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_host_skill_state (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT
                );
                """,
            )
        self._ingest_all_legacy_json(project_root)

    # ── legacy discovery ──

    def _runtime_legacy_path(self, project_root: Path, session_id: str) -> Path:
        return (
            project_root
            / ".MEMORY"
            / ".runtime"
            / "sessions"
            / session_id
            / "host-skill-state.json"
        )

    def _older_legacy_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "host-skill-state.json"

    def _ingest_all_legacy_json(self, project_root: Path) -> None:
        # Sweep both legacy locations. Session IDs appear under either
        # tree, so walking both catches everything; ingest is idempotent
        # per session_id — if a sqlite row already exists the legacy
        # files are simply deleted.
        seen_session_ids: set[str] = set()
        runtime_dir = project_root / ".MEMORY" / ".runtime" / "sessions"
        sessions_dir = project_root / ".MEMORY" / "sessions"
        for root_dir in (runtime_dir, sessions_dir):
            if not root_dir.is_dir():
                continue
            for entry in root_dir.iterdir():
                if entry.is_dir():
                    seen_session_ids.add(entry.name)
        for session_id in seen_session_ids:
            self._ingest_single_legacy(project_root, session_id)

    def _ingest_single_legacy(self, project_root: Path, session_id: str) -> None:
        # Prefer the runtime-subdir file because that's what the current
        # code writes. The older file, if present, is stale cache and
        # gets deleted without influencing the ingested row.
        newer = self._runtime_legacy_path(project_root, session_id)
        older = self._older_legacy_path(project_root, session_id)
        source: Path | None = None
        if newer.is_file():
            source = newer
        elif older.is_file():
            source = older

        if source is None:
            return

        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt JSON stays on disk for operator triage; don't
            # delete or insert anything so the runtime can rebuild the
            # cache from skill_trigger_state on next touch.
            return
        if not isinstance(raw, dict):
            return

        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT 1 FROM session_host_skill_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is None:
                # Strip the legacy `path` field — it pointed at the JSON
                # file we're about to delete and would mislead readers
                # if surfaced back out of the store.
                clean_payload = {k: v for k, v in raw.items() if k != "path"}
                conn.execute(
                    "INSERT INTO session_host_skill_state (session_id, payload, updated_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, json.dumps(clean_payload), self._timestamp()),
                )

        # Delete BOTH legacy files so the project doesn't carry stale
        # cache after ingest. Use unlink(missing_ok=True) defensively —
        # a concurrent run could have already cleaned one of them.
        try:
            newer.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            older.unlink(missing_ok=True)
        except OSError:
            pass

    # ── public API ──

    def get(self, project_root: Path, session_id: str) -> dict[str, Any] | None:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT payload FROM session_host_skill_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["payload"] or "{}")
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set(self, project_root: Path, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._timestamp()
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO session_host_skill_state (session_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (session_id, json.dumps(payload), now),
            )
        return dict(payload)
