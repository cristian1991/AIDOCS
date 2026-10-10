"""Extract the useful lines from shell-command output.

code_run_command used to dump up to 4000 chars of stdout + 2000 of
stderr on every call. For pytest/build/test runs, 99% of that is
dots and scroll — the agent cares about the pass/fail counts, the
FAILED lines, and the tracebacks. Everything else is context burn.

This module extracts the meaningful lines via regex families:
  - pytest summary (N passed, M failed, K skipped, ...)
  - pytest per-test status (PASSED path::test, FAILED path::test, ...)
  - tracebacks (E   AssertionError: ...)
  - build errors (error: ..., SyntaxError: ...)
  - lint findings (F401, E501, etc.)
  - generic error lines (Error:, ERROR, Exception:)

If nothing matches, returns an empty string — the caller collapses
to "ok" when exit_code=0 + no interesting output.
"""

from __future__ import annotations

import re

# pytest summary: "3 passed, 1 failed in 0.45s" or longer variants.
_PYTEST_SUMMARY = re.compile(
    r"=+\s*(\d+\s+(?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?|deselected)"
    r"(?:,\s*\d+\s+(?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?|deselected))*"
    r"(?:\s+in\s+[\d.]+\s*s(?:econds)?)?)\s*=+",
    re.IGNORECASE,
)

# Per-test status lines — keep FAILED / ERROR / XFAIL / XPASS / SKIP,
# drop PASSED (usually the majority). Matches both pytest orderings:
#   "FAILED path::test"          (short summary / -rf output)
#   "path::test FAILED"           (default verbose output)
_PYTEST_FAIL_LINE = re.compile(
    r"^.*\b(FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b.*$",
    re.MULTILINE,
)

# Short pytest summary "short test summary info" block.
_PYTEST_SHORT_SUMMARY = re.compile(
    r"^=+\s*short test summary info\s*=+\s*$(.*?)(?=^=|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Assertion lines in tracebacks.
_TRACEBACK_ERROR_LINE = re.compile(
    r"^(E\s+.+|\s*[\w.]+Error:\s*.+|\s*AssertionError:.*)$",
    re.MULTILINE,
)

# Generic error patterns (build, lint, compiler).
_GENERIC_ERROR = re.compile(
    r"^.*(?:error[:\s]|exception|traceback\s*\(most recent|FATAL|CRITICAL).+$",
    re.MULTILINE | re.IGNORECASE,
)

# Lint tool output (ruff/flake8-style: path:line:col: code message).
_LINT_FINDING = re.compile(
    r"^\s*[^\s:]+:\d+:\d+:\s+[A-Z]\d{2,4}\s+.+$",
    re.MULTILINE,
)

# Exit code prefix from some test runners.
_EXIT_CODE = re.compile(
    r"^(?:exit_code|exit code|return code):\s*(-?\d+)$",
    re.MULTILINE | re.IGNORECASE,
)


_MAX_SUMMARY_CHARS = 1500


def summarize_run_output(
    stdout: str,
    stderr: str,
    exit_code: int,
) -> str:
    """Extract useful lines from a command's combined output.

    Priority (top to bottom; we stop when _MAX_SUMMARY_CHARS is hit):
      1. pytest-style summary line
      2. pytest short-summary block
      3. FAILED/ERROR/XFAIL test lines
      4. Traceback `E` lines and `<Type>Error:` lines
      5. Lint findings
      6. Generic error/exception/traceback lines

    Empty string iff there's nothing worth showing AND exit_code==0;
    caller collapses that to "ok".
    """
    combined = ""
    if stdout:
        combined += stdout
    if stderr:
        combined += ("\n" if combined else "") + stderr

    if not combined.strip():
        return ""

    extracted: list[str] = []
    seen: set[str] = set()

    def _add(line: str) -> bool:
        """Add a line if unseen and under budget. True while budget remains."""
        clean = line.strip()
        if not clean or clean in seen:
            return True
        seen.add(clean)
        extracted.append(clean)
        return sum(len(x) + 1 for x in extracted) < _MAX_SUMMARY_CHARS

    # 1. pytest summary line (most useful single extraction).
    for m in _PYTEST_SUMMARY.finditer(combined):
        if not _add(m.group(1).strip()):
            return "\n".join(extracted)

    # 2. pytest short-summary block (FAILED tests + reasons).
    for m in _PYTEST_SHORT_SUMMARY.finditer(combined):
        body = m.group(1).strip()
        for line in body.splitlines():
            if not _add(line):
                return "\n".join(extracted)

    # 3. Per-test failures.
    for m in _PYTEST_FAIL_LINE.finditer(combined):
        if not _add(m.group(0)):
            return "\n".join(extracted)

    # 4. Traceback body lines.
    for m in _TRACEBACK_ERROR_LINE.finditer(combined):
        if not _add(m.group(0)):
            return "\n".join(extracted)

    # 5. Lint findings.
    for m in _LINT_FINDING.finditer(combined):
        if not _add(m.group(0)):
            return "\n".join(extracted)

    # 6. Generic errors — last resort. Skip if we already have content
    # from higher-priority extractors (generic error regex is noisy).
    if not extracted:
        for m in _GENERIC_ERROR.finditer(combined):
            if not _add(m.group(0)):
                return "\n".join(extracted)

    return "\n".join(extracted)
