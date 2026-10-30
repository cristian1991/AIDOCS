"""Session-start environment preflight — the #769 stale-venv guard.

Answers ONE question at session start: does the venv actually running
``aidocs_mcp`` satisfy ``aidocs_mcp``'s OWN declared requirements, and is
pip's dependency graph internally consistent?

This catches the class where a declared dependency is present but installed
BELOW its floor (e.g. ``fastmcp 3.3.1`` under a declared ``fastmcp>=3.4.4``).
An import-presence check can't see it — the module imports fine — so the
drift only surfaces later as environment-only test failures that read like
product regressions. Surfacing it at session start turns a multi-hour
misdiagnosis into a one-line "rebuild your venv" bullet.

Design:
  * Audits the RUNNING distribution's declared requirements via
    ``importlib.metadata`` — works both installed and from source, and needs
    no repo tree or pyproject path.
  * Core requirements only (extras like dev/slop/ML may be intentionally
    absent); environment markers honored (a ``python_version < "3.11"`` pin
    never counts as missing on 3.13).
  * FAIL-OPEN everywhere: any error yields ``ok=True`` + a ``skipped_reason``.
    A preflight that blocks the session it is trying to protect is a bug.
  * Read-only, no network. ``pip check`` runs a short-timeout subprocess.

Every seam (requires/version lookup, pip runner) is injectable for tests.
"""

from __future__ import annotations

import importlib
import importlib.metadata as _md
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

PACKAGE_IMPORT_NAME = "aidocs_mcp"

DIST_NAME = "aidocs-mcp"
PackageNotFound = _md.PackageNotFoundError

_PIP_CHECK_TIMEOUT = 15
_PIP_CHECK_CLEAN = "no broken requirements found"


def _default_requires_lookup(name: str) -> list[str]:
    return list(_md.requires(name) or [])


def _default_version_lookup(name: str) -> str:
    return _md.version(name)


def _parse_manifest_project_deps(path: Path, norm_name: str) -> list[str] | None:
    """Read ``[project].dependencies`` from a pyproject whose ``[project].name``
    matches ``norm_name`` (normalized). Returns None on any mismatch/error so a
    sibling manifest (e.g. a vendored package) is never mistaken for ours."""
    try:
        import tomllib as _toml
    except ModuleNotFoundError:  # pragma: no cover — py<3.11 only
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except Exception:  # noqa: BLE001
            return None
    try:
        data = _toml.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    project = data.get("project") or {}
    name = str(project.get("name", "")).replace("-", "_").lower()
    if name != norm_name:
        return None
    deps = project.get("dependencies")
    return list(deps) if isinstance(deps, list) else None


def _source_manifest_deps(dist_name: str, package_import_name: str) -> list[str] | None:
    """Locate the SOURCE ``pyproject.toml`` for ``package_import_name`` by
    walking up from the imported module and matching ``[project].name`` to
    ``dist_name``. Returns its declared dependencies, or None when no source
    tree is adjacent (a wheel install) — the caller then falls back to
    installed distribution metadata.

    Why prefer source: a built wheel freezes the floors current at build time,
    so a wheel can declare ``fastmcp>=3.2.4`` while the live source manifest
    already requires ``>=3.4.4``. Auditing the source floors is what catches
    the #769 drift class on the source/editable installs AIDOCS runs from.
    """
    try:
        mod = importlib.import_module(package_import_name)
        start = Path(mod.__file__).resolve().parent  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None
    norm = dist_name.replace("-", "_").lower()
    cur = start
    for _ in range(5):  # aidocs_mcp/ -> server/ -> mcp/(pyproject) is 2 hops
        deps = _parse_manifest_project_deps(cur / "pyproject.toml", norm)
        if deps is not None:
            return deps
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def audit_declared_floors(
    *,
    dist_name: str = DIST_NAME,
    package_import_name: str = PACKAGE_IMPORT_NAME,
    include_extras: bool = False,
    requires_lookup: Callable[[str], list[str]] | None = None,
    version_lookup: Callable[[str], str] | None = None,
    manifest_floors_lookup: Callable[[], list[str] | None] | None = None,
) -> dict[str, Any]:
    """Compare each declared runtime requirement against the installed version.

    Floor source precedence (each catches a different drift shape):
      1. an injected ``manifest_floors_lookup`` (tests),
      2. the SOURCE ``pyproject.toml`` when a source/editable tree is adjacent
         (authoritative on the gate clones where AIDOCS runs editable),
      3. installed distribution metadata (the only option for a wheel install).
    Auto-location (2 and 3) runs only when no explicit ``requires_lookup`` is
    injected, so existing metadata-based callers/tests keep their semantics.

    Returns ``{ok, checked, below_floor:[{package,installed,required}],
    missing:[{package,required}], floor_source, skipped_reason}``. Fail-open:
    metadata errors set ``skipped_reason`` and ``ok=True``.
    """
    version_lookup = version_lookup or _default_version_lookup

    try:
        from packaging.requirements import Requirement
    except Exception as exc:  # noqa: BLE001 — packaging always ships with pip
        return _skipped(f"packaging unavailable: {exc!r}")

    # ── resolve floor specs by precedence ────────────────────────────────
    raw: list[str] | None = None
    floor_source = "installed_metadata"
    if manifest_floors_lookup is not None:
        try:
            raw = manifest_floors_lookup()
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            floor_source = "source_manifest"
    elif requires_lookup is None:  # pure production path — prefer source
        src = _source_manifest_deps(dist_name, package_import_name)
        if src:
            raw, floor_source = src, "source_manifest"
    if raw is None:
        rl = requires_lookup or _default_requires_lookup
        try:
            raw = list(rl(dist_name) or [])
        except Exception as exc:  # noqa: BLE001
            return _skipped(f"requires({dist_name!r}) failed: {exc!r}")

    below_floor: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    checked = 0

    for spec in raw:
        try:
            req = Requirement(spec)
        except Exception:  # noqa: BLE001 — a malformed line never breaks the audit
            continue
        # Extras (dev/slop/ML) are opt-in installs — never audit them by default.
        if not include_extras and req.marker is not None and "extra" in str(req.marker):
            continue
        # Honor environment markers (python_version, sys_platform, ...).
        if req.marker is not None:
            try:
                if not req.marker.evaluate():
                    continue
            except Exception:  # noqa: BLE001 — unevaluable marker → skip, don't crash
                continue
        checked += 1
        try:
            installed = version_lookup(req.name)
        except PackageNotFound:
            missing.append({"package": req.name, "required": str(req.specifier) or str(spec)})
            continue
        except Exception:  # noqa: BLE001
            checked -= 1  # unknown lookup failure — don't count or flag
            continue
        # URL / no-specifier requirements are presence-only (already satisfied here).
        if not str(req.specifier):
            continue
        try:
            ok = req.specifier.contains(installed, prereleases=True)
        except Exception:  # noqa: BLE001
            continue
        if not ok:
            below_floor.append(
                {"package": req.name, "installed": installed, "required": str(req.specifier)},
            )

    return {
        "ok": not below_floor and not missing,
        "checked": checked,
        "below_floor": below_floor,
        "missing": missing,
        "floor_source": floor_source,
        "skipped_reason": None,
    }


_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_pip_check() -> tuple[int, str, str]:
    # #345: routed through audited_run so this pip-check spawn lands a
    # process-audit ledger row (coverage-true-by-construction spawn seal). The
    # run= lambda IS the registered direct-run AST callsite
    # (LEGACY_SUBPROCESS_FINGERPRINTS: env_floor_audit.py/_run_pip_check).
    from .shell_egress_service import audited_run

    proc = audited_run(
        [sys.executable, "-m", "pip", "check"],
        fingerprint=("env_floor_audit.py", "_run_pip_check", "subprocess.run"),
        reason="env-floor declared-floor preflight `pip check` — read-only metadata probe, fixed argv, no shell, no agent input, fail-open",
        run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
        capture_output=True,
        text=True,
        timeout=_PIP_CHECK_TIMEOUT,
        check=False,
        creationflags=_WIN_NO_WINDOW,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def pip_check(*, runner: Callable[[], tuple[int, str, str]] | None = None) -> dict[str, Any]:
    """Run ``pip check`` for dependency-graph consistency. Fail-open."""
    runner = runner or _run_pip_check
    try:
        code, out, err = runner()
    except Exception as exc:  # noqa: BLE001 — timeout / pip absent → skip, never block
        return {"ok": True, "issues": [], "skipped_reason": f"pip check unavailable: {exc!r}"}
    text = (out + "\n" + err).strip()
    if code == 0 or _PIP_CHECK_CLEAN in text.lower():
        return {"ok": True, "issues": [], "skipped_reason": None}
    issues = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return {"ok": False, "issues": issues, "skipped_reason": None}


def audit_environment(
    *,
    dist_name: str = DIST_NAME,
    run_pip_check: bool = True,
    requires_lookup: Callable[[str], list[str]] | None = None,
    version_lookup: Callable[[str], str] | None = None,
    pip_runner: Callable[[], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """Full session-start environment preflight: declared-floor audit + pip
    check, folded into one fail-open verdict with a human summary.
    """
    floors = audit_declared_floors(
        dist_name=dist_name,
        requires_lookup=requires_lookup,
        version_lookup=version_lookup,
    )
    pc = pip_check(runner=pip_runner) if run_pip_check else {"ok": True, "issues": [], "skipped_reason": "not_run"}

    # The FLOOR AUDIT is the authority for the session-start verdict — it is the
    # exact #769 class (a declared dep missing or below its floor). ``pip check``
    # is ADVISORY only: a dev/gate venv routinely carries a lint tool (semgrep)
    # whose over-strict transitive pins conflict with the app's deliberately
    # newer deps, so letting pip check flip the verdict would cry wolf every
    # session. Its result is kept in the payload for context, never as the
    # trigger.
    ok = bool(floors["ok"])
    return {
        "ok": ok,
        "checked": floors["checked"],
        "below_floor": floors["below_floor"],
        "missing": floors["missing"],
        "floor_source": floors.get("floor_source"),
        "pip_check": pc,
        "pip_advisory": (not pc["ok"]),
        "skipped_reason": floors["skipped_reason"],
        "summary": _summarize(floors, pc),
    }


def _summarize(floors: dict[str, Any], pc: dict[str, Any]) -> str:
    # Floor drift is the real signal → an actionable "rebuild" message.
    if not floors["ok"]:
        parts: list[str] = []
        for b in floors["below_floor"]:
            parts.append(f"{b['package']} {b['installed']} is below required {b['required']}")
        for m in floors["missing"]:
            parts.append(f"{m['package']} (required {m['required']}) is not installed")
        detail = "; ".join(parts) if parts else "environment drift detected"
        note = ""
        if not pc["ok"] and pc["issues"]:
            note = f" (pip check also reports {len(pc['issues'])} conflict(s))"
        return (
            f"Environment drift: {detail}{note}. Rebuild/sync the venv from the "
            "manifest before trusting test results."
        )
    # Floors satisfied → OK. A pip conflict here is third-party pin skew
    # (advisory), not an app-environment problem — never a session warning.
    base = f"Environment OK — {floors['checked']} declared dependencies satisfy their floors."
    if not pc["ok"] and pc["issues"]:
        return f"{base} Advisory: pip check reports {len(pc['issues'])} third-party pin conflict(s), non-blocking."
    return base


_CACHE: dict[str, Any] | None = None


def cached_environment() -> dict[str, Any]:
    """Process-memoized :func:`audit_environment` for the session-start hot
    path. The installed environment cannot change within a running process,
    so the ``pip check`` subprocess runs at most once per process. Fail-open:
    an unexpected error yields a skipped-but-ok verdict.
    """
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = audit_environment()
        except Exception as exc:  # noqa: BLE001 — never let the guard block startup
            _CACHE = {
                **_skipped(f"env audit failed: {exc!r}"),
                "pip_check": {"ok": True, "issues": []},
                "summary": "",
            }
    return _CACHE


def _skipped(reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "checked": 0,
        "below_floor": [],
        "missing": [],
        "floor_source": None,
        "skipped_reason": reason,
    }
