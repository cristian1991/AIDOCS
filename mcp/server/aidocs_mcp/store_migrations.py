"""Crash-atomic store migration helpers (#242 postmortem, 2026-07-04).

THE INCIDENT: a table-rebuild migration (CREATE __new → INSERT SELECT →
DROP old → RENAME) executed its DDL via ``conn.executescript``, which
COMMITS any open transaction and then runs statements in autocommit. An
MCP-server kill between DROP and RENAME left the store's table gone and
every row stranded in the ``__new`` table — and the next ``init_db``
CREATE IF NOT EXISTS quietly built an EMPTY table over the evidence.

Two rules, enforced by these helpers:

* ``atomic_rebuild`` — every multi-statement rebuild runs as explicit
  ``execute()`` calls inside ONE ``BEGIN IMMEDIATE`` … ``COMMIT``. A killed
  process rolls back to the pre-migration state; there is no window where
  the table does not exist.
* ``recover_interrupted_rename`` — init_db calls this BEFORE its
  CREATE IF NOT EXISTS: if a stranded ``<table>__new`` (rows present) sits
  next to a missing-or-empty ``<table>``, the interrupted rename is
  COMPLETED instead of masked.

Views: SQLite refuses DDL on a table referenced by a broken view (the
canonical_rows view blocked the incident's manual repair). Both helpers
accept ``guard_views`` — views dropped before the work and recreated from
their saved SQL inside the SAME transaction.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator, Sequence

__all__ = [
    "atomic_rebuild",
    "atomic_migration",
    "recover_interrupted_rename",
    "table_exists",
    "table_count",
]


@contextlib.contextmanager
def atomic_migration(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a Python-interleaved migration all-or-nothing (#243).

    ``atomic_rebuild`` takes a static SQL statement list; some migrations
    (session_freeze, rbac) must READ legacy rows, DERIVE new values in Python,
    then INSERT — the compute is interleaved with the DDL/DML. This context
    manager wraps that whole read→compute→write sequence in ONE explicit
    transaction: it commits any caller-opened transaction first (so the
    caller's earlier schema-ensure survives a migration rollback), runs the
    body under ``BEGIN IMMEDIATE``, and COMMITs on success / ROLLBACKs on any
    exception. A process kill mid-body leaves the journal to roll back to the
    intact pre-migration state — there is never a window where the source rows
    are gone but the destination is not yet populated (the freeze-store
    fail-open: an empty new table committed while rows strand in ``_legacy``).
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def table_count(conn: sqlite3.Connection, name: str) -> int:
    if not table_exists(conn, name):
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])


def _view_sql(conn: sqlite3.Connection, view: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (view,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def atomic_rebuild(
    conn: sqlite3.Connection,
    statements: Sequence[str],
    *,
    guard_views: Sequence[str] = (),
) -> None:
    """Run a multi-statement migration all-or-nothing.

    Commits any caller-opened transaction first (deliberately — the caller's
    pre-migration schema-ensure must survive a migration rollback), then runs
    every statement inside ONE explicit transaction. On any error the whole
    rebuild rolls back and the exception propagates; on a process kill SQLite
    journal recovery rolls back — either way the table never half-exists.
    """
    if conn.in_transaction:
        conn.commit()
    view_sqls: list[str] = []
    for view in guard_views:
        sql = _view_sql(conn, view)
        if sql:
            view_sqls.append(sql)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for view, _sql in zip(guard_views, view_sqls, strict=False):
            conn.execute(f'DROP VIEW IF EXISTS "{view}"')
        for stmt in statements:
            conn.execute(stmt)
        for sql in view_sqls:
            conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def recover_interrupted_rename(
    conn: sqlite3.Connection,
    table: str,
    *,
    suffix: str = "__new",
    finish_statements: Sequence[str] = (),
    guard_views: Sequence[str] = (),
) -> bool:
    """Heal a rebuild that died between DROP and RENAME.

    Trigger: ``<table><suffix>`` exists WITH rows while ``<table>`` is missing
    or empty (an empty ``<table>`` means a later CREATE IF NOT EXISTS already
    masked the interruption — the incident's exact shape). Completes the
    rename (dropping the empty masking table first) plus any
    ``finish_statements`` (index recreation), atomically. Returns True when a
    recovery ran.

    Never triggers when ``<table>`` has rows: a populated table plus a
    populated ``__new`` is an ambiguous state a human must resolve — healing
    would have to pick which data survives.
    """
    stranded = f"{table}{suffix}"
    if not table_exists(conn, stranded) or table_count(conn, stranded) == 0:
        return False
    if table_exists(conn, table) and table_count(conn, table) > 0:
        return False  # ambiguous — do not guess
    statements: list[str] = []
    if table_exists(conn, table):
        statements.append(f'DROP TABLE "{table}"')
    statements.append(f'ALTER TABLE "{stranded}" RENAME TO "{table}"')
    statements.extend(finish_statements)
    atomic_rebuild(conn, statements, guard_views=guard_views)
    return True
