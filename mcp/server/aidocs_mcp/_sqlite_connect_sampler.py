"""Who is opening SQLite connections, and from where (P0 runaway 2026-08-05).

THE MEASUREMENT THIS ANSWERS. Operator sampled the managed HTTP backend over
~113 seconds: +2,255 handles, +1.491 GB private, +93.1 CPU seconds -- roughly
1,200 handles/minute and 82% of one core, while the DIRECT backend sat
completely unchanged at 203 handles / 97 MB.

RATE, STATED CORRECTLY. That is ~20 HANDLES per second. A WAL connection costs
THREE handles (db, -wal, -shm), so it is ~6.7 CONNECTIONS/second sustained --
not the "20 connections/sec" an earlier draft of this docstring claimed. The
distinction changed the search: 20/s suggested a tight background timer, while
6.7/s across a window holding only a handful of tool calls pointed at PER-CALL
cost. Per-call is what it proved to be, and the near-empty idle samples were
the evidence -- operator-idle is not process-idle, and an empty idle window
must not be read as "no runaway".

WHAT IT WAS. One ai_find cost 3,123 connects, +415 MB and +49s CPU; 2,162 came
from ONE chain -- project_index_sitter.reconcile -> sync_code_files ->
config.py:803 -> config_resolver._db_layer -- the effective `include_tests`
setting resolved PER FILE, two config layers each, over ~1,081 files. The
sitter runs on its own thread and never entered the request_config_scope()
that both other entrypoints already use.

CONTROLLED EQUAL-WORKLOAD RESULT (6 identical reconciles, single process):

    steady-state opens/round    749  ->   26
    CPU per round             ~15.3s -> ~11.5s
    wall per round            ~18.9s -> ~13.0s
    peak live connections        32  ->   33    (bounded either way by #756)
    OS handles                  147  ->  148    (flat in both, by #756)

Handles and peak are flat in BOTH columns because deterministic closure was
already in place for both runs. That is the division of labour between the two
fixes: closure BOUNDS the handles, the request scope REMOVES the connects.

OFF BY DEFAULT AND FREE WHEN OFF: one module-level truthiness check per connect
unless AIDOCS_SQLITE_SAMPLE names a target file. Read the env var per call
rather than caching it at import, so sampling can be switched on for a running
daemon without a restart.

Usage:
    AIDOCS_SQLITE_SAMPLE=D:/tmp/sqlite-connects.tsv aidocs service start
    # let it idle ~60s with no MCP traffic, then:
    cut -f4 D:/tmp/sqlite-connects.tsv | sort | uniq -c | sort -rn | head -20

The CHAIN column matters as much as the callsite: the innermost frame is
usually a shared helper, and it is the frames ABOVE it that name the scheduler
actually driving the loop.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
import traceback
from pathlib import Path

_ENV = "AIDOCS_SQLITE_SAMPLE"
_DEPTH_ENV = "AIDOCS_SQLITE_SAMPLE_DEPTH"  # 0 = no stack walk (cheap mode)
_lock = threading.Lock()
_seq = itertools.count(1)
_active: dict[int, tuple[int, str]] = {}  # id(conn) -> (conn_no, db)
_peak = 0
_PID = os.getpid()

# Fixed, machine-readable columns:
#   ts  pid  event  conn_no  active  peak  thread  db_full  callsite  chain
_COLUMNS = "ts pid event conn_no active peak thread db callsite chain"


def snapshot() -> dict[str, object]:
    """Active/peak plus a per-db breakdown — for assertions, not for humans."""
    per_db: dict[str, int] = {}
    for _no, db in _active.values():
        per_db[db] = per_db.get(db, 0) + 1
    return {"pid": _PID, "active": len(_active), "peak": _peak, "per_db": per_db}


def _emit(event: str, conn_no: int, db: str, callsite: str = "", chain: str = "") -> None:
    target = os.environ.get(_ENV)
    if not target:
        return
    with _lock, open(target, "a", encoding="utf-8") as handle:
        handle.write(
            f"{time.time():.3f}\t{_PID}\t{event}\t{conn_no}\t{len(_active)}\t{_peak}\t"
            f"{threading.current_thread().name}\t{db}\t{callsite}\t{chain}\n"
        )


def note_close(conn: object) -> None:
    """Record an EXPLICIT close, from the closing-connection wrappers.

    Open counts ALONE cannot prove the runaway is sealed: a path that opens
    3,000 connections and closes all 3,000 is indistinguishable from one that
    leaks them if you only count opens. Closes are what make `active` and
    `peak` mean anything, and `peak` is the number that actually bounds the
    process.
    """
    try:
        entry = _active.pop(id(conn), None)
        if entry is not None:
            _emit("close", entry[0], entry[1])
    except Exception:  # noqa: BLE001,S110 -- diagnostics must never break the caller
        pass


def install_global_hook() -> None:
    """Sample EVERY sqlite3.connect in the process, not just the two factories.

    The class factories cover CodeIndexStore and the ~25 SQLiteIndexStoreBase
    subclasses, but ~55 modules call sqlite3.connect(path) directly and would
    be invisible. A runaway hiding in one of those would read as "no background
    connects", which is exactly the wrong conclusion to draw from an empty
    file. Wrapping the module function closes that blind spot.

    The wrapper also returns a subclass that reports its own close(), so the
    ledger sees both halves of every connection's life.

    Idempotent, and a no-op unless sampling is armed at import time.
    """
    if not os.environ.get(_ENV):
        return
    import sqlite3  # noqa: PLC0415 -- only imported on the diagnostic path

    if getattr(sqlite3.connect, "_aidocs_sampled", False):
        return
    real = sqlite3.connect

    class _Tracked(sqlite3.Connection):
        def close(self):  # noqa: ANN201
            note_close(self)
            return super().close()

    def sampled(*args: object, **kwargs: object):  # noqa: ANN202
        kwargs.setdefault("factory", _Tracked)
        try:
            conn = real(*args, **kwargs)
        except TypeError:
            # A caller passing its own factory that does not accept ours.
            kwargs.pop("factory", None)
            conn = real(*args, **kwargs)
        if args:
            sample_connect("sqlite3.connect", str(args[0]), conn=conn)
        return conn

    sampled._aidocs_sampled = True  # type: ignore[attr-defined]
    sqlite3.connect = sampled  # type: ignore[assignment]


def sample_connect(store: str, db_path: Path | str, conn: object | None = None) -> None:
    """Record one OPEN: when, which pid, which db (FULL path), who called.

    Never raises. A diagnostic that can break the server it is diagnosing is
    worse than no diagnostic — this runs inside the hot connect path.

    The stack walk is the expensive part (traceback.extract_stack reads source
    lines). It is bounded by AIDOCS_SQLITE_SAMPLE_DEPTH=0, which keeps the
    open/close ledger — the part that proves stability — while dropping the
    attribution needed only while hunting a culprit.
    """
    global _peak
    target = os.environ.get(_ENV)
    if not target:
        return
    try:
        conn_no = next(_seq)
        db_full = str(db_path)
        if conn is not None:
            _active[id(conn)] = (conn_no, db_full)
            if len(_active) > _peak:
                _peak = len(_active)
        caller = chain = ""
        if os.environ.get(_DEPTH_ENV, "10") != "0":
            stack = traceback.extract_stack()[:-1]
            here = Path(__file__).name
            caller = "<unknown>"
            for frame in reversed(stack):
                name = Path(frame.filename).name
                # Skip this module, the store factories, and PLUMBING. The
                # first run returned "contextlib.py:141 __enter__" for 36 of
                # 36 rows: callers reach the factory through @contextmanager
                # wrappers, so the innermost non-store frame is the decorator,
                # not the code that wanted a connection. Skipping the stdlib
                # machinery is what makes this column name a culprit instead
                # of a corridor.
                if name in (here, "code_index_store.py", "_sqlite_index_store_base.py"):
                    continue
                if name in ("contextlib.py", "functools.py", "typing.py"):
                    continue
                caller = f"{name}:{frame.lineno} {frame.name}"
                break
            chain = " < ".join(f"{Path(f.filename).name}:{f.lineno}" for f in stack[-10:])
        _emit("open", conn_no, db_full, f"{store}|{caller}", chain)
    except Exception:  # noqa: BLE001,S110 -- diagnostics must never break the caller
        pass
