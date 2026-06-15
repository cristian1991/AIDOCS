"""Command-run output rendering.

Classifies the command (test framework vs. probe vs. build) and builds
TextContent blocks appropriate for each:

- Test runs: parse pass/fail counts, show pretty header + full failure
  tracebacks. User sees green/red ribbon, agent sees terse structured
  counts unless tests failed (then it needs the tracebacks).
- Probe runs (python -c, python script.py, node, curl, ls, git): never
  summarize — return stdout+stderr verbatim, capped. Agent + user both
  see exactly what ran.
- Build runs (npm run build, cargo build, tsc, go build): parse errors
  and warnings, summary header + error bodies.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.types import TextContent

from .tool_display import _tc, _tc_user

_PYTEST_CMD_RE = re.compile(r"\bpytest\b|\bpython\s+-m\s+pytest\b", re.IGNORECASE)
_UNITTEST_CMD_RE = re.compile(r"\bpython\s+-m\s+unittest\b", re.IGNORECASE)
_NPM_TEST_RE = re.compile(
    r"\bnpm\s+(run\s+)?test\b|\byarn\s+test\b|\bpnpm\s+test\b|\bbun\s+test\b",
    re.IGNORECASE,
)
_JEST_RE = re.compile(r"\bjest\b|\bvitest\b", re.IGNORECASE)
_GO_TEST_RE = re.compile(r"\bgo\s+test\b", re.IGNORECASE)
_CARGO_TEST_RE = re.compile(r"\bcargo\s+test\b", re.IGNORECASE)

_BUILD_CMD_RE = re.compile(
    r"\b(npm\s+run\s+build|yarn\s+build|pnpm\s+build|tsc|webpack|vite\s+build|"
    r"cargo\s+build|go\s+build|make|ninja|gradle|mvn\s+package|dotnet\s+build)\b",
    re.IGNORECASE,
)


def classify_command(command: str) -> str:
    """Return 'test', 'build', or 'probe'."""
    if not command or not command.strip():
        return "probe"
    if _PYTEST_CMD_RE.search(command) or _UNITTEST_CMD_RE.search(command):
        return "test-python"
    if _NPM_TEST_RE.search(command) or _JEST_RE.search(command):
        return "test-node"
    if _GO_TEST_RE.search(command):
        return "test-go"
    if _CARGO_TEST_RE.search(command):
        return "test-rust"
    if _BUILD_CMD_RE.search(command):
        return "build"
    return "probe"


_PYTEST_SUMMARY_RE = re.compile(
    r"^=+\s*(?P<body>[^=]*?(passed|failed|error|skipped|xfailed|xpassed)[^=]*?)\s*=+\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_PYTEST_COUNTS_RE = re.compile(
    r"(?P<n>\d+)\s+(?P<kind>passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings)",
    re.IGNORECASE,
)
_PYTEST_FAILED_TEST_RE = re.compile(
    r"^FAILED\s+(?P<test>\S+)(?:\s+-\s+(?P<reason>.+))?$",
    re.MULTILINE,
)

# Backlog #86 spec B: hard byte budget for the failed-IDs section.
# 16 KB ≈ 200 IDs at 80 chars each. Beyond this, the renderer emits a
# canonical omission line with the exact count instead of bloating
# the agent's context window.
_MAX_FAILED_IDS_BYTES = 16 * 1024


def _parse_pytest(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract structured test results from pytest output."""
    combined = f"{stdout}\n{stderr}"
    counts: dict[str, int] = {}
    duration: str | None = None
    summary_line: str | None = None

    # Find final summary ribbon — the last matching line wins.
    summary_lines = _PYTEST_SUMMARY_RE.findall(combined)
    if summary_lines:
        body = summary_lines[-1][0]
        # Preserve the body verbatim so the renderer can surface
        # pytest's own bottom line (backlog #86 spec A).
        summary_line = body.strip()
        for m in _PYTEST_COUNTS_RE.finditer(body):
            kind = m.group("kind").lower().rstrip("s")
            counts[kind] = int(m.group("n"))
        # Duration is usually `in 1.23s` at the end of the ribbon.
        dur_m = re.search(r"in\s+([\d.]+)s", body)
        if dur_m:
            duration = f"{dur_m.group(1)}s"

    failed_tests: list[dict[str, str]] = []
    for m in _PYTEST_FAILED_TEST_RE.finditer(combined):
        failed_tests.append(
            {
                "test": m.group("test"),
                "reason": (m.group("reason") or "").strip(),
            },
        )

    return {
        "framework": "pytest",
        "counts": counts,
        "duration": duration,
        "failed_tests": failed_tests,
        "summary_line": summary_line,
    }


def render_test_output(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    framework: str,
) -> tuple[list[TextContent], dict[str, Any]]:
    """Render a test run: user blocks + agent structured payload.

    Returns (content_blocks, structured) — structured is terse counts
    for the agent, content is the pretty rendering for the user. On
    failure, the traceback/stderr is added as user-visible AND agent-
    visible (agent needs it to fix).
    """
    if framework == "test-python":
        parsed = _parse_pytest(stdout, stderr)
    else:
        # Other test frameworks — fall back to returning raw output
        # until specific parsers are written. Structured carries only
        # the exit code for now.
        parsed = {"framework": framework, "counts": {}, "duration": None, "failed_tests": []}

    counts = parsed.get("counts") or {}
    passed = int(counts.get("passed", 0))
    failed = int(counts.get("failed", 0)) + int(counts.get("error", 0))
    skipped = int(counts.get("skipped", 0))
    duration = parsed.get("duration") or ""

    # Header.
    if exit_code == 0 and failed == 0:
        icon = "✅"
        status = "PASS"
    elif failed > 0:
        icon = "❌"
        status = "FAIL"
    else:
        icon = "⚠️"
        status = "ERROR"
    bits: list[str] = []
    if passed:
        bits.append(f"{passed} passed")
    if failed:
        bits.append(f"{failed} failed")
    if skipped:
        bits.append(f"{skipped} skipped")
    if duration:
        bits.append(duration)
    summary_bits = " · ".join(bits) if bits else f"exit {exit_code}"

    blocks: list[TextContent] = []
    blocks.append(_tc_user(f"{icon} {status} · {summary_bits}"))
    blocks.append(_tc_user(f"`$ {command}`"))

    # Backlog #86 spec A: surface pytest's own summary line verbatim
    # when present. Cheap, predictable, contains failure counts +
    # duration in one line agents/operators recognize.
    summary_line = parsed.get("summary_line")
    if summary_line:
        blocks.append(_tc(f"pytest: {summary_line}"))

    # Backlog #86 spec B: failed-IDs are high-value. List as many as
    # fit under MAX_FAILED_IDS_BYTES; if truncated, append the
    # canonical omission line. No premature `… N more`. The
    # `_failed_tests_emitted` count is reused below for the
    # structured payload so render + structured agree.
    failed_list = parsed.get("failed_tests") or []
    _failed_tests_emitted = 0
    if failed_list:
        blocks.append(_tc(f"Failed tests ({len(failed_list)}):"))
        budget_used = 0
        for ft in failed_list:
            line = f"  ✗ {ft['test']}"
            if ft.get("reason"):
                line += f" — {ft['reason']}"
            line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline
            if budget_used + line_bytes > _MAX_FAILED_IDS_BYTES and _failed_tests_emitted > 0:
                break
            blocks.append(_tc(line))
            budget_used += line_bytes
            _failed_tests_emitted += 1
        omitted = len(failed_list) - _failed_tests_emitted
        if omitted > 0:
            blocks.append(
                _tc(
                    f"  ... {omitted} more failed test IDs omitted; re-read artifact/log if needed"
                ),
            )

    # Backlog #86 spec C: drop tracebacks/stderr tails by default.
    # The summary line + failed IDs already carry the actionable
    # signal; tracebacks are spam for the common diagnostic flow.
    # Operators/agents who need tracebacks can re-read the retained
    # log artifact (spec D N-window retention).

    structured = {
        "ok": exit_code == 0 and failed == 0,
        "exit_code": exit_code,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration": duration,
    }
    if failed_list:
        # Reuse the renderer's emitted count so structured + render
        # agree on truncation. Same byte budget by construction.
        cap = _failed_tests_emitted or len(failed_list)
        structured["failed_tests"] = [
            f"{ft['test']}" + (f": {ft['reason']}" if ft.get("reason") else "")
            for ft in failed_list[:cap]
        ]
        if cap < len(failed_list):
            structured["failed_tests_omitted"] = len(failed_list) - cap
    return blocks, structured


def render_probe_output(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    max_lines: int = 200,
) -> tuple[list[TextContent], dict[str, Any]]:
    """Render a probe/diagnostic run: verbatim stdout+stderr for both
    agent and user. Agent needs what it printed to debug.
    """
    blocks: list[TextContent] = []
    icon = "✅" if exit_code == 0 else "❌"
    blocks.append(_tc(f"{icon} `$ {command}` · exit {exit_code}"))

    def _emit_section(label: str, text: str) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        lines = text.splitlines()
        if len(lines) > max_lines:
            shown = lines[:max_lines]
            blocks.append(_tc(f"{label} (first {max_lines}/{len(lines)}):"))
            for line in shown:
                blocks.append(_tc(line))
            blocks.append(_tc(f"… {len(lines) - max_lines} more lines"))
        else:
            blocks.append(_tc(f"{label}:"))
            for line in lines:
                blocks.append(_tc(line))

    _emit_section("stdout", stdout)
    _emit_section("stderr", stderr)
    structured = {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout_lines": len((stdout or "").splitlines()),
        "stderr_lines": len((stderr or "").splitlines()),
    }
    return blocks, structured


_COPY_LOCK_ERROR_PATTERN = re.compile(
    # MSBuild file-copy/lock errors during dotnet build on Windows. These
    # are runtime-environment noise (another process is holding the DLL
    # open — usually a previous test-runner or visiblestudio), NOT real
    # compile errors. Separating them gives agents a clear signal that
    # the build logically succeeded but the output stage failed.
    r"("
    r"error\s+MSB30(?:20|21|26|27|30|31)\b"  # cannot-copy family
    r"|Exceeded\s+retry\s+count\s+of\s+\d+"
    r"|process\s+cannot\s+access\s+the\s+file"
    r"|being\s+used\s+by\s+another\s+process"
    r")",
    re.IGNORECASE,
)


def render_build_output(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> tuple[list[TextContent], dict[str, Any]]:
    """Render a build run: errors+warnings extracted from stderr.
    Falls back to probe rendering when no errors detected.

    C# / dotnet build on Windows: MSB3021/MSB3026/MSB3027 "cannot copy
    DLL because another process holds a lock" errors are runtime-env
    noise, not compile errors. The header separates them so agents
    don't chase phantom compile failures. A build that fails with ONLY
    copy-lock errors is effectively a successful compile blocked by a
    busy output dir.
    """
    err = (stderr or "") + "\n" + (stdout or "")
    # Rough error/warning extraction — tuned for common compilers.
    # Matches both ':'-delimited style ('error: foo') AND code-style
    # ('error CS1061: foo', 'error MSB3027: foo'). Line-anchored so a
    # stray 'error' in prose doesn't trip.
    error_lines = re.findall(
        r"^.*?\b(?:error|ERROR|Error)\b(?:\s+[A-Z]+\d+)?\s*[:\[].+$",
        err,
        re.MULTILINE,
    )
    warning_lines = re.findall(
        r"^.*?\b(?:warning|WARN)\b(?:\s+[A-Z]+\d+)?\s*[:\[].+$",
        err,
        re.MULTILINE,
    )
    copy_lock_lines = [line for line in error_lines if _COPY_LOCK_ERROR_PATTERN.search(line)]
    real_error_lines = [line for line in error_lines if not _COPY_LOCK_ERROR_PATTERN.search(line)]

    icon = "✅" if exit_code == 0 else "❌"
    status = "BUILD OK" if exit_code == 0 else "BUILD FAILED"
    # Special case: exit_code != 0 but ALL errors are copy-lock. The
    # compile succeeded; downgrade to an advisory amber status so the
    # agent knows the code is fine, the output-dir was busy.
    if exit_code != 0 and copy_lock_lines and not real_error_lines:
        icon = "⚠️"
        status = "BUILD OK · copy-lock (compile succeeded, DLL copy failed)"

    blocks: list[TextContent] = []
    bits = [status]
    if real_error_lines:
        bits.append(f"{len(real_error_lines)} real errors")
    if copy_lock_lines:
        bits.append(f"{len(copy_lock_lines)} copy-lock errors")
    if warning_lines:
        bits.append(f"{len(warning_lines)} warnings")
    blocks.append(_tc(f"{icon} {' · '.join(bits)}"))
    blocks.append(_tc(f"`$ {command}`"))

    if real_error_lines:
        blocks.append(_tc("errors (compile):"))
        blocks.append(_tc("```"))
        for line in real_error_lines[:30]:
            blocks.append(_tc(line))
        blocks.append(_tc("```"))
        if len(real_error_lines) > 30:
            blocks.append(_tc(f"… {len(real_error_lines) - 30} more compile errors"))
    if copy_lock_lines:
        blocks.append(
            _tc(
                "copy-lock errors (a process is holding a build output "
                "file; compile itself succeeded):",
            ),
        )
        blocks.append(_tc("```"))
        for line in copy_lock_lines[:10]:
            blocks.append(_tc(line))
        blocks.append(_tc("```"))
        if len(copy_lock_lines) > 10:
            blocks.append(_tc(f"… {len(copy_lock_lines) - 10} more copy-lock errors"))

    structured = {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "errors": len(real_error_lines),
        "copy_lock_errors": len(copy_lock_lines),
        "warnings": len(warning_lines),
    }
    return blocks, structured


def digest_for_run(command: str, tail: str, exit_code: int) -> str:
    """One-line signal from run tail for notification injection.

    Test frameworks → counts + first failed test on fail.
    Builds → error count on fail.
    Probe / unknown → tail's last non-empty line (truncated).
    Keep under ~100 chars; callers cap at 200.
    """
    if not tail:
        return ""
    kind = classify_command(command) if command else "probe"
    try:
        if kind == "test-python":
            parsed = _parse_pytest(tail, "")
            counts = parsed.get("counts") or {}
            bits: list[str] = []
            for key in ("passed", "failed", "error", "skipped"):
                n = counts.get(key)
                if n:
                    bits.append(f"{n} {key}")
            summary = ", ".join(bits) if bits else ""
            if exit_code != 0 and parsed.get("failed_tests"):
                first = parsed["failed_tests"][0].get("test", "")
                if first:
                    short = first.rsplit("::", 1)[-1][:50]
                    return f"{summary} · {short}" if summary else short
            return summary
        if kind == "build":
            # Count error-ish lines — cheaper than re-rendering.
            err_lines = [ln for ln in tail.splitlines() if re.search(r"\b(error|ERROR)\b", ln)]
            if exit_code != 0:
                return f"{len(err_lines)} error{'s' if len(err_lines) != 1 else ''}"
            return "ok"
    except Exception:
        pass
    # Probe / fallback: last non-empty line, truncated.
    for ln in reversed(tail.splitlines()):
        ln = ln.strip()
        if ln:
            return ln[:100]
    return ""
