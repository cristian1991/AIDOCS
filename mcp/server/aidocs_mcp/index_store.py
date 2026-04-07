from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .session_store import SessionStore


class IndexStore:
    """Derived SQLite index for memory and session metadata."""

    def __init__(self, session_store: SessionStore) -> None:
        self.session_store = session_store

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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    title TEXT,
                    status TEXT,
                    owner TEXT,
                    goal TEXT,
                    last_updated TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_files (
                    path TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    title TEXT,
                    content_text TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_links (
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    PRIMARY KEY (source_path, target_path)
                );
                """
            )
            self._ensure_column(conn, "memory_files", "title", "TEXT")
            self._ensure_column(conn, "memory_files", "content_text", "TEXT NOT NULL DEFAULT ''")

    def status(self, project_root: Path) -> dict[str, object]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            memory_count = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]
            link_count = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        return {
            "db_path": str(self.db_path(project_root)),
            "sessions": int(session_count),
            "memory_files": int(memory_count),
            "memory_links": int(link_count),
            "freshness": self._memory_freshness(project_root),
        }

    def sync_sessions(self, project_root: Path) -> int:
        self.init_db(project_root)
        sessions = self.session_store.list_sessions(project_root)
        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM sessions")
            conn.executemany(
                """
                INSERT INTO sessions (session_id, path, title, status, owner, goal, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s.session_id,
                        str(s.path),
                        s.title,
                        s.status,
                        s.owner,
                        s.goal,
                        s.last_updated,
                    )
                    for s in sessions
                ],
            )
        return len(sessions)

    def sync_memory_files(self, project_root: Path) -> int:
        self.init_db(project_root)
        memory_root = project_root / ".MEMORY"
        file_rows: list[tuple[str, str, str, str | None, str]] = []
        link_rows: list[tuple[str, str]] = []

        for path in sorted(memory_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".aidocs"}:
                continue
            rel = path.relative_to(memory_root).as_posix()
            if rel.startswith("archive/"):
                continue
            kind = self._kind_for(rel)
            text = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            title = self._extract_title(text)
            file_rows.append((rel, kind, checksum, title, text))
            link_rows.extend((rel, target) for target in self._extract_internal_links(path, memory_root, text))

        unique_link_rows = list(dict.fromkeys(link_rows))

        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM memory_files")
            conn.execute("DELETE FROM memory_links")
            conn.executemany(
                "INSERT INTO memory_files (path, kind, checksum, title, content_text) VALUES (?, ?, ?, ?, ?)",
                file_rows,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO memory_links (source_path, target_path) VALUES (?, ?)",
                unique_link_rows,
            )
        return len(file_rows)

    def sync_all(self, project_root: Path) -> dict[str, int]:
        return {
            "sessions": self.sync_sessions(project_root),
            "memory_files": self.sync_memory_files(project_root),
        }

    def search_memory(self, project_root: Path, query: str, limit: int = 10) -> list[dict[str, str]]:
        self.init_db(project_root)
        needle = query.strip()
        if not needle:
            return []
        pattern = f"%{needle}%"
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, kind, title, content_text
                FROM memory_files
                WHERE path LIKE ? OR COALESCE(title, '') LIKE ? OR content_text LIKE ?
                ORDER BY CASE
                    WHEN COALESCE(title, '') LIKE ? THEN 0
                    WHEN path LIKE ? THEN 1
                    ELSE 2
                END, path ASC
                LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "kind": row["kind"],
                "title": row["title"] or "",
                "snippet": self._make_snippet(row["content_text"], needle),
            }
            for row in rows
        ]

    def _kind_for(self, relative_path: str) -> str:
        if relative_path.startswith(".aidocs/"):
            return "aidocs"
        if relative_path.startswith("sessions/"):
            return "session"
        if relative_path.startswith("rules/"):
            return "rule"
        if relative_path.startswith("system/"):
            return "system"
        if relative_path.startswith("domains/"):
            return "domain"
        if relative_path.startswith("roadmaps/"):
            return "roadmap"
        if relative_path.startswith("specs/"):
            return "spec"
        if relative_path.startswith("config/"):
            return "config"
        if relative_path.startswith("related-projects/"):
            return "related_project"
        if relative_path.startswith("daily/"):
            return "daily"
        return "memory"

    def _extract_internal_links(self, path: Path, memory_root: Path, text: str) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()

        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
            link = match.group(1).strip().replace("\\", "/")
            link = link.split("#", 1)[0].split("?", 1)[0].strip()
            if not link or link.startswith("http"):
                continue
            resolved = self._resolve_link(path, memory_root, link)
            if resolved is not None and resolved not in seen:
                seen.add(resolved)
                links.append(resolved)
        return links

    def _resolve_link(self, source_path: Path, memory_root: Path, link: str) -> str | None:
        if link.startswith("/.MEMORY/"):
            candidate = memory_root / link.removeprefix("/.MEMORY/")
        else:
            candidate = (source_path.parent / link).resolve()
        try:
            resolved = candidate.resolve()
        except FileNotFoundError:
            return None
        try:
            return resolved.relative_to(memory_root).as_posix()
        except ValueError:
            return None

    def _extract_title(self, text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return None

    def _make_snippet(self, text: str, needle: str, width: int = 120) -> str:
        lower = text.lower()
        index = lower.find(needle.lower())
        if index < 0:
            return text[:width].replace("\n", " ").strip()
        start = max(0, index - width // 3)
        end = min(len(text), index + width)
        return text[start:end].replace("\n", " ").strip()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _memory_freshness(self, project_root: Path) -> dict[str, object]:
        memory_root = project_root / ".MEMORY"
        tracked_files: dict[str, tuple[str, int]] = {}
        if memory_root.is_dir():
            for path in sorted(memory_root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in {".md", ".aidocs"}:
                    continue
                rel = path.relative_to(memory_root).as_posix()
                if rel.startswith("archive/"):
                    continue
                tracked_files[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(), int(path.stat().st_mtime_ns))

        indexed_rows: dict[str, sqlite3.Row] = {}
        with self.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum FROM memory_files ORDER BY path"):
                indexed_rows[str(row["path"])] = row

        indexed_path_set = set(indexed_rows)
        tracked_path_set = set(tracked_files)
        missing_paths = sorted(tracked_path_set - indexed_path_set)
        extra_paths = sorted(indexed_path_set - tracked_path_set)
        drifted_paths = sorted(
            path
            for path in tracked_path_set & indexed_path_set
            if str(indexed_rows[path]["checksum"] or "") != tracked_files[path][0]
        )
        reasons: list[str] = []
        if missing_paths or extra_paths:
            reasons.append("path_drift")
        if drifted_paths:
            reasons.append("content_drift")
        if tracked_files and not indexed_rows:
            reasons.append("missing_index_state")
            state = "missing"
        elif reasons:
            state = "stale"
        else:
            state = "ready"
        latest_source_mtime_ns = max((item[1] for item in tracked_files.values()), default=None)
        return {
            "state": state,
            "reasons": reasons,
            "tracked_paths": len(tracked_files),
            "indexed_paths": len(indexed_rows),
            "drifted_paths": sorted(dict.fromkeys(missing_paths + drifted_paths + extra_paths)),
            "missing_paths": missing_paths,
            "extra_paths": extra_paths,
            "latest_source_mtime_ns": latest_source_mtime_ns,
        }
