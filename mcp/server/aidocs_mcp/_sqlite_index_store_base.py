from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class SQLiteIndexStoreBase:
    def index_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index"

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 120% enforceable: SQLite defaults foreign_keys=OFF per-connection,
        # which makes every FK constraint in the kingdom inert. Turn it ON
        # here so cascade-deletes and reference checks actually fire.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def session(self, project_root: Path) -> Iterator[sqlite3.Connection]:
        """Context manager that commits AND closes the connection on exit.

        Python's sqlite3 ``with conn:`` block commits or rolls back but
        never closes the handle. On Windows the open handle keeps a WAL
        lock that blocks other processes (and pytest's tmp-dir cleanup)
        until Python GCs the connection. Every store that uses this
        method is safe under parallel test runs.
        """
        conn = self.connect(project_root)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _timestamp(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
