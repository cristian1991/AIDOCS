"""Detached subprocess runner — output piped to file, never blocks agent.

Why detached? Synchronous ai_run blocks the MCP event loop for the
duration of the subprocess. A 10-minute pytest run freezes the whole
conversation. Worse, the full output lands in the agent's context
even when it's 200KB of passing-test dots.

Detached flow:
  1. spawn_detached(cmd) → returns run_id immediately, writes stdout
     + stderr merged into .MEMORY/.runs/<run_id>.log as bytes arrive.
  2. A monitor thread observes process exit, records exit_code and
     duration_ms to execution_runs, then terminates.
  3. Agent polls get_run_status(run_id) / tail_run_log(run_id) to
     check progress or fetch output only when it wants to.

500ms inline-tail optimization: spawn_detached waits up to 500ms for
the subprocess to exit. If it does (version checks, quick linters),
the response includes done=True + tail so the agent gets the common
case in one round-trip.

Audit: every run_id is recorded in execution_runs via
ExecutionIndexStore.record_run; the logs directory is evidence that
task_id linkage plus Merkle chain already cover. Log files are LRU-
evicted when .MEMORY/.runs/ size exceeds the cap.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGS_SUBDIR = Path(".MEMORY") / ".runs"

# Windows: the daemon runs console-less (pythonw). Without this flag every
# background subprocess spawn allocates a NEW visible console window
# (#333 Phase 2). ORed into creationflags at background-mode callsites only
# — foreground=True deliberately pops CREATE_NEW_CONSOLE so the operator can
# watch the command live; that path is left untouched. POSIX no-op.
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
# Phoenix 2026-05-09: bumped 0.5→0.7 — fewer commands tip into the
# detached/notify path on slow shells (Windows process startup adds
# ~150-300ms before the command even starts running).
INLINE_TAIL_WAIT_SECONDS = 0.7
INLINE_TAIL_BYTES = 2048  # ~500 tokens; plenty for version strings
DEFAULT_TAIL_BYTES = 4096  # ai_run_output default
RUNS_DIR_SIZE_CAP_BYTES = 200 * 1024 * 1024  # 200 MB total
# Count cap (Empire directive 2026-05-31): keep at most this many recent
# completed runtime reports — runs pile up and fill the server otherwise.
# Enforced alongside the size cap; live (in-flight) runs are never evicted.
RUNS_DIR_MAX_FILES = 5
# Default timeout for detached runs. The CEILING now lives in
# box_profile (HARD_TIMEOUT_CEILING_SECONDS + effective_timeout_ceiling,
# #456/#466) — the old MAX_RUN_TIMEOUT_* constants died with the silent
# clamp; requested timeouts are honored exactly or refused explicitly.
DEFAULT_RUN_TIMEOUT_SECONDS = 600


# Timeout ceiling resolution moved to box_profile.effective_timeout_ceiling
# (#456/#466): ONE box-profile authority derives the ceiling (operator-set
# run.max_timeout_seconds wins; otherwise box-adaptive), and requests over
# the ceiling are REFUSED explicitly in resolve_run_timeout — the old
# silent clamp here (max(1, min(requested, ceiling))) dropped the caller's
# value without a word and tree-killed long suites mid-run.


# Module-level registry: run_id → live monitor so kill_run can reach
# the Popen and signal it. Populated on spawn, cleaned on completion.
_LIVE_RUNS: dict[str, _LiveRun] = {}
_LIVE_RUNS_LOCK = threading.Lock()


def _pids_exposed(project_root: Path | None) -> bool:
    """Check `observability.expose_pids` — dashboard-toggled. When off,
    PID is hidden from agent-facing tool responses to keep context clean.
    """
    if project_root is None:
        return False
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "observability.expose_pids",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


# Post-completion cache: run_id → {"exit_code", "duration_seconds"}
# so get_run_status / tail_run_log keep returning the final state
# after the monitor tears down the live entry. Bounded so long
# sessions don't grow unbounded.
_FINISHED_RUNS: dict[str, dict[str, Any]] = {}
_FINISHED_RUNS_LOCK = threading.Lock()
_FINISHED_RUNS_CAP = 500


def _record_finished(run_id: str, exit_code: int, duration: float) -> None:
    with _FINISHED_RUNS_LOCK:
        if len(_FINISHED_RUNS) >= _FINISHED_RUNS_CAP:
            # LRU-ish: drop arbitrary oldest key. This cache is
            # advisory — logs are the real audit trail.
            try:
                oldest = next(iter(_FINISHED_RUNS))
                _FINISHED_RUNS.pop(oldest, None)
            except StopIteration:
                pass
        _FINISHED_RUNS[run_id] = {
            "exit_code": int(exit_code),
            "duration_seconds": round(duration, 2),
        }


def _status_sidecar_path(project_root: Path, run_id: str) -> Path:
    """Persistent exit_code + duration so status survives MCP restart."""
    return _logs_dir(project_root) / f"{run_id}.status"


def delete_run_artifacts(project_root: Path, run_id: str) -> bool:
    """Drop the (cmd, log, status) triplet for a completed run.

    Backlog #32: artifacts pile up forever otherwise. Called from
    ai_run_output after a successful tail of a completed run, so
    "single read = single use" — agent gets the output, file is gone.

    Best-effort: any per-file failure is swallowed. Returns True if
    at least one file was deleted, False if no triplet existed.

    Safety: caller is responsible for ensuring the run is COMPLETED
    before calling. Deleting in-flight artifacts would orphan the
    live spawner's writes; this helper does not double-check status
    (the read-on-completed contract in ai_run_output is the gate).
    """
    deleted = False
    for path in (
        _cmd_path(project_root, run_id),
        _log_path(project_root, run_id),
        _status_sidecar_path(project_root, run_id),
    ):
        try:
            if path.exists():
                path.unlink()
                deleted = True
        except OSError:
            # Windows file-lock or transient permission — leave the
            # file in place; next call (or session-end sweep when it
            # lands) can clean up. Better than crashing the read.
            pass
    return deleted


def sweep_orphan_run_artifacts(
    project_root: Path,
    max_age_seconds: int = 86400,
) -> int:
    """Drop triplets for completed runs older than max_age_seconds.

    #32 follow-up: ai_run_output deletes triplets on read, but
    runs the agent never reads (background spawn, agent crash,
    operator pivot) leave artifacts forever. This sweep catches
    those orphans.

    Safety contract:
      - Only deletes triplets where the .status sidecar exists
        (proxy for "run completed and persisted exit/duration")
      - Only deletes when ALL THREE files in the triplet are older
        than max_age_seconds — protects against deleting an in-
        flight run whose log is still being written
      - Best-effort per-file; OSError swallowed (Windows lock)

    Returns count of run_ids fully swept. Does NOT touch live runs
    (no .status file) or runs younger than the threshold.

    Default 24h matches the typical "operator forgot, agent moved
    on" window; configurable per project once #32 grows a config
    schema entry.
    """
    import time as _time

    runs_dir = _logs_dir(project_root)
    if not runs_dir.is_dir():
        return 0
    now = _time.time()
    swept = 0
    for status_file in runs_dir.glob("*.status"):
        try:
            run_id = status_file.stem
            cmd_file = _cmd_path(project_root, run_id)
            log_file = _log_path(project_root, run_id)
            # All-three-old check: protects in-flight runs whose
            # status was eagerly written but log is still appending.
            stats = []
            for f in (status_file, cmd_file, log_file):
                if not f.exists():
                    continue
                try:
                    stats.append(now - f.stat().st_mtime)
                except OSError:
                    stats = []
                    break
            if not stats or min(stats) < max_age_seconds:
                continue
            if delete_run_artifacts(project_root, run_id):
                swept += 1
        except OSError:
            continue
    return swept


def _write_status_sidecar(
    project_root: Path,
    run_id: str,
    exit_code: int,
    duration: float,
    *,
    session_id: str = "",
    agent_context_id: str = "",
    lane_id: str = "",
) -> None:
    """Persist completion plus the canonical run-owner identity tuple."""
    try:
        _status_sidecar_path(project_root, run_id).write_text(
            (
                f"{exit_code}\n{duration:.2f}\n{session_id}\n"
                f"{lane_id}\n{agent_context_id}\n"
            ),
            encoding="utf-8",
        )
    except OSError:
        pass



def _read_status_sidecar(
    project_root: Path,
    run_id: str,
) -> dict[str, Any] | None:
    try:
        raw = (
            _status_sidecar_path(project_root, run_id)
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
    except (OSError, UnicodeDecodeError):
        return None
    if len(raw) < 2:
        return None
    try:
        return {
            "exit_code": int(raw[0].strip()),
            "duration_seconds": float(raw[1].strip()),
            "session_id": raw[2].strip() if len(raw) >= 3 else "",
            "lane_id": raw[3].strip() if len(raw) >= 4 else "",
            "agent_context_id": raw[4].strip() if len(raw) >= 5 else "",
        }
    except (ValueError, IndexError):
        return None



def run_owner_mismatch(
    sidecar: dict[str, Any] | None,
    *,
    session_id: str,
    agent_context_id: str,
) -> str:
    """Return the first ownership mismatch for an attributed run."""
    if not sidecar:
        return ""
    expected_session = str(sidecar.get("session_id") or "").strip()
    if expected_session and expected_session != (session_id or "").strip():
        return "cross_session_run_id"
    expected_actor = str(sidecar.get("agent_context_id") or "").strip()
    if expected_actor and expected_actor != (agent_context_id or "").strip():
        return "cross_actor_run_id"
    return ""


@dataclass
class _LiveRun:
    run_id: str
    proc: subprocess.Popen
    log_path: Path
    started_at: float
    timeout_seconds: int
    command: str = ""
    project_root: Path | None = None
    session_id: str = ""
    agent_context_id: str = ""
    lane_id: str = ""
    # #466: which timeout governs this run ("requested" | "default") so
    # the timeout-kill log marker states WHO set the limit that fired.
    timeout_governed_by: str = "requested"



def _cmd_path(project_root: Path, run_id: str) -> Path:
    """Sidecar that stores the original command for a run_id so
    ai_run_output can route to the right renderer (test vs build
    vs probe) even after process restart wipes _LIVE_RUNS.
    """
    return _logs_dir(project_root) / f"{run_id}.cmd"


def _read_run_command(project_root: Path, run_id: str) -> str:
    """Best-effort lookup. Returns "" if no sidecar (older runs)."""
    try:
        return _cmd_path(project_root, run_id).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _compute_run_id(command: str, started_at: float) -> str:
    """Content-addressed run_id stable across retries within a
    1-second bucket (so double-invocations of the same command dedupe).
    """
    bucket = int(started_at)
    raw = f"{command}|{bucket}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"r_{digest}"


def _logs_dir(project_root: Path) -> Path:
    path = project_root / LOGS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_path(project_root: Path, run_id: str) -> Path:
    return _logs_dir(project_root) / f"{run_id}.log"


def _foreground_wrap(
    command: str,
    *,
    bash_path: str | None = None,
) -> tuple[Any, bool]:
    """Wrap a command so it opens in a visible terminal window.

    Returns (popen_arg, ok). On failure returns (error_message, False).

    Batch B (canonical 2026-04-29): cmd.exe is permanently refused.
    Windows foreground spawns directly through Git Bash + the
    CREATE_NEW_CONSOLE creation flag — the bash process owns the new
    console, no cmd.exe shim. The wrapped command uses the bash
    ``read`` builtin to keep the window open after the command exits.

    POSIX: probes for a terminal emulator on PATH and launches bash
    through it. Same wrap shape (read-to-close).

    The ``bash_path`` kwarg is the resolver-determined Git Bash /
    native bash path. If omitted (None), we fall back to
    ``shutil.which("bash")``; the caller in spawn_detached always
    passes the resolved path so this fallback is defense-in-depth.
    """
    import shutil as _shutil

    if bash_path is None:
        bash_path = _shutil.which("bash")
    if not bash_path:
        return (
            "foreground unsupported: no Bash-compatible provider "
            "available. Install Git for Windows (Windows) or bash "
            "(Linux/macOS).",
            False,
        )

    # Window-keep-open wrap. Bash-native: capture exit status, prompt,
    # block on read, propagate exit. ``_aidocs_ec`` is namespaced so
    # the user's command can't accidentally collide with a $?-reading
    # variable.
    wrapped_command = (
        f"{command}; _aidocs_ec=$?; echo; "
        f'echo "[exit $_aidocs_ec] press enter to close"; read; '
        f"exit $_aidocs_ec"
    )

    if sys.platform == "win32":
        # Direct bash + CREATE_NEW_CONSOLE (set in
        # _popen_kwargs_for_platform when foreground=True). No
        # cmd.exe involvement; bash owns the new console.
        return ([bash_path, "-c", wrapped_command], True)

    # POSIX terminal emulator probing.
    candidates = [
        # (binary, args-template). All wrappers route through
        # ``bash -c`` (NOT ``-lc``) per the AIDOCS shell provider
        # lock — no login-shell startup file sourcing.
        ("gnome-terminal", ["--", bash_path, "-c"]),
        ("konsole", ["-e", bash_path, "-c"]),
        ("xterm", ["-hold", "-e", bash_path, "-c"]),
        ("alacritty", ["-e", bash_path, "-c"]),
        ("kitty", [bash_path, "-c"]),
        ("wezterm", ["start", "--", bash_path, "-c"]),
        # macOS — Terminal.app via `open`
        ("open", ["-a", "Terminal"]),
    ]
    for binary, args in candidates:
        if _shutil.which(binary):
            if binary == "open":
                # macOS Terminal.app — write the wrapped command to
                # a tmp .command file so the user sees output and
                # the window stays open. Using bash -c semantics in
                # the script body for parity with non-macOS paths.
                import stat
                import tempfile

                script = f"#!{bash_path}\n{wrapped_command}\n"
                fd = tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".command",
                    delete=False,
                )
                try:
                    fd.write(script)
                    fd.flush()
                    path = fd.name
                finally:
                    fd.close()
                os.chmod(
                    path,
                    os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP,
                )
                return ([binary, "-a", "Terminal", path], True)
            # Linux terminal emulators.
            return ([binary] + args + [wrapped_command], True)
    return (
        "foreground unsupported: no terminal emulator found on PATH "
        "(tried gnome-terminal, konsole, xterm, alacritty, kitty, "
        "wezterm, open/Terminal.app). Use foreground=false or install "
        "a terminal emulator.",
        False,
    )


def _popen_kwargs_for_platform(foreground: bool = False) -> dict[str, Any]:
    """Platform-specific Popen flags.

    Background (default): isolated process group so kill_run doesn't
    take out the MCP server. Windows CREATE_NEW_PROCESS_GROUP (0x200)
    lets CTRL_BREAK_EVENT target just the subprocess tree; POSIX uses
    start_new_session for an isolated pgid.

    Foreground: spawn with a visible terminal window so the operator
    can watch the command live. On Windows, CREATE_NEW_CONSOLE (0x10)
    pops a new cmd.exe window; on POSIX the caller wraps the command
    through a terminal emulator so we just need start_new_session
    here. Output still goes to the log file (stdout/stderr redirected
    to the log handle), so foreground=true is belt-and-suspenders:
    operator watches live, agent reads the log later.
    """
    if sys.platform == "win32":
        # CREATE_NEW_CONSOLE (0x10) — new cmd window, child attaches to it.
        # CREATE_NEW_PROCESS_GROUP (0x200) — isolated signal group.
        flags = 0x10 if foreground else (0x200 | _WIN_NO_WINDOW)
        return {"creationflags": flags}
    return {"start_new_session": True}


def spawn_detached(
    command: str,
    project_root: Path,
    *,
    timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    shell: bool = True,
    cwd: Path | None = None,
    foreground: bool = False,
    argv_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch a subprocess, stream output to a log file, return
    immediately with run_id.

    foreground=True opens a new terminal window so the operator can
    watch the command run live. On Windows this spawns a new cmd
    console; on POSIX the command is wrapped through a terminal
    emulator (x-terminal-emulator / gnome-terminal / alacritty if
    available — fails cleanly when no emulator is on PATH).

    In foreground mode, stdout/stderr go to the visible terminal,
    NOT the log file — the log file stays empty because the whole
    point is the operator watching the window. Agent still gets
    run_id + state + exit_code via get_run_status.

    If the process finishes within INLINE_TAIL_WAIT_SECONDS, the
    response includes done=True + tail (background mode) so quick
    commands don't cost three tool round-trips.

    argv_command (#466, ai_test detach): when given, the process is
    spawned DIRECTLY from this argv (shell=False, no bash -c wrap, no
    shell resolver) — the caller owns argv safety (ai_test's validated
    token law). `command` then serves only as the display/classify
    string (defaults to the joined argv). foreground is unsupported in
    argv mode. `env` optionally replaces the inherited environment.

    Timeout governance (#466): the caller's timeout_seconds is honored
    EXACTLY up to the box ceiling (box_profile.effective_timeout_ceiling);
    an over-ceiling request returns an explicit refusal naming the
    ceiling — never a silent clamp. Every success response carries a
    `governance` block stating which timeout governs and the box profile.
    """
    # ai_run boundary trace (dev.runtime.ai_run_trace, dev flavor only).
    from ._dev_trace import trace as _trace_sd

    def _bc_sd(marker: str) -> None:
        _trace_sd(project_root, marker)

    _bc_sd("E1 entered spawn_detached")

    if argv_command is not None:
        if foreground:
            return {"ok": False, "err": "argv_command does not support foreground mode"}
        if not argv_command or not all(str(t).strip() for t in argv_command):
            return {"ok": False, "err": "empty command"}
        if not command or not command.strip():
            command = subprocess.list2cmdline([str(t) for t in argv_command])
    if not command or not command.strip():
        return {"ok": False, "err": "empty command"}

    _bc_sd("E0a before timeout governance")
    from .box_profile import resolve_run_timeout

    timeout_verdict = resolve_run_timeout(
        timeout_seconds,
        tool_default=DEFAULT_RUN_TIMEOUT_SECONDS,
        project_root=project_root,
    )
    _bc_sd("E1a after timeout governance")
    if not timeout_verdict["ok"]:
        # Explicit over-ceiling refusal (#466) — nothing is spawned.
        return {**timeout_verdict, "command": command}
    governed_timeout = int(timeout_verdict["timeout_seconds"])
    governance: dict[str, Any] = {
        "timeout_seconds": governed_timeout,
        "timeout_governed_by": timeout_verdict["timeout_governed_by"],
        "timeout_ceiling_seconds": timeout_verdict["timeout_ceiling_seconds"],
        "box_profile": timeout_verdict["box_profile"],
    }
    started_at = time.time()
    run_id = _compute_run_id(command, started_at)

    log_path = _log_path(project_root, run_id)
    _bc_sd("E1b after run_id/log_path/cmd_path computation")

    # AIDOCS shell provider lock — Batch B spawn flip
    # (canonical 2026-04-29). Resolves a Bash-compatible provider
    # before every spawn. shell=False + [bash, -c, command] argv
    # form. cmd.exe never. /bin/sh / dash never. PowerShell only via
    # superadmin flag AND Batch C provider implementation.
    #
    # The `shell` parameter on this function is now a no-op for the
    # spawn shape — kept in the signature for API compatibility but
    # ignored. Resolver verdict drives dispatch.
    from .shell_resolver import resolve_shell as _resolve_shell

    # Reuse spawn_session_id resolution by hoisting it BEFORE spawn
    # (was previously after-spawn for the _LiveRun monitor stamp; we
    # still set it again at the same site below for back-compat).
    _early_session_id = ""
    try:
        from . import managed_mode_service as _mm_attr
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id as _ccsid,
        )

        _bc_sd("E1c before first get_mode")
        host_sid = _ccsid()
        managed = _mm_attr.ManagedModeService().get_mode(
            project_root,
            host_session_id=host_sid,
        )
        _bc_sd("E1d after first get_mode")
        if managed.get("active"):
            _early_session_id = str(managed.get("session_id") or "").strip()
    except Exception:
        _bc_sd("E1d after first get_mode: raised")
        _early_session_id = ""

    if argv_command is not None:
        # #466 argv mode (ai_test detach): the process execs DIRECTLY
        # from the caller-validated argv. No shell provider is involved
        # (nothing to resolve, no bash -c wrap) — ai_test's shell=False
        # doctrine is preserved end-to-end. audited_popen below still
        # lands the process-audit ledger row.
        _bc_sd("E2 argv mode: shell resolver skipped")
        log_file = open(log_path, "wb")
        popen_stdout = log_file
        popen_stderr = subprocess.STDOUT
        popen_argv: Any = [str(t) for t in argv_command]
        return _spawn_and_track(
            _bc_sd=_bc_sd,
            command=command,
            project_root=project_root,
            cwd=cwd,
            env=env,
            foreground=False,
            governed_timeout=governed_timeout,
            governance=governance,
            run_id=run_id,
            log_path=log_path,
            log_file=log_file,
            popen_argv=popen_argv,
            popen_stdout=popen_stdout,
            popen_stderr=popen_stderr,
            started_at=started_at,
        )

    _bc_sd("E1e before resolve_shell call")
    resolved = _resolve_shell(
        project_root,
        session_id=_early_session_id or None,
    )
    _bc_sd(f"E2 after resolve_shell verdict={resolved.verdict}")
    # Emit shell_provider_resolved audit event for every spawn,
    # regardless of verdict. Lock 2: no command content in payload.
    try:
        from .shell_resolver import _emit_resolution_event

        _emit_resolution_event(
            project_root=project_root,
            session_id=_early_session_id or None,
            source_kind="code_runner_detached.spawn_detached",
            capability_name="ai_run",
            status=("allowed" if resolved.verdict == "usable" else "observed"),
            payload=dict(resolved.audit_payload),
        )
        _bc_sd("E3 after audit emit")
    except Exception:
        _bc_sd("E3 after audit emit: raised")
    if resolved.verdict != "usable":
        return {
            "ok": False,
            "err": resolved.rejection_reason or ("no Bash-compatible provider available"),
            "blocked_by": "shell_provider_unavailable",
        }

    if foreground:
        # Visible-window mode. Output goes to the terminal; log file
        # remains empty. Agent uses get_run_status for completion.
        resolved_cmd, wrapper_ok = _foreground_wrap(
            command,
            bash_path=resolved.path,
        )
        if not wrapper_ok:
            return {"ok": False, "err": resolved_cmd}  # err message
        # Touch the log so the file exists (get_run_status relies on it).
        log_path.write_bytes(b"")
        log_file = None
        popen_stdout = subprocess.DEVNULL
        popen_stderr = subprocess.DEVNULL
        popen_argv = resolved_cmd
    else:
        log_file = open(log_path, "wb")
        popen_stdout = log_file
        popen_stderr = subprocess.STDOUT
        # Batch B argv: [bash, -c, command]. shell=False so the
        # platform shell (cmd.exe / dash) is never invoked.
        popen_argv = [resolved.path, "-c", command]
    _bc_sd(f"E4 after log file open run_id={run_id}")

    return _spawn_and_track(
        _bc_sd=_bc_sd,
        command=command,
        project_root=project_root,
        cwd=cwd,
        env=env,
        foreground=foreground,
        governed_timeout=governed_timeout,
        governance=governance,
        run_id=run_id,
        log_path=log_path,
        log_file=log_file,
        popen_argv=popen_argv,
        popen_stdout=popen_stdout,
        popen_stderr=popen_stderr,
        started_at=started_at,
    )


def _spawn_and_track(
    *,
    _bc_sd: Any,
    command: str,
    project_root: Path,
    cwd: Path | None,
    env: dict[str, str] | None,
    foreground: bool,
    governed_timeout: int,
    governance: dict[str, Any],
    run_id: str,
    log_path: Path,
    log_file: Any,
    popen_argv: Any,
    popen_stdout: Any,
    popen_stderr: Any,
    started_at: float,
) -> dict[str, Any]:
    """Shared spawn tail for both dispatch shapes (shell-wrapped and
    #466 argv mode): audited Popen, attribution stamp, _LiveRun +
    monitor registration, inline-tail fast path. Every response carries
    the #466 `governance` block (governing timeout + source + ceiling +
    box profile).
    """
    _bc_sd("E5 before Popen")
    try:
        # #335 Phase 1 extension: routed through audited_popen so every
        # detached-run spawn lands a process-audit ledger row (pure
        # observability). The inner passthrough lambda IS the registered
        # legacy AST callsite ('code_runner_detached.py', '_spawn_and_track',
        # 'subprocess.Popen') — the fingerprint doctrine gate keeps seeing
        # the identical semantic callsite (hoisted out of spawn_detached for
        # the #466 argv/shell dispatch split), and every kwarg (including
        # the platform-specific creationflags) passes through audited_popen
        # UNCHANGED.
        from .shell_egress_service import audited_popen

        _env_kwargs: dict[str, Any] = {"env": env} if env is not None else {}
        proc = audited_popen(
            popen_argv,
            fingerprint=("code_runner_detached.py", "_spawn_and_track", "subprocess.Popen"),
            reason="detached-run-dispatch",
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            shell=False,  # Batch B: shell=True forbidden everywhere.
            cwd=str(cwd or project_root),
            stdin=subprocess.DEVNULL,
            stdout=popen_stdout,
            stderr=popen_stderr,
            **_env_kwargs,
            **_popen_kwargs_for_platform(foreground=foreground),
        )
        _bc_sd(f"E6 after Popen pid={proc.pid}")
    except Exception as exc:
        if log_file is not None:
            log_file.close()
        return {"ok": False, "err": f"spawn failed: {exc}"}

    # #50 (canonical 2026-04-26): stamp attribution at spawn time so
    # the monitor's enqueue scopes notifications correctly. session_id
    # comes from the calling conductor's managed_mode binding (per-#58
    # mapping); lane_id from the spawner-set env var (cannot be forged
    # from the worker subprocess). Both empty when context isn't
    # available — drain falls back to legacy unfiltered behavior.
    spawn_session_id = ""
    spawn_agent_context_id = ""
    spawn_lane_id = ""
    try:
        import os as _os_attr

        spawn_lane_id = _os_attr.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
        from . import managed_mode_service as _mm_attr
        from .mcp_server_runtime_helpers import (
            current_calling_agent_context_id as _actor_context_id,
            current_calling_host_session_id as _ccsid,
        )

        host_sid = _ccsid()
        spawn_agent_context_id = _actor_context_id(project_root)
        managed = _mm_attr.ManagedModeService().get_mode(
            project_root,
            host_session_id=host_sid,
        )
        if managed.get("active"):
            spawn_session_id = str(managed.get("session_id") or "").strip()
    except Exception:
        spawn_session_id = ""
        spawn_agent_context_id = ""
        spawn_lane_id = spawn_lane_id or ""
    _bc_sd("E7 after second get_mode")

    live = _LiveRun(
        run_id=run_id,
        proc=proc,
        log_path=log_path,
        started_at=started_at,
        timeout_seconds=governed_timeout,
        command=command,
        project_root=project_root,
        session_id=spawn_session_id,
        agent_context_id=spawn_agent_context_id,
        lane_id=spawn_lane_id,
        timeout_governed_by=str(governance.get("timeout_governed_by", "requested")),
    )
    with _LIVE_RUNS_LOCK:
        _LIVE_RUNS[run_id] = live
    # Persist command for the renderer to classify on later poll.
    try:
        _cmd_path(project_root, run_id).write_text(command, encoding="utf-8")
    except OSError:
        pass
    _bc_sd("E8 after _LIVE_RUNS reg + cmd_path write")

    monitor = threading.Thread(
        target=_monitor_run,
        args=(live, log_file),
        daemon=True,
        name=f"code_run_monitor_{run_id}",
    )
    monitor.start()
    _bc_sd("E9 after monitor thread start, before inline-tail wait")

    expose_pid = _pids_exposed(project_root)

    # Foreground spawns skip the inline-tail optimization: stdout
    # went to the terminal window, log is empty, and the operator is
    # watching live anyway. Return immediately with the run_id so the
    # agent can poll for exit if it cares.
    if foreground:
        out: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "foreground": True,
            "done": False,
            "governance": governance,
        }
        if expose_pid:
            out["pid"] = proc.pid
        return out

    # 700ms inline-tail: quick commands (version checks, fast linters)
    # return done=True in the spawn response so the agent doesn't pay
    # a 3-round-trip cost for the common case.
    try:
        proc.wait(timeout=INLINE_TAIL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _bc_sd("E10 after inline-tail wait (timeout) before return")
        out = {
            "ok": True,
            "run_id": run_id,
            "log": str(log_path.relative_to(project_root)).replace("\\", "/"),
            "done": False,
            "governance": governance,
        }
        if expose_pid:
            out["pid"] = proc.pid
        return out

    # Process finished within the inline window. Let the monitor flush
    # and close; then read the tail for the response.
    _bc_sd("E10 after inline-tail wait (proc done) before return")
    monitor.join(timeout=1.0)
    # Phoenix 2026-05-09: 📣 notifications should ONLY surface for
    # runs that DETACHED (took longer than the inline window).
    # Inline-completed runs already deliver their result to the agent
    # in this response — the monitor thread has unconditionally
    # enqueued a notification by the time monitor.join returns;
    # dismiss it now so the surface doesn't double-signal what the
    # agent already got inline. Best-effort: a missed dismissal just
    # leaves a single stale notification, recoverable via the
    # ai_notifications_clear MCP tool.
    try:
        from . import run_notifications as _rn_inline_dismiss

        _rn_inline_dismiss.dismiss_run(
            project_root,
            run_id=run_id,
            session_id=spawn_session_id,
            agent_context_id=spawn_agent_context_id,
            lane_id=spawn_lane_id,
        )
    except Exception:
        pass
    tail = _read_tail(log_path, INLINE_TAIL_BYTES)
    exit_code = proc.returncode
    out = {
        "ok": exit_code == 0,
        "run_id": run_id,
        "done": True,
        "exit_code": exit_code,
        "tail": tail,
        "governance": governance,
    }
    if expose_pid:
        out["pid"] = proc.pid
    return out


def _monitor_run(live: _LiveRun, log_file) -> None:
    """Wait for the subprocess, enforce timeout, clean up registry,
    close the log. Runs in a daemon thread. log_file is None in
    foreground mode (output went to the visible terminal).
    """
    try:
        try:
            live.proc.wait(timeout=live.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process(live.proc)
            try:
                live.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # #392: reap the whole tree, not just the direct child — a
                # timed-out run may have spawned a server that outlives its
                # parent and keeps files (build DLLs) locked.
                from .code_runner import _kill_process_tree

                _kill_process_tree(live.proc)
                try:
                    live.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            # #466 honest timeout governance: a timed-out run's log
            # previously just truncated mid-output with no explanation.
            # Stamp WHICH timeout fired and whose value it was so the
            # reader never has to guess what governed the kill.
            if log_file is not None:
                try:
                    log_file.write(
                        (
                            f"\n[aidocs-governor] run terminated: exceeded its "
                            f"{live.timeout_seconds}s timeout "
                            f"(governed_by={live.timeout_governed_by})\n"
                        ).encode("utf-8"),
                    )
                    log_file.flush()
                except Exception:
                    pass
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        exit_code = live.proc.returncode if live.proc.returncode is not None else -1
        duration = time.time() - live.started_at
        _record_finished(live.run_id, exit_code, duration)
        # Persist to sidecar so status survives MCP restart — the
        # in-process cache (_FINISHED_RUNS) evaporates on restart and
        # leaves done-runs without exit_code in the status header.
        if live.project_root is not None:
            # #50 L4: stamp attribution into the sidecar so
            # ai_run_output can refuse cross-session reads.
            _write_status_sidecar(
                live.project_root,
                live.run_id,
                exit_code,
                duration,
                session_id=live.session_id,
                agent_context_id=live.agent_context_id,
                lane_id=live.lane_id,
            )
            # Fold the observed duration into the rate-limit gate's
            # per-bucket stats + enqueue a run-done notification so
            # the agent learns about completion on its next tool call
            # without polling. Both are best-effort; any failure here
            # must never break the monitor. (2026-04-20: notify is
            # unconditional — no opt-out. Paired with the ai_run_output
            # hard-block on not-done runs, this eliminates polling as
            # a pattern.)
            try:
                from . import run_notifications as _rn
                from .run_bucket_classifier import (
                    bucket_key_for,
                    outcome_for_exit_code,
                )
                from .run_duration_bucket_store import (
                    RunDurationBucketStore,
                )

                outcome = outcome_for_exit_code(exit_code)
                bkey = bucket_key_for(live.command).key()
                if outcome != "unknown":
                    try:
                        RunDurationBucketStore().record_observation(
                            live.project_root,
                            bkey,
                            outcome,
                            int(duration * 1000),
                        )
                    except Exception:
                        pass
                    digest = ""
                    try:
                        from .run_output_renderer import digest_for_run

                        tail = _read_tail(live.log_path, 8192)
                        digest = digest_for_run(live.command, tail, exit_code)
                    except Exception:
                        pass
                    try:
                        _rn.enqueue(
                            live.project_root,
                            run_id=live.run_id,
                            command=live.command,
                            exit_code=exit_code,
                            outcome=outcome,
                            duration_ms=int(duration * 1000),
                            bucket_key=bkey,
                            digest=digest,
                            session_id=live.session_id,
                            # #356: actor axis — a worker run's notice must
                            # follow the worker (actor-less records dodge the
                            # attributed inline dismiss).
                            agent_context_id=live.agent_context_id,
                            lane_id=live.lane_id,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        with _LIVE_RUNS_LOCK:
            _LIVE_RUNS.pop(live.run_id, None)


def _terminate_process(proc: subprocess.Popen) -> None:
    """Platform-aware graceful termination."""
    try:
        if sys.platform == "win32":
            # CTRL_BREAK_EVENT only works because of
            # CREATE_NEW_PROCESS_GROUP at spawn time.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            # Signal the whole process group — catches subprocesses the
            # command may have spawned. start_new_session at spawn time
            # gave us a dedicated pgid.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def get_run_status(
    project_root: Path,
    run_id: str,
) -> dict[str, Any]:
    """Check the state of a run. Fast — no subprocess interaction
    beyond a non-blocking poll on the live registry.
    """
    log_path = _log_path(project_root, run_id)
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)

    if live is not None:
        running = live.proc.poll() is None
        out: dict[str, Any] = {
            "run_id": run_id,
            "state": "running" if running else "done",
            "exit_code": live.proc.returncode if not running else None,
            "started_at": live.started_at,
            "duration_seconds": round(time.time() - live.started_at, 2),
            "log_bytes": _safe_size(log_path),
        }
        if _pids_exposed(project_root):
            out["pid"] = live.proc.pid
        return out

    if not log_path.is_file():
        return {"run_id": run_id, "state": "unknown"}

    # Log exists but monitor already cleaned up — run finished.
    # Return cached exit_code + duration if we recorded them.
    with _FINISHED_RUNS_LOCK:
        cached = _FINISHED_RUNS.get(run_id)
    out: dict[str, Any] = {
        "run_id": run_id,
        "state": "done",
        "log_bytes": _safe_size(log_path),
    }
    if cached:
        out["exit_code"] = cached["exit_code"]
        out["duration_seconds"] = cached["duration_seconds"]
    else:
        # In-process cache gone (e.g. MCP restart since the run
        # finished). Fall back to the persistent status sidecar so
        # the header still shows exit + duration.
        sidecar = _read_status_sidecar(project_root, run_id)
        if sidecar:
            out["exit_code"] = sidecar["exit_code"]
            out["duration_seconds"] = sidecar["duration_seconds"]
    return out


def tail_run_log(
    project_root: Path,
    run_id: str,
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """Read the tail of a run's log. Optional wait_seconds blocks
    until the process finishes OR the deadline passes.

    tail_bytes caps output at roughly N bytes, decoded utf-8 with
    replacement for invalid sequences (log bytes can be anything).
    """
    log_path = _log_path(project_root, run_id)
    if wait_seconds > 0:
        _wait_for_finish(run_id, wait_seconds)
    if not log_path.is_file():
        return {"ok": False, "err": f"no log for run {run_id}"}

    tail = _read_tail(log_path, max(256, int(tail_bytes)))
    status = get_run_status(project_root, run_id)
    # Surface the original command so the renderer can classify
    # (test/build/probe) and pick the right output formatter.
    command = ""
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)
        if live is not None:
            command = live.command
    if not command:
        command = _read_run_command(project_root, run_id)
    out: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "command": command,
        "state": status.get("state"),
        "exit_code": status.get("exit_code"),
        "tail": tail,
        "log_bytes": _safe_size(log_path),
    }
    if "pid" in status:
        out["pid"] = status["pid"]
    return out


def kill_run(project_root: Path, run_id: str) -> dict[str, Any]:
    """Stop a running subprocess. No-op if already finished."""
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)
    if live is None:
        return {"ok": True, "run_id": run_id, "state": "not_running"}
    _terminate_process(live.proc)
    try:
        live.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # #392: ALWAYS reap the whole tree. Graceful termination (or a parent that
    # exits on its own) can leave a spawned server child alive and orphaned —
    # the classic "killed the run but the web server kept the build DLL locked"
    # bug. _kill_process_tree force-kills parent AND descendants (Windows
    # taskkill /F /T, POSIX killpg SIGKILL); harmless no-op if already exited.
    from .code_runner import _kill_process_tree

    _kill_process_tree(live.proc)
    return {"ok": True, "run_id": run_id, "state": "killed"}


def _wait_for_finish(run_id: str, wait_seconds: float) -> None:
    """Block up to wait_seconds for the run's monitor to finish."""
    deadline = time.time() + max(0.0, min(wait_seconds, 120.0))
    while time.time() < deadline:
        with _LIVE_RUNS_LOCK:
            live = _LIVE_RUNS.get(run_id)
        if live is None:
            return
        if live.proc.poll() is not None:
            # Give the monitor thread a moment to tear down.
            time.sleep(0.05)
            continue
        time.sleep(0.1)


def _read_tail(path: Path, max_bytes: int) -> str:
    """Read up to max_bytes from the end of a file, decode safely."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(-max_bytes, os.SEEK_END)
        data = fh.read()
    return data.decode("utf-8", errors="replace")


# #484 (Emperor 2026-07-19): bounds for the live-tail progress read on a
# RUNNING job — last ~30 lines or 8KB, whichever is smaller. Both bounds
# are stated in the honest "tail of M bytes total" label (no-silent-caps
# law); the full log stays in the run store.
RUNNING_TAIL_MAX_LINES = 30
RUNNING_TAIL_MAX_BYTES = 8192


def live_run_owner(run_id: str) -> dict[str, str]:
    """Spawn attribution for a LIVE run; {} when unknown/unattributed.

    Shape matches what run_owner_mismatch expects, so the running-read
    ownership gate (#484) can reuse the same mismatch logic the
    completed-read gate (#50 L4) uses against the .status sidecar —
    which does not exist yet while the run is still executing.
    """
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)
    if live is None:
        return {}
    return {
        "session_id": live.session_id or "",
        "agent_context_id": live.agent_context_id or "",
    }


def running_progress(project_root: Path, run_id: str) -> dict[str, Any]:
    """Progress snapshot for a RUNNING job (#484, Emperor 2026-07-19).

    Replaces the bare "still running — wait for 📣 notify" refusal with
    real signal: elapsed time from the recorded spawn start (the
    _LiveRun registry carries started_at from spawn; a run only reports
    state='running' while its registry entry is alive, so no extra
    persistence is needed) plus a bounded, honestly-labeled tail of the
    live log.

    Fail-quiet by contract: an unreadable or absent log degrades to
    status+elapsed only — never an error shape. The tail read uses the
    same read-only binary open as _read_tail: it requests no exclusive
    access, so on Windows it never locks or blocks the child that holds
    the log open for append.

    Finished jobs never reach this function — the completed-read path
    and its payload shape are unchanged.
    """
    out: dict[str, Any] = {"ok": True, "run_id": run_id, "status": "running"}
    with _LIVE_RUNS_LOCK:
        live = _LIVE_RUNS.get(run_id)
    if live is not None:
        out["started_at"] = live.started_at
        out["elapsed_seconds"] = round(time.time() - live.started_at, 2)
    try:
        log_path = (
            live.log_path if live is not None else _log_path(project_root, run_id)
        )
        total = log_path.stat().st_size
        text = _read_tail(log_path, RUNNING_TAIL_MAX_BYTES)
        shown = text.splitlines()[-RUNNING_TAIL_MAX_LINES:]
        out["output_bytes_so_far"] = total
        out["output_tail"] = "\n".join(shown)
        out["output_tail_label"] = (
            f"tail of {total} bytes total (last {len(shown)} lines shown; "
            f"bounds: {RUNNING_TAIL_MAX_LINES} lines / "
            f"{RUNNING_TAIL_MAX_BYTES} bytes)"
        )
    except Exception:
        # Fail-quiet: status+elapsed only (charter #484). Never an error.
        out.pop("output_bytes_so_far", None)
        out.pop("output_tail", None)
        out.pop("output_tail_label", None)
    return out


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def evict_old_logs(project_root: Path) -> dict[str, Any]:
    """LRU-evict completed run logs if .MEMORY/.runs/ exceeds the
    directory size cap. Never evicts logs for runs in _LIVE_RUNS —
    those are still being written.

    Returns {"evicted": N, "bytes_freed": M}.
    """
    logs = _logs_dir(project_root)
    entries: list[tuple[Path, float, int]] = []
    total = 0
    with _LIVE_RUNS_LOCK:
        live_ids = set(_LIVE_RUNS.keys())
    for child in logs.iterdir():
        if not child.is_file() or child.suffix != ".log":
            continue
        stem = child.stem
        if stem in live_ids:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append((child, stat.st_mtime, stat.st_size))
        total += stat.st_size
    over_size = total > RUNS_DIR_SIZE_CAP_BYTES
    over_count = len(entries) > RUNS_DIR_MAX_FILES
    if not over_size and not over_count:
        return {"evicted": 0, "bytes_freed": 0}

    # Oldest first; evict until BOTH caps are satisfied — keep at most the
    # RUNS_DIR_MAX_FILES newest completed reports AND stay under the size
    # cap. Count cap stops the pile-up (Empire: max 5 recent) that the size
    # cap alone allowed (thousands of tiny logs never tripping 200 MB).
    entries.sort(key=lambda t: t[1])
    evicted = 0
    freed = 0
    remaining = len(entries)
    for path, _mtime, size in entries:
        if total <= RUNS_DIR_SIZE_CAP_BYTES and remaining <= RUNS_DIR_MAX_FILES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        # Drop the matching .status sidecar too, so a report is evicted as
        # a unit (no orphan status files left behind).
        try:
            path.with_suffix(".status").unlink()
        except OSError:
            pass
        evicted += 1
        freed += size
        total -= size
        remaining -= 1
    return {"evicted": evicted, "bytes_freed": freed}
