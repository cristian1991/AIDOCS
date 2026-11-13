"""Strike-ledger alarm, stored OUTSIDE aidocs.sqlite3 (E78-B, 2026-11-12).

The strike ledger (execution_events) and the session degraded flag
(session_query_gate) live in the same aidocs.sqlite3. An alarm written only
there vanishes in exactly the outage it reports. And the in-process
DegradedLatch is invisible to the dashboard: the local dashboard reads its
snapshot from a CLI subprocess and the web dashboard from the outer-gate
server, neither of which is the hook/MCP process that raises the alarm.

So the alarm is a small JSON file next to the database. The dashboard
snapshot reads it FIRST, before any database read, and merges it into
``degraded_state`` for every surface that builds a snapshot from the project
root. It is project-wide: the ledger is per project, so an outage is too.

Two files, each only ever replaced whole by an atomic write (temp file in the
same directory, fsync, os.replace), never moved or deleted:

  strike_ledger_alarm.json      {generation, reason, raised_at, sessions}
                                written by raise_alarm; every raise mints a
                                NEW generation.
  strike_ledger_alarm_ack.json  {acked_generation, acked_at, by}
                                written by clear_alarm, and only when the
                                generation the operator saw is the alarm's
                                CURRENT generation.

The alarm is ACTIVE when the alarm file exists and its generation differs
from acked_generation (or there is no ack). A stale ack can never cover a
newer alarm, because the newer alarm has a different generation. A crash at
any step leaves either the old ack or the new one, both whole; neither can
hide an alarm it does not name. An unreadable alarm file or ack file reads as
DEGRADED, never as healthy; an unreadable alarm file cannot be acknowledged
(only a later raise, which rewrites it whole, replaces it).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ALARM_FILENAME = "strike_ledger_alarm.json"
ACK_FILENAME = "strike_ledger_alarm_ack.json"
UNREADABLE_GENERATION = "unreadable"
_MAX_SESSIONS = 50
_REPLACE_ATTEMPTS = 3


def alarm_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / ALARM_FILENAME


def ack_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / ACK_FILENAME


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _unreadable(what: str, exc: BaseException, generation: str) -> dict[str, Any]:
    return {
        "degraded": True,
        "reason": f"strike ledger {what} unreadable: {type(exc).__name__}"[:160],
        "raised_at": "",
        "sessions": [],
        "generation": generation,
    }


def _parse(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not an object")
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` whole: temp file in the same directory, fsync, then
    os.replace. On failure only our own temp file is removed."""
    fd, tmp = tempfile.mkstemp(prefix=".alarm-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a reader holding the target open blocks replace.
                if attempt + 1 == _REPLACE_ATTEMPTS:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_raw_alarm(project_root: Path) -> dict[str, Any] | None:
    """The alarm file as written (acked or not); None when absent."""
    path = alarm_path(project_root)
    if not path.exists():
        return None
    try:
        data = _parse(path)
    except Exception as exc:  # noqa: BLE001 -- unreadable is degraded
        return _unreadable("alarm", exc, UNREADABLE_GENERATION)
    data["degraded"] = True
    data["generation"] = str(data.get("generation") or "")
    return data


def read_alarm(project_root: Path) -> dict[str, Any] | None:
    """The ACTIVE alarm, or None when there is none (absent or acked)."""
    alarm = _read_raw_alarm(project_root)
    if alarm is None:
        return None
    if alarm["generation"] in ("", UNREADABLE_GENERATION):
        return alarm  # cannot be matched by any ack: stays active
    path = ack_path(project_root)
    if not path.exists():
        return alarm
    try:
        acked = str(_parse(path).get("acked_generation") or "")
    except Exception as exc:  # noqa: BLE001 -- unreadable ack is degraded
        # Keep the alarm's own generation so the operator can acknowledge it
        # again, which rewrites the ack file whole.
        out = dict(alarm)
        out["reason"] = (
            f"{alarm.get('reason') or 'strike ledger unavailable'}; "
            f"strike ledger alarm ack unreadable: {type(exc).__name__}"
        )[:240]
        return out
    return None if acked == alarm["generation"] else alarm


def read_alarm_safely(project_root: Path) -> dict[str, Any] | None:
    """read_alarm that never raises (a failing stat is degraded too)."""
    try:
        return read_alarm(project_root)
    except Exception as exc:  # noqa: BLE001
        return _unreadable("alarm", exc, UNREADABLE_GENERATION)


def raise_alarm(project_root: Path, *, session_id: str, reason: str) -> bool:
    """Write a NEW alarm generation atomically. False if it could not be
    written. Never creates a .MEMORY tree that is not already there."""
    path = alarm_path(project_root)
    if not path.parent.parent.is_dir():
        return False
    try:
        prior = _read_raw_alarm(project_root) or {}
        sessions = [s for s in prior.get("sessions", []) if isinstance(s, str)]
        if session_id and session_id not in sessions:
            sessions.append(session_id)
        path.parent.mkdir(exist_ok=True)
        _atomic_write(
            path,
            {
                "generation": uuid.uuid4().hex,
                "reason": str(reason or "strike ledger unavailable")[:160],
                "raised_at": _iso_now(),
                "sessions": sessions[-_MAX_SESSIONS:],
            },
        )
        return True
    except Exception:  # noqa: BLE001 -- the caller has other surfaces
        return False


def clear_alarm(project_root: Path, *, generation: str, by: str = "operator") -> str:
    """Acknowledge the alarm generation the operator saw.

    Writes the ack file ONLY when ``generation`` equals the alarm file's
    current generation; never moves or deletes the alarm. Returns
    ``"absent"`` (no alarm file), ``"cleared"`` (ack written), ``"kept"``
    (no generation given, a different -- newer -- alarm is current, or the
    alarm file is unreadable; nothing written) or ``"error"`` (the ack could
    not be written; the alarm stays active).
    """
    alarm = _read_raw_alarm(project_root)
    if alarm is None:
        return "absent"
    current = alarm["generation"]
    if not generation or current in ("", UNREADABLE_GENERATION) or generation != current:
        return "kept"
    try:
        _atomic_write(
            ack_path(project_root),
            {"acked_generation": generation, "acked_at": _iso_now(), "by": str(by or "")},
        )
    except Exception:  # noqa: BLE001
        return "error"
    return "cleared"


def merge_alarm(state: dict[str, Any], alarm: dict[str, Any] | None) -> dict[str, Any]:
    """Fold an already-read ACTIVE alarm into a dashboard ``degraded_state``."""
    if not alarm:
        return state
    out = dict(state)
    prior = str(out.get("reason") or "") if out.get("degraded") else ""
    reason = str(alarm.get("reason") or "strike ledger unavailable")
    out["degraded"] = True
    out["reason"] = (f"{reason}; {prior}" if prior and prior != reason else reason)[:240]
    if not prior:
        out["degraded_at"] = str(alarm.get("raised_at") or "")
    out.setdefault("degraded_at", "")
    out.setdefault("last_failure_event_id", "")
    out["alarm_generation"] = str(alarm.get("generation") or "")
    out["strike_ledger_alarm"] = alarm
    return out

