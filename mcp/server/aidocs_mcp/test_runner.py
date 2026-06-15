"""Language-agnostic test-runner resolution for the ``ai_test`` tool.

``ai_test`` is the subagent-safe replacement for ``ai_run``: a lane worker
must be able to run the project's tests WITHOUT raw shell access (raw shell is
how a worker can write to gate code / evade the gate). This module is the
pure, testable core: detect the project's test framework, then build a SAFE
argv (never a shell string — argv form, shell=False, so there is no injection
surface and no metacharacter can smuggle a second command).

Supported frameworks (the languages AIDOCS indexes): Python (pytest), .NET
(dotnet test), Rust (cargo test), Go (go test), JS/TS (npm/jest/vitest). The
registry is data-driven so a new language is one entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Framework registry ───────────────────────────────────────────────
# Each entry: marker globs that identify the project, the base argv, and how a
# name filter is expressed. `filter_flag` → "<flag> <value>"; if filter is
# positional (None flag) the value is appended as a bare arg.
_FRAMEWORKS: dict[str, dict] = {
    "pytest": {
        "markers": ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py"),
        "base": [sys.executable, "-m", "pytest", "-q"],
        "filter_flag": "-k",
        "accepts_paths": True,
    },
    "dotnet": {
        "markers": ("*.sln", "*.csproj"),
        "base": ["dotnet", "test", "--nologo"],
        "filter_flag": "--filter",
        "accepts_paths": False,  # dotnet test takes a project/solution, not file paths
    },
    "cargo": {
        "markers": ("Cargo.toml",),
        "base": ["cargo", "test"],
        "filter_flag": None,  # cargo test <name> is positional
        "accepts_paths": False,
    },
    "go": {
        "markers": ("go.mod",),
        "base": ["go", "test", "./..."],
        "filter_flag": "-run",
        "accepts_paths": False,
    },
    "node": {
        "markers": ("package.json",),
        "base": ["npm", "test", "--"],
        "filter_flag": "-t",  # jest/vitest -t <name>; npm passes after --
        "accepts_paths": True,
    },
}

# Detection precedence — first framework whose markers match wins. Python
# first (AIDOCS itself), then compiled, then node (package.json is common as a
# tooling sidecar even in non-JS repos, so it is last).
_DETECT_ORDER = ("pytest", "dotnet", "cargo", "go", "node")

# Characters that must never reach an argv token — even though shell=False
# makes them inert, rejecting them keeps inputs honest and blocks attempts to
# smuggle flags/paths.
_UNSAFE = set(";|&$<>`\n\r\t\"'\\")


class TestRunnerError(ValueError):
    """Unsafe input or no detectable framework."""

    __test__ = False  # not a pytest test class despite the Test* name


def detect_framework(cwd: Path) -> str | None:
    """Return the test framework for the project rooted at *cwd*, or None.

    Marker files are matched at the project root only (no deep walk) — the
    worker runs from the subtree that holds the project's build manifest.
    """
    cwd = Path(cwd)
    for fw in _DETECT_ORDER:
        for marker in _FRAMEWORKS[fw]["markers"]:
            if "*" in marker:
                if any(cwd.glob(marker)):
                    return fw
            elif (cwd / marker).exists():
                return fw
    return None


def _validate_token(token: str, *, kind: str) -> str:
    token = token.strip()
    if not token:
        raise TestRunnerError(f"empty {kind}")
    if any(c in _UNSAFE for c in token):
        raise TestRunnerError(f"unsafe {kind} {token!r}: shell metacharacters are not allowed")
    return token


def _validate_path(p: str) -> str:
    p = _validate_token(p, kind="test path")
    norm = p.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        raise TestRunnerError(f"absolute test path not allowed: {p!r}")
    if norm.startswith("../") or "/../" in norm or norm == "..":
        raise TestRunnerError(f"parent-escaping test path not allowed: {p!r}")
    if p.startswith("-"):
        raise TestRunnerError(f"test path may not look like a flag: {p!r}")
    return p


def build_test_argv(
    framework: str,
    *,
    paths: list[str] | None = None,
    name_filter: str = "",
) -> list[str]:
    """Build the SAFE argv for *framework*. Pure — no execution.

    `paths` are project-relative test paths (only for frameworks that accept
    them). `name_filter` is a single test-name filter mapped to the
    framework's flag. Raises TestRunnerError on an unknown framework or any
    unsafe token. There is NO free-form argument passthrough — that is the
    whole point (a worker cannot turn ai_test into arbitrary shell).
    """
    spec = _FRAMEWORKS.get(framework)
    if spec is None:
        raise TestRunnerError(f"unknown test framework {framework!r}")
    argv = list(spec["base"])

    safe_paths = [_validate_path(p) for p in (paths or [])]
    if safe_paths and not spec["accepts_paths"]:
        raise TestRunnerError(f"{framework} does not accept file paths; use name_filter instead")
    # Paths go before the filter flag for pytest; for node (after `--`) append too.
    argv.extend(safe_paths)

    if name_filter:
        f = _validate_token(name_filter, kind="name_filter")
        flag = spec["filter_flag"]
        if flag is None:
            argv.append(f)  # positional (cargo)
        else:
            argv.extend([flag, f])
    return argv


def resolve_test_command(
    cwd: Path,
    *,
    framework: str = "",
    paths: list[str] | None = None,
    name_filter: str = "",
) -> tuple[str, list[str]]:
    """Resolve (framework, argv) for *cwd*. Explicit *framework* overrides
    detection. Raises TestRunnerError if none can be determined.
    """
    fw = (framework or "").strip().lower() or detect_framework(cwd)
    if not fw:
        raise TestRunnerError(
            "no test framework detected (looked for pyproject/csproj/Cargo.toml/"
            "go.mod/package.json); pass framework= explicitly"
        )
    return fw, build_test_argv(fw, paths=paths, name_filter=name_filter)
