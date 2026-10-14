"""Interrupt-safe pre-execution audit for the LOCAL stdio tool chokepoint.

#441 (causal-turn-interrupt-integrity spec) — the stdio twin of the WebMCP
three-phase discipline in ``outer_gate_audit.three_phase_audited_execute``
(#93 phase 1). The wrapper in ``mcp_server._real_instrumented_call_tool``
already writes ``tool_call_started`` BEFORE the tool executes; this module
formalizes that row as the durable TOOL-ATTEMPT (intent) record and pins
the spec's ordering law around it:

  1. INTENT audit BEFORE execution. If the intent row cannot be durably
     recorded AND the tool is MUTATING-tier, REFUSE and do NOT execute —
     fail closed, nothing mutated. A mid-execution interrupt (process
     kill, ^C, host disconnect) can therefore never yield an
     executed-but-unaudited mutation: either the intent row is on disk
     first, or the side-effect boundary is never crossed.
  2. For a non-mutating (read-tier) tool a failed intent audit is
     tolerated — there is no state change a post-hoc audit could miss.
  3. RESULT audit AFTER execution. A failure there is AUDIT_DEGRADED —
     the mutation STANDS and is already intent-audited, so the deed is
     never lost and never retroactively fail-closed.

Tier resolution: ``tool_is_mutating`` consults the declared tool contract
(``tool_interface._TOOLS`` — tier M/A or class edit/run/import/admin) and
falls back to ``tool_gate_service.classify_tool_action`` buckets for
undeclared/external names. Unknown read-shaped tools resolve non-mutating;
declared metadata always wins.
"""

from __future__ import annotations

import atexit
import queue
import sys
import threading
import time
from typing import Any, Callable

# Mutating tiers per the WebMCP manifest vocabulary (outer_gate_manifest):
# Tier M (surgical edit) and Tier A (admin). Tier R is read-only; Tier L
# is selector/list. Classes that imply side effects regardless of tier.
_MUTATING_TIERS = frozenset({"M", "A"})
_MUTATING_CLASSES = frozenset({"edit", "run", "import", "admin"})
# classify_tool_action buckets that imply side effects for tools with no
# declared ToolSpec (native/external names reaching the local wrapper).
_MUTATING_BUCKETS = frozenset({"edit", "run", "agent"})


class IntentAuditRefused(RuntimeError):
    """A MUTATING-tier tool call was refused because its pre-execution
    intent audit could not be durably recorded (fail closed; nothing
    executed, nothing mutated)."""


def tool_is_mutating(tool_name: str) -> bool:
    """Resolve whether ``tool_name`` is mutating-tier for the intent gate.

    Declared contract first (tool_interface), coarse action bucket as the
    fallback. Best-effort on lookup errors — an unresolvable name falls
    back to the bucket classifier, never raises.
    """
    bare = str(tool_name or "").strip()
    for prefix in ("mcp__aidocs__", "mcp__playwright__", "mcp__"):
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix) :]
            break
    try:
        from .tool_interface import _TOOLS

        spec = _TOOLS.get(bare) or _TOOLS.get(bare.lower())
        if spec is not None:
            return (
                str(spec.tier).strip().upper() in _MUTATING_TIERS
                or str(spec.cls).strip().lower() in _MUTATING_CLASSES
            )
    except Exception:
        pass
    try:
        from .tool_gate_service import classify_tool_action

        return classify_tool_action(bare) in _MUTATING_BUCKETS
    except Exception:
        # Cannot classify at all → treat as mutating (fail-closed bias:
        # an unclassifiable tool must not dodge the intent gate).
        return True


#: Markers that identify SQLITE_BUSY / lock contention rather than a damaged
#: store. Matched against the whole __cause__ chain, because the audit writer
#: wraps the sqlite error (AuditWriteUnavailable: "... after 4 attempts:
#: database is locked").
_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "could not acquire",
    "sqlite_busy",
)


def _looks_busy(exc: BaseException) -> bool:
    """Is this failure CONTENTION rather than damage? Walks the cause chain."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        blob = f"{type(cur).__name__}: {cur}".lower()
        if any(m in blob for m in _BUSY_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _audit_write_remedy(exc: BaseException) -> str:
    """The remedy line for a refused audit write — TRUE to the actual cause.

    #850 clause 3. This said "Restore the audit store and retry" for EVERY
    failure. Measured 2026-08-20: a routine WAL checkpoint held the write lock
    for ~40 minutes and every audited tool refused with that sentence -- so the
    message pointed the operator at a DESTRUCTIVE remedy for a database that was
    not damaged at all, merely busy. Nothing was wrong with the store; it was
    doing its own maintenance.

    Worse, the obvious reflex is the harmful one: the operator asked whether to
    run `aidocs service restart` during that outage, and the correct answer was
    NO -- a restart interrupts exactly the checkpoint whose completion ends the
    outage. Waiting cleared it. A refusal that omits that steers toward the one
    action that prolongs the problem.

    Same family as #906/#910/#914/#919: an outcome that misnames its own cause
    sends the reader to the wrong investigation. Here it sends them to a
    destructive one.
    """
    if _looks_busy(exc):
        return (
            "The audit store is BUSY, not damaged — this is lock contention, "
            "most often a WAL checkpoint folding the write-ahead log back into "
            "the database. It clears ITSELF when the checkpoint completes. "
            "REMEDY: wait and retry; read-only tools keep working meanwhile. "
            "DO NOT restart the daemon and DO NOT restore the store — a restart "
            "interrupts the very maintenance that ends this state (#850)."
        )
    return (
        "The audit store could not be written and the cause is NOT lock "
        "contention, so it may genuinely be damaged. Restore the audit store "
        "and retry."
    )


def intent_audit_or_refuse(
    record_intent: Callable[[], Any],
    *,
    is_mutating: bool,
    tool_name: str,
) -> bool:
    """Phase 1 — durable INTENT record BEFORE the side-effect boundary.

    Returns True when the intent row landed. When ``record_intent``
    raises: a MUTATING tool is refused via :class:`IntentAuditRefused`
    (fail closed — the caller must not execute); a non-mutating tool
    proceeds un-intent-audited (returns False, doctrine rule 2).
    """
    try:
        record_intent()
        return True
    except Exception as exc:
        if is_mutating:
            raise IntentAuditRefused(
                f"intent_audit_unrecorded: refusing to execute mutating tool "
                f"'{tool_name}' — its pre-execution audit row could not be "
                f"durably recorded ({type(exc).__name__}: {exc}). Nothing "
                f"was executed. {_audit_write_remedy(exc)}"
            ) from exc
        return False


def result_audit_degraded(
    record_result: Callable[[], Any],
    *,
    tool_name: str,
) -> bool:
    """Phase 3 — RESULT audit AFTER execution; failure degrades, never
    raises. Returns True when the result audit FAILED (audit_degraded):
    the executed deed stands (it is intent-audited) and the caller must
    still return the tool result. A stderr note keeps the gap observable.
    """
    try:
        record_result()
        return False
    except Exception as exc:
        try:
            sys.stderr.write(
                f"[aidocs audit] RESULT audit degraded for '{tool_name}' "
                f"(intent row already durable; result stands): "
                f"{type(exc).__name__}: {exc}\n"
            )
        except Exception:
            pass
        return True


# ── Phase 3 off the response path (2026-08-23) ──────────────────────────
# MEASURED: with `execution_events` at 235,307 rows the audit DB's write
# lock saturated, and `record_run` -> `_write_with_retry` spent its full
# bounded budget (4 attempts x 10s busy_timeout) on EACH of the two writes
# `_record_tool_execution_state` performs -- SYNCHRONOUSLY, in front of
# `return result`. The daemon logged the correct degrade
# ("intent row already durable; result stands") only AFTER ~40s of silence,
# by which point the operator had cancelled a tool call that had already
# finished executing.
#
# Doctrine rule 3 already says a phase-3 failure is TOLERATED. A tolerated
# failure must not be allowed to cost the caller the whole retry budget
# first, so the write moves to a single background worker and the response
# returns immediately. Nothing about rule 1 changes: `intent_audit_or_refuse`
# stays synchronous and fail-closed, because the durability of the intent row
# is an ORDERING guarantee (on disk BEFORE the side-effect boundary) and a
# queued row is not on disk.
#
# BOUNDED on purpose, twice over:
#   * the queue has a ceiling -- an unbounded one would trade a stalled
#     response for unbounded memory under exactly the saturation this fixes;
#   * `put` never blocks -- a blocking put on a full queue is just the
#     original stall behind one more layer. A full queue degrades in O(1)
#     through the same `result_audit_degraded` note, so the operator still
#     sees every lost row.
#: Roughly a minute of sustained tool calls at the observed peak rate.
_DEFERRED_MAX = 512
#: Bounded shutdown flush. A short-lived process (hook subprocess, CLI) must
#: not silently drop the result rows it queued, but must not hang on them.
_DEFERRED_EXIT_FLUSH_S = 5.0

_deferred_lock = threading.Lock()
_deferred_queue: queue.Queue[tuple[Callable[[], Any], str]] | None = None
_deferred_worker: threading.Thread | None = None


def _deferred_loop(work: queue.Queue[tuple[Callable[[], Any], str]]) -> None:
    while True:
        record_result, tool_name = work.get()
        try:
            # Same degrade semantics, same stderr note, same never-raises
            # contract -- only the THREAD it runs on has changed.
            result_audit_degraded(record_result, tool_name=tool_name)
        finally:
            work.task_done()


def _deferred_channel() -> queue.Queue[tuple[Callable[[], Any], str]]:
    """The queue + its worker, created once per process, on first use."""
    global _deferred_queue, _deferred_worker
    with _deferred_lock:
        if _deferred_queue is None:
            _deferred_queue = queue.Queue(maxsize=max(1, _DEFERRED_MAX))
        if _deferred_worker is None or not _deferred_worker.is_alive():
            _deferred_worker = threading.Thread(
                target=_deferred_loop,
                args=(_deferred_queue,),
                name="aidocs-result-audit",
                daemon=True,
            )
            _deferred_worker.start()
        return _deferred_queue


def result_audit_deferred(
    record_result: Callable[[], Any],
    *,
    tool_name: str,
) -> bool:
    """Phase 3, off the response path.

    Returns True when the result audit was handed to the background writer
    (it will land, or degrade loudly, on its own). Returns False when the
    queue was saturated and the audit degraded immediately. NEVER raises and
    NEVER blocks -- the caller returns the tool result either way.
    """
    try:
        _deferred_channel().put_nowait((record_result, tool_name))
        return True
    except queue.Full:
        try:
            sys.stderr.write(
                f"[aidocs audit] RESULT audit degraded for '{tool_name}' "
                f"(intent row already durable; result stands): "
                f"deferred audit queue full ({_DEFERRED_MAX})\n"
            )
        except Exception:
            pass
        return False
    except Exception as exc:  # noqa: BLE001 — phase 3 never breaks a result
        try:
            sys.stderr.write(
                f"[aidocs audit] RESULT audit degraded for '{tool_name}' "
                f"(intent row already durable; result stands): "
                f"{type(exc).__name__}: {exc}\n"
            )
        except Exception:
            pass
        return False


def flush_deferred_audits(timeout: float = _DEFERRED_EXIT_FLUSH_S) -> bool:
    """Wait (bounded) for queued result audits to be written.

    Returns True when the queue drained inside ``timeout``. Used by tests and
    by the atexit hook; never on the response path.
    """
    work = _deferred_queue
    if work is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while work.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


def reset_deferred_audits() -> None:
    """Drop the queue + worker so the next submission rebuilds them from the
    CURRENT module settings. Test seam only."""
    global _deferred_queue, _deferred_worker
    with _deferred_lock:
        _deferred_queue = None
        _deferred_worker = None


atexit.register(flush_deferred_audits)
