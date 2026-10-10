"""#489 phase 2: a stack sampler that attributes hook evaluation time.

Measured 2026-10-09 from the broker's own ring: evaluation, not queueing,
dominates (queue p50 0.02 ms; eval p95 4.1 s, max 43.4 s). SQLite accounts for
only part of it (a 43.4 s SessionStart had 8.9 s SQLite; prompts take 2-5 s
with ~1 s SQLite). The remainder had no attribution, and the obvious suspect
(spaCy) was a guess. This module replaces the guess with a measurement.

HOW. While one evaluation runs, a daemon thread reads that thread's current
stack (``sys._current_frames``) every ``interval_s`` and charges the interval
to:

* the LIBRARY doing the work: the first frame from the top that is neither
  stdlib nor unknown (``spacy``, ``thinc``, ``numpy``, ``chromadb``, ``aidocs``,
  a vendored package under ``aidocs_mcp/_vendor/<pkg>``, ...);
* the innermost ``aidocs`` function (``file.py:function``), i.e. which of our
  call sites the time was spent under.

HONEST LIMITS. Sampling, not tracing: a cost shorter than the interval can be
missed, and C-level work (SQLite, model inference) is charged to the Python
frame that called it -- an inference, not a proof. It reads only its target
thread, so work handed to other threads is not counted.

OFF BY DEFAULT (r0b2 BLOCK 5c9ece29-30c). A sampler thread on every prompt is
extra work on the very path that misses its 10 s budget, so nothing samples
unless the operator ARMS a window:

    python -m aidocs_mcp.hook_stack_sampler arm --minutes 30 [--max-events 50]
    python -m aidocs_mcp.hook_stack_sampler disarm

The arm file (``<runtime root>/hook-sampler.json``, written atomically and
owner-only) is read per heavy event; an expired, over-long (> MAX_WINDOW_S) or
malformed arm is OFF. A window profiles at most ``max_events`` (<=
MAX_EVENTS_CEILING) evaluations IN TOTAL: each one claims a slot file under
``hook-sampler-slots/`` and then fences on the arm file, so neither a broker
restart, a second broker nor a clock jump resets the ceiling, and disarm or a
re-arm stops the previous window (CONTRACT at the slot store). Disarmed, the
cost is one failed file open per UserPromptSubmit/SessionStart.

BOUNDED OUTPUT. Library labels must look like a package name and function
sites like ``file.py:function``; anything else is ``other``, and at most
MAX_LIBRARIES libraries / MAX_SITES sites are kept, ``other`` included. File
paths, locals, arguments and prompt text are never recorded.
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePath
from typing import Any, Callable

__all__ = [
    "PROFILED_EVENTS", "StackSampler", "arm", "classify_frame_file", "disarm", "maybe_sampler",
]

#: The events worth profiling: the ones measured over the client budget.
PROFILED_EVENTS = frozenset({"UserPromptSubmit", "SessionStart"})

ARM_FILENAME = "hook-sampler.json"
SLOTS_DIRNAME = "hook-sampler-slots"
MAX_WINDOW_S = 2 * 3600
MAX_EVENTS_CEILING = 200
DEFAULT_MAX_EVENTS = 50
MAX_LIBRARIES = 16
MAX_SITES = 64

DEFAULT_INTERVAL_S = 0.01
_JOIN_S = 0.05
_MAX_DEPTH = 300
_TOP_SITES = 12
_PASSIVE = frozenset({"stdlib", "unknown"})
_OTHER = "other"
_LIB_SHAPE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_FILE_SHAPE = re.compile(r"[a-z0-9_]{1,64}\.py")
_FUNC_SHAPE = re.compile(r"[A-Za-z_<][A-Za-z0-9_<>]{0,63}")
_WINDOW_SHAPE = re.compile(r"[0-9a-f]{16}")


def classify_frame_file(filename: str) -> str:
    """The library a frame's file belongs to (see the module docstring)."""
    if not filename:
        return "unknown"
    parts = [p.lower() for p in PurePath(filename.replace("\\", "/")).parts]
    # aidocs_mcp first: the installed runtime lives under site-packages/aidocs_mcp.
    if "aidocs_mcp" in parts:
        i = parts.index("aidocs_mcp")
        if i + 2 < len(parts) and parts[i + 1] == "_vendor":
            return _bounded_library(parts[i + 2].removesuffix(".py"))
        return "aidocs"
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                return _bounded_library(parts[i + 1].removesuffix(".py").split(".")[0])
    if "lib" in parts or any(p.startswith("python3") for p in parts):
        return "stdlib"
    if filename.startswith("<"):
        return "unknown"
    return _OTHER


def _bounded_library(name: str) -> str:
    return name if _LIB_SHAPE.fullmatch(name or "") else _OTHER


def _site_label(filename: str, co_name: str) -> str:
    """``file.py:function`` for one of our frames, or ``other`` when either part
    is not identifier-shaped. Only the file NAME, never its path."""
    name = PurePath(str(filename or "").replace("\\", "/")).name
    func = str(co_name or "")
    if _FILE_SHAPE.fullmatch(name) and _FUNC_SHAPE.fullmatch(func):
        return f"{name}:{func}"
    return _OTHER


def _default_classify(code: Any) -> str:
    return classify_frame_file(getattr(code, "co_filename", "") or "")


class StackSampler:
    """Sample one thread's stack while armed. Use as a context manager."""

    def __init__(
        self,
        thread_id: int,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        classify: Callable[[Any], str] = _default_classify,
    ) -> None:
        self._tid = thread_id
        self._interval = max(float(interval_s), 0.001)
        self._classify = classify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples = 0
        self._sampled_ms = 0.0
        self._by_library: collections.Counter[str] = collections.Counter()
        self._by_site: collections.Counter[str] = collections.Counter()

    def __enter__(self) -> "StackSampler":
        try:
            self._thread = threading.Thread(target=self._run, name="aidocs-hook-sampler", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 -- an instrument never breaks the evaluation
            self._thread = None
        return self

    def __exit__(self, *exc: Any) -> None:
        # A SHORT join: the sampler is a daemon thread that stops at its next
        # wake, so the evaluation never waits on it beyond _JOIN_S. A sample
        # still in flight after the join is discarded (see _sample).
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=_JOIN_S)
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        # Charge the MEASURED gap since the previous sample, not the nominal
        # interval: timer resolution and the GIL stretch the real gap (measured
        # ~30 ms for a nominal 10 ms on Windows), so a nominal charge undercounts.
        last = time.perf_counter()
        while not self._stop.wait(self._interval):
            now = time.perf_counter()
            gap_ms, last = (now - last) * 1000.0, now
            try:
                frame = sys._current_frames().get(self._tid)  # noqa: SLF001
                if frame is not None:
                    self._sample(frame, gap_ms)
            except Exception:  # noqa: BLE001
                pass

    def _sample(self, frame: Any, gap_ms: float) -> None:
        library = ""
        site = ""
        depth = 0
        while frame is not None and depth < _MAX_DEPTH and not (library and site):
            code = frame.f_code
            kind = self._classify(code)
            if not library and kind not in _PASSIVE:
                library = kind
            if not site and kind == "aidocs":
                site = _site_label(code.co_filename, code.co_name)
            frame = frame.f_back
            depth += 1
        if self._stop.is_set():
            return  # the evaluation already ended; never charge after summary()
        self._samples += 1
        self._sampled_ms += gap_ms
        _charge(self._by_library, library or "stdlib", gap_ms, MAX_LIBRARIES)
        if site:
            _charge(self._by_site, site, gap_ms, MAX_SITES)

    def summary(self) -> dict:
        """``{interval_ms, samples, sampled_ms, by_library_ms, top_aidocs_ms}`` (ms = measured)."""
        return {
            "interval_ms": round(self._interval * 1000.0, 1),
            "samples": self._samples,
            "sampled_ms": round(self._sampled_ms, 1),
            "by_library_ms": {k: round(v, 1) for k, v in self._by_library.most_common()},
            "top_aidocs_ms": [[k, round(v, 1)] for k, v in self._by_site.most_common(_TOP_SITES)],
        }


def _charge(counter: collections.Counter, label: str, ms: float, cap: int) -> None:
    """Add ``ms`` to ``label``. ``cap`` bounds the keys INCLUDING ``other``: a
    new label arriving when only the ``other`` slot is left folds into it."""
    if label not in counter and label != _OTHER and len(counter) >= cap - 1:
        label = _OTHER
    counter[label] += ms


# ── the operator's arm window ───────────────────────────────────────────────────

# ONE AUTHORITY: THE ARM FILE (r0b2 ac3b4365-d91). Slot i of a window is claimed
# by exclusively creating <slots>/<window>.<i>; the claim then FENCES on the
# arm file: it profiles only if the arm file, re-read after the slot exists,
# still names this window and the window is still open. Slot files are NEVER
# removed (r0b2 c0561384-f94: any pruning races a concurrent arm/disarm into
# deleting a live window's spent slots). Window ids are random, so slots of
# past windows are inert; each arm adds at most MAX_EVENTS_CEILING empty files.
# Concurrent arm()s: the last atomic replace is the current window.
#
# CONTRACT. (a) At most max_events profiled evaluations per window, for any
# number of brokers, restarts, stale snapshots, clock jumps and concurrent
# arm/disarm: O_EXCL hands each slot name out once and no slot is ever
# deleted. (b) Once arm() or
# disarm() has returned, no claim for the previous window profiles; a claim
# whose fence read came first may finish the one evaluation it is sampling.
# The per-process counter is only a search hint.
_budget_lock = threading.Lock()
_next_slot: dict = {"window": None, "n": 0}


def _arm_path() -> Path:
    from .runtime_generations import runtime_root

    return runtime_root(Path.home()) / ARM_FILENAME


def _slots_dir() -> Path:
    return _arm_path().parent / SLOTS_DIRNAME


def _read_arm() -> dict | None:
    """The arm window if it is well-formed, current and not over-long; else None."""
    try:
        body = json.loads(_arm_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    until, max_events, window = body.get("until"), body.get("max_events"), body.get("window")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return None
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
        return None
    if not isinstance(window, str) or not _WINDOW_SHAPE.fullmatch(window):
        return None
    now = time.time()
    if not (now < until <= now + MAX_WINDOW_S):
        return None
    return {"until": float(until), "max_events": min(max_events, MAX_EVENTS_CEILING), "window": window}


def _is_current(window: dict) -> bool:
    current = _read_arm()
    return current is not None and current["window"] == window["window"]


def _take_budget(window: dict) -> bool:
    """Claim one profiled evaluation from this window's durable budget (see
    CONTRACT above). Any filesystem failure means no profile (fail closed)."""
    with _budget_lock:
        if _next_slot["window"] != window["window"]:
            _next_slot["window"], _next_slot["n"] = window["window"], 0
        n = _next_slot["n"]
        if n >= window["max_events"] or not _is_current(window):
            return False
        slots = _slots_dir()
        try:
            slots.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        while n < window["max_events"]:
            try:
                fd = os.open(slots / f"{window['window']}.{n}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                n += 1  # another broker, or this one before a restart, holds it
                continue
            except OSError:
                return False
            os.close(fd)
            _next_slot["n"] = n + 1
            # The fence: spent either way, profiles only if still current.
            return _is_current(window)
        _next_slot["n"] = n
        return False


def _write_private(path: Path, text: str) -> None:
    """Atomic replace through an owner-only temp file in the same directory
    (mkstemp is 0600 on POSIX; on Windows it inherits the user-profile ACL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hook-sampler.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def arm(*, minutes: float, max_events: int = DEFAULT_MAX_EVENTS) -> dict:
    """Open a profiling window (operator act). Clamped to the bounds above; a
    new window gets a fresh id and so a fresh durable budget. The previous
    window stops profiling once this returns."""
    seconds = max(1.0, min(float(minutes) * 60.0, float(MAX_WINDOW_S)))
    body = {
        "until": time.time() + seconds,
        "max_events": max(1, min(int(max_events), MAX_EVENTS_CEILING)),
        "window": secrets.token_hex(8),
    }
    _write_private(_arm_path(), json.dumps(body))
    return body


def disarm() -> None:
    """Close the window: no claim profiles once this returns. An unlink failure
    other than "already gone" is raised, never reported as disarmed."""
    with contextlib.suppress(FileNotFoundError):
        _arm_path().unlink()


def maybe_sampler(event: str) -> Any:
    """A sampler for the current thread if ``event`` is profiled AND the operator
    armed a window with budget left; else a no-op context."""
    if event in PROFILED_EVENTS:
        window = _read_arm()
        if window is not None and _take_budget(window):
            return StackSampler(threading.get_ident())
    return contextlib.nullcontext(None)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Arm or disarm the #489 hook stack sampler.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("arm")
    a.add_argument("--minutes", type=float, default=30.0)
    a.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    sub.add_parser("disarm")
    args = ap.parse_args(argv)
    if args.cmd == "arm":
        print(json.dumps(arm(minutes=args.minutes, max_events=args.max_events)))
    else:
        try:
            disarm()
        except OSError as exc:
            print(f"NOT disarmed: {exc}", file=sys.stderr)
            return 1
        print("disarmed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
