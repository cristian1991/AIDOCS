"""Cross-project registry — sqlite-backed.

Replaces the legacy `.MEMORY/config/related-projects.md` markdown
parser (fragile format, no transactions, no concurrent writers) with
a sqlite store. Legacy markdown is one-time-imported on first read so
existing projects don't lose their entries.

Public API (used by mcp_server_runtime_helpers.resolve_related_root,
the related_project_* MCP tools, and agent_spawn_worker_async's
target_project resolver):

    list_related_projects(project_root) → list[dict]
    get_related_project(project_root, name) → dict | None
    resolve_related_project_path(project_root, name) → Path | None
    register(project_root, name, path, notes=None) → dict (new)
    unregister(project_root, name) → dict (new)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RelatedProjectService:
    def __init__(self) -> None:
        self._initialized: set[Path] = set()
        self._imported_legacy: set[Path] = set()

    # ── DB plumbing ──

    def _db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "related_projects.sqlite3"

    def _legacy_md_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "related-projects.md"

    def _init_db(self, project_root: Path) -> None:
        if project_root in self._initialized:
            return
        db = self._db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS related_projects (
                    name           TEXT PRIMARY KEY COLLATE NOCASE,
                    path           TEXT NOT NULL,
                    registered_at  TEXT NOT NULL,
                    reason         TEXT,
                    notes          TEXT
                )
                """,
            )
            # Additive migration — pre-existing DBs need the `reason`
            # column added. sqlite has no IF NOT EXISTS for ADD COLUMN
            # so we probe and swallow the duplicate-column error.
            try:
                conn.execute("ALTER TABLE related_projects ADD COLUMN reason TEXT")
            except sqlite3.OperationalError:
                pass
        self._initialized.add(project_root)
        self._import_legacy_markdown_once(project_root)

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        self._init_db(project_root)
        conn = sqlite3.connect(str(self._db_path(project_root)))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    # ── Legacy markdown import (one-shot) ──

    def _import_legacy_markdown_once(self, project_root: Path) -> None:
        """Migrate `.MEMORY/config/related-projects.md` rows into
        sqlite on first DB init. Idempotent — flagged in-process so
        repeat init calls don't re-parse. Markdown file stays in
        place (don't break anything that still reads it externally),
        but new writes go to sqlite only.
        """
        if project_root in self._imported_legacy:
            return
        self._imported_legacy.add(project_root)
        md = self._legacy_md_path(project_root)
        if not md.is_file():
            return
        entries: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for raw in md.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                if current:
                    entries.append(current)
                current = {"name": line[3:].strip()}
                continue
            if current is None or not line.startswith("-") or ":" not in line:
                continue
            body = line[1:].strip()
            key, value = body.split(":", 1)
            current[key.strip().lower().replace(" ", "_")] = value.strip().strip("`")
        if current:
            entries.append(current)
        if not entries:
            return
        now = self._now_iso()
        with sqlite3.connect(str(self._db_path(project_root))) as conn:
            for entry in entries:
                name = (entry.get("name") or "").strip()
                path = (entry.get("path") or "").strip()
                if not name or not path:
                    continue
                # Legacy markdown used both `reason` and `purpose`
                # (same intent, different label in different vintages).
                # Fold `purpose` into `reason` on import.
                reason = entry.get("reason") or entry.get("purpose") or None
                notes = entry.get("notes") or None
                conn.execute(
                    """
                    INSERT OR IGNORE INTO related_projects
                        (name, path, registered_at, reason, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, path, now, reason, notes),
                )
            conn.commit()

    # ── Read API ──

    def list_related_projects(self, project_root: Path) -> list[dict[str, str]]:
        with self._connect(project_root) as conn:
            rows = conn.execute(
                "SELECT name, path, registered_at, reason, notes "
                "FROM related_projects ORDER BY name",
            ).fetchall()
        return [
            {
                "name": row["name"],
                "path": row["path"],
                "registered_at": row["registered_at"],
                **({"reason": row["reason"]} if row["reason"] else {}),
                **({"notes": row["notes"]} if row["notes"] else {}),
            }
            for row in rows
        ]

    def get_related_project(self, project_root: Path, name: str) -> dict[str, str] | None:
        clean = (name or "").strip()
        if not clean:
            return None
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT name, path, registered_at, reason, notes "
                "FROM related_projects WHERE name = ?",
                (clean,),
            ).fetchone()
        if row is None:
            return None
        result: dict[str, str] = {
            "name": row["name"],
            "path": row["path"],
            "registered_at": row["registered_at"],
        }
        if row["reason"]:
            result["reason"] = row["reason"]
        if row["notes"]:
            result["notes"] = row["notes"]
        return result

    def resolve_related_project_path(self, project_root: Path, name: str) -> Path | None:
        entry = self.get_related_project(project_root, name)
        if not entry:
            return None
        raw = entry.get("path")
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        return candidate if candidate.exists() else None

    # ── Write API ──

    def register(
        self,
        project_root: Path,
        name: str,
        path: str,
        reason: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Register a related project. `reason` is audited intent
        (why these two projects are linked — 'legacy source',
        'shared deployment', 'client-feedback coordination'); `notes`
        is free-form context. Both optional but `reason` is the
        audit-grade field for cross-project work attribution.
        """
        clean = (name or "").strip()
        if not clean:
            return {"ok": False, "error": "name is required"}
        target = Path(path).resolve()
        if not target.is_absolute():
            return {"ok": False, "error": f"path must be absolute: {path}"}
        if not target.is_dir():
            return {"ok": False, "error": f"path is not a directory: {target}"}
        if not (target / ".MEMORY").is_dir():
            return {
                "ok": False,
                "error": (
                    f"path is not AIDOCS-managed (no .MEMORY/): {target}. "
                    f"Run aidocs bootstrap there first."
                ),
            }
        existed = self.get_related_project(project_root, clean) is not None
        with self._connect(project_root) as conn:
            conn.execute(
                """
                INSERT INTO related_projects
                    (name, path, registered_at, reason, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    path = excluded.path,
                    registered_at = excluded.registered_at,
                    reason = COALESCE(excluded.reason, related_projects.reason),
                    notes = COALESCE(excluded.notes, related_projects.notes)
                """,
                (clean, str(target), self._now_iso(), reason, notes),
            )
            conn.commit()
        return {
            "ok": True,
            "name": clean,
            "path": str(target),
            "action": "updated" if existed else "added",
        }

    def unregister(self, project_root: Path, name: str) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            return {"ok": False, "error": "name is required"}
        with self._connect(project_root) as conn:
            cur = conn.execute("DELETE FROM related_projects WHERE name = ?", (clean,))
            conn.commit()
        return {"ok": True, "name": clean, "removed": cur.rowcount > 0}
