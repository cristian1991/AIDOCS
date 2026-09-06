"""Per-session TodoWrite state store.

Persists the most recent TodoWrite payload for a (project_root, session_id)
so the PostToolUse hook can diff incoming payloads against prior state and
map transitions to task_begin/task_update/task_complete.

Schema:
    session_todos(
        session_id TEXT PRIMARY KEY,
        todos_json TEXT NOT NULL,  -- JSON-serialized list of todo items
        updated_at TEXT NOT NULL
    )

Lives in the project's SQLite index (.MEMORY/.index/aidocs.sqlite3) so it
shares a connection pool with execution_events and the query gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: RUNTIME (the helper's default). Both tables are live
# session state: the last TodoWrite payload and the per-actor task slot.
# Neither is an authority -- the slot is a mutual-exclusion claim, not a
# permission, and the only write whose loss RELAXES anything is the claim
# itself, whose claimant a power cut kills in the same instant. A lost
# close leaves the slot ACTIVE, which is the fail-CLOSED direction. This
# also runs on every TodoWrite PostToolUse event, so NORMAL earns its keep.
from ._sqlite_connect import connect as _canonical_connect


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _init(conn: sqlite3.Connection) -> None:
    """Ensure actor-scoped todo/task state, migrating the legacy session row."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = conn.execute("PRAGMA table_info(session_todos)").fetchall()
        names = {str(row[1]) for row in columns}
        pk = [
            str(row[1])
            for row in sorted(columns, key=lambda row: int(row[5] or 0))
            if int(row[5] or 0)
        ]
        expected_pk = ["session_id", "agent_context_id", "lane_id"]
        if columns and ({"agent_context_id", "lane_id"} - names or pk != expected_pk):
            conn.execute("ALTER TABLE session_todos RENAME TO session_todos_legacy_scope")
            conn.execute(
                """
                CREATE TABLE session_todos (
                    session_id TEXT NOT NULL,
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    lane_id TEXT NOT NULL DEFAULT '',
                    todos_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, agent_context_id, lane_id)
                )
                """,
            )
            if {"agent_context_id", "lane_id"} <= names:
                conn.execute(
                    "INSERT INTO session_todos "
                    "(session_id, agent_context_id, lane_id, todos_json, updated_at) "
                    "SELECT session_id, COALESCE(agent_context_id, ''), "
                    "COALESCE(lane_id, ''), todos_json, updated_at "
                    "FROM session_todos_legacy_scope",
                )
            else:
                conn.execute(
                    "INSERT INTO session_todos "
                    "(session_id, agent_context_id, lane_id, todos_json, updated_at) "
                    "SELECT session_id, '', '', todos_json, updated_at "
                    "FROM session_todos_legacy_scope",
                )
            conn.execute("DROP TABLE session_todos_legacy_scope")
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_todos (
                    session_id TEXT NOT NULL,
                    agent_context_id TEXT NOT NULL DEFAULT '',
                    lane_id TEXT NOT NULL DEFAULT '',
                    todos_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, agent_context_id, lane_id)
                )
                """,
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actor_task_state (
                session_id TEXT NOT NULL,
                agent_context_id TEXT NOT NULL,
                lane_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent_context_id, lane_id)
            )
            """,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class TodoStateStore:
    """Read/write the most recent TodoWrite payload per actor task owner."""

    @staticmethod
    def _resolve_owner(
        project_root: Path,
        agent_context_id: str = "",
        lane_id: str = "",
    ) -> tuple[str, str]:
        """Resolve a worker owner and fail closed if its actor is unavailable.

        Delegates worker detection + lane resolution to the shared
        #463/#457 seam (task_actor_identity) so todo ownership keys on
        the SAME (actor, lane) pair as the actor task slots. Explicit
        caller-passed values always win.
        """
        actor_id = str(agent_context_id or "").strip()
        lane = str(lane_id or "").strip()
        try:
            from .task_actor_identity import resolve_task_actor

            resolved_actor, resolved_lane, is_worker = resolve_task_actor(project_root)
        except Exception:
            resolved_actor, resolved_lane, is_worker = "", "", False
        is_worker = is_worker or bool(lane)
        if not is_worker:
            return actor_id, lane
        if not actor_id:
            actor_id = resolved_actor
        if not lane:
            lane = resolved_lane
        if not actor_id:
            raise RuntimeError("worker todo state requires canonical agent_context_id")
        return actor_id, lane

    def get(
        self,
        project_root: Path,
        session_id: str,
        *,
        agent_context_id: str = "",
        lane_id: str = "",
    ) -> list[dict[str, Any]]:
        db = _db_path(project_root)
        if not db.is_file():
            return []
        actor_id, lane = self._resolve_owner(project_root, agent_context_id, lane_id)
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT todos_json FROM session_todos "
                "WHERE session_id = ? AND agent_context_id = ? AND lane_id = ?",
                (session_id, actor_id, lane),
            ).fetchone()
        if not row:
            return []
        try:
            data = json.loads(row[0])
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict)]

    def set(
        self,
        project_root: Path,
        session_id: str,
        todos: list[dict[str, Any]],
        *,
        agent_context_id: str = "",
        lane_id: str = "",
    ) -> None:
        db = _db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        actor_id, lane = self._resolve_owner(project_root, agent_context_id, lane_id)
        now = datetime.now(UTC).isoformat()
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO session_todos "
                "(session_id, agent_context_id, lane_id, todos_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, actor_id, lane, json.dumps(todos, default=str), now),
            )
            conn.commit()

    def clear(
        self,
        project_root: Path,
        session_id: str,
        *,
        agent_context_id: str = "",
        lane_id: str = "",
    ) -> None:
        db = _db_path(project_root)
        if not db.is_file():
            return
        actor_id, lane = self._resolve_owner(project_root, agent_context_id, lane_id)
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            conn.execute(
                "DELETE FROM session_todos "
                "WHERE session_id = ? AND agent_context_id = ? AND lane_id = ?",
                (session_id, actor_id, lane),
            )
            conn.commit()


class ActorTaskStateStore:
    """Durable worker task state keyed by session + canonical actor + lane."""

    def get(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str,
        lane_id: str = "",
    ) -> dict[str, Any] | None:
        db = _db_path(project_root)
        if not db.is_file() or not agent_context_id:
            return None
        with _canonical_connect(db) as conn:
            conn.row_factory = sqlite3.Row
            _init(conn)
            row = conn.execute(
                "SELECT task_id, state_json, status, updated_at "
                "FROM actor_task_state WHERE session_id = ? "
                "AND agent_context_id = ? AND lane_id = ?",
                (session_id, agent_context_id, lane_id),
            ).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"] or "{}")
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        return {
            **state,
            "task_id": str(row["task_id"] or ""),
            "status": str(row["status"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "session_id": session_id,
            "agent_context_id": agent_context_id,
            "lane_id": lane_id,
        }

    def active_row_for_actor(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str,
    ) -> dict[str, Any] | None:
        """The actor's ACTIVE task row on this session, whatever lane (#599).

        The row key is (session, actor, LANE), but ownership of a task is
        per ACTOR — the lane is attribution. That distinction is not
        academic: whether a caller resolves as a lane worker depends on a
        per-PROCESS latch and a principal lookup, so the SAME agent can
        present lane "L" on one request and "" on the next. Keyed reads
        then miss a row that plainly exists, the caller looks task-less,
        and it either gets refused or reaches for the shared session slot
        — both of which are the #599 failure wearing a different hat.

        Returns the most recently updated active row (lane included), or
        None. Never guesses across actors: the actor id is exact.
        """
        db = _db_path(project_root)
        actor = str(agent_context_id or "").strip()
        if not db.is_file() or not actor:
            return None
        with _canonical_connect(db) as conn:
            conn.row_factory = sqlite3.Row
            _init(conn)
            row = conn.execute(
                "SELECT lane_id FROM actor_task_state "
                "WHERE session_id = ? AND agent_context_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id, actor),
            ).fetchone()
        if row is None:
            return None
        return self.get(project_root, session_id, actor, str(row["lane_id"] or ""))

    def active_owner_of_task(
        self,
        project_root: Path,
        session_id: str,
        task_id: str,
    ) -> str:
        """The actor whose slot ACTIVELY holds ``task_id``, or "" if none.

        Ownership is the thing that makes a task un-stealable (#599). A
        task_complete may close a task it OWNS, and a task nobody owns
        (the legacy actor-less contract), but never a task standing in
        another actor's active slot. Returns "" both for "no such row"
        and "the holder already closed it" — in either case there is no
        live owner to protect, so the caller may proceed.

        Deliberately returns the FIRST active holder rather than a list:
        the slot key is (session, actor, lane) and one task_id is minted
        per begin, so a second holder would itself be the bug.
        """
        db = _db_path(project_root)
        tid = str(task_id or "").strip()
        if not db.is_file() or not tid:
            return ""
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT agent_context_id FROM actor_task_state "
                "WHERE session_id = ? AND task_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id, tid),
            ).fetchone()
        if row is None:
            return ""
        return str(row[0] or "")

    def set(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str,
        lane_id: str,
        *,
        task_id: str,
        state: dict[str, Any],
        status: str = "active",
    ) -> dict[str, Any]:
        if not agent_context_id:
            raise ValueError("actor task state requires canonical agent_context_id")
        db = _db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO actor_task_state "
                "(session_id, agent_context_id, lane_id, task_id, state_json, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    agent_context_id,
                    lane_id,
                    task_id,
                    json.dumps(state, default=str, sort_keys=True),
                    status,
                    now,
                ),
            )
            conn.commit()
        return self.get(project_root, session_id, agent_context_id, lane_id) or {}

    def merge_state(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str,
        lane_id: str = "",
        *,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read-merge-write ONE actor slot under a SINGLE write lock.

        LOST UPDATES, same shape as the failure ledger's (measured 2026-08-29,
        operator ruling "every agent has its own ledger, no cross-corruption").
        `ai_task(mode='update')` on the worker path was `get()` on one connection
        -- opened, read, CLOSED -- then a merge in Python, then `set()` on a
        SECOND connection issuing `INSERT OR REPLACE`, which rewrites the ENTIRE
        `state_json` blob from the caller's stale copy. Two concurrent updates to
        one slot therefore lost one WHOLESALE: goal, blockers, upcoming,
        relevant_files, everything the loser had recorded, with no error and no
        trace.

        That is reachable today, not theoretical: `stable_actor_id` hashes
        (project, host_kind, host_session_id) and NOTHING else (#650 A1, open),
        so N subagents of one conversation share ONE slot by construction --
        exactly the row this method serialises.

        BEGIN IMMEDIATE takes the write lock BEFORE the read, so the merge is
        always derived from what is actually stored. `UPDATE ... WHERE` rather
        than `INSERT OR REPLACE`: this modifies a row it READ, and a row that is
        gone must stay gone rather than be resurrected from a stale snapshot.

        Returns the refreshed slot, or None when there is no open task to update
        -- the caller's "no_open_task" refusal, decided under the same lock that
        would have granted it.
        """
        actor = str(agent_context_id or "").strip()
        db = _db_path(project_root)
        if not actor or not db.is_file():
            return None
        now = datetime.now(UTC).isoformat()
        with _canonical_connect(db) as conn:
            conn.row_factory = sqlite3.Row
            _init(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT task_id, state_json FROM actor_task_state "
                    "WHERE session_id = ? AND agent_context_id = ? AND lane_id = ?",
                    (session_id, actor, lane_id),
                ).fetchone()
                task_id = str(row["task_id"] or "") if row is not None else ""
                if not task_id:
                    conn.rollback()
                    return None
                try:
                    state = json.loads(row["state_json"] or "{}")
                except Exception:
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                state.update(updates)
                conn.execute(
                    "UPDATE actor_task_state SET state_json = ?, status = ?, "
                    "updated_at = ? WHERE session_id = ? AND agent_context_id = ? "
                    "AND lane_id = ?",
                    (
                        json.dumps(state, default=str, sort_keys=True),
                        "active",
                        now,
                        session_id,
                        actor,
                        lane_id,
                    ),
                )
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        return self.get(project_root, session_id, actor, lane_id)

    def clear(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str,
        lane_id: str = "",
    ) -> None:
        db = _db_path(project_root)
        if not db.is_file() or not agent_context_id:
            return
        with _canonical_connect(db, row_factory=False) as conn:
            _init(conn)
            conn.execute(
                "DELETE FROM actor_task_state WHERE session_id = ? "
                "AND agent_context_id = ? AND lane_id = ?",
                (session_id, agent_context_id, lane_id),
            )
            conn.commit()


def diff_todos(
    prev: list[dict[str, Any]],
    curr: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two TodoWrite payloads and return transition signals.

    Returns:
        {
            "first_submission": bool,  # no prior state
            "all_completed": bool,     # every item now completed
            "completed_now": list,     # items that went ->completed this turn
            "started_now": list,       # items that went pending->in_progress
            "first_in_progress": dict | None,  # current in_progress item
            "pending_count": int,
            "in_progress_count": int,
            "completed_count": int,
            "total": int,
        }
    Keys on todo items: 'content' (description), 'status', 'activeForm'.

    """

    def _status(t: dict[str, Any]) -> str:
        return str(t.get("status", "")).lower()

    def _key(t: dict[str, Any]) -> str:
        return str(t.get("content", ""))

    prev_by_key = {_key(t): _status(t) for t in prev}

    completed_now: list[dict[str, Any]] = []
    started_now: list[dict[str, Any]] = []
    for t in curr:
        k = _key(t)
        s = _status(t)
        prev_s = prev_by_key.get(k)
        if s == "completed" and prev_s != "completed":
            completed_now.append(t)
        elif s == "in_progress" and prev_s != "in_progress":
            started_now.append(t)

    first_in_progress = next(
        (t for t in curr if _status(t) == "in_progress"),
        None,
    )
    statuses = [_status(t) for t in curr]
    return {
        "first_submission": len(prev) == 0 and len(curr) > 0,
        "all_completed": bool(curr) and all(s == "completed" for s in statuses),
        "completed_now": completed_now,
        "started_now": started_now,
        "first_in_progress": first_in_progress,
        "pending_count": statuses.count("pending"),
        "in_progress_count": statuses.count("in_progress"),
        "completed_count": statuses.count("completed"),
        "total": len(curr),
    }
