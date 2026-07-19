"""Structured code execution — `ai_run` only.

The historical `code_build` / `code_test` TOOL FUNCTIONS were
removed 2026-05-29 (Empire re-seal: 'remove the tools from the
agent calls, dont remove the result formatter'). They had no live
agent-reachable callers since the sync MCP wrappers were retired
2026-04-20; removal closes the audit-trail gap where build/test
invocations bypassed the modern ai_run gate cascade.

What was REMOVED:
  - def code_build(...)  — agent-facing tool function
  - def code_test(...)   — agent-facing tool function

What was KEPT (result formatters / parsers — still useful for
downstream code that holds raw build/test output and wants
structured shapes):
  - dataclass BuildResult + .to_dict()
  - dataclass TestResult + .to_dict()
  - _detect_build_command, _detect_test_command (manifest-driven
    auto-detection helpers)
  - _parse_test_counts, _extract_test_failures, _extract_summary_line
    (raw-output → structured-fields formatters)
  - ai_run (arbitrary command with output capping — the canonical
    egress path; migrates to ShellEgressService next)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_TIMEOUT = 60  # seconds — must return control to MCP within this window
_MAX_OUTPUT_CHARS = 4000  # cap output to ~1000 tokens
_MAX_ERROR_CHARS = 2000
_MAX_FAILURE_LINES = 50  # max failure detail lines

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
# ORed into creationflags at each Popen/run callsite; POSIX no-op.
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Hard cap exposed to MCP tool schema. Agents cannot exceed this — a
# stuck subprocess holding the harness for 10+ minutes was the actual
# regression that motivated this constant. 180s gives slow test files
# headroom without letting "I'll just run the full suite" tank the
# whole conductor + lane flow.
MAX_RUN_TIMEOUT = 180


@dataclass(slots=True)
class RunResult:
    success: bool
    exit_code: int
    command: str
    duration_seconds: float
    stdout_lines: int
    stderr_lines: int
    stdout_preview: str
    stderr_preview: str
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "success": self.success,
            "exit_code": self.exit_code,
            "command": self.command,
            "duration_seconds": round(self.duration_seconds, 2),
            "stdout_lines": self.stdout_lines,
            "stderr_lines": self.stderr_lines,
        }
        if self.stderr_preview:
            result["stderr"] = self.stderr_preview
        if self.stdout_preview:
            result["stdout"] = self.stdout_preview
        if self.truncated:
            result["truncated"] = True
        return result


@dataclass(slots=True)
class TestResult:
    success: bool
    exit_code: int
    command: str
    duration_seconds: float
    passed: int
    failed: int
    skipped: int
    errors: int
    total: int
    failures: list[str]  # failure detail lines
    summary_line: str
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "success": self.success,
            "exit_code": self.exit_code,
            "command": self.command,
            "duration_seconds": round(self.duration_seconds, 2),
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "total": self.total,
            "summary": self.summary_line,
        }
        if self.failures:
            result["failures"] = self.failures
        if self.truncated:
            result["truncated"] = True
        return result


@dataclass(slots=True)
class BuildResult:
    success: bool
    exit_code: int
    command: str
    duration_seconds: float
    error_lines: list[str]
    warning_count: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "success": self.success,
            "exit_code": self.exit_code,
            "command": self.command,
            "duration_seconds": round(self.duration_seconds, 2),
        }
        if self.warning_count:
            result["warnings"] = self.warning_count
        if self.error_lines:
            result["errors"] = self.error_lines
        if self.truncated:
            result["truncated"] = True
        return result


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process AND its descendants.

    subprocess.run + capture_output blocks on pipe-read after the
    timeout fires when the child has spawned grandchildren that
    keep the pipes open (pytest-xdist workers, npm-spawned node,
    cmd.exe wrapping a long-running tool). The harness then hangs
    well past timeout. The fix: launch in a dedicated process
    group so we can signal the whole tree, then drain pipes with
    a short bounded wait.
    """
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill /T tree-kills children, /F forces. Quiet on
            # missing-pid; we just want the side effect.
            # #345: routed through audited_run — tree-kill actions belong in
            # the ledger too. Passthrough lambda IS the registered AST
            # callsite; kwargs pass through UNCHANGED.
            from .shell_egress_service import audited_run

            audited_run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                fingerprint=("code_runner.py", "_kill_process_tree", "subprocess.run"),
                reason="code-runner-tree-kill",
                run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=_WIN_NO_WINDOW,
            )
        else:
            # POSIX: signal the entire process group we created via
            # start_new_session=True (pgid == pid).
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except ProcessLookupError:
                pass
    except Exception:
        # Last-resort kill of just the parent — better than nothing.
        try:
            proc.kill()
        except Exception:
            pass


def _emit_egress_refused(cwd: Path, command: str, floor: dict) -> bool:
    """Audit a destructive-floor refusal on the sync shell path. Never raises;
    records command_hash (not the raw command, per audit doctrine). Returns True
    iff the audit row was recorded — the CALLER refuses regardless (refusal is
    independent of audit success); the return only drives truthful reporting of
    audit_recorded vs audit_degraded.
    """
    try:
        import hashlib

        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            cwd,
            event_kind="shell_egress_refused",
            source_kind="code_runner._run_process",
            session_id=None,
            capability_name="ai_run",
            action_kind="run",
            target_entity="",
            status="refused",
            payload={
                "matched_rule": floor.get("matched_rule"),
                "reason": floor.get("reason"),
                "command_hash": hashlib.sha256((command or "").encode("utf-8")).hexdigest(),
            },
        )
        return True
    except Exception:
        return False


def _run_process(
    command: str,
    cwd: Path,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[int, str, str, float]:
    """Run a command and return (exit_code, stdout, stderr, duration).

    Uses Popen + communicate(timeout=...) + tree-kill on timeout to
    guarantee return-within-timeout-window even when the child spawns
    sub-processes that ignore signals or block on pipes.
    """
    start = time.monotonic()
    # Destructive-primitive floor (canonical 2026-05-24). The MCP ai_run tool
    # enforces the full gate cascade, but THIS sync path (git_ops →
    # code_runner.ai_run, plus the legacy code_build/code_test) reaches the
    # shell without it. Apply the unbypassable destructive floor here so an
    # injected `; rm -rf ~` or `curl x | sh` fails CLOSED on every egress, not
    # just the gated MCP surface. Fail closed if the floor cannot evaluate.
    try:
        from .bash_policy import evaluate_destructive_floor

        _floor = evaluate_destructive_floor(command)
    except Exception as exc:
        return (
            -1,
            "",
            f"shell egress refused: destructive floor unavailable ({exc!r})",
            time.monotonic() - start,
        )
    if not _floor.get("allowed"):
        # Refusal is INDEPENDENT of audit success — we block either way and only
        # report which happened (audit_recorded vs audit_degraded).
        _audited = _emit_egress_refused(cwd, command, _floor)
        _astat = "audit_recorded" if _audited else "audit_degraded"
        return (
            -1,
            "",
            f"shell egress refused ({_astat}): {_floor.get('reason')}",
            time.monotonic() - start,
        )
    # stdin=DEVNULL is load-bearing: without it the child inherits the
    # MCP server's stdio (which IS its client RPC channel), starves
    # the MCP stream, and the call hangs until the client drops the
    # connection. Diagnosed 2026-04-19 when direct server.call_tool()
    # worked fine but MCP-over-stdio invocations died with
    # "Connection closed" — same subprocess.Popen, different parent
    # stdin fate.
    popen_kwargs: dict[str, object] = dict(
        shell=True,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Detach into our own process group so _kill_process_tree can
    # take down children that don't inherit signal handlers cleanly.
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _WIN_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    # AIDOCS shell provider lock — Batch B spawn flip
    # (canonical 2026-04-29). Resolve a Bash-compatible provider
    # and dispatch via shell=False + [bash, -c, command]. Refuse
    # cleanly when no provider available. session_id is unavailable
    # in this sync entrypoint; resolver runs without dev-override.
    from .shell_resolver import (
        _emit_resolution_event as _emit_audit,
    )
    from .shell_resolver import (
        resolve_shell as _resolve_shell,
    )

    resolved = _resolve_shell(cwd, session_id=None)
    try:
        _emit_audit(
            project_root=cwd,
            session_id=None,
            source_kind="code_runner._run_process",
            capability_name="ai_run",
            status=("allowed" if resolved.verdict == "usable" else "observed"),
            payload=dict(resolved.audit_payload),
        )
    except Exception:
        pass
    if resolved.verdict != "usable":
        return (
            -1,
            "",
            resolved.rejection_reason or ("no Bash-compatible provider available"),
            time.monotonic() - start,
        )

    # Batch B argv: [bash, -c, command]. shell=False everywhere.
    popen_kwargs["shell"] = False
    popen_argv = [resolved.path, "-c", command]

    try:
        # #335 Phase 1 extension: routed through audited_popen so every
        # code-runner spawn lands a process-audit ledger row (pure
        # observability). The inner passthrough lambda IS the registered
        # legacy AST callsite ('code_runner.py', '_run_process',
        # 'subprocess.Popen') — the fingerprint doctrine gate keeps
        # seeing the identical semantic callsite, and all popen_kwargs
        # pass through audited_popen UNCHANGED.
        from .shell_egress_service import audited_popen

        proc = audited_popen(
            popen_argv,
            fingerprint=("code_runner.py", "_run_process", "subprocess.Popen"),
            reason="code-runner-exec",
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            **popen_kwargs,
        )
    except Exception as exc:
        return -1, "", str(exc), time.monotonic() - start

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        duration = time.monotonic() - start
        return proc.returncode, stdout or "", stderr or "", duration
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        # Drain pipes with a short bounded wait so we don't leak fds
        # but also don't re-block on a hung child grandchild.
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        duration = time.monotonic() - start
        msg = f"Command timed out after {timeout}s (process tree killed)."
        if stderr:
            msg = f"{msg}\n{stderr}"
        return -1, stdout or "", msg, duration
    except Exception as exc:
        _kill_process_tree(proc)
        return -1, "", str(exc), time.monotonic() - start


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Cap text length, return (text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    # Keep first half + last half for context
    half = max_chars // 2
    return text[:half] + f"\n\n... ({len(text) - max_chars} chars truncated) ...\n\n" + text[
        -half:
    ], True


def ai_run(
    project_root: Path,
    command: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    max_output: int = _MAX_OUTPUT_CHARS,
    reachability: str = "agent_reachable",
) -> RunResult:
    """Run an arbitrary command with capped output.

    Doctrine 2026-05-29 (Empire re-seal — shell-egress chokepoint):
    this entrypoint DELEGATES to ShellEgressService.execute_shell,
    which applies the unified gate cascade (destructive floor +
    heuristic judge with reachability-aware fail-closed + lifecycle
    preflight + timeout + shared post-exec output_guard fail-closed
    + audit). There is no second seal: this is the single egress
    path for code_runner.ai_run, and the gates run exactly ONCE in
    the service. _run_process remains in this module only as a
    private helper for the internal RunResult mapping; it is NOT
    called by ai_run anymore.
    """
    from .shell_egress_service import default_service

    res = default_service().execute_shell(
        command,
        cwd=str(project_root),
        timeout_s=float(timeout),
        reachability=reachability,  # type: ignore[arg-type]
        audit_tag="code_runner.ai_run",
    )
    # Map ShellEgressResult onto the legacy RunResult shape so
    # downstream callers don't have to change. exit_code falls back
    # to -1 (the sync code_runner convention) when the service
    # refused without executing.
    exit_code = res.rc if res.rc is not None else -1
    stdout = res.stdout
    stderr = res.stderr
    duration = res.duration_s
    stdout_capped, stdout_trunc = _cap(stdout, max_output)
    stderr_capped, stderr_trunc = _cap(stderr, _MAX_ERROR_CHARS)

    return RunResult(
        success=res.ok,
        exit_code=exit_code,
        command=command,
        duration_seconds=duration,
        stdout_lines=stdout.count("\n"),
        stderr_lines=stderr.count("\n"),
        stdout_preview=stdout_capped.strip(),
        stderr_preview=stderr_capped.strip(),
        truncated=stdout_trunc or stderr_trunc,
    )


# ── Auto-detection ──


def _detect_build_command(project_root: Path) -> str:
    """Detect the project's build command from manifest files."""
    if (project_root / "package.json").is_file():
        return "npm run build"
    if (project_root / "Cargo.toml").is_file():
        return "cargo build"
    if (project_root / "pyproject.toml").is_file():
        return "python -m build"
    if (project_root / "go.mod").is_file():
        return "go build ./..."
    for csproj in project_root.glob("*.csproj"):
        return "dotnet build"
    for sln in project_root.glob("*.sln"):
        return "dotnet build"
    if (project_root / "Makefile").is_file():
        return "make"
    return ""


def _detect_test_command(project_root: Path) -> str:
    """Detect the project's test command from manifest files."""
    if (project_root / "pyproject.toml").is_file() or (project_root / "setup.py").is_file():
        return "python -m pytest -q --tb=short"
    if (project_root / "package.json").is_file():
        return "npm test"
    if (project_root / "Cargo.toml").is_file():
        return "cargo test"
    if (project_root / "go.mod").is_file():
        return "go test ./..."
    for csproj in project_root.glob("*.csproj"):
        return "dotnet test"
    return ""


# ── Test output parsing ──

import re

_PYTEST_SUMMARY = re.compile(
    r"(\d+)\s+passed"
    r"(?:.*?(\d+)\s+failed)?"
    r"(?:.*?(\d+)\s+skipped)?"
    r"(?:.*?(\d+)\s+error)?",
)

_JEST_SUMMARY = re.compile(
    r"Tests:\s+(?:(\d+)\s+failed,\s+)?(?:(\d+)\s+skipped,\s+)?(\d+)\s+passed",
)

_DOTNET_SUMMARY = re.compile(
    r"Passed!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)"
    r"|Failed!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)",
)


def _parse_test_counts(output: str) -> tuple[int, int, int, int, int]:
    """Parse test counts from output. Returns (passed, failed, skipped, errors, total)."""
    # Try pytest
    m = _PYTEST_SUMMARY.search(output)
    if m:
        passed = int(m.group(1) or 0)
        failed = int(m.group(2) or 0)
        skipped = int(m.group(3) or 0)
        errors = int(m.group(4) or 0)
        return passed, failed, skipped, errors, passed + failed + skipped + errors

    # Try jest
    m = _JEST_SUMMARY.search(output)
    if m:
        failed = int(m.group(1) or 0)
        skipped = int(m.group(2) or 0)
        passed = int(m.group(3) or 0)
        return passed, failed, skipped, 0, passed + failed + skipped

    # Try dotnet
    m = _DOTNET_SUMMARY.search(output)
    if m:
        if m.group(1) is not None:
            return int(m.group(2)), int(m.group(1)), 0, 0, int(m.group(1)) + int(m.group(2))
        if m.group(3) is not None:
            return int(m.group(4)), int(m.group(3)), 0, 0, int(m.group(3)) + int(m.group(4))

    # Fallback: count PASSED/FAILED lines
    lines = output.splitlines()
    passed = sum(1 for l in lines if "PASSED" in l or "passed" in l.lower())
    failed = sum(1 for l in lines if "FAILED" in l or "failed" in l.lower())
    return passed, failed, 0, 0, passed + failed


def _extract_test_failures(output: str) -> list[str]:
    """Extract failure details from test output."""
    failures: list[str] = []
    lines = output.splitlines()
    in_failure = False

    for line in lines:
        stripped = line.strip()
        # pytest FAILED markers
        if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
            failures.append(stripped[:300])
            in_failure = True
            continue
        # Assertion errors
        if "AssertionError" in stripped or "AssertionError" in stripped:
            failures.append(stripped[:300])
            continue
        # Generic error/failure lines near failure markers
        if in_failure and stripped and not stripped.startswith("="):
            if len(failures) < _MAX_FAILURE_LINES:
                failures.append(stripped[:300])
        if stripped.startswith("====") or stripped.startswith("----"):
            in_failure = False

    return failures


def _extract_summary_line(output: str) -> str:
    """Extract the summary line from test output."""
    lines = output.strip().splitlines()
    # Work backwards to find summary
    for line in reversed(lines):
        stripped = line.strip()
        if any(kw in stripped.lower() for kw in ("passed", "failed", "error", "ok")):
            if not stripped.startswith("=") and not stripped.startswith("-"):
                return stripped[:200]
    return ""
