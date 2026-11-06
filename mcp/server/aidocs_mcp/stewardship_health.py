"""``stewardship_degraded_unrecorded`` -- the health flag for the narrow,
OPERATOR-ACCEPTED availability exception (r0b2 round 6, option a).

THE EXCEPTION. A Stop whose failure-stewardship check degraded (the ledger
evaluation or the module itself failed) normally records that as debt in the
Stop dispatch ledger or the audit store. When BOTH refuse and the host is
re-entering Stop (``stop_hook_active``), the turn seals anyway rather than
wedging -- an availability exception, never a verified or safe seal. This
module is the extra signal the operator required for it.

WHERE IT LIVES. Process memory of the warm hook broker -- the long-lived
process that runs ClaudeHookHandler in-process for every hook event
(hook_broker -> evaluate_hook_event). ``in_long_lived=True`` is passed only
when the caller is inside ``hook_write_behind.broker_evaluation()``. In the
cold fallback path (the broker did not answer and a fresh hook process runs
the handler) there is no long-lived holder: the flag is NOT recorded and the
caller says so on stderr; nothing claims otherwise.

WHAT IT HOLDS. Per PROJECT + host session (the broker multiplexes project
roots; ``project_key`` is the canonical root, round 8): first_seen,
last_seen, project, session, error_type, count. Never exception text, never
transcript content. A flag is looked up, shown, consulted and flushed only
for its own project, and flushed only into that project's own store.
Deduplicated per entry; ``last_seen`` is re-stamped at most once per
``_MIN_UPDATE_INTERVAL_S`` (count still increments); at most
``_MAX_ENTRIES`` sessions, further sessions only bump ``overflow``.

HOW IT IS SHOWN. ``notice_for_session`` is the fixed warning appended to
every UserPromptSubmit additionalContext for that session while the flag is
pending (hook_pipeline._ups_stewardship_degraded_notice) -- the surface the
host shows the agent.

HOW IT CLEARS. ``flush_pending`` writes each flag as a durable
``stewardship_degraded_unrecorded`` audit event through the first writer that
works again (DUTY 2 calls it on a Stop whose stores are healthy) and clears
only the entries whose write returned an event id.

WHAT IT CANNOT DO. A broker restart, deploy or generation flip starts a new
process: pending flags are LOST, honestly. There is no persistence claim.
No sidecar file is written: a fixed-path sidecar would only ever be a hint
(on a full or locked disk it may not persist) and is never proof, so it was
left out rather than shipped as something a reader might trust.

GUARDRAIL. ``session_seal_is_unverified`` is the answer for any downstream
consumer: an availability-exception seal is never proof of a clean turn and
authorizes nothing (no privileged write, merge, deploy or test pass).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

FLAG_NAME = "stewardship_degraded_unrecorded"

#: last_seen is re-stamped at most this often per entry (count always bumps).
_MIN_UPDATE_INTERVAL_S = 60.0
#: distinct (project, session) entries held; more only bump the overflow counter.
_MAX_ENTRIES = 64

_lock = threading.Lock()
_entries: dict[str, dict[str, object]] = {}
_overflow = 0


def _reset_process_state() -> None:
    """What a fresh process starts with (tests; a restart does the same)."""
    global _overflow
    with _lock:
        _entries.clear()
        _overflow = 0


def project_key(project_root: object) -> str:
    """Canonical project identity (round 8). The warm broker multiplexes
    project roots, so a flag belongs to ONE project. The absolute, normalized
    path with trailing separators stripped, so two spellings of one root
    (``x/``, ``x/.``, ``x/sub/..``) are one project.

    CASE IS FOLDED ONLY BY ``os.path.normcase`` (round 9): it lower-cases on
    Windows, whose filesystem treats ``Demo`` and ``demo`` as one entry, and
    is a no-op on POSIX, where they are two distinct projects. An
    unconditional ``.lower()`` would merge two real POSIX roots back into one
    flag -- the cross-project misattribution round 8 removed."""
    raw = str(project_root or "").strip()
    if not raw:
        return ""
    try:
        raw = os.path.normcase(os.path.normpath(os.path.abspath(raw)))
    except Exception:
        pass
    return raw.rstrip("\\/") or raw


def entry_key(project_root: object, session: str) -> str:
    sid = str(session or "").strip() or "_unknown_session"
    return f"{project_key(project_root)}\x00{sid}"


def note_degraded_unrecorded(
    project_root: object,
    session: str,
    error_type: str,
    *,
    in_long_lived: bool,
    now: float | None = None,
) -> str:
    """Record the flag for ONE project + host session. Returns ``recorded``
    (new), ``deduped`` (existing, re-stamped), ``rate_limited`` (existing,
    counted only), ``overflow``, ``not_recorded_no_project`` or
    ``not_recorded_no_long_lived_process``."""
    global _overflow
    if not in_long_lived:
        return "not_recorded_no_long_lived_process"
    pkey = project_key(project_root)
    if not pkey:
        return "not_recorded_no_project"
    ts = float(now if now is not None else time.time())
    sid = str(session or "").strip() or "_unknown_session"
    key = entry_key(project_root, sid)
    etype = str(error_type or "").strip()[:80] or "unknown"
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            if len(_entries) >= _MAX_ENTRIES:
                _overflow += 1
                return "overflow"
            _entries[key] = {
                "first_seen": ts,
                "last_seen": ts,
                "project": pkey,
                "session": sid,
                "error_type": etype,
                "count": 1,
            }
            return "recorded"
        entry["count"] = int(entry["count"]) + 1  # type: ignore[call-overload]
        if ts - float(entry["last_seen"]) < _MIN_UPDATE_INTERVAL_S:  # type: ignore[arg-type]
            return "rate_limited"
        entry["last_seen"] = ts
        entry["error_type"] = etype
        return "deduped"


def snapshot() -> dict[str, object]:
    with _lock:
        return {
            "flag": FLAG_NAME,
            "entries": {k: dict(v) for k, v in _entries.items()},
            "overflow": _overflow,
        }


def session_seal_is_unverified(project_root: object, session: str) -> bool:
    with _lock:
        return entry_key(project_root, session) in _entries


def session_seal_is_unverified_anywhere(project_root: object, session: str) -> bool | None:
    """``session_seal_is_unverified`` for a caller in ANY process. In the
    broker it answers from memory; elsewhere (the MCP daemon) it asks the
    broker over its existing loopback status request. True / False, or None
    when the broker cannot answer -- unknown, never "clean"."""
    if session_seal_is_unverified(project_root, session):
        return True
    try:
        from .hook_broker_client import fetch_broker_timings

        reply = fetch_broker_timings(include_rows=False)
    except Exception:
        return None
    if not isinstance(reply, dict) or reply.get("available") is not True:
        return None
    flag = reply.get(FLAG_NAME)
    entries = flag.get("entries") if isinstance(flag, dict) else None
    if not isinstance(entries, dict):
        return None
    return entry_key(project_root, session) in entries


def notice_for_session(project_root: object, session: str) -> str:
    """The fixed warning for this project + session while its flag is
    pending, else ""."""
    with _lock:
        entry = _entries.get(entry_key(project_root, session))
        if entry is None:
            return ""
        count, etype = int(entry["count"]), str(entry["error_type"])  # type: ignore[call-overload]
    return (
        f"⚠️ AIDOCS {FLAG_NAME}: {count} turn(s) of this session sealed as an "
        f"operator-accepted AVAILABILITY EXCEPTION -- failure stewardship was "
        f"NOT verified ({etype}) and no debt store accepted the record. Those "
        "seals are not proof of a clean turn and authorize nothing (no merge, "
        "deploy, privileged write or test pass). Tell the operator; this notice "
        "stays until a working audit writer records it."
    )


def flush_pending(project_root: object, writer: Callable[[dict[str, object]], object]) -> int:
    """Write each pending flag OF THIS PROJECT through ``writer`` (which
    writes into this project's own store and returns an event id or raises);
    other projects' flags are never touched -- no opportunistic flush into
    the caller's store. COMPARE-AND-CLEAR on ``count``: a bump that lands
    while the write is in flight is not cleared -- the entry stays with the
    unwritten remainder as its count. Returns how many entries had a
    confirmed write."""
    pkey = project_key(project_root)
    if not pkey:
        return 0
    with _lock:
        pending = [(k, dict(v)) for k, v in _entries.items() if v.get("project") == pkey]
    cleared = 0
    for key, entry in pending:
        try:
            event_id = writer(entry)
        except Exception:
            continue
        if not event_id:
            continue
        written = int(entry["count"])  # type: ignore[call-overload]
        with _lock:
            current = _entries.get(key)
            if current is not None:
                remaining = int(current["count"]) - written  # type: ignore[call-overload]
                if remaining <= 0:
                    _entries.pop(key, None)
                else:
                    current["count"] = remaining
                    current["first_seen"] = entry["last_seen"]
        cleared += 1
    return cleared
