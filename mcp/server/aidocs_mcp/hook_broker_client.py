"""Hook broker reference client (#332, #335 Phase 3) — the thin side.

This is the client the thin ``claude_hook`` calls instead of importing the
whole gate stack: ask the resident broker (hosted by the watchdog; see
hook_broker.py) to evaluate the hook event, then apply the event-specific
fallback when the broker cannot answer.

CONTRACT — SECURITY FLOOR (test-pinned in tests/host/test_hook_broker.py;
non-negotiable):

  * Returns ``{"response": <hook JSON dict | None>}`` ONLY for a TRUSTED
    broker answer. ``{"response": None}`` is a real verdict ("hook has no
    output; proceed") — distinct from failure.
  * Returns ``None`` on ANY failure: no state file, dead port, connect or
    read timeout, oversized/malformed reply, ``ok: false``, protocol
    version mismatch, or an echo that does not match the session/root the
    question was about.
  * ``None`` means the caller must apply the event's explicit fallback.
    Enforcement-bearing hooks (PreToolUse and peers) evaluate locally and
    never fail open. UserPromptSubmit emits a labeled sqlite-only grounding
    block instead of cold-loading the advisory UPS pipeline; ``/aidocs`` keeps
    local evaluation so bootstrap works before a daemon exists.
  * Only ever dials 127.0.0.1 (the state file carries a port, never a
    host), so a tampered state file cannot exfiltrate hook payloads.
  * Replies are trusted only for the session/root they were asked about:
    the broker echoes ``payload.session_id`` and the requested
    ``project_root``; any mismatch discards the reply.

DIAGNOSTICS (#504) — additive, never floor-relaxing:

  * ``evaluate_via_broker_with_reason`` returns ``(result, reason)``: the
    SAME result as ``evaluate_via_broker`` plus a ``REASON_*`` name that says
    WHICH failure produced the ``None``. ``evaluate_via_broker`` is a thin
    wrapper over it and its contract above is unchanged.
  * ``build_degraded_user_prompt_response(payload, reason=...)`` NAMES that
    reason. Previously all failures printed one sentence, so a reachable-but-
    slow broker was indistinguishable from a missing daemon or a tampered
    registration — the banner actively misdirected operators.
  * ``session_mismatch`` / ``root_mismatch`` are integrity events, not
    availability events: they escalate with a durable audit record
    (``.MEMORY/audit/hook_broker_untrusted.jsonl``) and a stderr operator
    notice. Escalation is wrapped so it can never break the discard itself.

Budget: ~50ms connect, ~2s total — a hung broker must cost less than the
cold interpreter spawn it replaces. Both numbers come from
:mod:`hook_budget`; they used to be written here AND in hook_broker, which
is how the client's 2s drifted from the broker's 5s (#489).

Pure stdlib at module scope, with ONE deliberate exception: ``hook_budget``,
which is itself import-free (it imports ``time`` and nothing else) and
exists precisely so the deadline cannot be written down twice. The
invariant that matters is unchanged — importing THIS module must not drag in
the gate stack, so nothing heavier may ever be added here.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import time
from pathlib import Path

from . import hook_budget as _hook_budget
from ._sqlite_connect import connect as _canonical_connect

_PROTOCOL_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_ACTIONABLE_STATUSES = ("open", "in_progress", "blocked")
_PRIORITY_ORDER = ("critical", "urgent", "high", "normal", "low", "idea")

# ── #504: named failure reasons ──────────────────────────────────────────
# Every ``None`` from the broker client now travels with a reason. The
# SECURITY FLOOR is untouched: ``None`` still selects the caller's explicit
# fallback and is never an implicit allow. The reason is additive
# diagnostics so ten distinct failures stop reporting one sentence.
REASON_NO_REGISTRATION = "no_registration"
REASON_BAD_PROTOCOL = "bad_protocol"
REASON_CONNECT_REFUSED = "connect_refused"
REASON_CONNECTION_CLOSED = "connection_closed"
REASON_TIMED_OUT = "timed_out"
REASON_OVERSIZE = "oversize"
REASON_NOT_JSON = "not_json"
REASON_BROKER_ERROR = "broker_error"
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_ROOT_MISMATCH = "root_mismatch"
#: #502: the registration exists but its custody cannot be proven — it (or
#: the directory holding it) is a redirect, or is writable by a principal
#: other than its owner. Whoever can write it chooses the port AND the
#: bearer, so the rendezvous is NOT dialled: the payload would otherwise go
#: to a listener of the writer's choosing, and an "ok, proceed" answer from
#: it would read as a real verdict.
REASON_UNTRUSTED_REGISTRATION = "untrusted_registration"

#: Reasons that mean discovery or the answer could not be TRUSTED — the
#: broker answered about a DIFFERENT session or root, or the registration
#: itself was unverifiable. These are integrity events, not availability
#: events — they escalate loudly instead of degrading quietly.
UNTRUSTED_REPLY_REASONS = frozenset(
    {
        REASON_SESSION_MISMATCH,
        REASON_ROOT_MISMATCH,
        REASON_UNTRUSTED_REGISTRATION,
    }
)

UNTRUSTED_AUDIT_RELPATH = ".MEMORY/audit/hook_broker_untrusted.jsonl"

#: How an integrity audit's own location was CHOSEN (#607). Recorded with the
#: event, because a security record that cannot say where it belongs is how the
#: banner ended up naming a file that was not there.
AUDIT_BASIS_PROJECT = "commissioned_project_root"
AUDIT_BASIS_EMPIRE_HOME = "empire_home_fallback"
AUDIT_BASIS_UNRESOLVED = "unresolved"


def _empire_audit_fallback() -> Path | None:
    """``<empire home>/audit/hook_broker_untrusted.jsonl``.

    Derived from the ONE owner of the empire-home path (``empire_db_path``), so
    this is not a rival resolver and honours ``AIDOCS_EMPIRE_DB``.
    """
    try:
        from .empire_audit_store import empire_db_path

        return empire_db_path().parent / "audit" / "hook_broker_untrusted.jsonl"
    except Exception:  # noqa: BLE001 — an unresolvable sink is REPORTED, below
        return None


def _resolve_audit_sink(cwd: object) -> tuple[Path | None, str]:
    """Where this event's audit record belongs, and on what BASIS.

    #607: this used to be a raw join — ``.MEMORY/audit/...`` onto the payload's
    ``cwd``, followed by ``mkdir(parents=True)``. Nothing resolved anything, so
    a cwd that had drifted (a shell one-liner's ``cd mcp/server`` persists
    between tool calls) minted a whole stray ``.MEMORY`` tree three levels
    inside the real project and wrote SECURITY EVIDENCE into it, while the
    banner told the operator to inspect a repo-root path that did not exist.

    Resolve, never adopt: walk UP to the nearest COMMISSIONED AIDOCS root via
    ``find_aidocs_project_root`` — the single owner of that signal, and the
    same rule ``project_scope._validate`` already applies to a DECLARED root.
    Only such a root ever gets a ``.MEMORY`` tree created here.

    With no commissioned ancestor, both remaining wrong answers are refused:
    adopting an arbitrary directory, and dropping the record (a vanished
    security audit is worse than a misplaced one). The evidence goes to the
    empire-home sink and the basis SAYS SO, so "no project root was resolved"
    is a representable third state rather than a silent success.
    """
    raw = str(cwd or "").strip()
    if raw:
        root = None
        try:
            from .mcp_server_runtime_helpers import find_aidocs_project_root

            root = find_aidocs_project_root(Path(raw).expanduser())
        except Exception:  # noqa: BLE001 — no resolver ⇒ unresolved, not cwd
            root = None
        if root is not None:
            return (
                root.joinpath(*UNTRUSTED_AUDIT_RELPATH.split("/")),
                AUDIT_BASIS_PROJECT,
            )
    fallback = _empire_audit_fallback()
    if fallback is not None:
        return (fallback, AUDIT_BASIS_EMPIRE_HOME)
    return (None, AUDIT_BASIS_UNRESOLVED)


def _audit_location(event: dict) -> str:
    """The stamped sink as an operator-readable phrase — never a blank.

    An unwritable sink SAYS SO; it is not rendered as an empty path, which a
    reader would parse as "somewhere I failed to notice".
    """
    return str(event.get("audit_path") or "").strip() or (
        "NOT WRITTEN — no audit sink could be resolved (basis="
        f"{event.get('audit_basis') or AUDIT_BASIS_UNRESOLVED})"
    )


def _append_untrusted_audit(event: dict, cwd: object) -> str:
    """SINGLE writer for every untrusted-* audit record (#607).

    Stamps the resolved sink and its basis INTO the event before serializing,
    so the record states where it lives and why — and the caller's operator
    banner names that exact path instead of a relpath that may lead nowhere
    (law 311bf3e6: a named remedy must be where it says it is).
    """
    sink, basis = _resolve_audit_sink(cwd)
    event["audit_basis"] = basis
    event["audit_path"] = str(sink) if sink is not None else ""
    line = json.dumps(event, ensure_ascii=False)
    if sink is not None:
        sink.parent.mkdir(parents=True, exist_ok=True)
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return line


_DEFAULT_RECOVERY = (
    "Recovery: restore the local AIDOCS daemon; this turn may have "
    "reduced context."
)
_REASON_RECOVERY = {
    REASON_NO_REGISTRATION: (
        "Recovery: no usable broker registration was found — start or "
        "restart the local AIDOCS daemon."
    ),
    REASON_BAD_PROTOCOL: (
        "Recovery: broker/client protocol mismatch — the running daemon is "
        "a different AIDOCS build; restart it from this checkout."
    ),
    REASON_CONNECT_REFUSED: (
        "Recovery: the registration names a port nothing is listening on — "
        "the daemon died or the state file is stale; restart the daemon."
    ),
    REASON_CONNECTION_CLOSED: (
        "Recovery: the broker accepted the connection then hung up without "
        "answering — check the daemon log for a crash mid-evaluation."
    ),
    REASON_TIMED_OUT: (
        "Recovery: the broker WAS reachable but did not answer inside the "
        "client budget — it is alive and slow, not missing (see #489)."
    ),
    REASON_UNTRUSTED_REGISTRATION: (
        "Recovery: treat this host as compromised until proven otherwise. "
        "Stop the daemon, inspect who can write the broker registration "
        "directory, remove every write grant that is not the owner's, then "
        "restart the daemon so it re-registers under a hardened tree."
    ),
    REASON_OVERSIZE: (
        "Recovery: the reply exceeded the client's size cap — inspect what "
        "the broker returns for this event."
    ),
    REASON_NOT_JSON: (
        "Recovery: the reply was not valid JSON in the expected shape — "
        "something other than the broker may hold that port."
    ),
    REASON_BROKER_ERROR: (
        "Recovery: the broker answered ok=false — check the daemon log for "
        "the evaluation error."
    ),
}


def _bounded_line(value: object, max_chars: int = 500) -> str:
    """Collapse untrusted stored text to one bounded line for hook output."""
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _find_project_db(cwd: object) -> Path | None:
    """Find the nearest project index DB without importing project services."""
    raw = str(cwd or "").strip()
    if not raw:
        return None
    try:
        current = Path(raw).expanduser()
    except Exception:
        return None
    if current.is_file():
        current = current.parent
    for root in (current, *current.parents):
        candidate = root / ".MEMORY" / ".index" / "aidocs.sqlite3"
        if candidate.is_file():
            return candidate
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _resolve_bound_session_id(
    conn: sqlite3.Connection,
    host_session_id: str,
) -> str:
    """Resolve the host actor's AIDOCS session from canonical SQLite state."""
    if host_session_id:
        columns = _table_columns(conn, "aidocs_managed_per_conductor")
        host_column = (
            "host_session_id"
            if "host_session_id" in columns
            else "cli_session_id"
            if "cli_session_id" in columns
            else ""
        )
        if host_column and "session_id" in columns:
            try:
                order_clause = (
                    " ORDER BY last_updated DESC" if "last_updated" in columns else ""
                )
                row = conn.execute(
                    f"SELECT session_id FROM aidocs_managed_per_conductor "
                    f"WHERE {host_column} = ?{order_clause} LIMIT 1",
                    (host_session_id,),
                ).fetchone()
                if row and str(row[0] or "").strip():
                    return str(row[0]).strip()
            except sqlite3.Error:
                pass

        columns = _table_columns(conn, "session_query_gate")
        if {"session_id", "last_host_session_id"}.issubset(columns):
            try:
                order_clause = (
                    " ORDER BY updated_at DESC" if "updated_at" in columns else ""
                )
                row = conn.execute(
                    "SELECT session_id FROM session_query_gate "
                    f"WHERE last_host_session_id = ?{order_clause} LIMIT 1",
                    (host_session_id,),
                ).fetchone()
                if row and str(row[0] or "").strip():
                    return str(row[0]).strip()
            except sqlite3.Error:
                pass

    columns = _table_columns(conn, "aidocs_managed")
    if {"active", "session_id"}.issubset(columns):
        try:
            row = conn.execute(
                "SELECT session_id FROM aidocs_managed "
                "WHERE id = 1 AND active = 1 LIMIT 1"
            ).fetchone()
            if row and str(row[0] or "").strip():
                return str(row[0]).strip()
        except sqlite3.Error:
            pass
    return ""


def _read_active_task_goal(
    conn: sqlite3.Connection,
    session_id: str,
) -> str:
    if not session_id:
        return ""
    columns = _table_columns(conn, "tasks")
    if not {"session_id", "goal", "status"}.issubset(columns):
        return ""
    try:
        order_clause = (
            " ORDER BY created_at DESC, rowid DESC"
            if "created_at" in columns
            else " ORDER BY rowid DESC"
        )
        row = conn.execute(
            "SELECT goal FROM tasks "
            f"WHERE session_id = ? AND status = 'active'{order_clause} LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return _bounded_line(row[0]) if row else ""


def _read_actionable_backlog_counts(
    conn: sqlite3.Connection,
) -> tuple[int, dict[str, int]]:
    columns = _table_columns(conn, "project_backlog")
    if not {"status", "priority"}.issubset(columns):
        return 0, {}
    placeholders = ",".join("?" for _ in _ACTIONABLE_STATUSES)
    try:
        rows = conn.execute(
            "SELECT priority, COUNT(*) FROM project_backlog "
            f"WHERE status IN ({placeholders}) GROUP BY priority",
            _ACTIONABLE_STATUSES,
        ).fetchall()
    except sqlite3.Error:
        return 0, {}
    counts = {str(priority): int(count) for priority, count in rows}
    return sum(counts.values()), counts


def _stamp_line(received_at_ms: object, elapsed_ms: object) -> str:
    """"When did this prompt reach AIDOCS, and how long did we wait?" (#489)

    Returns "" when neither is known, so a caller with no timing info gets a
    banner with no stamp rather than the word None. A missing measurement must
    look missing, never like a measured zero.
    """
    parts: list[str] = []
    try:
        if received_at_ms is not None:
            stamp = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(int(received_at_ms) / 1000.0)
            )
            millis = int(received_at_ms) % 1000
            parts.append(f"hit AIDOCS {stamp}.{millis:03d}Z")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        if elapsed_ms is not None:
            parts.append(f"degraded after {int(float(elapsed_ms))}ms")
    except (TypeError, ValueError):
        pass
    return f" [{'; '.join(parts)}]" if parts else ""


def build_degraded_user_prompt_response(
    payload: dict,
    *,
    reason: str | None = None,
    received_at_ms: int | None = None,
    elapsed_ms: float | None = None,
) -> dict:
    """Return a fast, labeled UPS context when the broker did not answer.

    ``reason`` is the named failure from :func:`evaluate_via_broker_with_reason`
    and is NAMED in the banner (#504). Ten distinct failures used to print the
    identical sentence "warm hook broker unavailable", which actively
    misdirected operators: a reachable-but-slow broker read as a missing one.
    Callers that do not have a reason get an explicitly *unreported* banner —
    never an invented cause.

    This path is intentionally SQLite-only and stdlib-only: no NLP, palace,
    runtime, or handler imports. It preserves useful grounding without paying
    the cold full-pipeline cost that can exceed the host's 30-second hook budget.
    """
    db_path = _find_project_db(payload.get("cwd"))
    session_id = ""
    active_goal = ""
    total = 0
    counts: dict[str, int] = {}
    db_readable = False
    if db_path is not None:
        try:
            # Canonical connect (#755), read-only: this is the DEGRADED path
            # that runs when the broker did not answer, so it must be the
            # cheapest and least destructive read in the tree -- it may not
            # create a database, and (with the closing factory) it may not
            # leak the handle it opened while everything else is already
            # going wrong. row_factory=False keeps the plain tuples the
            # readers below index positionally.
            with _canonical_connect(
                db_path, timeout=0.2, read_only=True, row_factory=False
            ) as conn:
                db_readable = True
                session_id = _resolve_bound_session_id(
                    conn, str(payload.get("session_id") or "").strip()
                )
                active_goal = _read_active_task_goal(conn, session_id)
                total, counts = _read_actionable_backlog_counts(conn)
        except sqlite3.Error:
            pass

    count_parts = [
        f"{counts[priority]} {priority}"
        for priority in _PRIORITY_ORDER
        if counts.get(priority)
    ]
    backlog_line = (
        f"Backlog: {total} actionable"
        + (f" — {', '.join(count_parts)}." if count_parts else ".")
        if db_readable
        else "Backlog: unavailable (project index DB missing or unreadable)."
    )
    task_line = (
        f"Active task: {active_goal}"
        if active_goal
        else "Active task: none resolved for this host session."
    )
    # A reason may be COMPOSITE — "<code>: <the refusing service's own words>".
    # The headline keeps the short code so the banner stays scannable; the detail
    # goes to the recovery line, which is where an operator looks for what to DO.
    _raw_reason = reason or "unreported"
    _reason_code, _, _reason_detail = _raw_reason.partition(": ")
    _reason_code = _reason_code.strip() or "unreported"
    _reason_detail = _reason_detail.strip()
    named = _reason_code
    # #607 / law 311bf3e6: name the path the audit ACTUALLY lands in. The bare
    # relpath below used to be resolved by the reader against the repo root —
    # precisely where the file was not.
    audit_sink, audit_basis = _resolve_audit_sink(payload.get("cwd"))
    audit_where = (
        str(audit_sink)
        if audit_sink is not None
        else f"NOT WRITTEN — no audit sink could be resolved (basis={audit_basis})"
    )
    if reason == REASON_UNTRUSTED_REGISTRATION:
        # #502: a custody failure is NOT an outage. The availability banner
        # would have told the operator to restart the daemon, which on a
        # tampered tree just re-registers under the same open rendezvous.
        header = [
            (
                "🚨 AIDOCS SECURITY — hook broker registration REFUSED "
                f"({named}){_stamp_line(received_at_ms, elapsed_ms)}."
            ),
            (
                "The file that names the broker's port and bearer is writable "
                "by a principal that is not its owner, so it was NOT dialled "
                "and no prompt was sent to it. Full NLP, memory, doctrine and "
                "palace surfacing were skipped for this prompt; runtime tool "
                "gates remain authoritative and still evaluate locally. This "
                f"is an audited integrity event: inspect {audit_where}."
            ),
        ]
        recovery = _REASON_RECOVERY[REASON_UNTRUSTED_REGISTRATION]
    elif reason in UNTRUSTED_REPLY_REASONS:
        header = [
            (
                f"🚨 AIDOCS SECURITY — hook broker reply DISCARDED ({named})"
                f"{_stamp_line(received_at_ms, elapsed_ms)}."
            ),
            (
                "The broker answered about a DIFFERENT session or project root "
                "than the one asked about; the client discarded the reply and "
                "evaluated locally. This is an audited integrity event, not an "
                f"outage: inspect {audit_where} and the broker "
                "registration before trusting this host again."
            ),
        ]
        recovery = (
            "Recovery: treat the daemon as untrusted — stop it, verify the "
            "broker registration, and restart it before relying on warm hooks."
        )
    else:
        header = [
            ("⚠️ AIDOCS UPS DEGRADED — warm hook broker did not answer "
            f"(reason: {named}){_stamp_line(received_at_ms, elapsed_ms)}."),
            (
                "Full NLP, memory, doctrine, and palace surfacing were skipped "
                "for this prompt; runtime tool gates remain authoritative."
            ),
        ]
        # The refusing service's own words WIN over our generic hint: it knows
        # why it refused and usually names the remedy, and overwriting that with
        # "check the daemon log" destroys the only actionable thing we were given.
        recovery = (
            f"Recovery: {_reason_detail}"
            if _reason_detail
            else _REASON_RECOVERY.get(_reason_code, _DEFAULT_RECOVERY)
        )

    context = "\n".join([*header, task_line, backlog_line, recovery])
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def _classify_exception(exc: BaseException) -> str:
    """Map an unexpected exception to a named reason (#504).

    Only reached on a failure path, so it costs the hot path nothing (#332).
    """
    if isinstance(exc, TimeoutError):  # socket.timeout is a TimeoutError
        return REASON_TIMED_OUT
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, KeyError)):
        return REASON_NO_REGISTRATION
    if isinstance(exc, json.JSONDecodeError):
        return REASON_NOT_JSON
    if isinstance(exc, OSError):
        # ConnectionRefused/Reset/Aborted and friends: the registration
        # exists but nothing usable is behind it.
        return REASON_CONNECT_REFUSED
    return f"exception:{type(exc).__name__}"


def _escalate_untrusted_reply(
    reason: str,
    payload: dict,
    project_root: str,
    reply: dict,
) -> None:
    """Loudly record a broker reply that was about someone else (#504).

    ``session_mismatch`` / ``root_mismatch`` mean the broker answered about a
    different session or project root and the client discarded the answer.
    That is an integrity event, not an outage, so it gets a durable audit
    record plus a stderr operator notice — never a quiet degrade line.
    """
    event = {
        "event": "hook_broker.untrusted_reply",
        "reason": reason,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_session_id": str(payload.get("session_id") or ""),
        "requested_project_root": project_root,
        "reply_session_id": _bounded_line(reply.get("session_id"), 200),
        "reply_project_root": _bounded_line(reply.get("project_root"), 500),
        "hook_event_name": _bounded_line(payload.get("hook_event_name"), 100),
        "discarded": True,
    }
    line = _append_untrusted_audit(event, project_root)

    import sys

    print(
        "🚨 AIDOCS SECURITY — UNTRUSTED hook broker reply DISCARDED "
        f"({reason}). The broker answered about a different session/root than "
        "the one asked about; the client fell back to local evaluation. "
        f"Audit record: {_audit_location(event)}. "
        f"audit={line}",
        file=sys.stderr,
        flush=True,
    )


def _discard_untrusted(
    reason: str, payload: dict, project_root: str, reply: dict
) -> str:
    """Escalate, then hand back the reason. Escalation can never break the floor."""
    try:
        _escalate_untrusted_reply(reason, payload, project_root, reply)
    except Exception:  # noqa: BLE001 — diagnostics must never fail the discard
        pass
    return reason


def _registration_custody(state_path) -> tuple[bool, str]:
    """Custody verdict for the registration, as the CLIENT needs it (#502).

    Differs from :func:`hook_broker.registration_custody_ok` in exactly one
    way, and deliberately: an ABSENT registration is not a custody failure.
    A host with no daemon running is the ordinary case and must keep
    reporting ``no_registration`` — crying "security" at every un-started
    daemon would train operators to ignore the one banner that matters.
    """
    try:
        if not Path(state_path).exists():
            return (True, "absent — handled as no_registration")
        from .hook_broker import registration_custody_ok

        return registration_custody_ok(state_path)
    except Exception as exc:  # noqa: BLE001 — unprovable custody is refused
        return (False, f"custody check failed ({type(exc).__name__})")


def _escalate_untrusted_registration(
    payload: dict, state_path, detail: str
) -> None:
    """Loudly record a registration whose custody could not be proven (#502).

    Same durable audit sink and stderr channel as an untrusted REPLY: both
    are integrity events. The registration's contents are never read here,
    so nothing that file claims — least of all its bearer — can reach the
    audit record.
    """
    import sys

    event = {
        "event": "hook_broker.untrusted_registration",
        "reason": REASON_UNTRUSTED_REGISTRATION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_session_id": str(payload.get("session_id") or ""),
        "hook_event_name": _bounded_line(payload.get("hook_event_name"), 100),
        "registration_dir": _bounded_line(Path(state_path).parent, 500),
        "custody_detail": _bounded_line(detail, 300),
        "discarded": True,
    }
    line = _append_untrusted_audit(event, payload.get("cwd"))

    print(
        "🚨 AIDOCS SECURITY — UNTRUSTED hook broker registration REFUSED "
        f"({detail}). The rendezvous naming the broker's port and bearer is "
        "writable by a principal that is not its owner, so it was NOT "
        "dialled and no hook payload was sent to it. The hook fell back to "
        "local evaluation; enforcement still holds. "
        f"Audit record: {_audit_location(event)}. "
        f"audit={line}",
        file=sys.stderr,
        flush=True,
    )


def _discard_untrusted_registration(
    payload: dict, state_path, detail: str
) -> str:
    """Escalate, then hand back the reason. Escalation can never break the floor."""
    try:
        _escalate_untrusted_registration(payload, state_path, detail)
    except Exception:  # noqa: BLE001 — diagnostics must never fail the refusal
        pass
    return REASON_UNTRUSTED_REGISTRATION


def evaluate_via_broker_with_reason(
    payload: dict,
    *,
    state_path: Path | None = None,
    connect_timeout: float = _hook_budget.HOOK_CONNECT_TIMEOUT_S,
    total_timeout: float = _hook_budget.HOOK_ROUNDTRIP_BUDGET_S,
    env: dict | None = None,
) -> tuple[dict | None, str | None]:
    """Like :func:`evaluate_via_broker`, but also NAMES the failure (#504).

    Returns ``(result, reason)`` where ``result`` is exactly what
    :func:`evaluate_via_broker` returns — ``{"response": ...}`` for a trusted
    answer, ``None`` for ANY failure — and ``reason`` is ``None`` on success or
    one of the ``REASON_*`` names otherwise. The reason is purely additive
    diagnostics: it does NOT relax the security floor, and ``None`` still
    selects the caller's explicit fallback rather than an implicit allow.
    """
    deadline = time.monotonic() + max(total_timeout, 0.01)
    try:
        # ── discovery ────────────────────────────────────────────────
        if state_path is None:
            from .hook_broker import broker_state_path  # tiny, lazy

            state_path = broker_state_path()
        # #502: CUSTODY BEFORE CONTENT. The registration names the port and
        # the bearer, so reading it on trust hands the choice of listener to
        # whoever can write it — and the echo checks below cannot catch a
        # rogue, which echoes by construction. Prove custody first, and on
        # failure do not dial at all: the payload (an operator prompt, on
        # UserPromptSubmit) must never reach an unverified rendezvous.
        ok, detail = _registration_custody(state_path)
        if not ok:
            return None, _discard_untrusted_registration(
                payload, state_path, detail
            )
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if state.get("v") != _PROTOCOL_VERSION:
            return None, REASON_BAD_PROTOCOL
        port = int(state["port"])
        token = str(state.get("token") or "")
        if not (0 < port < 65536) or not token:
            return None, REASON_NO_REGISTRATION

        project_root = str(payload.get("cwd") or "").strip()
        request = {
            "v": _PROTOCOL_VERSION,
            "kind": "hook_eval",
            "token": token,
            "payload": payload,
            "project_root": project_root,
            # #489: tell the broker WHEN we asked and HOW LONG we will wait, so
            # it can drop a request whose caller has already given up instead of
            # holding the serialized evaluation lock for a dead client. Wall
            # clock, not monotonic — it is compared across processes.
            "sent_at_ms": _hook_budget.now_ms(),
            "client_budget_s": float(total_timeout),
        }
        if env:
            request["env"] = dict(env)

        # ── round trip (hard deadline throughout) ────────────────────
        # Connect is classified separately from the read: a refusal and a
        # connect timeout are indistinguishable on some platforms, and both
        # mean "nothing usable is behind the registration". Reserving
        # ``timed_out`` for the READ phase keeps it meaning what #489 needs
        # it to mean: the broker WAS reachable and did not answer in time.
        try:
            sock = socket.create_connection(
                ("127.0.0.1", port), timeout=max(connect_timeout, 0.001)
            )
        except OSError:
            return None, REASON_CONNECT_REFUSED
        with sock as conn:
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buf = b""
            closed = False
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, REASON_TIMED_OUT
                conn.settimeout(remaining)
                chunk = conn.recv(65536)
                if not chunk:
                    closed = True
                    break
                buf += chunk
                if len(buf) > _MAX_RESPONSE_BYTES:
                    return None, REASON_OVERSIZE
        if closed and b"\n" not in buf:
            return None, REASON_CONNECTION_CLOSED

        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        # ── trust checks ─────────────────────────────────────────────
        if not isinstance(reply, dict):
            return None, REASON_NOT_JSON
        if reply.get("v") != _PROTOCOL_VERSION:
            return None, REASON_BAD_PROTOCOL
        if reply.get("ok") is not True:
            # CARRY THE BROKER'S OWN WORDS (2026-08-03). The protocol has always
            # sent them — `{"v": 1, "ok": false, "error": "<reason>"}`,
            # hook_broker.py:49 — and this line used to drop them on the floor,
            # so every refusal reached the operator as the opaque "broker_error"
            # plus "check the daemon log for the evaluation error".
            #
            # MEASURED COST: the common case is not an evaluation error at all. It is
            # `broker_code_stale`, which is a DELIBERATE refusal that already
            # explains itself and names its remedy ("restart the AIDOCS service
            # so the watchdog re-imports"), and which fires after every edit to
            # the runtime package because a refresh restarts the daemon child but
            # not the watchdog hosting the broker (#609). An operator editing the
            # tree therefore saw a permanent, unexplained degradation banner
            # pointing at a log, for a condition whose one-line fix the broker had
            # already written down.
            detail = _bounded_line(reply.get("error"), 800)
            return None, (
                f"{REASON_BROKER_ERROR}: {detail}" if detail else REASON_BROKER_ERROR
            )
        if reply.get("session_id") != str(payload.get("session_id") or ""):
            # answer for someone else's session — discard AND escalate
            return None, _discard_untrusted(
                REASON_SESSION_MISMATCH, payload, project_root, reply
            )
        if reply.get("project_root") != project_root:
            # answer for a different root — discard AND escalate
            return None, _discard_untrusted(
                REASON_ROOT_MISMATCH, payload, project_root, reply
            )
        response = reply.get("response")
        if response is not None and not isinstance(response, dict):
            return None, REASON_BAD_PROTOCOL
        return {"response": response}, None
    except Exception as exc:  # noqa: BLE001 — ANY failure → local evaluation
        return None, _classify_exception(exc)


def shape_timings_reply(reply: dict, *, include_rows: bool) -> dict:
    """Shape the broker's timing reply for a caller.

    THE RING IS NOT A REPORT (2026-08-21). The 256 raw rows were ~38KB of the
    ~57KB ``aidocs service status`` payload — two thirds of what an operator
    reads to answer "is the daemon healthy". The summary beside them already
    aggregates exactly those rows (p50/p95/max per phase, ``late_by_event``,
    ``queue_share`` and a named ``verdict``), and ``summarize_timings``' own
    docstring says it exists so nobody has to "eyeball 256 rows and guess
    again". Shipping both buried the answer under the data it replaced.

    Split out as a pure function so the omission is testable WITHOUT a socket —
    a test that re-implemented this shaping would pin its own copy, not the
    shipped path.
    """
    summary = reply.get("summary") or {}
    rows = reply.get("timings") or []
    if include_rows:
        return {"available": True, "summary": summary, "timings": rows}
    return {
        "available": True,
        "summary": summary,
        "timings_omitted": len(rows),
        "timings_hint": (
            "raw ring rows omitted from this view; the summary above "
            "aggregates them. Call fetch_broker_timings(include_rows=True) "
            "for the rows themselves."
        ),
    }


def fetch_broker_timings(
    *,
    state_path: Path | None = None,
    connect_timeout: float = _hook_budget.HOOK_CONNECT_TIMEOUT_S,
    total_timeout: float = 1.0,
    include_rows: bool = True,
) -> dict:
    """Read the broker's in-memory timing ring across the process boundary (#489).

    THE MISSING READER. The ring lives in the WATCHDOG process; nothing in the
    daemon or the CLI could ever see it, which is why #489's own next step
    ("read queue_ms vs eval_ms") had never been executed and an earlier pass
    guessed instead. This asks the broker for it over the loopback socket it
    already owns.

    Returns ``{"available": True, "summary": {...}, "timings": [...]}`` or
    ``{"available": False, "reason": "<REASON_*>"}``. NEVER raises and never
    blocks meaningfully: a status call must not become an outage, and a broker
    that will not answer its own diagnostics is itself the finding.

    Read-only by construction — there is no request shape here that can cause an
    evaluation, so an operator inspecting the numbers cannot perturb them.
    """
    try:
        if state_path is None:
            from .hook_broker import broker_state_path

            state_path = broker_state_path()
        # #502: the diagnostics surface dials the same rendezvous, so it gets
        # the same custody proof. An operator reading the numbers must not be
        # the one path that still talks to an unverified listener.
        ok, detail = _registration_custody(state_path)
        if not ok:
            return {
                "available": False,
                "reason": _discard_untrusted_registration({}, state_path, detail),
            }
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if state.get("v") != _PROTOCOL_VERSION:
            return {"available": False, "reason": REASON_BAD_PROTOCOL}
        port = int(state["port"])
        token = str(state.get("token") or "")
        if not (0 < port < 65536) or not token:
            return {"available": False, "reason": REASON_NO_REGISTRATION}
        from .hook_broker import TIMINGS_KIND

        request = {"v": _PROTOCOL_VERSION, "kind": TIMINGS_KIND, "token": token}
        deadline = time.monotonic() + max(total_timeout, 0.05)
        try:
            sock = socket.create_connection(
                ("127.0.0.1", port), timeout=max(connect_timeout, 0.001)
            )
        except OSError:
            return {"available": False, "reason": REASON_CONNECT_REFUSED}
        with sock as conn:
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"available": False, "reason": REASON_TIMED_OUT}
                conn.settimeout(remaining)
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_RESPONSE_BYTES:
                    return {"available": False, "reason": REASON_OVERSIZE}
        if b"\n" not in buf:
            return {"available": False, "reason": REASON_CONNECTION_CLOSED}
        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            return {"available": False, "reason": REASON_BROKER_ERROR}
        return shape_timings_reply(reply, include_rows=include_rows)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never raise
        return {"available": False, "reason": _classify_exception(exc)}


def evaluate_via_broker(
    payload: dict,
    *,
    state_path: Path | None = None,
    connect_timeout: float = 0.05,
    total_timeout: float = 2.0,
    env: dict | None = None,
):
    """Try the resident broker; return ``{"response": ...}`` or ``None``.

    ``payload`` is the EXACT JSON the claude_hook subprocess reads on
    stdin. See the module docstring for the trust/floor contract — in
    short: ``None`` selects the caller's explicit event-specific fallback;
    it is never an implicit allow.

    This is the pinned floor signature and its semantics are unchanged.
    Callers that want to REPORT why the broker did not answer should use
    :func:`evaluate_via_broker_with_reason` (#504).
    """
    return evaluate_via_broker_with_reason(
        payload,
        state_path=state_path,
        connect_timeout=connect_timeout,
        total_timeout=total_timeout,
        env=env,
    )[0]
