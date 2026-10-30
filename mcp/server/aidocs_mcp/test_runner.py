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

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# #477 (War BA): CreateProcess on Windows resolves bare names only to .exe —
# `npm`/`npx` are .cmd shims, so spawning argv ["npm", ...] with shell=False
# dies with WinError 2. shutil.which honors PATHEXT and returns the concrete
# launcher path (npm.cmd), which subprocess CAN spawn. Precedent:
# shell_xray._BINARY_EXTS already normalizes npm.cmd/code.cmd on Windows.
_IS_WINDOWS = os.name == "nt"
# Windows: the daemon runs console-less (pythonw), so an unflagged spawn pops a
# visible console window per call (#333 Phase 2). NAMED `_WIN_NO_WINDOW` to match
# every other module — the spawn-surface seal scans for that literal name as
# windowless evidence, so a locally-invented synonym reads as an UNFLAGGED spawn
# and fails the seal. One name, or the scanner cannot see it.
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _which_launcher(name: str) -> str | None:
    return shutil.which(name)


def resolve_windows_launcher(
    argv0: str,
    *,
    is_windows: bool | None = None,
    which: Any = None,
) -> str:
    """Resolve a bare argv head to its concrete launcher path on Windows.

    Non-Windows, path-like tokens (contain a separator or extension), and
    unresolvable names pass through unchanged — the spawn error stays
    actionable when a toolchain is genuinely missing.
    """
    if is_windows is None:
        is_windows = _IS_WINDOWS
    if which is None:
        which = _which_launcher
    if not is_windows:
        return argv0
    if "/" in argv0 or "\\" in argv0 or Path(argv0).suffix:
        return argv0
    resolved = which(argv0)
    if not resolved:
        return argv0
    # Only .cmd/.bat shims need the concrete path — CreateProcess finds
    # .exe binaries (cargo/dotnet/go) by bare name on PATH just fine, and
    # existing consumers pin the bare-name argv for those.
    if Path(str(resolved)).suffix.lower() in {".cmd", ".bat"}:
        return str(resolved)
    return argv0

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

def _project_requirements(cwd: Path) -> list[str]:
    """The tree's own declared requirements — runtime deps plus the `dev` extra.

    THE `dev` EXTRA IS THE POINT for a test interpreter: pyproject calls it "the
    P0 dev toolchain — every gate (local, CI, deploy script) MUST install
    `mcp[dev]` so the set is uniform". An interpreter that no longer satisfies
    those pins is exactly the one that produces mystery test failures.

    Read from pyproject rather than from an installed copy: the question is what
    the TREE requires, and comparing that against what an interpreter actually
    has is the whole measurement.
    """
    try:
        import tomllib

        with (cwd / "pyproject.toml").open("rb") as fh:
            project = tomllib.load(fh).get("project") or {}
        reqs = [str(r) for r in (project.get("dependencies") or [])]
        extras = project.get("optional-dependencies") or {}
        reqs += [str(r) for r in (extras.get("dev") or [])]
        return [r for r in reqs if str(r).strip()]
    except Exception:  # noqa: BLE001 — a diagnostic must never break a test run
        return []


def interpreter_drift(
    interpreter: str,
    cwd: Path,
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any] | None:
    """Does the DECLARED interpreter still satisfy the tree's declared pins?

    WHY THIS EXISTS (2026-08-29). `test.interpreter` fixed "no interpreter
    exists on a clone" — `.venv` is untracked, so a synced checkout can never
    carry one — and created a second failure mode doing it: that interpreter is
    a SNAPSHOT. The gate's was built once from one release's `[dev]` extra. When
    the project's pins move, the interpreter and the tree disagree, and the
    symptom is a TEST failure (an import error, a missing plugin) which reads as
    "the code is broken" and sends the reader into the wrong file entirely.

    THE FIRST VERSION COMPARED `project.version`, AND THAT SIGNAL WAS NEARLY
    INERT. Measured on this repo the same day it shipped: version last moved
    2026-07-19 (63ca823b6, "bump to v2.5.1") — six weeks and hundreds of commits
    earlier. A check that can fire about three times a year, while the
    dependency set moves on any deploy, is WORSE than none: it offers the
    confidence of a green tripwire that is not actually watching.

    So it now compares THE THING THAT ACTUALLY BREAKS RUNS — the tree's declared
    requirements against what the interpreter has installed. That fires exactly
    when a pin moves, stays silent otherwise, needs no cooperation from whoever
    built the venv, and needs no build-time stamp.

    THE CHILD DOES THE EVALUATING, deliberately. Specifiers and environment
    markers must resolve in the TARGET interpreter's own environment —
    `sys_platform`, `python_version` and the installed set are properties of
    that process, not of the daemon asking. Evaluating here would answer a
    subtly different question, and would be wrong on exactly the cross-platform
    markers that matter.

    IT REPORTS, IT DOES NOT REFUSE, and it does not repair. Refusing would be
    worse than the disease — a drifted env usually still runs the suite — and
    repairing means `pip install` from a tenant's lockfile as the gate user, the
    exact execution surface the declaration was chosen to avoid.

    SILENT WHENEVER IT CANNOT ESTABLISH DRIFT: no pyproject, no declared
    requirements, `packaging` absent in the target (not a test venv), a non-zero
    exit, a timeout, unparseable output, or any exception. An honest "no claim"
    beats a confident wrong one, and a diagnostic that becomes the reason a
    suite cannot run is worse than no diagnostic at all.
    """
    requirements = _project_requirements(cwd)
    if not requirements:
        return None

    probe = (
        "import sys,json\n"
        "try:\n"
        "    from packaging.requirements import Requirement\n"
        "    import importlib.metadata as md\n"
        "except Exception:\n"
        "    sys.exit(9)\n"
        "out=[]\n"
        "for spec in json.load(sys.stdin):\n"
        "    try:\n"
        "        r=Requirement(spec)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if r.marker is not None and not r.marker.evaluate():\n"
        "        continue\n"
        "    try:\n"
        "        have=md.version(r.name)\n"
        "    except Exception:\n"
        "        out.append([r.name,None,str(r.specifier)]);continue\n"
        "    if r.specifier and not r.specifier.contains(have,prereleases=True):\n"
        "        out.append([r.name,have,str(r.specifier)])\n"
        "json.dump(out,sys.stdout)\n"
    )

    # #345 SPAWN SEAL: routed through audited_run so this lands a process-audit
    # ledger row. Gate 1c caught the first draft calling subprocess.run directly
    # and was right — this launches a FOREIGN INTERPRETER named by project
    # config. The run= lambda is the registered direct-run AST callsite.
    from .shell_egress_service import audited_run

    try:
        proc = audited_run(
            [interpreter, "-c", probe],
            fingerprint=("test_runner.py", "interpreter_drift", "subprocess.run"),
            reason=(
                "ask the declared test interpreter whether it still satisfies "
                "the tree's declared requirements — fixed argv, no shell, no "
                "agent input, diagnostic only and fail-silent"
            ),
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
            input=json.dumps(requirements),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            creationflags=_WIN_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None  # incl. exit 9: no `packaging` — not a test venv, no claim
    try:
        rows = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None

    # BOUNDED. A wholesale-stale env would otherwise paste its entire dependency
    # list into every ai_test result; the count carries the magnitude, the
    # sample carries the shape.
    sample = [
        {"package": str(n), "installed": v, "required": str(s)} for n, v, s in rows[:8]
    ]
    return {
        "interpreter": interpreter,
        "unsatisfied_count": len(rows),
        "unsatisfied": sample,
        "detail": (
            f"the declared test interpreter does not satisfy {len(rows)} of this "
            "tree's declared requirements, so a failure below may be environment "
            "drift rather than a code defect. Rebuild it against this tree, or "
            "clear `test.interpreter` if the tree now has its own .venv."
        ),
    }


def build_test_env(
    framework: str,
    environ: Mapping[str, str],
    interpreter: str = "",
) -> dict[str, str]:
    """Return the hermetic environment for the resolved test command.

    ``ai_test`` owns argv construction, so an inherited ``PYTEST_ADDOPTS``
    must not silently append xdist, basetemp, plugin, or target arguments.
    Other environment variables are preserved because project interpreters and
    toolchains still need their normal PATH/home/runtime configuration.

    THE RESOLVED INTERPRETER'S OWN bin/Scripts DIR GOES FIRST ON PATH
    (2026-08-29). ``<python> -m pytest`` runs without it, but a suite that
    shells out to a console script installed BESIDE that python — ruff,
    alembic, the project's own entry point — resolves the ambient one or exits
    127. That is the #469 failure one venv over, and ai_run has prepended a
    venv's bin dir since #469 while ai_test never did.

    ai_run guesses candidate venv LOCATIONS (run_dir/.venv, root/.venv,
    root/mcp/.venv). That instrument does not fit here: a DECLARED
    ``test.interpreter`` may sit outside the project tree entirely, and the
    interpreter we actually resolved is the only thing that knows its own
    directory. So this takes the path rather than searching for one.
    """
    env = dict(environ)
    if framework == "pytest":
        env.pop("PYTEST_ADDOPTS", None)
    if interpreter:
        bin_dir = os.path.dirname(os.path.abspath(interpreter))
        if bin_dir and os.path.isdir(bin_dir):
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env




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


def _project_interpreter(cwd: Path, declared: str = "") -> tuple[str | None, list[str]]:
    """The PROJECT's own test interpreter, and every path tried getting there.

    Never the ambient/gate sys.executable: a remote gate's sys.executable is the
    minimal gate venv with no pytest, so the project's tests must run in an
    interpreter that carries the project's deps.

    DECLARED FIRST (2026-08-29). Discovery alone cannot work on a CLONE: `.venv`
    is untracked, so a git checkout — which is exactly what the gate syncs for a
    tenant — can NEVER contain one. Measured live: ai_test on the gate reached
    `mcp/` correctly (cwd resolution and the auto-mcp rule both worked) and then
    refused because mcp/.venv/bin/python does not exist and structurally cannot.

    The declaration is how a box points at an interpreter it ALREADY has. The
    VPS builds a full test venv every deploy — gate 2b runs the whole suite in
    /opt/aidocs/custody/<run>/venv — so the box can run these tests; this
    resolver simply could not see that interpreter, because it only ever looked
    inside the clone.

    WHY A DECLARATION AND NOT AUTO-PROVISIONING: building the env on demand
    means `pip install` from a tenant's lockfile, executing that repo's build
    hooks as the gate user on a shared host. A declared path adds NO execution
    surface — it names an interpreter that already exists and was already
    trusted by whoever put it there.

    A relative declaration resolves against `cwd`, so a project can say
    ".venv-ci/bin/python" without hardcoding a machine path.

    Returns (interpreter | None, tried) — `tried` exists so the refusal can name
    every candidate instead of one, which is what made the live failure read as
    "wrong directory" when it was "no interpreter anywhere".
    """
    tried: list[str] = []
    if declared:
        cand = Path(declared)
        if not cand.is_absolute():
            cand = cwd / declared
        tried.append(f"{cand} (declared: test.interpreter)")
        if cand.is_file():
            return str(cand), tried
    for rel in (".venv/bin/python", ".venv/bin/python3", ".venv/Scripts/python.exe"):
        cand = cwd / rel
        tried.append(str(cand))
        if cand.is_file():
            return str(cand), tried
    return None, tried


def resolve_test_command(
    cwd: Path,
    *,
    framework: str = "",
    paths: list[str] | None = None,
    name_filter: str = "",
    interpreter: str = "",
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
    argv = build_test_argv(fw, paths=paths, name_filter=name_filter)
    # A python framework (pytest) is templated with the ambient sys.executable; on
    # the remote gate that is the minimal gate venv (no pytest). Substitute the
    # PROJECT's interpreter (its deps), or refuse actionably — never silently run
    # the gate's python against the project's tests.
    if argv and argv[0] == sys.executable:
        interp, tried = _project_interpreter(cwd, interpreter)
        if interp is None:
            # THE REFUSAL NAMES EVERY CANDIDATE, and a remedy that exists.
            # It used to name one path and say "provision the project's test
            # runtime" — which named nothing callable, and read as "wrong
            # directory" when the truth was "no interpreter anywhere". On a
            # CLONE there never can be one: .venv is untracked.
            raise TestRunnerError(
                "no project test interpreter found. Tried, in order: "
                + "; ".join(tried)
                + ". A git clone cannot contain a .venv (it is untracked), so "
                "on a synced checkout declare one instead: set "
                "`test.interpreter` to a python that already has this "
                "project's test deps (absolute, or relative to the test root). "
                "The gate's own deploy builds exactly such an interpreter per "
                "run under the custody dir."
            )
        argv[0] = interp
    # #477: bare launcher names (npm, dotnet, cargo, go) must resolve to a
    # concrete path on Windows — CreateProcess cannot exec `npm` (an npm.cmd
    # shim) by bare name with shell=False (WinError 2, War BA).
    argv[0] = resolve_windows_launcher(argv[0])
    return fw, argv
