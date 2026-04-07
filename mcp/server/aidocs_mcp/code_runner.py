"""Structured code execution tools — bash replacements for token efficiency.

Instead of raw bash output flooding the context, these tools run common
operations and return structured results with capped output:

- code_build: run build command, return success/fail + errors only
- code_test: run test suite, return pass/fail counts + failure details
- code_run: run arbitrary command with output capping

Each returns a compact JSON result instead of raw terminal output.
Agents save ~80% tokens compared to raw bash for typical operations.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_TIMEOUT = 120  # seconds
_MAX_OUTPUT_CHARS = 4000  # cap output to ~1000 tokens
_MAX_ERROR_CHARS = 2000
_MAX_FAILURE_LINES = 50  # max failure detail lines


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


def _run_process(
    command: str,
    cwd: Path,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[int, str, str, float]:
    """Run a command and return (exit_code, stdout, stderr, duration)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        return proc.returncode, proc.stdout, proc.stderr, duration
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return -1, "", f"Command timed out after {timeout}s", duration
    except Exception as exc:
        duration = time.monotonic() - start
        return -1, "", str(exc), duration


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Cap text length, return (text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    # Keep first half + last half for context
    half = max_chars // 2
    return text[:half] + f"\n\n... ({len(text) - max_chars} chars truncated) ...\n\n" + text[-half:], True


def code_run(
    project_root: Path,
    command: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    max_output: int = _MAX_OUTPUT_CHARS,
) -> RunResult:
    """Run an arbitrary command with capped output."""
    exit_code, stdout, stderr, duration = _run_process(command, project_root, timeout)
    stdout_capped, stdout_trunc = _cap(stdout, max_output)
    stderr_capped, stderr_trunc = _cap(stderr, _MAX_ERROR_CHARS)

    return RunResult(
        success=exit_code == 0,
        exit_code=exit_code,
        command=command,
        duration_seconds=duration,
        stdout_lines=stdout.count("\n"),
        stderr_lines=stderr.count("\n"),
        stdout_preview=stdout_capped.strip(),
        stderr_preview=stderr_capped.strip(),
        truncated=stdout_trunc or stderr_trunc,
    )


def code_build(
    project_root: Path,
    command: str = "",
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> BuildResult:
    """Run a build command, return success/fail + errors only.

    Auto-detects build system if no command given.
    """
    if not command:
        command = _detect_build_command(project_root)
    if not command:
        return BuildResult(
            success=False, exit_code=-1, command="(none)",
            duration_seconds=0, error_lines=["No build command detected. Provide one explicitly."],
            warning_count=0, truncated=False,
        )

    exit_code, stdout, stderr, duration = _run_process(command, project_root, timeout)
    combined = stdout + "\n" + stderr

    # Extract error and warning lines
    error_lines: list[str] = []
    warning_count = 0
    for line in combined.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ("error", "failed", "fatal", "cannot find", "not found", "exception")):
            if len(error_lines) < _MAX_FAILURE_LINES:
                error_lines.append(line.strip()[:300])
        if "warning" in lower or "warn" in lower:
            warning_count += 1

    return BuildResult(
        success=exit_code == 0,
        exit_code=exit_code,
        command=command,
        duration_seconds=duration,
        error_lines=error_lines,
        warning_count=warning_count,
        truncated=len(error_lines) >= _MAX_FAILURE_LINES,
    )


def code_test(
    project_root: Path,
    command: str = "",
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> TestResult:
    """Run a test suite, return pass/fail counts + failure details.

    Auto-detects test framework if no command given.
    """
    if not command:
        command = _detect_test_command(project_root)
    if not command:
        return TestResult(
            success=False, exit_code=-1, command="(none)",
            duration_seconds=0, passed=0, failed=0, skipped=0, errors=0, total=0,
            failures=["No test command detected. Provide one explicitly."],
            summary_line="", truncated=False,
        )

    exit_code, stdout, stderr, duration = _run_process(command, project_root, timeout)
    combined = stdout + "\n" + stderr

    # Parse test results
    passed, failed, skipped, errors, total = _parse_test_counts(combined)
    failures = _extract_test_failures(combined)
    summary_line = _extract_summary_line(combined)

    return TestResult(
        success=exit_code == 0 and failed == 0 and errors == 0,
        exit_code=exit_code,
        command=command,
        duration_seconds=duration,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        total=total,
        failures=failures[:_MAX_FAILURE_LINES],
        summary_line=summary_line,
        truncated=len(failures) > _MAX_FAILURE_LINES,
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
    r"(?:.*?(\d+)\s+error)?"
)

_JEST_SUMMARY = re.compile(
    r"Tests:\s+(?:(\d+)\s+failed,\s+)?(?:(\d+)\s+skipped,\s+)?(\d+)\s+passed"
)

_DOTNET_SUMMARY = re.compile(
    r"Passed!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)"
    r"|Failed!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)"
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
