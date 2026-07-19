"""Process-audit ledger — runtime observability for subprocess spawns.

Backlog #335 Phase 1 (process-audit war). Operators and agents should
never have to GUESS what subprocesses this server spawned, why, and
when. Every spawn routed through ``shell_egress_service.audited_popen``
lands one row here at launch (``record_spawn``) and is completed on
exit (``record_reap``).

PURE OBSERVABILITY — this module records; it never gates. The
enforcement authority stays ``shell_egress_service.
LEGACY_SUBPROCESS_FINGERPRINTS`` + the AST doctrine test
(test_legacy_subprocess_callsite_fingerprints); the
``callsite_fingerprint`` column here simply NAMES the registry row a
spawn came from so the ledger joins back to the allow-list.

Lifecycle mirrors ``empire_audit_store``: a machine-level sqlite at
``~/.aidocs/process_audit.sqlite3`` (spawns are a runtime/process
concern, not a per-project one), env override
``AIDOCS_PROCESS_AUDIT_DB`` so tests never touch the operator's real
ledger, per-connection open/close, and an identity-validated
``SchemaMemo`` so the idempotent DDL is skipped on hot paths but
re-runs when the file is recycled (pytest tmp-path reuse, operator
deletes).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema_memo import SchemaMemo

_SCHEMA_ENSURED = SchemaMemo()


def process_audit_db_path() -> Path:
    """Ledger sqlite path. Honors AIDOCS_PROCESS_AUDIT_DB so tests never
    touch the operator's real ``~/.aidocs/process_audit.sqlite3``."""
    override = os.environ.get("AIDOCS_PROCESS_AUDIT_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "process_audit.sqlite3"


def connect() -> sqlite3.Connection:
    db = process_audit_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    db_path = process_audit_db_path()
    if _SCHEMA_ENSURED.is_current(db_path):
        return
    conn = connect()
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS process_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    pid INTEGER,
                    ppid INTEGER,
                    argv_json TEXT NOT NULL,
                    callsite_fingerprint TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    session_id TEXT,
                    -- reap fields: honest NULLs until record_reap fires;
                    -- a NULL exit_code means "still running (or never reaped)".
                    exit_code INTEGER,
                    duration_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_process_audit_callsite
                    ON process_audit(callsite_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_process_audit_session
                    ON process_audit(session_id);
                """,
            )
            # Idempotent migration (#335 BUG 2 sweeper): swept_at marks an
            # orphaned open row (reap thread lost to a server restart,
            # pid provably dead) as accounted-for WITHOUT fabricating an
            # exit_code/duration — those stay honest NULLs.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(process_audit)")}
            if "swept_at" not in cols:
                conn.execute("ALTER TABLE process_audit ADD COLUMN swept_at TEXT")
    finally:
        conn.close()
    _SCHEMA_ENSURED.mark(db_path)


def fingerprint_key(fingerprint: str | Sequence[str]) -> str:
    """Canonical text form of a callsite fingerprint.

    Accepts either the canonical string itself or a tuple prefix of a
    LEGACY_SUBPROCESS_FINGERPRINTS row — typically
    ``(relpath, enclosing_fn, callee_kind)`` — and joins it with ``::``
    (e.g. ``'aidocs_service.py::spawn::subprocess.Popen'``).
    """
    if isinstance(fingerprint, str):
        return fingerprint
    return "::".join(str(part) for part in fingerprint)


# ── Write path ───────────────────────────────────────────────────────


def record_spawn(
    pid: int | None,
    ppid: int | None,
    argv: Sequence[Any],
    fingerprint: str | Sequence[str],
    reason: str,
    session_id: str | None = None,
) -> int:
    """Stamp one spawn row at launch time. Returns the ledger row id the
    caller hands back to ``record_reap`` when the process exits."""
    init_db()
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO process_audit "
                "(ts, pid, ppid, argv_json, callsite_fingerprint, reason, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    pid,
                    ppid,
                    json.dumps([str(a) for a in argv]),
                    fingerprint_key(fingerprint),
                    reason,
                    session_id,
                ),
            )
            return int(cur.lastrowid or 0)
    finally:
        conn.close()


def record_reap(row_id: int, exit_code: int | None, duration_ms: int | None) -> None:
    """Complete a spawn row once the process has exited.

    A real reap is the truth — it also clears any ``swept_at`` marker a
    sweeper may have stamped in the interim."""
    init_db()
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE process_audit SET exit_code = ?, duration_ms = ?, "
                "swept_at = NULL WHERE id = ?",
                (exit_code, duration_ms, row_id),
            )
    finally:
        conn.close()


def sweep_stale_open_rows(
    min_age_seconds: int = 300,
    pid_alive=None,
) -> int:
    """Mark orphaned open rows as swept (#335 BUG 2 reaper-gap seal).

    ``audited_popen`` reaps on a DAEMON wait-thread; a server restart
    mid-run loses the thread and the row would sit "open" forever,
    inflating the open count and silently undercounting the benchmark.
    Mirrors ``session_lane_agents_store.reap_crashed``: an open row past
    the ``min_age_seconds`` grace window is swept when its pid is
    provably dead, or — for rows that never recorded a usable pid and so
    can never be reaped by anything — by staleness alone.

    HONESTY LAW: sweeping stamps ``swept_at`` only. ``exit_code`` and
    ``duration_ms`` stay NULL (unknown), never fabricated, so swept rows
    are excluded from duration analytics. A late real ``record_reap``
    wins and clears the marker. Returns the number of rows swept.
    """
    if pid_alive is None:
        from .session_lane_agents_store import _pid_alive as pid_alive

    init_db()
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - max(0, int(min_age_seconds))
    conn = connect()
    try:
        # duration_ms is the reap marker — audited_popen's reaper ALWAYS
        # stamps it, while exit_code can be an honest NULL on a reaped row
        # (proc.wait raised → exit unknown). Keying on exit_code would
        # re-sweep genuinely reaped rows.
        rows = conn.execute(
            "SELECT id, ts, pid FROM process_audit "
            "WHERE duration_ms IS NULL AND swept_at IS NULL",
        ).fetchall()
        stale_ids: list[int] = []
        for row in rows:
            try:
                spawned = datetime.fromisoformat(row["ts"]).timestamp()
            except (TypeError, ValueError):
                spawned = 0.0  # unparseable ts = provably stale bookkeeping
            if spawned >= cutoff:
                continue  # inside the grace window — reap thread may still fire
            pid = row["pid"]
            has_pid = pid is not None and int(pid) > 0
            if has_pid and pid_alive(int(pid)):
                continue  # provably alive — legitimately still running
            stale_ids.append(int(row["id"]))
        if not stale_ids:
            return 0
        swept_ts = now.isoformat(timespec="milliseconds")
        placeholders = ",".join("?" for _ in stale_ids)
        with conn:
            conn.execute(
                f"UPDATE process_audit SET swept_at = ? "
                f"WHERE id IN ({placeholders}) AND duration_ms IS NULL",
                (swept_ts, *stale_ids),
            )
        return len(stale_ids)
    finally:
        conn.close()


# ── Query helpers ────────────────────────────────────────────────────


def _rows(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    init_db()
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def tail(n: int = 20) -> list[dict]:
    """Newest-first slice of the ledger."""
    return _rows(
        "SELECT * FROM process_audit ORDER BY id DESC LIMIT ?",
        (max(1, int(n)),),
    )


def by_callsite(key: str | Sequence[str], n: int = 100) -> list[dict]:
    """Every spawn recorded for one callsite fingerprint (tuple or
    canonical string), newest first."""
    return _rows(
        "SELECT * FROM process_audit WHERE callsite_fingerprint = ? "
        "ORDER BY id DESC LIMIT ?",
        (fingerprint_key(key), max(1, int(n))),
    )


def by_session(sid: str, n: int = 100) -> list[dict]:
    """Every spawn stamped with one session_id, newest first."""
    return _rows(
        "SELECT * FROM process_audit WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (sid, max(1, int(n))),
    )


def _duration_analytics(conn: sqlite3.Connection, group_column: str) -> dict[str, dict[str, Any]]:
    """Per-``group_column`` (callsite_fingerprint | reason) benchmark shape:
    spawn_count (all rows), reaped_count (rows with a recorded duration),
    and avg/max/total duration_ms over the reaped subset (SQLite AVG/MAX/SUM
    ignore NULLs, so an all-open group reports honest ``None`` durations
    rather than a fabricated 0)."""
    rows = conn.execute(
        f"SELECT {group_column}, COUNT(*), COUNT(duration_ms), "
        "AVG(duration_ms), MAX(duration_ms), SUM(duration_ms) "
        f"FROM process_audit GROUP BY {group_column} ORDER BY COUNT(*) DESC",
    )
    out: dict[str, dict[str, Any]] = {}
    for key, spawn_count, reaped_count, avg_ms, max_ms, total_ms in rows:
        out[key] = {
            "spawn_count": spawn_count,
            "reaped_count": reaped_count,
            "avg_duration_ms": round(avg_ms, 2) if avg_ms is not None else None,
            "max_duration_ms": max_ms,
            "total_duration_ms": total_ms,
        }
    return out


def stats() -> dict:
    """Ledger shape at a glance: totals, open (never reaped) vs reaped,
    swept_unknown (orphans marked by sweep_stale_open_rows — exit
    unknown, honestly excluded from both open and the benchmark),
    per-callsite / per-reason spawn counts, and (additive) per-callsite /
    per-reason duration benchmark analytics.

    Runs the orphan sweeper opportunistically first (best-effort) so the
    reported open count is never inflated by provably-dead orphans."""
    init_db()
    try:
        sweep_stale_open_rows()
    except Exception:  # noqa: BLE001 — observability must never break reads
        pass
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM process_audit").fetchone()[0]
        open_ = conn.execute(
            # duration_ms is the reap marker (see sweep_stale_open_rows) —
            # a reaped row may carry an honest NULL exit_code.
            "SELECT COUNT(*) FROM process_audit "
            "WHERE duration_ms IS NULL AND swept_at IS NULL",
        ).fetchone()[0]
        swept = conn.execute(
            "SELECT COUNT(*) FROM process_audit WHERE swept_at IS NOT NULL",
        ).fetchone()[0]
        by_cs = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT callsite_fingerprint, COUNT(*) FROM process_audit "
                "GROUP BY callsite_fingerprint ORDER BY COUNT(*) DESC",
            )
        }
        by_reason = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT reason, COUNT(*) FROM process_audit "
                "GROUP BY reason ORDER BY COUNT(*) DESC",
            )
        }
        callsite_durations = _duration_analytics(conn, "callsite_fingerprint")
        reason_durations = _duration_analytics(conn, "reason")
    finally:
        conn.close()
    return {
        "total": total,
        "open": open_,
        "swept_unknown": swept,
        "reaped": total - open_ - swept,
        "by_callsite": by_cs,
        "by_reason": by_reason,
        "callsite_durations": callsite_durations,
        "reason_durations": reason_durations,
    }


# ── ai_process_audit mode dispatcher (read-only) ─────────────────────


def process_audit_query(
    mode: str = "tail",
    n: int = 20,
    key: str = "",
    session_id: str = "",
) -> dict:
    """Backing impl for the read-only ``ai_process_audit`` tool.

    Modes (hyphen and underscore forms both accepted):
      tail        — newest-first n rows
      list        — oldest-first n rows
      by-callsite — rows for one callsite fingerprint (requires ``key``)
      by-session  — rows for one session_id (requires ``session_id``)
      stats       — totals + per-callsite / per-reason counts
      census      — the COMPLETE spawn map (#335 one organism): every
                    static callsite -> fingerprint -> reason -> window
                    posture -> audited/registered, joined with the
                    runtime ledger's per-fingerprint spawn counts (plus
                    ``ledger_orphans``: fingerprints seen at runtime
                    with no live static callsite)
    """
    m = (mode or "tail").strip().lower().replace("-", "_")
    if m == "tail":
        rows = tail(n)
        return {"ok": True, "mode": "tail", "count": len(rows), "rows": rows}
    if m == "list":
        rows = _rows(
            "SELECT * FROM process_audit ORDER BY id ASC LIMIT ?",
            (max(1, int(n)),),
        )
        return {"ok": True, "mode": "list", "count": len(rows), "rows": rows}
    if m == "by_callsite":
        if not (key or "").strip():
            return {"ok": False, "error": "mode=by-callsite requires key (a callsite fingerprint, e.g. 'aidocs_service.py::spawn::subprocess.Popen')"}
        rows = by_callsite(key.strip(), n)
        return {"ok": True, "mode": "by-callsite", "key": fingerprint_key(key.strip()), "count": len(rows), "rows": rows}
    if m == "by_session":
        if not (session_id or "").strip():
            return {"ok": False, "error": "mode=by-session requires session_id"}
        rows = by_session(session_id.strip(), n)
        return {"ok": True, "mode": "by-session", "session_id": session_id.strip(), "count": len(rows), "rows": rows}
    if m == "stats":
        return {"ok": True, "mode": "stats", "stats": stats()}
    if m == "census":
        # Static map first (pure AST read — never spawns), then the
        # runtime join. Ledger trouble must never break the map: the
        # census is exactly the instrument that debugs the ledger.
        from .spawn_census import spawn_census

        census = spawn_census()
        ledger_error = ""
        by_cs: dict[str, int] = {}
        try:
            by_cs = stats()["by_callsite"]
        except Exception as exc:  # noqa: BLE001 — observability must never break reads
            ledger_error = f"{type(exc).__name__}: {exc}"
        for entry in census["entries"]:
            fp = entry.get("fingerprint")
            entry["ledger_spawns"] = by_cs.get(fp, 0) if fp else 0
        mapped = {e["fingerprint"] for e in census["entries"] if e.get("fingerprint")}
        census["ledger_orphans"] = sorted(
            {fp: n for fp, n in by_cs.items() if fp not in mapped}.items()
        )
        census["summary"]["ledger_total_spawns"] = sum(by_cs.values())
        census["summary"]["ledger_orphans"] = len(census["ledger_orphans"])
        if ledger_error:
            census["ledger_error"] = ledger_error
        return {"ok": True, "mode": "census", "census": census}
    return {
        "ok": False,
        "error": f"unknown mode {mode!r}; expected tail | list | by-callsite | by-session | stats | census",
    }
