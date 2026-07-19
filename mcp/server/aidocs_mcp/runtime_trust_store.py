"""Canonical runtime-trust authority — home-scoped SQLite.

The trusted-runtime decision (interpreter + package fingerprint, provenance,
remote-trust) used to live ONLY in ``~/.aidocs/runtime/runtime.json``, a loose
file an attacker (or a stray editor) can rewrite. This store is the SOURCE OF
AUTHORITY; ``runtime.json`` is demoted to a projection/cache (see
``package_integrity``). It is append-only — every ``record`` inserts an
immutable row, so the table is itself the audit trail of trust changes; the
latest row is the current trust.

Home-scoped (unlike the project-scoped index stores): the runtime is the
operator's machine-wide AIDOCS interpreter, so its trust lives next to the
runtime under ``~/.aidocs/runtime/runtime_trust.db``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .runtime_provisioner import runtime_root

_COLUMNS = (
    "interpreter_fingerprint",
    "package_fingerprint",
    "package_version",
    "provenance",
    "status",
    "remote_trustworthy",
)


class RuntimeTrustStore:
    def __init__(self, home: Path | str | None = None) -> None:
        self._home = Path(home) if home else Path.home()

    def db_path(self) -> Path:
        return runtime_root(self._home) / "runtime_trust.db"

    def _connect(self) -> sqlite3.Connection:
        p = self.db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_trust (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interpreter_fingerprint TEXT,
                    package_fingerprint TEXT,
                    package_version TEXT,
                    provenance TEXT,
                    status TEXT,
                    remote_trustworthy INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL,
                    source TEXT,
                    actor TEXT,
                    note TEXT,
                    payload_json TEXT
                );

                -- Append-only enforced at the SQLITE LAYER: a trust row, once
                -- written, can never be rewritten or removed (the table IS the
                -- audit trail). Tampering must INSERT a new row, which is then
                -- visible in history(). RAISE(ABORT) rolls back the statement.
                CREATE TRIGGER IF NOT EXISTS runtime_trust_no_update
                BEFORE UPDATE ON runtime_trust
                BEGIN
                    SELECT RAISE(ABORT,
                        'runtime_trust is append-only: UPDATE refused');
                END;

                CREATE TRIGGER IF NOT EXISTS runtime_trust_no_delete
                BEFORE DELETE ON runtime_trust
                BEGIN
                    SELECT RAISE(ABORT,
                        'runtime_trust is append-only: DELETE refused');
                END;
                """,
            )

    def record(
        self,
        fields: dict[str, Any],
        *,
        source: str = "",
        actor: str | None = None,
        note: str | None = None,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        """Append an immutable trust row (a trust CHANGE = a new audited row).
        Returns the inserted row as a dict (incl. id + recorded_at).
        """
        self.init_db()
        recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row = {c: fields.get(c) for c in _COLUMNS}
        row["remote_trustworthy"] = 1 if row.get("remote_trustworthy") else 0
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runtime_trust (
                    interpreter_fingerprint, package_fingerprint,
                    package_version, provenance, status, remote_trustworthy,
                    recorded_at, source, actor, note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["interpreter_fingerprint"],
                    row["package_fingerprint"],
                    row["package_version"],
                    row["provenance"],
                    row["status"],
                    row["remote_trustworthy"],
                    recorded_at,
                    source or "",
                    actor or "",
                    note or "",
                    json.dumps(payload or {}, sort_keys=True),
                ),
            )
            conn.commit()
            new_id = cur.lastrowid
        return {
            "id": new_id,
            **{c: row[c] for c in _COLUMNS},
            "remote_trustworthy": bool(row["remote_trustworthy"]),
            "recorded_at": recorded_at,
            "source": source or "",
            "actor": actor or "",
            "note": note or "",
        }

    def current(self) -> dict[str, Any] | None:
        """The latest (authoritative) trust row, or None if never recorded."""
        if not self.db_path().is_file():
            return None
        try:
            with self._connect() as conn:
                r = conn.execute("SELECT * FROM runtime_trust ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
        if r is None:
            return None
        d = dict(r)
        d["remote_trustworthy"] = bool(d.get("remote_trustworthy"))
        return d

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.db_path().is_file():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM runtime_trust ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["remote_trustworthy"] = bool(d.get("remote_trustworthy"))
            out.append(d)
        return out
