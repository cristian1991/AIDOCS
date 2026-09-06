"""Gate liveness — AIDOCS must be able to say when AIDOCS is not running.

Three REAL silent-death incidents (2026-07, one day): the hook DECLINED
itself on phantom package drift (Claude Code treats a verdict-less hook as
"proceed" -> gate OFF on every tool call, announced only by one stderr line);
spaCy was dead for weeks (click pin conflict -> analyze() always None -> grant
grammar / destructive-intent / DNT detection all silently inert); rbac.py
failed open on an empty user table. One defect, not three bugs: the system
reported green while ungoverned.

This module is the server-side health signal. It is computed in the daemon
(the process the watchdog keeps alive) from cheap on-disk evidence:

  * hook_traffic — claude_hook.main() drops a PULSE file after every
    successful evaluation. The universal notification injector feeds an
    "MCP traffic" clock on every governed tool call. Active traffic with a
    missing/stale pulse = hooks are NOT firing -> DEGRADED. No traffic =
    idle -> normal, never an alarm (no crying wolf).
  * hook_declines — integrity refusals and hook crashes land in
    ``<daemon_dir>/hook_failures.log`` (claude_hook breadcrumbs). Recent
    entries are a FIRST-CLASS degraded signal, not a log nobody opens.
  * nlp — a cached probe that imports spacy and runs a real analyze()
    through the one-door NLPService. None/import-failure = DEGRADED.

STATUS LAW (test-pinned in mcp/tests/host/test_gate_health.py):
  degraded > unknown > ok. UNKNOWN means "could not determine" and is a
  WARNING — it must NEVER render as ok/green. A health signal that fails
  open manufactures false confidence, which is the exact disease this
  module exists to cure (empire law: truth before green).

WHAT THIS PROVES / DOES NOT PROVE:
  * A fresh pulse proves hook subprocesses are being spawned and reaching a
    verdict on THIS MACHINE (the pulse is user-global, like the hook
    install itself) — it does not prove any particular verdict was correct.
  * The nlp probe proves the NLP stack imports and analyzes in THIS daemon
    process (same installed environment the hook uses) — not that every
    consumer wired to it behaves.
  * Absence of declines proves the absence of RECORDED declines only.

Surfaces: the notification rail (notification_injector — degraded/unknown
blocks on the agent's next tool call; ok is silent) and the dashboard
snapshot (runtime_presentation_service -> SessionsPage GateHealthCard —
where green is displayed, and unknown renders amber, never green).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

PULSE_FILENAME = "hook_pulse.json"
DECLINE_LOG_FILENAME = "hook_failures.log"

# MCP tool call within this window == "active session traffic".
ACTIVE_TRAFFIC_WINDOW_S = 300.0
# Hooks fire BEFORE each tool call, so during active traffic a pulse should
# always be recent. But an alarm must require SUSTAINED silence, never an
# instantaneous one: a freshly started daemon, or the very first tool call of
# a session, has legitimately not seen a pulse yet. Below this grace we report
# "pending" (amber on the dashboard, SILENT on the rail); past it, continued
# silence during active traffic IS the real DEGRADED alarm. This is the
# no-crying-wolf floor, and it costs at most one grace window of detection
# delay on a genuinely dead hook.
HOOK_SILENCE_GRACE_S = 120.0
# A pulse this far behind the live traffic no longer counts as accompanying it
# (hooks fire before every call, so a healthy pulse is always recent).
HOOK_STALE_S = 120.0
# Declines / hook failures younger than this are a live degraded signal.
DECLINE_RECENT_WINDOW_S = 3600.0
# NLP probe cache: success is re-verified hourly, failure retried faster.
NLP_PROBE_OK_TTL_S = 3600.0
NLP_PROBE_BAD_TTL_S = 300.0
# A probe that has not settled within this long is no longer "warming up" —
# it is a probe we CANNOT TRUST. That is UNKNOWN (a warning), never a pass.
PROBE_PENDING_GRACE_S = 120.0

# "pending" = warming up: not verified YET, so it can never rank as ok (the
# dashboard shows amber), but it is not evidence of death either, so the rail
# stays quiet. Ranks with unknown for the overall status — never green.
_STATUS_RANK = {"ok": 0, "idle": 0, "pending": 1, "unknown": 1, "degraded": 2}
# Statuses that are NOT evidence of a problem — the rail never shouts about
# these (idle = quiet session; pending = still warming up). Everything else
# (unknown, degraded) reaches the agent on its next tool call.
_QUIET_STATUSES = frozenset({"ok", "idle", "pending"})

# ── module state (daemon-process-local; reset seam for tests) ────────────

_LAST_MCP_ACTIVITY_TS: float | None = None
# First tool call this daemon ever saw — the floor of the "how long have hooks
# been silent?" window. Without it, a daemon that starts mid-session would
# measure silence from epoch 0 and scream instantly.
_FIRST_MCP_ACTIVITY_TS: float | None = None
_NLP_LOCK = threading.Lock()
_NLP_RESULT: dict | None = None
_NLP_RESULT_TS: float = 0.0
_NLP_THREAD: threading.Thread | None = None
# When the first probe was kicked — a probe still unsettled long after this is
# no longer "warming up", it is untrustworthy, and untrustworthy is UNKNOWN.
_NLP_FIRST_KICK_TS: float = 0.0


def _reset_for_tests() -> None:
    global _LAST_MCP_ACTIVITY_TS, _FIRST_MCP_ACTIVITY_TS
    global _NLP_RESULT, _NLP_RESULT_TS, _NLP_THREAD, _NLP_FIRST_KICK_TS
    _LAST_MCP_ACTIVITY_TS = None
    _FIRST_MCP_ACTIVITY_TS = None
    _NLP_FIRST_KICK_TS = 0.0
    with _NLP_LOCK:
        _NLP_RESULT = None
        _NLP_RESULT_TS = 0.0
        _NLP_THREAD = None
    _reset_hook_notice_state()


def _resolve_state_dir(state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from .aidocs_service import daemon_dir  # stdlib-only module

    return daemon_dir()


# ── evidence writers (called from claude_hook / the injector) ────────────


def record_hook_pulse(
    *,
    event: str = "",
    session_id: str = "",
    state_dir: Path | None = None,
    clock=time.time,
) -> None:
    """Drop the liveness pulse after a SUCCESSFUL hook evaluation.

    Called by ``claude_hook.main()`` (both broker and local paths — main is
    the funnel). Never called on an integrity decline: a declining hook must
    not look alive. Best-effort by design — a failed pulse write must never
    break the hook (the observer must not be able to kill what it observes;
    the health reader treats staleness honestly anyway).
    """
    try:
        d = _resolve_state_dir(state_dir)
        payload = {
            "ts": float(clock()),
            "event": str(event or ""),
            "session_id": str(session_id or ""),
            "pid": os.getpid(),
        }
        # pid in the tmp name: concurrent hook subprocesses must not clobber
        # each other's tmp before the atomic replace.
        tmp = d / f"{PULSE_FILENAME}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(d / PULSE_FILENAME)
    except Exception:
        pass


def read_hook_pulse(state_dir: Path | None = None) -> dict | None:
    """Newest pulse, or None when no pulse has ever been recorded.

    Raises on a present-but-corrupt file so the caller can classify it as
    UNKNOWN (a corrupt pulse is not the same as "no hooks") — internal
    callers wrap it; test callers get the honest dict/None.
    """
    d = _resolve_state_dir(state_dir)
    p = d / PULSE_FILENAME
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"pulse file is not a JSON object: {type(data).__name__}")
    return data


def record_hook_decline(
    reason: str,
    *,
    state_dir: Path | None = None,
    clock=time.time,
) -> None:
    """Append a DECLINE breadcrumb to hook_failures.log.

    Same file `_report_hook_failure` uses (one evidence trail, one reader).
    A decline means the gate is OFF for that event while Claude Code
    proceeds — the single worst failure mode — so it must leave more than a
    stderr line. Never raises.
    """
    try:
        d = _resolve_state_dir(state_dir)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
        line = f"{stamp} claude_hook DECLINED (gate is OFF for this event): {reason}"
        with (d / DECLINE_LOG_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def record_mcp_activity(clock=time.time) -> None:
    """Feed the 'active traffic' clock. Called by the universal notification
    injector on every governed tool call, so the hook-silence probe can tell
    'active session, hooks not arriving' (the ALARM) from 'idle' (normal).

    Also stamps the FIRST activity this daemon ever saw: silence is measured
    from that floor, so a daemon that starts mid-session cannot mistake its own
    youth for a dead hook.

    MONOTONIC BY CONSTRUCTION: 'last' only ever moves forward, 'first' only
    ever moves backward. An out-of-order or skewed clock must never drag the
    traffic clock into the past — that would make live traffic look IDLE, and
    idle never alarms. A stale timestamp must not be able to buy silence from
    the alarm that watches it (a fail-OPEN hole; pinned by test).
    """
    global _LAST_MCP_ACTIVITY_TS, _FIRST_MCP_ACTIVITY_TS
    try:
        now = float(clock())
    except Exception:
        return
    if _LAST_MCP_ACTIVITY_TS is None or now > _LAST_MCP_ACTIVITY_TS:
        _LAST_MCP_ACTIVITY_TS = now
    if _FIRST_MCP_ACTIVITY_TS is None or now < _FIRST_MCP_ACTIVITY_TS:
        _FIRST_MCP_ACTIVITY_TS = now


def last_mcp_activity() -> float | None:
    return _LAST_MCP_ACTIVITY_TS


# ── probes ───────────────────────────────────────────────────────────────


def _hook_traffic_probe(now: float, state_dir: Path | None) -> dict:
    """Are hooks FIRING? Only meaningful while tool traffic is actually flowing.

    idle      — no recent MCP traffic. Normal. Never an alarm (no crying wolf).
    ok        — a hook pulse accompanies the live traffic.
    pending   — traffic is live and there is no fresh pulse YET, but the
                silence is still inside HOOK_SILENCE_GRACE_S (fresh daemon,
                first calls of a session). Amber on the dashboard, SILENT on
                the rail: not verified yet, but not evidence of death either.
    degraded  — SUSTAINED silence: traffic kept flowing past the grace with no
                hook pulse. THE ALARM — the gate is not seeing the tool calls
                it is supposed to be governing.
    unknown   — the pulse file exists but cannot be read. Never green.
    """
    last_mcp = _LAST_MCP_ACTIVITY_TS
    if last_mcp is None or (now - last_mcp) > ACTIVE_TRAFFIC_WINDOW_S:
        return {
            "status": "idle",
            "reason": "no recent MCP tool traffic observed by this daemon process",
            "last_mcp_at": last_mcp,
            "last_pulse_at": None,
        }
    try:
        pulse = read_hook_pulse(state_dir)
    except Exception as exc:
        return {
            "status": "unknown",
            "reason": f"hook pulse file unreadable: {type(exc).__name__}: {exc}",
            "last_mcp_at": last_mcp,
            "last_pulse_at": None,
        }
    pulse_ts = float((pulse or {}).get("ts") or 0.0)
    if pulse is not None and (last_mcp - pulse_ts) <= HOOK_STALE_S:
        return {
            "status": "ok",
            "reason": "",
            "last_mcp_at": last_mcp,
            "last_pulse_at": pulse_ts,
        }
    # No fresh pulse. Measure how long hooks have ACTUALLY been silent: from
    # the newer of (the last pulse we saw) and (the first traffic this daemon
    # ever saw). Both floors matter — the first stops an ancient pulse from
    # inflating the window, the second stops a young daemon from screaming
    # about a silence that simply predates it.
    silence_since = max(pulse_ts, _FIRST_MCP_ACTIVITY_TS or last_mcp)
    silent_for = last_mcp - silence_since
    if silent_for <= HOOK_SILENCE_GRACE_S:
        return {
            "status": "pending",
            "reason": (
                f"no hook pulse yet for this traffic ({int(max(0.0, silent_for))}s) — "
                f"still inside the {int(HOOK_SILENCE_GRACE_S)}s warm-up grace; "
                f"NOT yet verified"
            ),
            "last_mcp_at": last_mcp,
            "last_pulse_at": pulse_ts or None,
        }
    if pulse is None:
        reason = (
            f"MCP tool traffic has been flowing for {int(silent_for)}s and NO hook "
            f"pulse has EVER been recorded — hooks are not firing (or not reaching "
            f"a verdict); these tool calls are NOT being governed"
        )
    else:
        reason = (
            f"MCP tool traffic is active but the newest hook pulse is {int(silent_for)}s "
            f"behind it — hooks have STOPPED accompanying tool calls"
        )
    return {
        "status": "degraded",
        "reason": reason,
        "last_mcp_at": last_mcp,
        "last_pulse_at": pulse_ts or None,
    }


# ── #770(b): DENIED vs UNGOVERNED classification ──────────────────────────
#
# Three breadcrumb writers append to hook_failures.log:
#   * gate_health.record_hook_decline() — the package-drift path in
#     claude_hook.main(). Its `reason` text embeds the EXACT posture chosen
#     for the event (see claude_hook._DRIFT_BANNER_EVENTS / #589):
#       "... DENIED (not silently permitted)"        -> PreToolUse hard-refused
#       "... degraded LOUDLY — refusal surfaced ..."  -> UPS/SessionStart/
#                                                         PostToolUse: gate ran,
#                                                         refusal reached the
#                                                         model as context
#       "NO verdict shape exists for {event} ..."     -> Stop/SubagentStop/
#                                                         PostCompact/unknown:
#                                                         the gate never ran
#   * claude_hook._report_hook_failure() — the hook loaded but CRASHED mid
#     evaluation. Its line says "gate fails OPEN for this event" outright:
#     no verdict was ever reached, unconditionally.
#   * claude_hook_shim._breadcrumb() — the stdlib-only fail-closed launcher
#     (#616). It carries "event=<name>" instead of a posture phrase; the
#     shim itself only reaches a verdict (deny for PreToolUse, banner for
#     UserPromptSubmit/SessionStart/PostToolUse) for those events — Stop-class
#     and unrecognized events get stderr only, no verdict shape at all.
#
# 'denied' = the gate ran and refused (governed — the system working).
# 'ungoverned' = the gate never reached a verdict for that event (the system
# failing). SECURITY FLOOR (#770 hard constraint): a line this cannot place
# confidently in 'denied' always falls to 'ungoverned' — an unrecognized
# shape must never read as the reassuring outcome, since that would hide a
# real bypass behind a healthy-looking number.
_UNGOVERNED_LINE_MARKERS = (
    "gate fails OPEN",  # hook crashed after loading — no verdict, ever
    "NO verdict shape exists",  # Stop-class / unknown under package drift
)
_DENIED_LINE_MARKERS = (
    "DENIED (not silently permitted)",  # PreToolUse hard-refused
    "degraded LOUDLY",  # UPS/SessionStart/PostToolUse — refusal surfaced, governed
    # #932: the inner hook crashed while WRAPPED by claude_hook_shim, which
    # converts any crash into refuse(crashed=True) — a DENY for the same call,
    # in the same second, on the very next line of this log. Counting the
    # crash-report line as ungoverned double-counted one governed denial as an
    # escape and produced a false "the gate may NOT be governing this session"
    # banner. claude_hook._report_hook_failure only emits this phrase when
    # AIDOCS_HOOK_SHIM is set; an UNWRAPPED crash still says "gate fails OPEN"
    # and still lands in _UNGOVERNED_LINE_MARKERS above, where it belongs.
    "the shim will DENY this call",
)
# Events the fail-closed shim itself reaches SOME verdict for (deny or
# banner) — mirrors claude_hook_shim._DENY_EVENT / _BANNER_EVENTS, duplicated
# here for the same reason the shim duplicates claude_hook's table: reading
# it must not require importing the module whose absence this is evidence of.
_SHIM_GOVERNED_EVENTS = frozenset(
    {"PreToolUse", "UserPromptSubmit", "SessionStart", "PostToolUse"}
)


#: The shim stamps `cause=<blocked_by>` on every breadcrumb it writes. This is
#: the ONE cause that is not a health signal: the enforcement package is
#: transiently absent because a runtime refresh is swapping it. The gate
#: refusing there is #589 working exactly as designed — "a gate that cannot
#: load its own code cannot be trusted to PERMIT, but can always be trusted to
#: REFUSE" — and the shim's own operator text calls it "the system working,
#: not a break". Counting it as degradation told the operator the gate might
#: not be governing every time AIDOCS updated itself.
#:
#: READ FROM THE STAMP, NOT FROM THE EXCEPTION TEXT. "No module named
#: 'aidocs_mcp'" also appears when an install is genuinely broken; only the
#: shim knows which case it decided, so only the shim's stamp is trusted.
_UPDATE_CAUSE = "hook_runtime_unloadable"

#: How long the package swap is allowed to be in progress before its silence
#: stops being benign. The shim tells operators to "EXPECT ABOUT A MINUTE, NOT
#: SECONDS - measured 2026-08-27: 37s of package absence inside a 91s
#: provision", and "if it outlasts a few minutes it is not an update". Past
#: this, update declines degrade again — an update that never finishes is a
#: broken runtime, and staying quiet about it would be the fail-open this
#: whole surface exists to prevent.
UPDATE_WINDOW_GRACE_S = 300.0


def _decline_cause(line: str) -> str:
    """The `cause=` the shim stamped, or "" for a line written before it did."""
    match = re.search(r"\bcause=(\S+)", line)
    return match.group(1) if match else ""


def _classify_decline_line(line: str) -> str:
    """Return 'denied' or 'ungoverned' for one hook_failures.log line. Never
    raises; an unrecognized shape is 'ungoverned' (see module note above)."""
    try:
        if any(marker in line for marker in _UNGOVERNED_LINE_MARKERS):
            return "ungoverned"
        if any(marker in line for marker in _DENIED_LINE_MARKERS):
            return "denied"
        if "shim REFUSED" in line:
            match = re.search(r"event=(\S+)", line)
            event = match.group(1) if match else ""
            return "denied" if event in _SHIM_GOVERNED_EVENTS else "ungoverned"
    except Exception:
        pass
    return "ungoverned"


def _decline_probe(now: float, state_dir: Path | None) -> dict:
    """#770(b): DENIED (the gate ran and refused a call — governed, the
    system working) and UNGOVERNED (the gate never reached a verdict for
    that event — not governed, the system failing) are counted separately.
    A single lumped count made a healthy, enforcing gate and a bypassed one
    look identical on this surface; see ``_classify_decline_line`` for how
    each breadcrumb line is placed, and its security-floor default.

    THIRD CATEGORY, 2026-09-05: declines the shim stamped ``cause=
    hook_runtime_unloadable`` are the RUNTIME UPDATE WINDOW. They are counted
    and reported but do NOT degrade — see ``_UPDATE_CAUSE`` — unless the window
    has stayed open past ``UPDATE_WINDOW_GRACE_S``, at which point it is not an
    update any more and says so.
    """
    d = _resolve_state_dir(state_dir)
    p = d / DECLINE_LOG_FILENAME
    if not p.is_file():
        return {
            "status": "ok",
            "reason": "",
            "recent_count": 0,
            "recent_denied_count": 0,
            "recent_ungoverned_count": 0,
            "recent_update_count": 0,
            "last_line": "",
        }
    try:
        lines = [
            ln.strip()
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    except Exception as exc:
        return {
            "status": "unknown",
            "reason": f"hook_failures.log unreadable: {type(exc).__name__}: {exc}",
            "recent_count": 0,
            "recent_denied_count": 0,
            "recent_ungoverned_count": 0,
            "recent_update_count": 0,
            "last_line": "",
        }
    recent = 0
    denied = 0
    ungoverned = 0
    update = 0
    oldest_update_age = 0.0
    last_recent_line = ""
    for ln in lines[-200:]:  # tail only; the log is append-only forever
        ts = _parse_leading_utc(ln)
        if ts is None or (now - ts) > DECLINE_RECENT_WINDOW_S:
            continue
        if _decline_cause(ln) == _UPDATE_CAUSE:
            update += 1
            oldest_update_age = max(oldest_update_age, now - ts)
            continue
        recent += 1
        last_recent_line = ln
        if _classify_decline_line(ln) == "denied":
            denied += 1
        else:
            ungoverned += 1
    if recent:
        return {
            "status": "degraded",
            "reason": (
                f"{recent} hook decline/failure breadcrumb(s) in the last "
                f"{int(DECLINE_RECENT_WINDOW_S // 60)}min — {denied} DENIED "
                f"(the gate ran and refused; governed) and {ungoverned} "
                f"UNGOVERNED (the gate never reached a verdict; NOT governed)"
            ),
            "recent_count": recent,
            "recent_denied_count": denied,
            "recent_ungoverned_count": ungoverned,
            "recent_update_count": update,
            "last_line": last_recent_line,
        }
    if update and oldest_update_age > UPDATE_WINDOW_GRACE_S:
        # An update that will not finish is not an update. Degrading here is
        # the honest read, and naming the age shows WHY this one stopped
        # being benign.
        return {
            "status": "degraded",
            "reason": (
                f"{update} runtime-update decline(s), the oldest "
                f"{int(oldest_update_age // 60)}min old — a package swap takes "
                f"about a minute. The enforcement package has been unloadable "
                f"too long to still be a refresh; run `aidocs runtime --fix` "
                f"under the runtime interpreter"
            ),
            "recent_count": 0,
            "recent_denied_count": 0,
            "recent_ungoverned_count": 0,
            "recent_update_count": update,
            "last_line": lines[-1] if lines else "",
        }
    if update:
        return {
            "status": "ok",
            "reason": (
                f"{update} decline(s) from a runtime update in the last "
                f"{int(DECLINE_RECENT_WINDOW_S // 60)}min — the gate refused "
                f"while its own package was being swapped, which is the "
                f"fail-closed contract working, not a health problem"
            ),
            "recent_count": 0,
            "recent_denied_count": 0,
            "recent_ungoverned_count": 0,
            "recent_update_count": update,
            "last_line": lines[-1] if lines else "",
        }
    return {
        "status": "ok",
        "reason": f"{len(lines)} historical entr(ies), none recent",
        "recent_count": 0,
        "recent_denied_count": 0,
        "recent_ungoverned_count": 0,
        "recent_update_count": 0,
        "last_line": lines[-1] if lines else "",
    }


def _parse_leading_utc(line: str) -> float | None:
    """Parse the `%Y-%m-%dT%H:%M:%SZ` stamp both breadcrumb writers emit."""
    try:
        import calendar

        stamp = line.split(" ", 1)[0]
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _run_nlp_probe(project_root: Path | None) -> dict:
    """The actual (uncached) NLP liveness check. Runs in a worker thread.

    Proves: spacy imports AND a real analyze() through the one-door
    NLPService returns a Doc in THIS process. The exact click-pin death
    (import spacy raising) and the analyze()-always-None corpse both land
    as DEGRADED with the offending exception in the reason.
    """
    try:
        import spacy  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — the import failing IS the finding
        return {
            "status": "degraded",
            "reason": f"import spacy failed: {type(exc).__name__}: {exc}",
        }
    if project_root is None:
        return {
            "status": "unknown",
            "reason": "no project root — cannot construct NLPService",
        }
    try:
        from .aidocs_nlp.service import get_service

        svc = get_service(Path(project_root))
        doc = svc.analyze(
            "the quick brown fox jumps over the lazy dog",
            timeout_ms=2000.0,
        )
        if doc is None:
            return {
                "status": "degraded",
                "reason": (
                    "NLPService.analyze() returned None on a plain English "
                    "probe (no pipeline loaded / timeout / pipeline error) — "
                    "NLP-backed security surfaces are inert"
                ),
            }
        return {"status": "ok", "reason": ""}
    except Exception as exc:  # noqa: BLE001 — classify, never crash the caller
        return {
            "status": "unknown",
            "reason": f"nlp probe crashed: {type(exc).__name__}: {exc}",
        }


def _nlp_probe(project_root: Path | None, now: float) -> dict:
    """Cached, non-blocking wrapper around the real probe.

    The first call kicks a background probe and reports PENDING — a tool call
    must never stall on a spaCy model load. Pending is amber on the dashboard
    (never green: the NLP stack is NOT verified yet) but silent on the rail (a
    warming probe is not evidence of a corpse).

    If the probe is STILL unsettled after PROBE_PENDING_GRACE_S that is no
    longer warm-up — it is a probe we cannot trust, which is UNKNOWN, and
    unknown is a WARNING that DOES reach the rail. A probe that never answers
    must not buy itself permanent silence.
    """
    with _NLP_LOCK:
        cached = _NLP_RESULT
        cached_ts = _NLP_RESULT_TS
        first_kick = _NLP_FIRST_KICK_TS
        thread_alive = _NLP_THREAD is not None and _NLP_THREAD.is_alive()
    if cached is not None:
        ttl = NLP_PROBE_OK_TTL_S if cached.get("status") == "ok" else NLP_PROBE_BAD_TTL_S
        if (now - cached_ts) > ttl and not thread_alive:
            _kick_nlp_probe(project_root)
        return dict(cached)
    if not thread_alive:
        _kick_nlp_probe(project_root)
        with _NLP_LOCK:
            first_kick = _NLP_FIRST_KICK_TS
    waited = (now - first_kick) if first_kick else 0.0
    if waited > PROBE_PENDING_GRACE_S:
        return {
            "status": "unknown",
            "reason": (
                f"nlp probe has not settled after {int(waited)}s — the NLP "
                f"security surface (grant grammar, destructive intent, DNT) "
                f"CANNOT be verified"
            ),
        }
    return {
        "status": "pending",
        "reason": "nlp probe warming up (first run in this daemon process)",
    }


def _kick_nlp_probe(project_root: Path | None) -> None:
    global _NLP_THREAD, _NLP_FIRST_KICK_TS

    def _worker() -> None:
        global _NLP_RESULT, _NLP_RESULT_TS
        result = _run_nlp_probe(project_root)
        with _NLP_LOCK:
            _NLP_RESULT = result
            _NLP_RESULT_TS = time.time()

    with _NLP_LOCK:
        if _NLP_THREAD is not None and _NLP_THREAD.is_alive():
            return
        if not _NLP_FIRST_KICK_TS:
            _NLP_FIRST_KICK_TS = time.time()
        _NLP_THREAD = threading.Thread(
            target=_worker, name="aidocs-gate-health-nlp-probe", daemon=True
        )
        _NLP_THREAD.start()


# ── the signal ───────────────────────────────────────────────────────────


def compute_gate_health(
    project_root: Path | None = None,
    *,
    now: float | None = None,
    state_dir: Path | None = None,
) -> dict:
    """The gate-health signal. Never raises; a probe that blows up reports
    UNKNOWN with the exception, and unknown can never rank as ok."""
    ts = float(now) if now is not None else time.time()
    probes: dict[str, dict] = {}
    for name, fn in (
        ("hook_traffic", lambda: _hook_traffic_probe(ts, state_dir)),
        ("hook_declines", lambda: _decline_probe(ts, state_dir)),
        ("nlp", lambda: _nlp_probe(project_root, ts)),
    ):
        try:
            probe = fn()
            if str(probe.get("status")) not in _STATUS_RANK:
                probe = {
                    "status": "unknown",
                    "reason": f"probe returned unrecognized status {probe.get('status')!r}",
                }
        except Exception as exc:  # noqa: BLE001 — a broken probe is UNKNOWN, not green
            probe = {
                "status": "unknown",
                "reason": f"probe crashed: {type(exc).__name__}: {exc}",
            }
        probes[name] = probe
    worst = max(_STATUS_RANK[str(p["status"])] for p in probes.values())
    status = {0: "ok", 1: "unknown", 2: "degraded"}[worst]
    return {
        "status": status,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "probes": probes,
    }


def snapshot_gate_health(project_root: Path | None) -> dict:
    """Dashboard entry point. Even a crash in compute_gate_health itself
    must land as UNKNOWN — the health section can be wrong about details
    but it can never be green-by-accident or silently absent."""
    try:
        return compute_gate_health(project_root)
    except Exception as exc:  # noqa: BLE001 — the last-resort honesty floor
        return {
            "status": "unknown",
            "reason": f"gate health computation failed: {type(exc).__name__}: {exc}",
            "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "probes": {},
        }


# ── hook on/off TRANSITION notice (operator directive 2026-07-15) ──
# The rail used to repeat "hooks off" on EVERY tool call while degraded — a
# token tax that never even told you WHEN the state changed. Instead we latch
# the hook state and, on each edge, emit a short notice a bounded number of
# times, then fall silent: "hooks off" on stop, "hooks back on" on resume. Each
# edge is its own event (a hook restart re-arms the burst), so "degraded vs
# active" is never ambiguous — you SEE the transition. A fresh session that
# settles straight into a healthy state is NOT a recovery and stays silent.
_HOOK_TRANSITION_SHOTS = 5
_HOOK_NOTICE_OFF = "Hooks temporarily off. Continue using AIDOCS tools."
_HOOK_NOTICE_ON = "Hooks on. Continue using AIDOCS tools."
_hook_notice_lock = threading.Lock()
_hook_notice_state: dict[str, object] = {"latched": None, "shots": 0}


def _reset_hook_notice_state() -> None:
    with _hook_notice_lock:
        _hook_notice_state["latched"] = None
        _hook_notice_state["shots"] = 0


def _hook_transition_notice(hook_status: str) -> str | None:
    """Edge-triggered on/off notice for the hook_traffic probe.

    Latches on a DEFINITE state (degraded => 'off', ok => 'on'); idle/pending/
    unknown are ambiguous and never flip the latch. On each edge it arms a
    bounded burst (_HOOK_TRANSITION_SHOTS) of the matching one-liner, then goes
    silent until the next edge. Exception: the first settle into a healthy state
    (no prior latch) is a normal quiet startup, not a recovery, so it latches
    'on' SILENTLY — the "healthy == silent rail" invariant the other probes rely
    on is preserved.
    """
    if hook_status == "degraded":
        current: str | None = "off"
    elif hook_status == "ok":
        current = "on"
    else:
        current = None  # idle / pending / unknown — no definite edge
    with _hook_notice_lock:
        prev = _hook_notice_state["latched"]
        if current is not None and current != prev:
            _hook_notice_state["latched"] = current
            _hook_notice_state["shots"] = (
                0 if (current == "on" and prev is None) else _HOOK_TRANSITION_SHOTS
            )
        latched = _hook_notice_state["latched"]
        shots = int(_hook_notice_state["shots"])
        if latched is None or shots <= 0:
            return None
        _hook_notice_state["shots"] = shots - 1
    return _HOOK_NOTICE_OFF if latched == "off" else _HOOK_NOTICE_ON


def nlp_degraded_ups_notice(
    project_root: Path | None,
    *,
    now: float | None = None,
) -> str | None:
    """#348 (WAR P Task 3): the UPS response itself must carry the NLP death.

    spaCy was silently dead on the operator box (click pin) while every
    NLP-backed surface degraded to keyword-only matching — and nothing at
    the PROMPT boundary said so. This is the per-prompt voice of the same
    cached, non-blocking probe the dashboard and rail consume:

      * degraded  -> loud notice (proof the NLP surface is inert);
      * unknown   -> loud notice (cannot verify — never silence);
      * ok        -> None (healthy is silent);
      * pending   -> None (a warming probe is not evidence of a corpse).

    Detectors stay fail-SAFE regardless (§X drop-on-doubt: no NLP signal
    => no minted intent); this notice exists so the DEGRADATION is never
    invisible. Never raises; silent under the proof runner for the same
    no-crying-wolf reason gate_health_notice is.
    """
    if os.environ.get("AIDOCS_DEPLOY_DRIVER"):
        return None
    ts = float(now) if now is not None else time.time()
    try:
        probe = _nlp_probe(project_root, ts)
    except Exception as exc:  # noqa: BLE001 — cannot verify == must warn
        probe = {
            "status": "unknown",
            "reason": f"nlp probe crashed: {type(exc).__name__}: {exc}",
        }
    status = str(probe.get("status") or "")
    if status not in ("degraded", "unknown"):
        return None
    reason = str(probe.get("reason") or "").strip()
    if status == "degraded":
        head = (
            "🛑 NLP SECURITY SURFACE DEGRADED — spaCy/NLPService is NOT "
            "working in this session."
        )
    else:
        head = (
            "⚠ NLP SECURITY SURFACE UNVERIFIED — the NLP liveness probe "
            "could not confirm spaCy/NLPService is working."
        )
    return "\n".join(
        [
            head,
            f"   nlp: {status} — {reason}",
            ("   Intent/grant detectors run fail-SAFE (drop-on-doubt): shape "
            "grants may under-detect until NLP is restored. Tell the operator."),
        ],
    )


def gate_health_notice(
    project_root: Path | None,
    *,
    now: float | None = None,
    state_dir: Path | None = None,
) -> str | None:
    """The rail block — what the AGENT is told on its next tool call.

    Fires ONLY on real evidence of a problem: a DEGRADED probe (proof the gate
    is not governing) or an UNKNOWN one (proof we cannot verify that it is).
    Returns None for ok / idle / pending — a healthy gate, a quiet session and
    a still-warming probe are all silent here. Green is DISPLAYED on the
    dashboard, never shouted on the rail; and the rail never cries wolf, so
    that when it does speak the agent must believe it.

    NOT APPLICABLE IN THE PROOF RUNNER. The crown gate's VPS suite runs pytest
    under ``AIDOCS_DEPLOY_DRIVER=1`` — a test process, NOT a governed agent
    session. No host hooks are installed there, so no pulse is EVER recorded,
    and past the warm-up grace this probe would (correctly, but uselessly)
    report DEGRADED on every tool call for the rest of the suite. That is
    crying wolf: it says nothing true about the operator's gate, and it appends
    an alarm block to thousands of tool results. A rail that speaks when it has
    nothing to say trains its reader to ignore it — and this rail must be
    believed the ONE time it matters. So the notice is silent under the proof
    driver. The DASHBOARD surface is untouched, and the operator's own session
    (which HAS hooks, and DOES pulse) is untouched: the alarm still fires for
    the only reader who can act on it.

    Never raises — but note that a failure to compute becomes UNKNOWN, which
    still speaks. The signal fails LOUD, never quiet.
    """
    if os.environ.get("AIDOCS_DEPLOY_DRIVER"):
        return None
    try:
        health = compute_gate_health(project_root, now=now, state_dir=state_dir)
    except Exception as exc:  # noqa: BLE001 — cannot verify == must warn
        health = {
            "status": "unknown",
            "probes": {
                "gate_health": {
                    "status": "unknown",
                    "reason": f"health computation failed: {type(exc).__name__}: {exc}",
                },
            },
        }
    probes = health.get("probes") or {}
    # Non-hook probes carrying trouble are GENUINE security signals (integrity
    # decline, nlp dead) — they keep the full loud banner on EVERY call. Only
    # when hook_traffic is the sole concern do we defer to the edge-triggered
    # on/off transition notice: the rail announces WHEN hooks stop and WHEN they
    # resume (a bounded burst each), instead of taxing every tool call with a
    # standing "hooks off" line that can't even tell you when it changed.
    other_loud = {
        name: probe
        for name, probe in probes.items()
        if name != "hook_traffic" and str(probe.get("status")) not in _QUIET_STATUSES
    }
    if not other_loud:
        hook_status = str((probes.get("hook_traffic") or {}).get("status") or "")
        return _hook_transition_notice(hook_status)

    # A non-hook probe is loud: render the full banner, folding in hook_traffic
    # too when it is also loud (so the alarm never reads as "just hooks off").
    loud = {
        name: probe
        for name, probe in probes.items()
        if str(probe.get("status")) not in _QUIET_STATUSES
    }
    detail_lines = [
        f"   {name}: {probe.get('status')} — {probe.get('reason', '')}"
        for name, probe in loud.items()
    ]
    degraded = any(str(p.get("status")) == "degraded" for p in loud.values())
    if degraded:
        head = (
            "🛑 GATE HEALTH: DEGRADED — the AIDOCS security gate may NOT be "
            "governing this session."
        )
    else:
        head = (
            "⚠ GATE HEALTH: UNKNOWN — the gate's liveness could NOT be verified. "
            "Unknown is a WARNING, never a pass."
        )
    tail = (
        "   Evidence: <daemon_dir>/hook_failures.log + hook_pulse.json. "
        "Tell the operator; do not assume the gate is enforcing."
    )
    return "\n".join([head, *detail_lines, tail])
