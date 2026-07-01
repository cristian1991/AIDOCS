"""Python bridge to the Roslyn-backed C# tool at tools/aidocs-csharp-outliner.

Doctrine: ONE Python module owns the subprocess contract with the .NET
          tool. Callers (outline extractor + edit validator) ask
          high-level questions ("give me the outline", "is this content
          valid") and never touch dotnet, JSON, or stdio directly.
          Daemon mode is the PREFERRED path; single-shot is the
          fallback when the daemon can't start.
Why:      single-shot subprocess mode pays ~250ms per call (dotnet
          fork + Roslyn JIT). The daemon (long-lived `serve` process)
          drops warm-call latency to ~15-20ms — measured 13× faster
          than single-shot, and faster than the existing regex
          extractor on the same .cshtml.
Apply:    callers call ``roslyn_outline(...)`` or ``roslyn_validate(...)``.
          Both return None when the Roslyn tool is unavailable
          (dotnet not on PATH, dll not built). Internally we try
          the daemon first; if startup fails, we transparently fall
          back to single-shot for that one call (and try the daemon
          again on the next).

Multi-tenant note (king directive, "space-faring empire" 2026-05-28):
    Outline + validate are STATELESS given source text — same input
    always produces same output, no project/session/workspace context.
    That means ONE daemon serves arbitrary concurrent clients via the
    daemon's internal worker pool (CPU-1, capped at 8). A web-MCP
    gate process serving 1000 clients still runs ONE daemon, RSS
    stays ~150MB regardless of client count. The daemon's response
    correlation is the integer `id` field per request — concurrent
    senders can be in flight without ordering constraints.
    For multi-PROCESS gate workers (uvicorn -w N), each worker spawns
    its own daemon — N daemons total, ~150MB × N, still tractable.

Availability rule:
    The Roslyn binary is considered available iff
      (1) `dotnet` is on PATH, AND
      (2) the built dll exists at
          <repo>/tools/aidocs-csharp-outliner/bin/Release/net9.0/aidocs-csharp-outliner.dll.

    The check is cached at module load. To re-check (e.g. after a
    rebuild in the same process), call ``invalidate_cache()``.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

# Output tuple shape used by the rest of AIDOCS (matches the legacy
# regex extractor's contract):
#   (symbol, kind, line, container, is_partial)
OutlineRow = tuple[str, str, int, str | None, bool]


def _aidocs_repo_root() -> Path | None:
    """Walk up from this module looking for the AIDOCS repo marker.

    Marker: a directory containing both ``mcp/`` and ``tools/``. This is
    deliberately AIDOCS-specific (not a generic 'find pyproject.toml')
    because the binary path is anchored to the AIDOCS layout.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "mcp").is_dir() and (parent / "tools").is_dir():
            return parent
    return None


@lru_cache(maxsize=1)
def _binary_path() -> Path | None:
    """Locate the built Roslyn tool DLL, or None if not available.

    The DLL is the output of ``dotnet build -c Release`` in
    tools/aidocs-csharp-outliner. We don't auto-build on miss — that's
    the operator's deliberate step (and CI/deploy script's job).
    """
    root = _aidocs_repo_root()
    if root is None:
        return None
    # Allow override via env so a packaged install can point at a
    # ship-time-placed binary outside the repo tree.
    override = os.environ.get("AIDOCS_CSHARP_ROSLYN_DLL", "").strip()
    if override:
        p = Path(override)
        return p if p.is_file() else None
    candidate = (
        root
        / "tools"
        / "aidocs-csharp-outliner"
        / "bin"
        / "Release"
        / "net9.0"
        / "aidocs-csharp-outliner.dll"
    )
    return candidate if candidate.is_file() else None


@lru_cache(maxsize=1)
def is_available() -> bool:
    """True iff dotnet is on PATH AND the Roslyn tool dll is present.

    Cached for the life of the process. Call ``invalidate_cache()``
    after a build/install to re-probe.
    """
    if shutil.which("dotnet") is None:
        return False
    return _binary_path() is not None


def invalidate_cache() -> None:
    """Forget the cached availability + binary-path probe AND tear down
    any running daemon, so the next request re-probes from scratch.

    Useful after a fresh ``dotnet build`` or an env-var change in the
    same Python process (e.g. tests that rebuild between cases).
    """
    global _daemon
    is_available.cache_clear()
    _binary_path.cache_clear()
    with _daemon_lock:
        if _daemon is not None:
            _daemon.shutdown()
            _daemon = None


class _RoslynDaemon:
    """Singleton-per-process supervisor for the `serve`-mode .NET process.

    Doctrine: one process per Python process, shared across all threads.
              Each request gets a unique integer id; the reader thread
              demultiplexes responses back to the waiting caller via a
              dict of Event+slot pairs.
    Why:      Roslyn parsing is thread-safe for independent SyntaxTrees,
              the daemon's worker pool already serializes stdout writes,
              and JSON-per-line framing means concurrent Python callers
              can fan in without ordering constraints.
    Apply:    callers go through ``request()``; the class handles
              auto-start on first use, supervised restart on crash,
              and clean shutdown via atexit. Subprocess stderr is
              discarded by default — if daemon misbehaves, inspect by
              flipping the env var AIDOCS_CSHARP_DAEMON_STDERR=1.
    """

    REQUEST_TIMEOUT_S = 30.0

    def __init__(self, dll_path: Path) -> None:
        self._dll = dll_path
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._id_gen = itertools.count(1)
        # id → (Event, response container).
        self._inflight: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._reader: threading.Thread | None = None
        self._ready_banner: dict[str, Any] | None = None

    def _start_unlocked(self) -> bool:
        """Start the daemon process. Caller holds self._lock.

        Returns True on success (process running + ready banner read).
        False on any startup failure — caller should fall back to
        single-shot mode.
        """
        if self._proc is not None and self._proc.poll() is None:
            return True
        stderr_target = (
            None if os.environ.get("AIDOCS_CSHARP_DAEMON_STDERR") == "1" else subprocess.DEVNULL
        )
        try:
            self._proc = subprocess.Popen(
                ["dotnet", "exec", str(self._dll), "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_target,
                text=True,
                bufsize=1,  # line-buffered
            )
        except OSError:
            self._proc = None
            return False
        # Read the ready banner (first line emitted by serve mode).
        assert self._proc.stdout is not None
        try:
            line = self._proc.stdout.readline()
            self._ready_banner = json.loads(line) if line else None
        except (OSError, json.JSONDecodeError):
            self._terminate_unlocked()
            return False
        if not self._ready_banner or not self._ready_banner.get("ready"):
            self._terminate_unlocked()
            return False
        # Spin up the reader thread.
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # One bad line is not a fatal protocol violation;
                # other inflight requests still drain via stream
                # framing (each response is one line).
                continue
            rid = int(msg.get("id") or 0)
            with self._lock:
                slot = self._inflight.pop(rid, None)
            if slot is not None:
                evt, container = slot
                container.append(msg)
                evt.set()
        # stdout closed → daemon died. Mark inflight slots as failed
        # so waiters wake up and fall back.
        with self._lock:
            for _, (evt, container) in list(self._inflight.items()):
                container.append({"error": "daemon stdout closed mid-request"})
                evt.set()
            self._inflight.clear()
            self._proc = None

    def _terminate_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.write(json.dumps({"id": 0, "mode": "quit"}) + "\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def request(self, mode: str, ext: str, content: str) -> dict[str, Any] | None:
        """Send one request; block until response or timeout.

        Returns the parsed response dict (with 'outline' / 'validate' /
        'error' field) or None if the daemon could not be reached.
        Caller checks for the appropriate field and translates to the
        Python contract (list/dict/None).
        """
        # Ensure daemon is up.
        with self._lock:
            if not self._start_unlocked():
                return None
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            rid = next(self._id_gen)
            evt = threading.Event()
            container: list[Any] = []
            self._inflight[rid] = (evt, container)

        req = {"id": rid, "mode": mode, "ext": ext, "content": content}
        try:
            line = json.dumps(req)
        except (TypeError, ValueError):
            with self._lock:
                self._inflight.pop(rid, None)
            return None
        try:
            # The stdin write is the only contended resource; we hold
            # the lock just long enough to serialize the write itself
            # (NOT the wait — wait happens unlocked so concurrent
            # callers don't block each other).
            with self._lock:
                if self._proc is None:
                    self._inflight.pop(rid, None)
                    return None
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            with self._lock:
                self._inflight.pop(rid, None)
                # The daemon's pipe is broken — mark dead so the next
                # request triggers a restart.
                self._terminate_unlocked()
            return None
        # Wait for response.
        if not evt.wait(self.REQUEST_TIMEOUT_S):
            with self._lock:
                self._inflight.pop(rid, None)
            return None
        return container[0] if container else None

    def shutdown(self) -> None:
        with self._lock:
            self._terminate_unlocked()


_daemon_lock = threading.Lock()
_daemon: _RoslynDaemon | None = None


def _get_daemon() -> _RoslynDaemon | None:
    global _daemon
    if _daemon is not None:
        return _daemon
    dll = _binary_path()
    if dll is None or shutil.which("dotnet") is None:
        return None
    with _daemon_lock:
        if _daemon is None:
            _daemon = _RoslynDaemon(dll)
            atexit.register(_daemon.shutdown)
    return _daemon


def _run(mode: str, stdin_text: str) -> dict[str, Any] | list[Any] | None:
    """Invoke the Roslyn tool in <mode> with content piped on stdin.

    Returns the parsed JSON (dict for validate, list for outline) on
    success. Returns None on any failure — broken JSON, non-zero exit
    we can't interpret, missing binary. Callers must handle None as
    "try the fallback backend."
    """
    dll = _binary_path()
    if dll is None:
        return None
    try:
        proc = subprocess.run(
            ["dotnet", "exec", str(dll), mode, "-"],
            input=stdin_text,
            capture_output=True,
            text=True,
            # 30s is generous — the tool processes one file at a time
            # and Roslyn parsing is fast. A hang here means dotnet
            # itself wedged, not a real parse issue.
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # mode=outline: rc=1 means "parsed with errors but emitted partial
    # outline". We still want to consume the JSON. mode=validate: rc
    # should always be 0 unless the tool itself broke.
    if mode == "outline" and proc.returncode not in (0, 1):
        return None
    if mode == "validate" and proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _entries_to_rows(parsed: list[Any]) -> list[OutlineRow]:
    rows: list[OutlineRow] = []
    for entry in parsed:
        try:
            rows.append(
                (
                    str(entry["symbol"]),
                    str(entry["kind"]),
                    int(entry["line"]),
                    (str(entry["container"]) if entry.get("container") is not None else None),
                    bool(entry["is_partial"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def roslyn_outline(text: str, *, ext: str = ".cs") -> list[OutlineRow] | None:
    """Extract an outline from C#-family source ``text``.

    Daemon path is tried first; on miss, falls back to single-shot.
    Returns the AIDOCS-canonical tuple list, or None if both paths
    fail (Roslyn unavailable). Empty list = parsed but no declarations.
    """
    if not is_available():
        return None
    # Daemon path.
    daemon = _get_daemon()
    if daemon is not None:
        resp = daemon.request("outline", ext, text)
        if resp is not None and "outline" in resp and isinstance(resp["outline"], list):
            return _entries_to_rows(resp["outline"])
    # Single-shot fallback. The daemon failure was logged; we don't
    # raise — caller may still get a valid answer this way.
    parsed = _run("outline", text)
    if not isinstance(parsed, list):
        return None
    return _entries_to_rows(parsed)


def roslyn_validate(content: str, *, ext: str = ".cs") -> dict[str, Any] | None:
    """Validate C# ``content`` for parse errors.

    Daemon path is tried first; falls back to single-shot on miss.
    Returns a dict mirroring the legacy ``validate_csharp_content``
    contract:
        {"ok": bool, "error_nodes": [...], "parser": str}
    Roslyn additionally emits a ``code`` field per error (e.g. "CS1002").

    Returns None on any tool-side failure — caller falls back.
    """
    if not is_available():
        return None
    daemon = _get_daemon()
    if daemon is not None:
        resp = daemon.request("validate", ext, content)
        if resp is not None and "validate" in resp and isinstance(resp["validate"], dict):
            v = resp["validate"]
            if "ok" in v and "error_nodes" in v:
                return v
    # Single-shot fallback.
    parsed = _run("validate", content)
    if not isinstance(parsed, dict):
        return None
    if "ok" not in parsed or "error_nodes" not in parsed:
        return None
    return parsed
