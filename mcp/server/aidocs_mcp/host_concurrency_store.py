"""Per-machine concurrency registry for AIDOCS conductor + worker processes.

Lives in the same install-wide sqlite DB as ConfigStore / KnownProjectsStore
(``~/.aidocs/config.sqlite3`` by default, overridable via
``AIDOCS_GLOBAL_CONFIG_DB``). One row per live process.

Why per-machine and not global-config or per-project:
  * A single operator/dashboard may control multiple machines (network
    mode is incoming).
  * Multiple operators may share one host.
  * The real constraint is OS + machine resources (RAM, file handles,
    parallel CLI subprocesses that each hold their own model context).
Project and install-wide scopes don't model that constraint.

Identity: we key rows on `hostname` from `socket.gethostname()`. That's
enough for the typical laptop / dev-box / named VM case. A future
expansion could hash the primary MAC to distinguish hosts that share
a hostname; not implemented yet because it hasn't been needed.

Liveness: every read path runs a cheap dead-pid sweep first. A process
whose PID is no longer alive on this host is removed from the registry
before the count is returned. This keeps the ceiling honest when
processes crash without calling `unregister`.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect


def _hostname() -> str:
    """Human-readable label for dashboard display. NOT used for keying
    — use machine_id() for that. Hostnames collide (VMs, cloned
    images, corporate fleets).
    """
    try:
        return socket.gethostname().strip().rstrip(".")
    except Exception:
        return "unknown-host"


def machine_id() -> str:
    """Stable, unique-per-host identifier for keying machine-scoped
    state. Survives reboots, distinguishes VM clones, and never changes.

    Resolution order:
      1. Linux: /etc/machine-id
      2. macOS: IOPlatformUUID via `ioreg`
      3. Windows: HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid
      4. Fallback: ~/.aidocs/machine-id (UUID4 generated on first miss)
    """
    cached = getattr(machine_id, "_cached", None)
    if cached:
        return cached
    mid: str | None = None
    try:
        if os.name == "posix":
            p = Path("/etc/machine-id")
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    mid = text
            if mid is None:
                # Doctrine 2026-05-29 (legacy-inventory migration):
                # operator-local host probe. Routes through the
                # ShellEgressService chokepoint so destructive_floor
                # protection, timeout, audit row, and output_guard
                # fail-closed scan all apply uniformly. Reachability
                # is `operator_local` because the call fires at host-
                # detection time, never on an agent-reachable path,
                # so judge + lifecycle preflight skip. The caller
                # already wraps every failure (including a fail-closed
                # output_guard withhold) in `mid = None` fallback to
                # `~/.aidocs/machine-id`, so the seal is intact even
                # if the guard withholds the UUID-shaped stdout.
                try:
                    from .shell_egress_service import default_service

                    res = default_service().execute_shell(
                        "ioreg -rd1 -c IOPlatformExpertDevice",
                        cwd=str(Path.home()),
                        timeout_s=2.0,
                        reachability="operator_local",
                        audit_tag="host_concurrency_store.machine_id",
                    )
                    for line in (res.stdout or "").splitlines():
                        if "IOPlatformUUID" in line:
                            _, _, rhs = line.partition("=")
                            candidate = rhs.strip().strip('"')
                            if candidate:
                                mid = candidate
                            break
                except Exception:
                    mid = None
        elif os.name == "nt":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography",
                    0,
                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                ) as k:
                    value, _ = winreg.QueryValueEx(k, "MachineGuid")
                    candidate = str(value).strip()
                    if candidate:
                        mid = candidate
            except Exception:
                mid = None
    except Exception:
        mid = None
    if not mid:
        fallback_path = Path.home() / ".aidocs" / "machine-id"
        try:
            if fallback_path.is_file():
                existing = fallback_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ).strip()
                if existing:
                    mid = existing
            if not mid:
                import uuid as _uuid

                new_id = _uuid.uuid4().hex
                fallback_path.parent.mkdir(parents=True, exist_ok=True)
                fallback_path.write_text(new_id + "\n", encoding="utf-8")
                mid = new_id
        except Exception:
            mid = f"host:{_hostname()}"
    try:
        machine_id._cached = mid  # type: ignore[attr-defined]
    except Exception:
        pass
    return mid


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Returns False on any error
    (treated as "dead" so a cleanup sweep never over-counts).
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: OpenProcess via kernel32. Using ctypes to avoid
            # a psutil dependency just for this.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            got = ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            )
            ctypes.windll.kernel32.CloseHandle(handle)
            if not got:
                return False
            STILL_ACTIVE = 259
            return exit_code.value == STILL_ACTIVE
        # POSIX: signal 0 probes without actually sending.
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class HostConcurrencyStore:
    """Registry of live AIDOCS-managed processes on this host machine."""

    def _global_db_path(self) -> Path:
        override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".aidocs" / "config.sqlite3"

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        db_path = self._global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # #755: through the ONE canonical connect — see execution_index_store for
        # the reasoning. Host concurrency is ephemeral runtime state, so RUNTIME.
        conn = _canonical_connect(db_path, durability=_Durability.RUNTIME)
        # This helper OWNS the handle and closes it below. Mark it borrowed so a
        # nested `with conn:` transaction inside the block commits without
        # closing it out from under us (see ClosingConnection).
        conn._aidocs_borrowed = True  # noqa: SLF001 -- our own subclass
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS host_live_processes (
                    machine_id TEXT NOT NULL,
                    worker_key TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    pid INTEGER,
                    kind TEXT NOT NULL,
                    project_root TEXT,
                    session_id TEXT,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY (machine_id, worker_key)
                );
                CREATE INDEX IF NOT EXISTS idx_host_live_processes_machine
                    ON host_live_processes(machine_id);
                """,
            )

    # ── Liveness sweep ──

    def _sweep_dead(self, conn: sqlite3.Connection) -> int:
        """Remove rows whose owning pid is no longer alive on this
        machine. Rows with NULL pid (thread-backed workers that rely
        on explicit unregister) are left alone — their liveness isn't
        pid-observable, and the caller always unregisters in finally.
        Keyed on machine_id so a DB shared via a synced homedir only
        sweeps THIS host's rows.
        """
        mid = machine_id()
        rows = conn.execute(
            "SELECT worker_key, pid FROM host_live_processes "
            "WHERE machine_id = ? AND pid IS NOT NULL",
            (mid,),
        ).fetchall()
        dead_keys = [
            row["worker_key"]
            for row in rows
            if row["pid"] is not None and not _is_pid_alive(int(row["pid"]))
        ]
        if not dead_keys:
            return 0
        placeholders = ",".join("?" for _ in dead_keys)
        conn.execute(
            f"DELETE FROM host_live_processes "
            f"WHERE machine_id = ? AND worker_key IN ({placeholders})",
            (mid, *dead_keys),
        )
        return len(dead_keys)

    # ── Public API ──

    def register(
        self,
        *,
        worker_key: str,
        kind: str,
        pid: int | None = None,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record a live process on this machine. Idempotent per
        (machine_id, worker_key). `pid` is optional — pass when the
        slot maps to a distinct OS process (subprocess Popen); leave
        None for thread-backed workers sharing the server pid. Callers
        always pair register with unregister; pid is only used for
        the dead-pid sweep backstop.
        """
        self.init_db()
        now = datetime.now(UTC).isoformat()
        mid = machine_id()
        host = _hostname()
        with self._session() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO host_live_processes
                   (machine_id, worker_key, hostname, pid, kind,
                    project_root, session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    str(worker_key),
                    host,
                    int(pid) if pid is not None else None,
                    str(kind),
                    str(project_root) if project_root else None,
                    session_id,
                    now,
                ),
            )

    def unregister(self, *, worker_key: str) -> None:
        """Drop the row for this machine + worker_key. No-op if gone."""
        self.init_db()
        mid = machine_id()
        with self._session() as conn:
            conn.execute(
                "DELETE FROM host_live_processes WHERE machine_id = ? AND worker_key = ?",
                (mid, str(worker_key)),
            )

    def reset(self) -> int:
        """Force-clear every live-process row for this machine. Returns
        the count cleared. Operator escape hatch when the counter is
        wedged by phantom rows (e.g. MCP crashed before workers' finally
        unregistered, AND pid was NULL so the sweep can't reclaim).
        Re-registration on next spawn restores correct state for any
        actually-live workers.
        """
        self.init_db()
        mid = machine_id()
        with self._session() as conn:
            cur = conn.execute(
                "DELETE FROM host_live_processes WHERE machine_id = ?",
                (mid,),
            )
            return cur.rowcount or 0

    def reconcile_against_os(self) -> int:
        """Aggressive sweep — checks every row, even those with NULL pid,
        against the OS process table on this machine. Rows with NULL pid
        are treated as 'unverifiable, presume dead if older than 5min'
        because there's no other liveness signal for them. Returns the
        count cleared. Called by check_machine_capacity when at-cap
        before refusing, to catch phantoms that the normal sweep can't.
        """
        from datetime import datetime as _dt
        from datetime import timedelta

        self.init_db()
        mid = machine_id()
        cleared = 0
        with self._session() as conn:
            cleared += self._sweep_dead(conn)
            cutoff = (_dt.now(UTC) - timedelta(minutes=5)).isoformat()
            cur = conn.execute(
                "DELETE FROM host_live_processes "
                "WHERE machine_id = ? AND pid IS NULL AND started_at < ?",
                (mid, cutoff),
            )
            cleared += cur.rowcount or 0
        return cleared

    def live_count(self) -> int:
        """Current live process count on this machine. Sweeps dead rows
        first so the number reflects actual state.
        """
        self.init_db()
        mid = machine_id()
        with self._session() as conn:
            self._sweep_dead(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM host_live_processes WHERE machine_id = ?",
                (mid,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_live(self) -> list[dict[str, object]]:
        """Return the sweep-cleaned list of live rows on this machine.
        Ordered by started_at ASC (oldest first) for dashboard display.
        """
        self.init_db()
        mid = machine_id()
        with self._session() as conn:
            self._sweep_dead(conn)
            rows = conn.execute(
                """SELECT machine_id, worker_key, hostname, pid, kind,
                          project_root, session_id, started_at
                   FROM host_live_processes
                   WHERE machine_id = ?
                   ORDER BY started_at ASC""",
                (mid,),
            ).fetchall()
        return [dict(r) for r in rows]


def check_machine_capacity(
    *,
    max_processes: int,
    kind: str,
) -> dict[str, object]:
    """Return {'ok': True} when the host can accept another spawn of
    `kind`, otherwise {'ok': False, 'error': ..., 'blocked_by':
    'machine_concurrency'}. Read-only — the caller registers on
    successful spawn.
    """
    store = HostConcurrencyStore()
    live = store.live_count()
    # Phoenix 2026-05-12 (Empire bug report): when at-cap, do one
    # aggressive reconciliation pass against the OS before refusing.
    # Catches phantoms that the normal pid-not-null sweep can't (rows
    # from before pid was being recorded, or from MCP crashes mid-spawn).
    if live >= int(max_processes):
        store.reconcile_against_os()
        live = store.live_count()
    if live >= int(max_processes):
        return {
            "ok": False,
            "error": (
                f"machine concurrency ceiling reached: {live}/"
                f"{max_processes} live AIDOCS processes on this host"
            ),
            "blocked_by": "machine_concurrency",
            "live_count": live,
            "max_processes": int(max_processes),
        }
    return {"ok": True, "live_count": live, "max_processes": int(max_processes)}
