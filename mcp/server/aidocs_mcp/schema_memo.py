"""Identity-validated per-process schema-ensure memo (2026-07-09).

Root cause this replaces (VPS Gate 2b 12-failure baseline): several stores
kept a process-level "schema ensured" set keyed by db PATH STRING. That
assumes a path is never recycled with a fresh database inside one process
lifetime — false in two real settings:

  * pytest with ``tmp_path_retention_policy=failed``: a passed test's tmp
    dir is deleted at teardown, and pytest's numbered-dir allocator then
    REUSES the same path (``<prefix>0``) for the next test whose name shares
    the same 30-char truncated prefix. Same path, brand-new empty db, stale
    memo → init_db skipped → ``sqlite3.OperationalError: no such table``.
  * the long-lived HTTP daemon: an operator re-inits/deletes a project's
    ``.MEMORY`` while the daemon is up — the recreated db hits the stale
    memo and every query on it breaks until a daemon restart.

Fix: memo the db file's IDENTITY (st_dev, st_ino), not just its path. A
memo hit requires the CURRENT file at that path to be the SAME file object
that was ensured. A deleted/recreated db has a new inode → miss → the
caller re-runs its idempotent DDL. The validation is one os.stat() — no
sqlite connection is opened, preserving the memo's entire point (skipping
the executescript tax on hot paths).
"""

from __future__ import annotations

import os
from pathlib import Path


def _file_identity(db_path: Path) -> tuple[int, int] | None:
    """(st_dev, st_ino) of the CURRENT file at db_path, or None when absent.

    st_ino is populated on Windows (NTFS file index) since Python 3.5, so
    the identity check is meaningful on every platform we ship to.
    """
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


class SchemaMemo:
    """Per-process memo: 'this exact db file already had its schema ensured'.

    Not thread-safe by design — a racing double-ensure is harmless (the DDL
    is idempotent); a dict write is atomic enough for the worst case to be
    one redundant ensure.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, int]] = {}

    def is_current(self, db_path: Path) -> bool:
        """True only when the memo'd identity matches the file on disk NOW."""
        ident = _file_identity(db_path)
        if ident is None:
            return False
        return self._seen.get(str(db_path)) == ident

    def mark(self, db_path: Path) -> None:
        """Record the current file identity. No-op when the file is absent."""
        ident = _file_identity(db_path)
        if ident is not None:
            self._seen[str(db_path)] = ident

    def clear(self) -> None:
        """Test hook — drop every memo (process-global memos need this)."""
        self._seen.clear()

    # Set-compatible surface — pre-existing tests (test_ups_sqlite_seal,
    # test_empire_audit_ledger) drive the memo through `key in`, `discard`.
    def __contains__(self, key: object) -> bool:
        return self.is_current(Path(str(key)))

    def discard(self, key: object) -> None:
        self._seen.pop(str(key), None)

    def add(self, key: object) -> None:
        self.mark(Path(str(key)))
