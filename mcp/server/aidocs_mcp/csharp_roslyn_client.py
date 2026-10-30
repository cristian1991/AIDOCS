"""Python bridge to the Roslyn-backed C# tool (tools/aidocs-csharp-outliner).

Doctrine: ONE Python module owns the subprocess contract with the .NET
          tool. Callers (outline extractor + edit validator) ask
          high-level questions ("give me the outline", "is this content
          valid") and never touch dotnet, JSON, or stdio directly.
          Daemon mode is the PREFERRED path; single-shot is the
          fallback when the daemon can't start.
Why:      single-shot subprocess mode pays ~250ms per call (dotnet
          fork + Roslyn JIT). The daemon (long-lived `serve` process)
          drops warm-call latency to ~15-20ms.
Apply:    ``roslyn_outline(...)`` RAISES ``OutlinerUnavailable`` when the
          trusted outliner cannot be used — C# indexing has no fallback, so
          the indexer must be told WHY (it marks the file NOT_INDEXED).
          ``roslyn_validate(...)`` returns None in that case (the edit
          validator keeps its tree-sitter floor; it does not index).

WHERE THE BINARY COMES FROM. Only ``csharp_outliner_artifact`` — the trusted
store written by the install step, every byte re-hashed against its manifest
immediately before each exec (daemon spawn, daemon respawn, single-shot run).
Nothing is discovered relative to this checkout, the git common dir, a tenant
tree, or an environment variable. A running daemon is torn down the moment the
trusted manifest changes, so it can never keep serving a build the manifest no
longer vouches for.

Multi-tenant note (design rule, 2026-05-28):
    Outline + validate are STATELESS given source text — same input
    always produces same output, no project/session/workspace context.
    That means ONE daemon serves arbitrary concurrent clients via the
    daemon's internal worker pool (CPU-1, capped at 8). The daemon's
    response correlation is the integer `id` field per request.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import subprocess
import threading
import time
from typing import Any

from .csharp_outliner_artifact import (
    REASON_EXEC_FAILED,
    OutlinerUnavailable,
    VerifiedOutliner,
    manifest_digest,
    resolve_trusted_outliner,
)

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Output tuple shape used by the rest of AIDOCS:
#   (symbol, kind, line, container, is_partial)
OutlineRow = tuple[str, str, int, str | None, bool]

__all__ = [
    "OutlineRow",
    "OutlinerUnavailable",
    "invalidate_cache",
    "is_available",
    "roslyn_outline",
    "roslyn_validate",
]


def is_available() -> bool:
    """True iff the trusted outliner verifies RIGHT NOW (full hash check)."""
    try:
        resolve_trusted_outliner()
    except OutlinerUnavailable:
        return False
    return True


def invalidate_cache() -> None:
    """Tear down any running daemon so the next request re-verifies and
    re-spawns from scratch (e.g. after an install, or between tests)."""
    global _daemon
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
    Apply:    callers go through ``request()``; the class handles
              auto-start on first use, supervised restart on crash,
              and clean shutdown via atexit. EVERY spawn (first start and
              each restart) re-verifies the trusted build first. Subprocess
              stderr is discarded unless AIDOCS_CSHARP_DAEMON_STDERR=1.
    """

    REQUEST_TIMEOUT_S = 30.0

    # Residency state machine (#: daemon must STAY resident):
    #   - a healthy daemon is REUSED (proc.poll() is None → fast return).
    #   - a daemon that died is RESTARTED (poll() not None → re-spawn).
    #   - repeated spawn failures back off, then after MAX_SPAWN_FAILURES
    #     the daemon is declared unavailable for the PROCESS LIFETIME so we
    #     stop re-forking a doomed `dotnet` on every call and use single-shot.
    MAX_SPAWN_FAILURES = 2
    RESTART_BACKOFF_S = 2.0

    def __init__(self, manifest_id: str) -> None:
        #: Identity of the trusted manifest this daemon was verified against.
        self.manifest_id = manifest_id
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._id_gen = itertools.count(1)
        # id → (Event, response container).
        self._inflight: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._reader: threading.Thread | None = None
        self._ready_banner: dict[str, Any] | None = None
        # State-machine bookkeeping.
        self._spawn_failures = 0
        self._giveup = False  # unavailable for the rest of this process
        self._last_attempt = 0.0  # monotonic time of last spawn attempt

    def _spawn_process(self) -> subprocess.Popen[str]:
        """Verify the trusted build, then spawn the `serve`-mode daemon.

        Raises OSError on spawn failure and OutlinerUnavailable when the
        build does not verify. Isolated as a seam so the residency state
        machine (and its tests) can be exercised without a real toolchain.
        """
        verified = resolve_trusted_outliner()  # immediately before the exec
        if verified.manifest_digest != self.manifest_id:
            raise OutlinerUnavailable(REASON_EXEC_FAILED, "trusted manifest changed under the daemon")
        stderr_target = (
            None if os.environ.get("AIDOCS_CSHARP_DAEMON_STDERR") == "1" else subprocess.DEVNULL
        )
        # #345: routed through audited_popen (ledger row per spawn).
        # Passthrough lambda IS the registered AST callsite; kwargs UNCHANGED.
        from .shell_egress_service import audited_popen

        return audited_popen(
            [verified.dotnet, "exec", str(verified.dll), "serve"],
            fingerprint=("csharp_roslyn_client.py", "_spawn_process", "subprocess.Popen"),
            reason="roslyn-daemon-spawn",
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=True,
            bufsize=1,  # line-buffered
            creationflags=_WIN_NO_WINDOW,
        )

    def _record_spawn_failure(self) -> None:
        """Account one failed spawn/handshake; latch giveup at the cap."""
        self._spawn_failures += 1
        if self._spawn_failures >= self.MAX_SPAWN_FAILURES:
            self._giveup = True

    def _start_unlocked(self) -> bool:
        """Ensure the daemon is up. Caller holds self._lock.

        Returns True when a resident process is ready to serve. False on
        any startup failure — caller falls back to single-shot mode. A
        False return NEVER re-forks on every call: within the backoff
        window and after giveup we short-circuit without spawning.
        OutlinerUnavailable from verification propagates (no exec at all).
        """
        # (a) Healthy daemon → reuse.
        if self._proc is not None and self._proc.poll() is None:
            return True
        # Dead handle — drop it before deciding whether to restart.
        self._proc = None
        # (c) This box already proved the daemon can't run — don't re-probe.
        if self._giveup:
            return False
        # Backoff: after a failed attempt, don't re-fork a doomed dotnet on
        # every call; let callers use single-shot until the window elapses.
        now = time.monotonic()
        if self._spawn_failures > 0 and (now - self._last_attempt) < self.RESTART_BACKOFF_S:
            return False
        self._last_attempt = now
        try:
            self._proc = self._spawn_process()
        except OSError:
            self._proc = None
            self._record_spawn_failure()
            return False
        # Read the ready banner (first line emitted by serve mode).
        assert self._proc.stdout is not None
        try:
            line = self._proc.stdout.readline()
            self._ready_banner = json.loads(line) if line else None
        except (OSError, json.JSONDecodeError):
            self._terminate_unlocked()
            self._record_spawn_failure()
            return False
        if not self._ready_banner or not self._ready_banner.get("ready"):
            self._terminate_unlocked()
            self._record_spawn_failure()
            return False
        # Healthy start — reset the failure ledger and spin up the reader.
        self._spawn_failures = 0
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


def _get_daemon() -> _RoslynDaemon:
    """The daemon bound to the CURRENT trusted manifest.

    Raises OutlinerUnavailable when there is no trusted manifest. A daemon
    verified against a different manifest is shut down and replaced, so a
    reinstall / uninstall / tamper of the manifest takes effect at once."""
    global _daemon
    current = manifest_digest()  # cheap: manifest read, no file hashing
    with _daemon_lock:
        if _daemon is not None and _daemon.manifest_id != current:
            _daemon.shutdown()
            _daemon = None
        if _daemon is None:
            _daemon = _RoslynDaemon(current)
            atexit.register(_daemon.shutdown)
        return _daemon


def _run(verified: VerifiedOutliner, mode: str, stdin_text: str) -> dict[str, Any] | list[Any] | None:
    """Invoke the VERIFIED Roslyn tool in <mode> with content piped on stdin.

    Returns the parsed JSON (dict for validate, list for outline) on
    success, None on any tool-side failure (broken JSON, bad exit, hang).
    """
    try:
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        proc = audited_run(
            [verified.dotnet, "exec", str(verified.dll), mode, "-"],
            fingerprint=("csharp_roslyn_client.py", "_run", "subprocess.run"),
            reason="roslyn-single-shot",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            input=stdin_text,
            capture_output=True,
            text=True,
            # 30s is generous — the tool processes one file at a time
            # and Roslyn parsing is fast. A hang here means dotnet
            # itself wedged, not a real parse issue.
            timeout=30,
            creationflags=_WIN_NO_WINDOW,
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


def roslyn_outline(text: str, *, ext: str = ".cs") -> list[OutlineRow]:
    """Extract an outline from C#-family source ``text`` via the TRUSTED tool.

    Daemon path first; on miss, a single-shot run of the freshly re-verified
    build. Raises ``OutlinerUnavailable`` (with a reason code) when the
    trusted outliner is missing, unverified, or fails — there is no fallback.
    Empty list = parsed, no declarations.
    """
    daemon = _get_daemon()
    resp = daemon.request("outline", ext, text)
    if resp is not None and "outline" in resp and isinstance(resp["outline"], list):
        return _entries_to_rows(resp["outline"])
    if ext.lower() in (".cshtml", ".razor"):
        # The single-shot path pipes stdin as `-` and the tool assumes .cs
        # there (#811), so Razor has no honest single-shot answer.
        raise OutlinerUnavailable(REASON_EXEC_FAILED, "daemon unavailable for Razor outline")
    verified = resolve_trusted_outliner()  # immediately before the exec
    parsed = _run(verified, "outline", text)
    if not isinstance(parsed, list):
        raise OutlinerUnavailable(REASON_EXEC_FAILED, "single-shot outline returned no outline")
    return _entries_to_rows(parsed)


def roslyn_validate(content: str, *, ext: str = ".cs") -> dict[str, Any] | None:
    """Validate C# ``content`` for parse errors.

    Daemon path is tried first; falls back to single-shot on miss.
    Returns a dict mirroring the legacy ``validate_csharp_content``
    contract:
        {"ok": bool, "error_nodes": [...], "parser": str}
    Roslyn additionally emits a ``code`` field per error (e.g. "CS1002").

    Returns None when the trusted tool is unavailable or fails — the edit
    validator (not an indexer) keeps its own tree-sitter floor.
    """
    try:
        daemon = _get_daemon()
        resp = daemon.request("validate", ext, content)
    except OutlinerUnavailable:
        return None
    if resp is not None and "validate" in resp and isinstance(resp["validate"], dict):
        v = resp["validate"]
        if "ok" in v and "error_nodes" in v:
            return v
    # #811: the single-shot path pipes content on stdin as `-`, and
    # Program.cs has no extension to read off a pipe — it assumes .cs.
    # So for Razor the ext hint is LOST there; do not call it. Return the
    # same no-op verdict the tool itself gives for Razor.
    if ext.lower() in (".cshtml", ".razor"):
        return {
            "ok": True,
            "error_nodes": [],
            "parser": "roslyn-net9 (razor validate not supported; outline-only)",
        }
    try:
        verified = resolve_trusted_outliner()  # immediately before the exec
    except OutlinerUnavailable:
        return None
    parsed = _run(verified, "validate", content)
    if not isinstance(parsed, dict):
        return None
    if "ok" not in parsed or "error_nodes" not in parsed:
        return None
    return parsed
