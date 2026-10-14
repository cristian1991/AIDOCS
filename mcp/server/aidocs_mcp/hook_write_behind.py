"""Bounded write-behind for NON-AUTHORITATIVE hook-path telemetry (#489).

MEASURED CAUSE (tests/host/test_hook_sqlite_contention_489.py, 2026-09-13). Every
PostToolUse evaluation wrote two rows to the project execution index
synchronously — ``hook_pipeline.record_hook_event`` (the ``hook_intercept``
breadcrumb) and ``LifecycleService.on_post_tool_use_audit`` (the ``native_tool_use``
completion row) — each a fresh connection + ``BEGIN IMMEDIATE`` on the shared
``aidocs.sqlite3`` with a 10s busy timeout and 4 retries (execution_index_store
``_WRITE_BUSY_TIMEOUT_MS`` / ``_WRITE_RETRY_ATTEMPTS``). With the daemon holding
that file's write lock (live: 611MB db, 137MB WAL, 83 "database is locked"), a
PostToolUse sat 4.6-19s in BEGIN IMMEDIATE, and the operator's UserPromptSubmit —
exclusive on the same session key — queued behind those readers and then paid its
own two telemetry writes on top. That is the 10s ``timed_out``.

WHAT MAY COME HERE. Only rows no gate reads for a decision: the hook_intercept
breadcrumb, the prompt_classified trace row, and the PostToolUse native_tool_use
completion row (readers: prompt skill suggestions and dashboard stats; the
read-before-edit gate reads ``tool_call_completed``/``tool_edit_completed`` rows
written by the MCP tool path, never these). NEVER here: freeze/strike records,
grants, memory ack receipts, ``user_prompt_received`` (the intent head), anything
``intent_audit_or_refuse`` depends on. Degrade speed before authority.

UNKNOWN IS NOT ZERO. The queue is bounded; a write that cannot be queued is
DROPPED, COUNTED per label, and announced once on stderr. A queued write that
later fails is counted as ``failed`` (before this it was a ``logger.debug`` —
silent). ``stats()`` is served on the broker's read-only timings reader.
"""

from __future__ import annotations

import contextlib
import contextvars
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

QUEUE_MAX = 1024

_lock = threading.Lock()
_queue: queue.Queue[tuple[str, contextvars.Context, Callable[[], Any]]] | None = None
_worker: threading.Thread | None = None
_counts: dict[str, dict[str, int]] = {}
_announced: set[str] = set()
_pause = threading.Event()
_pause.set()  # set = running


def _bump(label: str, key: str) -> None:
    with _lock:
        slot = _counts.setdefault(label, {"queued": 0, "written": 0, "failed": 0, "dropped": 0})
        slot[key] += 1


def _loop(work: queue.Queue) -> None:
    while True:
        label, ctx, fn = work.get()
        try:
            _pause.wait()
            try:
                ctx.run(fn)
                _bump(label, "written")
            except Exception as exc:  # noqa: BLE001 — counted, and said once
                _bump(label, "failed")
                _announce(label, f"write failed: {type(exc).__name__}: {exc}")
        finally:
            work.task_done()


def _channel() -> queue.Queue:
    global _queue, _worker
    with _lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, args=(_queue,), name="aidocs-hook-write-behind", daemon=True)
            _worker.start()
        return _queue


def _announce(label: str, what: str) -> None:
    key = f"{label}:{what.split(':', 1)[0]}"
    if key in _announced:
        return
    _announced.add(key)
    with contextlib.suppress(Exception):
        sys.stderr.write(f"[aidocs hook write-behind] {label}: {what} (counted; see hook broker timings)\n")


#: WHERE WRITE-BEHIND IS ALLOWED: only inside an evaluation the long-lived hook
#: broker is running (HookBroker._process enters ``broker_evaluation()``). The
#: signal is the broker's own evaluation scope, not an env var or a process guess:
#: the cold fresh-interpreter path (claude_hook run per event when the warm broker
#: is down) never enters it, so there every write stays INLINE, exactly as before.
#: Why not atexit-flush: a daemon thread dies with the interpreter, and a cold
#: process that exits right after its verdict would lose a queued row that no
#: counter ever saw. Inline has no such window.
_broker_scope: contextvars.ContextVar[bool] = contextvars.ContextVar("aidocs_hook_broker_scope", default=False)


@contextlib.contextmanager
def broker_evaluation():
    """Mark the current context as a warm-broker evaluation (write-behind allowed)."""
    token = _broker_scope.set(True)
    try:
        yield
    finally:
        _broker_scope.reset(token)


def submit(label: str, fn: Callable[[], Any]) -> bool:
    """Queue ``fn`` (run with the caller's contextvars). False = DROPPED (counted).

    Outside ``broker_evaluation()`` the write runs INLINE and its exceptions
    propagate to the caller, unchanged from before #489.
    """
    if not _broker_scope.get():
        fn()
        return True
    ctx = contextvars.copy_context()
    # The caller's per-evaluation SQLite ledger must not follow the write
    # behind: the wait it measures is no longer the evaluation's.
    from ._sqlite_connect import _ledger  # noqa: PLC0415

    ctx.run(_ledger.set, None)
    try:
        _channel().put_nowait((label, ctx, fn))
    except queue.Full:
        _bump(label, "dropped")
        _announce(label, "dropped: write-behind queue full")
        return False
    _bump(label, "queued")
    return True


def stats() -> dict[str, Any]:
    """Per-label counts plus pending depth. ``dropped``/``failed`` must be read, not assumed 0."""
    with _lock:
        labels = {k: dict(v) for k, v in _counts.items()}
    work = _queue
    return {
        "pending": int(work.unfinished_tasks) if work is not None else 0,
        "capacity": QUEUE_MAX,
        "dropped": sum(v["dropped"] for v in labels.values()),
        "failed": sum(v["failed"] for v in labels.values()),
        "by_label": labels,
    }


def flush(timeout: float = 30.0) -> bool:
    """Wait (bounded) until every queued write has run. Tests and shutdown."""
    work = _queue
    if work is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while work.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


@contextlib.contextmanager
def paused():
    """Hold the worker (tests): proves what is durable WITHOUT the write-behind."""
    _pause.clear()
    try:
        yield
    finally:
        _pause.set()


def reset_for_tests() -> None:
    with _lock:
        _counts.clear()
        _announced.clear()
