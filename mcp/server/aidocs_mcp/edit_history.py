"""Edit history — tracks file modifications for rollback capability.

Every edit_lines, batch_edit, str_replace, and create_file operation records
a snapshot of the file before and after modification. This enables:

1. Viewing what changed in each edit (diff)
2. Rolling back to any previous state
3. Audit trail of all file modifications

Storage: SQLite table in .MEMORY/.index/aidocs.sqlite3
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# #755: canonical connect — WAL, synchronous=NORMAL, busy_timeout,
# foreign_keys=ON. row_factory stays ROW (the helper's default): this store
# already read by name, so the hand-set line it replaces is redundant.
from ._sqlite_connect import connect as _canonical_connect

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS edit_history (
    edit_id TEXT PRIMARY KEY,
    session_id TEXT,
    file_path TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    agent_id TEXT,
    lane_id TEXT,
    start_line INTEGER,
    end_line INTEGER,
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rolled_back INTEGER NOT NULL DEFAULT 0
)
"""

_ENSURE_AGENT_COLUMNS = """
ALTER TABLE edit_history ADD COLUMN agent_id TEXT;
"""
_ENSURE_LANE_COLUMNS = """
ALTER TABLE edit_history ADD COLUMN lane_id TEXT;
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_edit_history_file ON edit_history (file_path, created_at)
"""


@dataclass(slots=True)
class EditRecord:
    edit_id: str
    session_id: str | None
    file_path: str
    tool_name: str
    start_line: int | None
    end_line: int | None
    old_content: str
    new_content: str
    created_at: str
    rolled_back: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "edit_id": self.edit_id,
            "session_id": self.session_id,
            "file_path": self.file_path,
            "tool_name": self.tool_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "old_content_length": len(self.old_content),
            "new_content_length": len(self.new_content),
            "created_at": self.created_at,
            "rolled_back": self.rolled_back,
        }


@dataclass(slots=True)
class RollbackResult:
    success: bool
    edit_id: str
    file_path: str
    message: str
    restored_content_length: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "edit_id": self.edit_id,
            "file_path": self.file_path,
            "message": self.message,
            "restored_content_length": self.restored_content_length,
        }


class EditHistoryStore:
    """SQLite-backed edit history for rollback support."""

    def _db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        path = self._db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(str(path))
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        # Migrate: add agent_id/lane_id columns if missing
        columns = {row[1] for row in conn.execute("PRAGMA table_info(edit_history)").fetchall()}
        if "agent_id" not in columns:
            try:
                conn.execute(_ENSURE_AGENT_COLUMNS)
            except sqlite3.OperationalError:
                pass
        if "lane_id" not in columns:
            try:
                conn.execute(_ENSURE_LANE_COLUMNS)
            except sqlite3.OperationalError:
                pass
        return conn

    def record_edit(
        self,
        project_root: Path,
        file_path: str,
        tool_name: str,
        old_content: str,
        new_content: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        lane_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Record a file edit for potential rollback. Returns edit_id."""
        edit_id = f"edit-{uuid4()}"
        now = datetime.now(UTC).isoformat()
        conn = self._connect(project_root)
        try:
            conn.execute(
                """INSERT INTO edit_history
                   (edit_id, session_id, file_path, tool_name, agent_id, lane_id,
                    start_line, end_line, old_content, new_content, created_at, rolled_back)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    edit_id,
                    session_id,
                    file_path,
                    tool_name,
                    agent_id,
                    lane_id,
                    start_line,
                    end_line,
                    old_content,
                    new_content,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return edit_id

    def list_edits(
        self,
        project_root: Path,
        *,
        file_path: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_rolled_back: bool = False,
    ) -> list[EditRecord]:
        """List recent edits, optionally filtered by file or session."""
        conn = self._connect(project_root)
        try:
            conditions = []
            params: list[object] = []
            if file_path:
                conditions.append("file_path = ?")
                params.append(file_path)
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if not include_rolled_back:
                conditions.append("rolled_back = 0")

            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = conn.execute(
                f"SELECT * FROM edit_history{where} ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()

            return [
                EditRecord(
                    edit_id=row["edit_id"],
                    session_id=row["session_id"],
                    file_path=row["file_path"],
                    tool_name=row["tool_name"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    old_content=row["old_content"],
                    new_content=row["new_content"],
                    created_at=row["created_at"],
                    rolled_back=bool(row["rolled_back"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def grep_edits(
        self,
        project_root: Path,
        *,
        pattern: str,
        in_old: bool = True,
        in_new: bool = True,
        file_glob: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[EditRecord]:
        """Search edit history for a pattern in old/new content.

        Bonus 2026-04-19. Forensics gap: when a line breaks, "what
        changed this line last?" is a manual journal trawl. The audit
        trail already has the data — this just exposes a SQL LIKE
        scan over old_content/new_content. ``file_glob`` is a SQL LIKE
        pattern (use % for wildcard).
        """
        conn = self._connect(project_root)
        try:
            conditions: list[str] = []
            params: list[object] = []
            content_clauses: list[str] = []
            like_pattern = f"%{pattern}%"
            if in_old:
                content_clauses.append("old_content LIKE ?")
                params.append(like_pattern)
            if in_new:
                content_clauses.append("new_content LIKE ?")
                params.append(like_pattern)
            if not content_clauses:
                return []
            conditions.append("(" + " OR ".join(content_clauses) + ")")
            if file_glob:
                conditions.append("file_path LIKE ?")
                params.append(file_glob)
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            where = " WHERE " + " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT * FROM edit_history{where} ORDER BY created_at DESC LIMIT ?",
                params + [int(limit)],
            ).fetchall()
            return [
                EditRecord(
                    edit_id=row["edit_id"],
                    session_id=row["session_id"],
                    file_path=row["file_path"],
                    tool_name=row["tool_name"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    old_content=row["old_content"],
                    new_content=row["new_content"],
                    created_at=row["created_at"],
                    rolled_back=bool(row["rolled_back"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_edit(self, project_root: Path, edit_id: str) -> EditRecord | None:
        """Get a specific edit by ID."""
        conn = self._connect(project_root)
        try:
            row = conn.execute(
                "SELECT * FROM edit_history WHERE edit_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                return None
            return EditRecord(
                edit_id=row["edit_id"],
                session_id=row["session_id"],
                file_path=row["file_path"],
                tool_name=row["tool_name"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                old_content=row["old_content"],
                new_content=row["new_content"],
                created_at=row["created_at"],
                rolled_back=bool(row["rolled_back"]),
            )
        finally:
            conn.close()

    def rollback(
        self,
        project_root: Path,
        edit_id: str,
    ) -> RollbackResult:
        """Rollback a specific edit — restore the file to its state before the edit.

        Only works for the most recent non-rolled-back edit on that file.
        If there are newer edits on the same file, rollback is rejected.
        """
        record = self.get_edit(project_root, edit_id)
        if record is None:
            return RollbackResult(
                success=False,
                edit_id=edit_id,
                file_path="",
                message=f"Edit {edit_id} not found.",
            )
        if record.rolled_back:
            return RollbackResult(
                success=False,
                edit_id=edit_id,
                file_path=record.file_path,
                message=f"Edit {edit_id} was already rolled back.",
            )

        # #132: a rollback is a NEW write judged under CURRENT policy — historical
        # permission (the original edit) is evidence, NOT authority ("a slipper in
        # the Queen's door with a timestamp on it"). Route the restore target
        # through the SAME canonical write gate as any fresh write, so a path that
        # is now protected (gate-config without elevation, hard-protected data, or
        # AIDOCS self-edit after dev-flavour is gone) is refused even though it was
        # once editable. Local import avoids the file_ops↔edit_history cycle.
        self._audit_rollback(project_root, "edit_rollback_attempted", record.file_path)
        try:
            from .file_ops import _resolve_path

            abs_path = _resolve_path(project_root, record.file_path, write=True)
        except ValueError as _auth_err:
            self._audit_rollback(
                project_root,
                "edit_rollback_refused",
                record.file_path,
                reason=str(_auth_err),
            )
            return RollbackResult(
                success=False,
                edit_id=edit_id,
                file_path=record.file_path,
                message=f"Rollback refused by current write policy: {_auth_err}",
            )

        # Check if there are newer edits on the same file
        conn = self._connect(project_root)
        try:
            newer = conn.execute(
                """SELECT COUNT(*) as cnt FROM edit_history
                   WHERE file_path = ? AND created_at > ? AND rolled_back = 0""",
                (record.file_path, record.created_at),
            ).fetchone()
            if newer and newer["cnt"] > 0:
                return RollbackResult(
                    success=False,
                    edit_id=edit_id,
                    file_path=record.file_path,
                    message=f"Cannot rollback: {newer['cnt']} newer edit(s) on {record.file_path}. Rollback the newest first.",
                )
        finally:
            conn.close()

        # Apply rollback based on tool type (abs_path already authorized above)
        if not abs_path.is_file():
            return RollbackResult(
                success=False,
                edit_id=edit_id,
                file_path=record.file_path,
                message=f"File {record.file_path} no longer exists.",
            )

        content = abs_path.read_text(encoding="utf-8")

        if record.tool_name == "str_replace":
            # Reverse str_replace: find new_content in file, replace with old_content
            if record.new_content not in content:
                return RollbackResult(
                    success=False,
                    edit_id=edit_id,
                    file_path=record.file_path,
                    message=f"Cannot rollback: replacement text no longer found in {record.file_path}. File may have been modified since.",
                )
            restored = content.replace(record.new_content, record.old_content, 1)
        elif record.tool_name == "edit_lines" and record.start_line is not None:
            # Reverse edit_lines: find new_content lines at the position, replace with old_content
            lines = content.splitlines()
            new_lines = record.new_content.split("\n") if record.new_content.strip() else []
            old_lines = record.old_content.split("\n") if record.old_content.strip() else []
            start_idx = record.start_line - 1
            end_idx = start_idx + len(new_lines)
            if end_idx > len(lines):
                return RollbackResult(
                    success=False,
                    edit_id=edit_id,
                    file_path=record.file_path,
                    message="Cannot rollback: file has fewer lines than expected. File may have been modified since.",
                )
            actual = lines[start_idx:end_idx]
            if actual != new_lines:
                return RollbackResult(
                    success=False,
                    edit_id=edit_id,
                    file_path=record.file_path,
                    message=f"Cannot rollback: content at lines {record.start_line}-{end_idx} has changed since the edit.",
                )
            restored_lines = lines[:start_idx] + old_lines + lines[end_idx:]
            restored = "\n".join(restored_lines)
            if content.endswith("\n"):
                restored += "\n"
        else:
            return RollbackResult(
                success=False,
                edit_id=edit_id,
                file_path=record.file_path,
                message=f"Cannot rollback: unsupported tool type '{record.tool_name}'.",
            )

        abs_path.write_text(restored, encoding="utf-8")

        # Mark as rolled back
        conn = self._connect(project_root)
        try:
            conn.execute(
                "UPDATE edit_history SET rolled_back = 1 WHERE edit_id = ?",
                (edit_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self._audit_rollback(project_root, "edit_rollback_applied", record.file_path)
        return RollbackResult(
            success=True,
            edit_id=edit_id,
            file_path=record.file_path,
            message=f"Rolled back {record.file_path} to state before {record.tool_name}.",
            restored_content_length=len(record.old_content),
        )

    @staticmethod
    def _audit_rollback(
        project_root: Path, event_kind: str, file_path: str, *, reason: str = ""
    ) -> None:
        """Best-effort audit trail for a rollback (#132): attempted / refused /
        applied. Never raises — the authorization decision itself is enforced by
        the caller regardless of whether the audit row lands."""
        try:
            from .execution_index_store import ExecutionIndexStore

            payload: dict = {"file_path": file_path}
            if reason:
                payload["reason"] = reason[:300]
            ExecutionIndexStore().record_event(
                project_root,
                event_kind=event_kind,
                source_kind="edit_history",
                capability_name="rollback",
                action_kind="rollback",
                target_entity=file_path,
                status=event_kind.rsplit("_", 1)[-1],
                payload=payload,
            )
        except Exception:
            pass

    def prune(
        self,
        project_root: Path,
        *,
        keep_days: int = 7,
    ) -> int:
        """Delete edit history older than keep_days. Returns count deleted."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        conn = self._connect(project_root)
        try:
            cursor = conn.execute(
                "DELETE FROM edit_history WHERE created_at < ? AND rolled_back = 1",
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def files_touched_summary(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Summary of files modified: path, edit count, agents, last edit time."""
        conn = self._connect(project_root)
        try:
            where = "WHERE rolled_back = 0"
            params: list[object] = []
            if session_id:
                where += " AND session_id = ?"
                params.append(session_id)
            rows = conn.execute(
                f"""
                SELECT file_path,
                       COUNT(*) as edit_count,
                       MAX(created_at) as last_edit,
                       GROUP_CONCAT(DISTINCT agent_id) as agents,
                       GROUP_CONCAT(DISTINCT lane_id) as lanes,
                       SUM(LENGTH(new_content) - LENGTH(old_content)) as net_chars
                FROM edit_history
                {where}
                GROUP BY file_path
                ORDER BY MAX(created_at) DESC
            """,
                params,
            ).fetchall()
            return [
                {
                    "file": row["file_path"],
                    "edits": row["edit_count"],
                    "last_edit": row["last_edit"],
                    "agents": [a for a in (row["agents"] or "").split(",") if a],
                    "lanes": [l for l in (row["lanes"] or "").split(",") if l],
                    "net_chars": row["net_chars"] or 0,
                }
                for row in rows
            ]
        finally:
            conn.close()
