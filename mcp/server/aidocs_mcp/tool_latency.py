"""Adaptive tool deadlines + late-result attachment (#1039 S2, #489).

THE DEFECT (operator report 2026-09-07): under concurrent load — Stryker plus
two agents — ``ai_investigate``, ``ai_find(symbols)`` and ``ai_trace(setting)``
died at the fixed 10s default; the same calls with ``timeout=90`` succeeded.
The work was not too slow to be useful; the DEADLINE was wrong for the
conditions, and when it fired the answer was thrown away. This session
corroborated it: repeated "warm hook broker did not answer (timed_out) …
degraded after 10001ms" banners.

The operator's prescription, verbatim: "adaptive default (measure recent p95
per tool, or read system load) and a server-side queue instead of a client
deadline, so a slow answer is late rather than lost."

Two mechanisms, both process-local and dependency-free:

1. ADAPTIVE DEFAULT. Every timed tool records its wall duration here. The
   default deadline for a tool becomes ``max(configured, p95 x 1.5)`` once it
   has enough samples, scaled up by how many tool calls are in flight right
   now (the cheapest honest load signal on every platform — ``os.getloadavg``
   does not exist on Windows). It NEVER goes below the configured default,
   NEVER above the ``tools.max_timeout`` ceiling, and a caller-supplied
   ``timeout=`` still wins outright, so the operator's knobs keep their
   meaning: this only lends a slow box more rope.

2. LATE RESULTS ("late rather than lost"). When a deadline fires the worker
   is no longer abandoned. The still-running future is parked under a key
   derived from the call (tool + arguments); a re-issue of the SAME call
   attaches to the running future instead of starting a second copy, and if
   it has finished in the meantime the answer is returned immediately (flagged
   ``late_result: true`` on dict payloads). The refusal names this so the
   agent's natural retry is the queue.
"""

from __future__ import annotations

import contextvars
import hashlib
import math
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

_LOCK = threading.Lock()
_SAMPLES: dict[str, deque[float]] = {}
_SAMPLE_WINDOW = 64
_MIN_SAMPLES = 5
_P95_HEADROOM = 1.5
_LOAD_STEP = 0.5  # +50% deadline per concurrent in-flight tool call
_LOAD_CAP = 3.0
_INFLIGHT: dict[str, int] = {}

_LATE_TTL_SECONDS = 300.0
_LATE_MAX = 64


@dataclass
class _Late:
    future: Future
    tool: str
    started: float
    finished: float | None = None
    attached: int = field(default=0)


_LATE: dict[str, _Late] = {}

# The deadline of the CURRENT tool call (monotonic seconds), for
# implementations that can return partial results before the hard kill.
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "aidocs_tool_deadline", default=None,
)


# ── samples ──


def record(tool: str, seconds: float) -> None:
    if not tool or seconds < 0:
        return
    with _LOCK:
        _SAMPLES.setdefault(tool, deque(maxlen=_SAMPLE_WINDOW)).append(float(seconds))


def samples(tool: str) -> int:
    with _LOCK:
        return len(_SAMPLES.get(tool, ()))


def p95(tool: str) -> float | None:
    with _LOCK:
        data = sorted(_SAMPLES.get(tool, ()))
    if not data:
        return None
    idx = max(0, math.ceil(0.95 * len(data)) - 1)
    return data[idx]


def reset_for_tests() -> None:
    with _LOCK:
        _SAMPLES.clear()
        _INFLIGHT.clear()
        _LATE.clear()


# ── in-flight (load) ──


def inflight_enter(tool: str) -> None:
    with _LOCK:
        _INFLIGHT[tool] = _INFLIGHT.get(tool, 0) + 1


def inflight_exit(tool: str) -> None:
    with _LOCK:
        n = _INFLIGHT.get(tool, 0) - 1
        if n <= 0:
            _INFLIGHT.pop(tool, None)
        else:
            _INFLIGHT[tool] = n


def inflight_total() -> int:
    with _LOCK:
        return sum(_INFLIGHT.values())


def load_factor(concurrent: int | None = None) -> float:
    """1.0 when nothing else is running; +50% per concurrent call, capped."""
    n = inflight_total() if concurrent is None else int(concurrent)
    return min(_LOAD_CAP, 1.0 + _LOAD_STEP * max(0, n))


# ── the adaptive default ──


def adaptive_default(
    tool: str,
    configured_default: int,
    *,
    ceiling: int = 0,
    concurrent: int | None = None,
) -> int:
    """The deadline to use when the caller supplied none.

    ``configured_default`` <= 0 means unlimited and is returned as-is.
    Otherwise: start from the configured default; if the tool has at least
    ``_MIN_SAMPLES`` recent samples raise it to ``ceil(p95 x 1.5)``; scale the
    result by the load factor; clamp to ``ceiling`` (0 = none). Never below
    the configured default — this lends rope, it never shortens it.
    """
    try:
        base = int(configured_default)
    except (TypeError, ValueError):
        return 0
    if base <= 0:
        return 0
    candidate = float(base)
    if samples(tool) >= _MIN_SAMPLES:
        measured = p95(tool)
        if measured is not None:
            candidate = max(candidate, measured * _P95_HEADROOM)
    candidate *= load_factor(concurrent)
    result = int(math.ceil(candidate))
    if ceiling and ceiling > 0:
        result = min(result, int(ceiling))
    return max(base, result)


def explain(tool: str) -> dict[str, Any]:
    """Measured facts for a refusal message — never guesses."""
    measured = p95(tool)
    return {
        "tool": tool,
        "samples": samples(tool),
        "p95_seconds": round(measured, 3) if measured is not None else None,
        "inflight": inflight_total(),
    }


# ── deadline contextvar (partial-result seam) ──


def set_deadline(seconds: float | None):
    """Publish the current call's deadline; returns a token for reset."""
    if seconds is None or seconds <= 0:
        return _deadline.set(None)
    return _deadline.set(time.monotonic() + float(seconds))


def reset_deadline(token) -> None:
    try:
        _deadline.reset(token)
    except Exception:
        pass


def remaining_seconds() -> float | None:
    """Seconds left before the current tool call's hard kill, or None when
    unlimited / not inside a timed call. An implementation that can stop
    early should do so at ~90% and flag ``truncated: true``."""
    d = _deadline.get()
    if d is None:
        return None
    return max(0.0, d - time.monotonic())


# ── late results ──


def _is_refusal(principal: str) -> bool:
    """Is this "principal" actually the #1043 REFUSAL wearing a string?

    `calling_principal` already maps it to "" and is the normal path. This is
    the second layer, at the point of use: any OTHER call site that reaches
    `stable_actor_id` directly and threads the raw value through must not get a
    key either. The refusal is truthy, so without an explicit check every
    generic consumer reads it as an actor — which is the whole reason this state
    keeps escaping.
    """
    try:
        from .task_actor_identity import identity_is_unproven

        return bool(identity_is_unproven(principal))
    except Exception:
        # Fail closed: if we cannot tell whether this is a refusal, do not cache.
        return True


def late_key(
    tool: str, args: tuple, kwargs: dict, *, principal: str = ""
) -> str | None:
    """The identity of a parked call: TOOL + ARGS + WHO ASKED.

    THE PRINCIPAL IS PART OF THE KEY, and leaving it out was a real defect in
    the first cut of this module (#1042). `_LATE` is process-global, and this
    process serves a conductor AND every subagent of that conversation — that is
    the premise of the S1 fix in this same series. Keyed on tool+args alone, two
    callers issuing the same call hash to ONE entry, so `late_lookup` hands
    caller B the future parked by caller A.

    That is already wrong for any tool whose answer depends on identity resolved
    INTERNALLY rather than passed in the arguments — ai_task(mode='status'),
    ai_msg(mode='inbox'), the notification surfaces, anything that calls
    `_resolve_session_id` — where the same arguments legitimately mean different
    answers for different callers.

    And it becomes a LEAK the moment per-principal read visibility lands
    (#1041): identical tool+args would then return different rows per principal
    by design, and an identity-less cache would serve one principal's filtered
    view to another. A visibility authority that a cache key can bypass is not
    an authority.

    AN UNPROVEN CALLER DISABLES REUSE ENTIRELY — returns None, no key, no park,
    no attach. The first cut of this fix rendered an empty principal as the
    literal bucket ``"anon"``, which is a SHARED BUCKET wearing a different
    name: every unidentified caller hashed to it and attached to the previous
    unidentified caller's result. That is precisely the collapse this function
    was written to remove, reintroduced one line below the paragraph explaining
    it — and the test shipped with it asserted only ``anon != named``, never
    ``anon_A != anon_B``, so it passed while testing the wrong distinction.

    There is no safe synthetic identity. A cache is an optimisation and reuse is
    optional; correctness is not. When we cannot say WHO is asking, the honest
    answer is to run the call again, not to guess that two strangers are the
    same agent.
    """
    who = (principal or "").strip()
    if not who or _is_refusal(who):
        return None
    try:
        rendered = repr((args, sorted(kwargs.items())))
    except Exception:
        rendered = repr((id(args), id(kwargs)))
    return (
        tool
        + ":"
        + hashlib.sha256(
            (who + "\0" + rendered).encode("utf-8", "replace")
        ).hexdigest()[:20]
    )


def calling_principal() -> str:
    """The actor axis a parked result is keyed on — the SAME one S1 adopted.

    `stable_actor_id` carries agent_id (that is what stopped a subagent closing
    its conductor's task), so it is the axis that also separates their parked
    results. It also REFUSES to answer when worker evidence says subagent and no
    agent axis is stamped, rather than handing back the parent's id — so this
    cannot inherit a conductor's identity for a caller that is not the
    conductor.

    Best-effort and fail-quiet: an unresolvable principal yields "", and
    `late_key` then returns None, which DISABLES reuse for that call. Running
    the work twice is the correct price for not knowing who is asking.
    """
    try:
        from .mcp_server_runtime_helpers import resolve_project_root
        from .task_actor_identity import identity_is_unproven, stable_actor_id

        actor = str(stable_actor_id(resolve_project_root()) or "")
        # THE REFUSAL IS NOT A PRINCIPAL (#1043). `stable_actor_id` reports the
        # refused state as a distinguished value so it cannot be mistaken for
        # the legacy actorless "" — but that value is a TRUTHY STRING, and this
        # function's contract is "a principal or nothing". Returned as-is it
        # would become a cache key, and two unstamped subagents would share one
        # bucket: the exact collapse the principal axis was added to remove,
        # arriving through the refusal meant to prevent it.
        #
        # Caught live by a RED xdist run before this shipped, which is the only
        # reason it is not in production: the state must DISABLE reuse.
        return "" if identity_is_unproven(actor) else actor
    except Exception:
        return ""


def _sweep_locked(now: float) -> None:
    dead = [
        k
        for k, v in _LATE.items()
        if (v.finished is not None and now - v.finished > _LATE_TTL_SECONDS)
        or (v.finished is None and now - v.started > _LATE_TTL_SECONDS * 4)
    ]
    for k in dead:
        _LATE.pop(k, None)
    while len(_LATE) > _LATE_MAX:
        oldest = min(_LATE.items(), key=lambda kv: kv[1].started)[0]
        _LATE.pop(oldest, None)


def late_lookup(key: str) -> Future | None:
    """The parked future for this exact call, if any (running or finished)."""
    now = time.monotonic()
    with _LOCK:
        _sweep_locked(now)
        entry = _LATE.get(key)
        if entry is None:
            return None
        entry.attached += 1
        return entry.future


def late_park(key: str, tool: str, future: Future) -> None:
    """Keep a timed-out call's future so a retry can attach to it."""
    now = time.monotonic()
    with _LOCK:
        _sweep_locked(now)
        if key in _LATE:
            return
        entry = _Late(future=future, tool=tool, started=now)
        _LATE[key] = entry

    def _mark_done(_f: Future) -> None:
        with _LOCK:
            e = _LATE.get(key)
            if e is not None and e.future is _f:
                e.finished = time.monotonic()

    future.add_done_callback(_mark_done)


def late_consume(key: str) -> None:
    """Drop a finished parked result once it has been delivered."""
    with _LOCK:
        _LATE.pop(key, None)


def late_status(key: str) -> str:
    with _LOCK:
        entry = _LATE.get(key)
        if entry is None:
            return "none"
        return "finished" if entry.future.done() else "running"
