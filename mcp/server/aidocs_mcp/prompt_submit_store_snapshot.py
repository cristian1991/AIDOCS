"""Exact scoped SQLite snapshots used by store-owned prompt-submit facades."""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence


Scope = tuple[str, tuple[Any, ...]]


def capture_scoped_rows(
    conn: sqlite3.Connection,
    scopes: Mapping[str, Scope],
) -> dict[str, Any]:
    """Capture exact rows for fixed table scopes; storage errors propagate."""
    conn.row_factory = sqlite3.Row
    tables: dict[str, dict[str, Any]] = {}
    any_rows = False
    for table, (where_sql, params) in scopes.items():
        columns = tuple(
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if not columns:
            raise RuntimeError(f"snapshot table unavailable: {table}")
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {where_sql}",
            params,
        ).fetchall()
        serialized = [tuple(row[column] for column in columns) for row in rows]
        tables[table] = {"columns": columns, "rows": serialized}
        any_rows = any_rows or bool(serialized)
    return {
        "captured": True,
        "existed": any_rows,
        "state": {"tables": tables},
    }


def restore_scoped_rows(
    conn: sqlite3.Connection,
    scopes: Mapping[str, Scope],
    snapshot: Mapping[str, Any],
) -> None:
    """Delete the scoped present state, then restore the exact captured rows."""
    if snapshot.get("captured") is not True:
        raise RuntimeError("refusing restore from an uncaptured snapshot")
    state = snapshot.get("state")
    if not isinstance(state, Mapping):
        raise RuntimeError("snapshot state is malformed")
    tables = state.get("tables")
    if not isinstance(tables, Mapping):
        raise RuntimeError("snapshot tables are malformed")

    with conn:
        for table, (where_sql, params) in scopes.items():
            table_state = tables.get(table)
            if not isinstance(table_state, Mapping):
                raise RuntimeError(f"snapshot missing table: {table}")
            columns = tuple(table_state.get("columns") or ())
            rows: Sequence[Sequence[Any]] = tuple(table_state.get("rows") or ())
            live_columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns or any(column not in live_columns for column in columns):
                raise RuntimeError(f"snapshot columns changed for table: {table}")
            conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
            if rows:
                names = ", ".join(columns)
                placeholders = ", ".join("?" for _ in columns)
                conn.executemany(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    rows,
                )
