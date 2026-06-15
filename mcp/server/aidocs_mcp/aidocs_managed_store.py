from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform PID liveness check.

    Bug 2026-05-13: os.kill(pid, 0) on Windows interacts badly with
    the Python signal handling layer in some pid/handle states — even
    though sig=0 is "no-op probe" on POSIX, on Windows it can leave a
    process handle / IPC primitive in a state that crashes the
    enclosing process's NEXT stdio read. Manifested as MCP-server
    transport death after tests/runtime/test_prune_dead_conductor_
    bindings ran. Root fix: use OpenProcess + GetExitCodeProcess via
    ctypes on Windows (the documented liveness probe), keep
    os.kill(pid, 0) on POSIX where it's a true no-op.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        import os as _os

        try:
            _os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


class AidocsManagedStore(SQLiteIndexStoreBase):
    """Project-local managed-mode state — sqlite-backed replacement for
    ``.MEMORY/config/aidocs-managed.json``.

    Single row per project ``(id=1)``. The row is upserted on every
    ``set()`` call. Ingests the legacy JSON on first ``init_db()`` and
    hard-deletes it so the project never carries two sources of truth.
    """

    _SCHEMA_KEYS: frozenset[str] = frozenset(
        {
            "active",
            "session_id",
            "activated_at",
            "last_updated",
            "source",
        },
    )

    def _legacy_json_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "aidocs-managed.json"

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS aidocs_managed (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    active INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    activated_at TEXT,
                    last_updated TEXT,
                    source TEXT
                );
                """,
            )
            # Additive migration (2026-04-22): stamp the MCP server's
            # per-process boot token on every bind so a restart of the
            # server forces a reconnect even when sqlite says
            # active=true. See managed_mode_service.current_boot_token.
            try:
                conn.execute("ALTER TABLE aidocs_managed ADD COLUMN bound_by_boot_token TEXT")
            except Exception:
                pass
            # Additive migration (2026-04-30): record when the FIRST-
            # EVER bootstrap completed (full _sync_bootstrap_indexes
            # ran). Subsequent managed-mode activations check this
            # column: NULL → first-ever (run full sync), non-NULL →
            # per-launch (read status only). Operator doctrine:
            # "if indexes have never been synced on managed-mode
            # bootstrap = first-ever." A project with zero code
            # files still completes a bootstrap (memory, schema,
            # capabilities, etc.); empty code_files is not the
            # right signal.
            try:
                conn.execute("ALTER TABLE aidocs_managed ADD COLUMN bootstrap_completed_at TEXT")
            except Exception:
                pass
            # #58 (canonical 2026-04-26): per-conductor mapping is the
            # authoritative source for "what session is THIS conductor
            # bound to." Project-keyed singleton above is now a
            # deprecated fallback. See security-gates.md §0.5
            # #50/#54 sub-clause "conductor-bound state keying."
            #
            # PK is cli_session_id alone — sqlite is per-project so
            # project_root is implicit. No cleanup of stale rows in
            # v1; tiny rows, irrelevant accumulation.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS aidocs_managed_per_conductor (
                    cli_session_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    source TEXT,
                    bound_by_boot_token TEXT
                );
                """,
            )
        self._ingest_legacy_json(project_root)

    def _ingest_legacy_json(self, project_root: Path) -> None:
        path = self._legacy_json_path(project_root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        data = {k: v for k, v in raw.items() if k in self._SCHEMA_KEYS}
        with self.session(project_root) as conn:
            existing = conn.execute("SELECT 1 FROM aidocs_managed WHERE id = 1").fetchone()
            if existing is not None:
                path.unlink()
                return
            conn.execute(
                """
                INSERT INTO aidocs_managed
                    (id, active, session_id, activated_at, last_updated, source)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    1 if bool(data.get("active")) else 0,
                    data.get("session_id"),
                    data.get("activated_at"),
                    data.get("last_updated"),
                    data.get("source"),
                ),
            )
        path.unlink()

    def get(self, project_root: Path) -> dict[str, Any]:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT active, session_id, activated_at, last_updated, "
                "source, bound_by_boot_token "
                "FROM aidocs_managed WHERE id = 1",
            ).fetchone()
        if row is None:
            return {
                "active": False,
                "session_id": None,
                "activated_at": None,
                "last_updated": None,
                "source": None,
                "bound_by_boot_token": None,
            }
        keys = row.keys() if hasattr(row, "keys") else []
        return {
            "active": bool(row["active"]),
            "session_id": row["session_id"],
            "activated_at": row["activated_at"],
            "last_updated": row["last_updated"],
            "source": row["source"],
            "bound_by_boot_token": (
                row["bound_by_boot_token"] if "bound_by_boot_token" in keys else None
            ),
        }

    def set(
        self,
        project_root: Path,
        *,
        session_id: str,
        source: str = "/aidocs",
        boot_token: str | None = None,
    ) -> dict[str, Any]:
        now = self._timestamp()
        current = self.get(project_root)
        # activated_at resets when the session identity changes because the
        # dashboard treats a session switch as a new bind window for uptime
        # display. Rebinding the same session refreshes last_updated only.
        session_changed = current.get("session_id") != session_id
        activated_at = (
            now if session_changed or not current.get("activated_at") else current["activated_at"]
        )
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO aidocs_managed
                    (id, active, session_id, activated_at, last_updated,
                     source, bound_by_boot_token)
                VALUES (1, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active = 1,
                    session_id = excluded.session_id,
                    activated_at = excluded.activated_at,
                    last_updated = excluded.last_updated,
                    source = excluded.source,
                    bound_by_boot_token = excluded.bound_by_boot_token
                """,
                (session_id, activated_at, now, source, boot_token),
            )
        return self.get(project_root)

    def clear(self, project_root: Path) -> dict[str, Any]:
        with self.session(project_root) as conn:
            conn.execute("DELETE FROM aidocs_managed WHERE id = 1")
        return self.get(project_root)

    # ── First-ever bootstrap timestamp (2026-04-30) ──
    # Operator doctrine: managed-mode bootstrap on a project where
    # bootstrap_completed_at IS NULL = first-ever bootstrap; run
    # full _sync_bootstrap_indexes. Otherwise the per-launch path
    # is light. The signal is independent of code-file count
    # (a project with zero source files still completes a bootstrap).

    def get_bootstrap_completed_at(
        self,
        project_root: Path,
    ) -> str | None:
        """Return ISO-ish timestamp of last full bootstrap, or None
        if bootstrap has never completed for this project.

        Self-bootstraps the schema via init_db() so a fresh DB
        returns None cleanly (correct: a project that's never been
        bootstrapped has no stamp). Without this, the read would
        raise OperationalError on a missing table and the broad
        except mask would lose the legitimate-NULL signal.
        """
        self.init_db(project_root)
        try:
            with self.session(project_root) as conn:
                row = conn.execute(
                    "SELECT bootstrap_completed_at FROM aidocs_managed WHERE id = 1",
                ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            value = row["bootstrap_completed_at"]
        except (KeyError, IndexError):
            return None
        return str(value) if value else None

    def stamp_bootstrap_completed(
        self,
        project_root: Path,
        *,
        when: str,
    ) -> None:
        """Stamp bootstrap_completed_at on the singleton row.
        Idempotent — overwrite is fine; the column is informational.
        Inserts a row if no managed-mode bind exists yet (first-ever
        bootstrap can run before any session is bound).

        Self-bootstraps the schema via init_db() so direct callers
        (without going through ManagedModeService) don't crash on a
        fresh DB. The init is idempotent — running it twice is a
        no-op when the table already exists.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT INTO aidocs_managed "
                "(id, active, bootstrap_completed_at) "
                "VALUES (1, 0, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "bootstrap_completed_at = excluded.bootstrap_completed_at",
                (when,),
            )

    # ── Per-conductor mapping (#58, canonical 2026-04-26) ──
    # Authoritative source for conductor-bound state. The singleton
    # methods above (get/set/clear) remain as deprecated fallback for
    # callers that don't yet plumb cli_session_id. New code MUST use
    # the per-conductor methods.

    def get_per_conductor(
        self,
        project_root: Path,
        *,
        cli_session_id: str,
    ) -> dict[str, Any] | None:
        """Return the mapping row for `cli_session_id` or None when
        no binding exists for this conductor on this project.
        """
        if not cli_session_id:
            return None
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT cli_session_id, session_id, activated_at, "
                "last_updated, source, bound_by_boot_token "
                "FROM aidocs_managed_per_conductor "
                "WHERE cli_session_id = ?",
                (cli_session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "active": True,
            "cli_session_id": row["cli_session_id"],
            "session_id": row["session_id"],
            "activated_at": row["activated_at"],
            "last_updated": row["last_updated"],
            "source": row["source"],
            "bound_by_boot_token": row["bound_by_boot_token"],
        }

    def set_per_conductor(
        self,
        project_root: Path,
        *,
        cli_session_id: str,
        session_id: str,
        source: str = "/aidocs",
        boot_token: str | None = None,
    ) -> dict[str, Any]:
        """Upsert the per-conductor mapping. activated_at resets when
        the session identity changes for this conductor; rebinding
        the same session refreshes last_updated only.
        """
        now = self._timestamp()
        existing = self.get_per_conductor(
            project_root,
            cli_session_id=cli_session_id,
        )
        session_changed = existing is None or existing.get("session_id") != session_id
        activated_at = (
            now
            if session_changed or not (existing and existing.get("activated_at"))
            else existing["activated_at"]
        )
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO aidocs_managed_per_conductor
                    (cli_session_id, session_id, activated_at,
                     last_updated, source, bound_by_boot_token)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cli_session_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    activated_at = excluded.activated_at,
                    last_updated = excluded.last_updated,
                    source = excluded.source,
                    bound_by_boot_token = excluded.bound_by_boot_token
                """,
                (cli_session_id, session_id, activated_at, now, source, boot_token),
            )
        result = self.get_per_conductor(
            project_root,
            cli_session_id=cli_session_id,
        )
        return result or {}

    def clear_per_conductor(
        self,
        project_root: Path,
        *,
        cli_session_id: str,
    ) -> None:
        """Remove the mapping for `cli_session_id`. No-op when absent."""
        if not cli_session_id:
            return
        with self.session(project_root) as conn:
            conn.execute(
                "DELETE FROM aidocs_managed_per_conductor WHERE cli_session_id = ?",
                (cli_session_id,),
            )

    def prune_dead_conductor_bindings(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Remove per_conductor rows whose boot-token PID isn't alive.

        Castle-doctrine III: the oracle is current. Dead-PID bindings
        accumulate forever otherwise (every MCP restart leaves one,
        plus every host crash). They quietly violate "reads return
        reality" and confuse diagnostics.

        Token format is `mcp-<pid>-<unix-ts>-<hash>` (set by
        managed_mode_service._MCP_SERVER_BOOT_TOKEN). Parse the PID,
        check liveness via os.kill(pid, 0) — works on POSIX and
        Windows since Python 3.2. Rows with unparseable tokens are
        kept (defensive — only delete what we KNOW is dead).

        Idempotent. Safe to call from MCP boot. Returns a small
        diagnostic dict for boot logs.
        """
        self.init_db(project_root)
        kept: list[dict[str, object]] = []
        pruned: list[dict[str, object]] = []
        unparseable: list[dict[str, object]] = []
        with self.session(project_root) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT cli_session_id, session_id, bound_by_boot_token "
                "FROM aidocs_managed_per_conductor",
            ).fetchall()
            for r in rows:
                token = (r["bound_by_boot_token"] or "").strip()
                # Parse: "mcp-<pid>-<unix>-<hash>" → pid is index 1.
                if not token.startswith("mcp-"):
                    unparseable.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "token": token,
                            "reason": "no_mcp_prefix",
                        },
                    )
                    continue
                parts = token.split("-")
                if len(parts) < 4:
                    unparseable.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "token": token,
                            "reason": "wrong_part_count",
                        },
                    )
                    continue
                try:
                    pid = int(parts[1])
                except (ValueError, TypeError):
                    unparseable.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "token": token,
                            "reason": "non_integer_pid",
                        },
                    )
                    continue
                if pid <= 0:
                    unparseable.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "token": token,
                            "reason": "non_positive_pid",
                        },
                    )
                    continue
                # PID-alive check. Use cross-platform _pid_is_alive
                # helper (POSIX: os.kill(pid, 0); Windows: OpenProcess
                # + GetExitCodeProcess via ctypes). Avoids the Windows
                # os.kill(pid, 0) side-effect that killed MCP server
                # transport on test runs — see _pid_is_alive docstring.
                if _pid_is_alive(pid):
                    kept.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "pid": pid,
                        },
                    )
                else:
                    # Dead — prune.
                    conn.execute(
                        "DELETE FROM aidocs_managed_per_conductor WHERE cli_session_id = ?",
                        (r["cli_session_id"],),
                    )
                    pruned.append(
                        {
                            "cli_session_id": r["cli_session_id"],
                            "session_id": r["session_id"],
                            "pid": pid,
                        },
                    )
        return {
            "pruned": pruned,
            "kept": kept,
            "unparseable": unparseable,
            "total_before": len(rows),
            "total_after": len(kept) + len(unparseable),
        }
