"""Run-done notification queue.

When a detached subprocess finishes, the monitor thread appends a
single JSON-lines record here. On the agent's next tool call,
`drain(project_root, session_id=...)` reads the queue, clears the
caller's records, and returns them for injection into the tool
response so the agent learns which runs completed without polling.

Design:
- One file per project: .MEMORY/.index/run_notifications.jsonl
- Filtered drain by session_id (#50, 2026-04-26): each session sees
  only its own records; other sessions' records stay in the file for
  their actual owners to drain.
- Empty-session records (no attribution) are invisible to filtered
  drains. They get garbage-collected in-drain after a grace window so
  the file doesn't grow ghosts.
- Records are terse — the full output stays in the per-run log file.

Record shape:
    {
      "run_id": str,
      "command": str,
      "exit_code": int,
      "outcome": "pass" | "fail" | "timeout",
      "duration_ms": int,
      "bucket_key": str,
      "session_id": str,   # attribution (#50): empty = unattributed
      "lane_id": str,      # informational; session is the boundary
      "finished_at": iso8601 (UTC, no Z),
    }
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_QUEUE_FILENAME = "run_notifications.jsonl"
_QUEUE_LOCK = threading.Lock()

# In-drain orphan GC (#50, 2026-04-26). Empty-session records older
# than this window get dropped on filtered drain calls. Not applied
# on unfiltered drains — those keep migration/test back-compat.
_ORPHAN_GRACE_SECONDS = 3600


def _queue_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / _QUEUE_FILENAME


def _finished_at_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _parse_finished_at_seconds(value: str) -> float | None:
    """Parse the queue's finished_at string back to epoch seconds.

    finished_at is written in UTC via time.gmtime (no Z suffix),
    so it must be parsed back as UTC via calendar.timegm. Using
    time.mktime would interpret the parsed struct as local time
    and produce a multi-hour skew on non-UTC hosts.

    Returns None when unparseable; caller treats None as 'unknown
    age' and skips GC for that record (conservative).
    """
    if not value:
        return None
    try:
        import calendar

        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def enqueue(
    project_root: Path,
    *,
    run_id: str,
    command: str,
    exit_code: int,
    outcome: str,
    duration_ms: int,
    bucket_key: str,
    digest: str = "",
    session_id: str = "",
    lane_id: str = "",
) -> None:
    """Append a run-done notification. Called by the monitor thread
    when a subprocess transitions to done. Safe to call repeatedly —
    dedupe is the reader's job (the monitor only fires once per run).

    session_id / lane_id (#50, 2026-04-26): attribution stamped at
    enqueue time. Empty session_id means 'unattributed' — those
    records are invisible to filtered drains and get GC'd in-drain
    after _ORPHAN_GRACE_SECONDS.
    """
    path = _queue_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    record = {
        "run_id": run_id,
        "command": command[:500],
        "exit_code": exit_code,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "bucket_key": bucket_key,
        "digest": digest[:200],
        "session_id": session_id or "",
        "lane_id": lane_id or "",
        "finished_at": _finished_at_now(),
        # surfaced_count (Phoenix 2026-05-10): how many times this
        # record has been displayed in a tool-call envelope. Bumped
        # by surface_for_session; auto-dismissed when it hits the
        # notifications.max_displays setting. 0 = never shown yet.
        "surfaced_count": 0,
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        with _QUEUE_LOCK, open(path, "a", encoding="utf-8") as fp:
            fp.write(line)
    except OSError:
        pass


def peek_for_session(
    project_root: Path,
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    """Phoenix 2026-05-07: peek (no remove) for surfacing in
    UPS/tool-call envelopes. Notifications PERSIST on every surface
    until explicitly dismissed via `dismiss_run(run_id)` (called by
    ai_run_output when the agent reads the output) — that's the
    'until satisfied' contract the Emperor named: a run-done
    notification stays visible across UPS + every tool call until
    the agent has actually read the output. Returns records owned
    by this session. Empty session_id returns [].
    """
    if not session_id:
        return []
    return [r for r in peek(project_root) if str(r.get("session_id") or "") == session_id]


def surface_for_session(
    project_root: Path,
    *,
    session_id: str,
    max_displays: int = 0,
) -> list[dict[str, Any]]:
    """Phoenix 2026-05-10: peek + bump surfaced_count + auto-dismiss.

    Companion to peek_for_session — used by notification_injector on
    every tool call. For each record owned by session_id:
      - increment its surfaced_count
      - drop it when the new count >= max_displays (auto-dismiss)
      - otherwise keep it (rewrites file with the bumped count)

    Returns the records still visible this turn (their pre-bump
    state is irrelevant — the format_block call doesn't render the
    counter). When max_displays <= 0, no auto-dismiss happens —
    classic 'until satisfied' behavior preserved. Empty session_id
    returns [] without touching the file.
    """
    if not session_id:
        return []
    records = peek(project_root)
    if not records:
        return []
    kept_for_session: list[dict[str, Any]] = []
    rewritten: list[dict[str, Any]] = []
    file_changed = False
    for rec in records:
        if str(rec.get("session_id") or "") != session_id:
            rewritten.append(rec)
            continue
        new_count = int(rec.get("surfaced_count", 0) or 0) + 1
        if max_displays > 0 and new_count >= max_displays:
            # Auto-dismiss: visible this turn (the bump that hit the
            # cap), then dropped from the file.
            file_changed = True
            kept_for_session.append({**rec, "surfaced_count": new_count})
            continue
        rec_copy = dict(rec)
        rec_copy["surfaced_count"] = new_count
        rewritten.append(rec_copy)
        kept_for_session.append(rec_copy)
        file_changed = True
    if not file_changed:
        return kept_for_session
    path = _queue_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _QUEUE_LOCK:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".run_notifications_",
                suffix=".tmp",
            )
            try:
                if rewritten:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                        for r in rewritten:
                            fp.write(json.dumps(r, sort_keys=True) + "\n")
                else:
                    os.close(tmp_fd)
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except OSError:
        pass
    return kept_for_session


def dismiss_run(project_root: Path, *, run_id: str) -> bool:
    """Phoenix 2026-05-07: remove a single record by run_id.
    Called by ai_run_output AFTER the agent has read the run's
    output — that completes the 'until satisfied' lifecycle. Returns
    True iff a record was actually removed. Concurrent-safe via
    _QUEUE_LOCK.
    """
    if not run_id:
        return False
    path = _queue_path(project_root)
    if not path.is_file():
        return False
    records = peek(project_root)
    if not records:
        return False
    keep = [r for r in records if str(r.get("run_id") or "") != run_id]
    if len(keep) == len(records):
        return False
    try:
        with _QUEUE_LOCK:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".run_notifications_",
                suffix=".tmp",
            )
            try:
                if keep:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                        for rec in keep:
                            fp.write(json.dumps(rec, sort_keys=True) + "\n")
                else:
                    os.close(tmp_fd)
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return False
    except OSError:
        return False
    return True


def dismiss_for_session(
    project_root: Path,
    *,
    session_id: str,
    run_id: str = "",
) -> int:
    """Phoenix 2026-05-09: bulk-dismiss run notifications for a session.

    Companion to ``dismiss_run`` (which is per-run, called by
    ai_run_output's auto-dismiss-on-output-read). This is the
    conductor escape hatch — the 📣 surface persists every tool call
    until output is read OR run is dismissed, but a long sequence of
    detached runs the agent never reads (sleep probes, status checks,
    fire-and-forget subprocesses) leaves permanent visual noise. The
    conductor calls this to clear the queue without round-tripping
    each ai_run_output.

    Behavior:
    - run_id="" → dismiss ALL records for this session.
    - run_id="r_..." → dismiss only that record (same effect as
      ``dismiss_run`` but session-scoped — refuses to touch records
      owned by another session).

    Returns the number of records removed. 0 means none matched.
    Concurrent-safe via _QUEUE_LOCK. Records owned by OTHER sessions
    are never touched.
    """
    if not session_id:
        return 0
    path = _queue_path(project_root)
    if not path.is_file():
        return 0
    records = peek(project_root)
    if not records:
        return 0

    def _matches(rec: dict[str, Any]) -> bool:
        if str(rec.get("session_id") or "") != session_id:
            return False
        if run_id and str(rec.get("run_id") or "") != run_id:
            return False
        return True

    keep = [r for r in records if not _matches(r)]
    removed_count = len(records) - len(keep)
    if removed_count == 0:
        return 0
    try:
        with _QUEUE_LOCK:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".run_notifications_",
                suffix=".tmp",
            )
            try:
                if keep:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                        for rec in keep:
                            fp.write(json.dumps(rec, sort_keys=True) + "\n")
                else:
                    os.close(tmp_fd)
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return 0
    except OSError:
        return 0
    return removed_count


def peek(project_root: Path) -> list[dict[str, Any]]:
    """Read current queue without clearing. Returns empty list when
    the queue is absent / unreadable.
    """
    path = _queue_path(project_root)
    if not path.is_file():
        return []
    try:
        with _QUEUE_LOCK:
            raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def drain(
    project_root: Path,
    *,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Atomically read and clear matching records (#50, 2026-04-26).

    When session_id is empty: legacy unfiltered behavior — drains
    every record, no GC. Used by tests and migration paths only.

    When session_id is set: returns only records matching that
    session_id; other sessions' records stay in the file for their
    actual owners. Empty-session records older than
    _ORPHAN_GRACE_SECONDS are GC'd (dropped from file, NOT returned)
    so the file can't accumulate ghosts.

    Concurrent enqueue between the read and rewrite is handled: the
    enqueue either lands before the rewrite (captured in our read)
    or after (sits in the new file for the next drain). Either way
    no records are silently lost.
    """
    path = _queue_path(project_root)
    if not path.is_file():
        return []
    records = peek(project_root)
    if not records:
        return []

    if not session_id:
        # Unfiltered drain — back-compat path. No GC, no filtering.
        returned = list(records)
        leftover: list[dict[str, Any]] = []
        gc_dropped: list[dict[str, Any]] = []
    else:
        returned, leftover, gc_dropped = _partition_records(
            records,
            session_id=session_id,
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _QUEUE_LOCK:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".run_notifications_",
                suffix=".tmp",
            )
            try:
                if leftover:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                        for rec in leftover:
                            fp.write(json.dumps(rec, sort_keys=True) + "\n")
                else:
                    os.close(tmp_fd)
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except OSError:
        return returned  # Still deliver what we read even if rewrite failed.

    if gc_dropped:
        _audit_orphan_gc(project_root, gc_dropped)
    return returned


def _partition_records(
    records: list[dict[str, Any]],
    *,
    session_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split records into (matched, leftover, gc_dropped).

    matched     — records belonging to session_id (returned to caller)
    leftover    — records belonging to OTHER sessions OR unattributed
                  records young enough to keep (rewritten to file)
    gc_dropped  — empty-session records older than the grace window
                  (silently discarded; counted for audit)
    """
    matched: list[dict[str, Any]] = []
    leftover: list[dict[str, Any]] = []
    gc_dropped: list[dict[str, Any]] = []
    now = time.time()
    for rec in records:
        rec_session = str(rec.get("session_id") or "")
        if rec_session == session_id:
            matched.append(rec)
            continue
        if rec_session:
            leftover.append(rec)
            continue
        age = _parse_finished_at_seconds(str(rec.get("finished_at") or ""))
        if age is None:
            leftover.append(rec)
        elif (now - age) >= _ORPHAN_GRACE_SECONDS:
            gc_dropped.append(rec)
        else:
            leftover.append(rec)
    return matched, leftover, gc_dropped


def _audit_orphan_gc(
    project_root: Path,
    dropped: list[dict[str, Any]],
) -> None:
    """Emit one audit event per filtered drain that GC'd records.
    count + oldest_dropped_age_seconds. Best-effort — audit failures
    must never break the drain pipeline.
    """
    if not dropped:
        return
    now = time.time()
    oldest_age = 0
    for rec in dropped:
        age = _parse_finished_at_seconds(str(rec.get("finished_at") or ""))
        if age is None:
            continue
        rec_age = int(now - age)
        oldest_age = max(oldest_age, rec_age)
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="notify_orphan_gc",
            source_kind="run_notifications",
            session_id="",
            capability_name="run_notifications.drain",
            action_kind="orphan_gc",
            status="ok",
            payload={
                "dropped_count": len(dropped),
                "oldest_dropped_age_seconds": oldest_age,
            },
        )
    except Exception:
        pass


def format_block(records: list[dict[str, Any]]) -> str:
    """One line per finished run. Carries: status icon, command, run_id
    (so the agent can pull output via ai_run_output), duration, and the
    digest (framework counts + first-failed-test for tests, error count
    for builds, last-line for probes) — see run_output_renderer.
    digest_for_run for the exact shape.
    """
    if not records:
        return ""
    lines: list[str] = []
    for r in records[:10]:
        outcome = str(r.get("outcome", ""))
        icon = {"pass": "✅", "fail": "❌", "timeout": "⏱"}.get(outcome, "•")
        dur_s = max(1, int(r.get("duration_ms", 0)) // 1000)
        cmd = str(r.get("command", ""))[:60]
        rid = r.get("run_id", "?")
        digest = str(r.get("digest", "")).strip()
        suffix = f" · {digest}" if digest else ""
        lines.append(f"📣 {icon} `{cmd}` ({rid}) · {dur_s}s{suffix}")
    if len(records) > 10:
        lines.append(f"📣 … {len(records) - 10} more")
    return "\n".join(lines)
