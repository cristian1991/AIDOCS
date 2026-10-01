"""Ruff edit gate: a Python edit may not INTRODUCE Ruff findings.

Operator goal: "edit tools already run AST; for Python also run ruff, so we
never fail on ruff." Precisely: an edit to a ``.py`` file may not introduce
findings selected by the BOUND PROJECT's own Ruff configuration.

THIS MODULE IS THE ONE IMPLEMENTATION. Both validators consume it and neither
re-implements discovery, invocation or parsing:

  * ``host_policy_service.validate_edit_syntax`` -- the pre-edit hook, an
    earlier defense on the reconstructed whole file;
  * ``file_ops._validate_write`` -- the write path, the EXACT authority: it
    judges the precise bytes about to be written against the precise snapshot
    they were built from.

CONTRACT
  * Scope: ``.py`` only (``.pyi`` and everything else -> ``not_configured``).
  * Config discovery walks from the TARGET FILE's parent upward, bounded by the
    project root, looking in each directory for ``.ruff.toml``, ``ruff.toml``,
    then a ``pyproject.toml`` that carries ``[tool.ruff]`` (Ruff's own
    per-directory precedence). Nothing found INSIDE the project ->
    ``not_configured`` (skip, not an error). A config above the project root is
    never inherited: when the walk finds nothing, Ruff is not run at all.
  * Invocation: ``ruff check --stdin-filename <canonical repo-relative path>
    --force-exclude --no-fix --no-cache --output-format json -`` with
    cwd = project root, for BEFORE and for AFTER with the SAME context. Ruff's
    native hierarchical discovery then resolves exactly the config found above
    (same precedence, nearest wins), and that config's relative
    ``extend-exclude`` resolves against its own directory. ``--config`` is
    deliberately NOT passed: measured on ruff 0.16, an explicit ``--config``
    re-roots relative excludes at the cwd, so a nested ``mcp/pyproject.toml``
    exclude silently stopped applying. ``--no-fix`` keeps a config's
    ``fix = true`` from turning stdout into fixed source; ``--no-cache`` keeps
    the gate from writing ``.ruff_cache`` into the project.
  * Exit 0 = no findings; exit 1 with a JSON list = findings (NORMAL); anything
    else (rc 2, a crash, non-JSON, a timeout, no binary) = ``validator_error``.
  * Runtime source: the ruff console script of the ACTIVE interpreter's
    environment (``sysconfig.get_path("scripts")``). Never PATH, never a
    project venv. ``python -m ruff`` works too (the wheel ships
    ``ruff/__main__.py``), but its locator also falls back to the base prefix
    and the per-user scripts dir, i.e. to a ruff OUTSIDE the active
    environment; deriving the path here keeps the one source deterministic and
    saves a second process hop.
  * Egress: spawned through ``shell_egress_service.audited_run`` (argv form,
    named fingerprint and reason, CREATE_NO_WINDOW on Windows).

NEW-FINDINGS SEMANTICS
  Findings are compared as a MULTISET of ``(rule_code, message)`` with counts;
  location is ignored so that lines moving around an untouched finding never
  count as new. ``after_count - before_count > 0`` for a key means that many
  new findings. The refusal lists code + message + the AFTER line/col and never
  echoes source text.

  KNOWN LIMITATION: because location is ignored, removing one violation and
  adding an identical one (same code, same message) elsewhere in the same edit
  is indistinguishable from an unchanged file, and passes.

FAIL CLOSED
  ``validator_error`` on a configured ``.py`` file is a REFUSAL (reasons
  ``ruff_validator_error`` / ``ruff_validator_unavailable``). Callers turn it
  into a refusal result; it is never raised into a generic "validator hiccup,
  continue" path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sysconfig
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

STATUS_CLEAN = "clean"
STATUS_NEW_FINDINGS = "new_findings"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_VALIDATOR_ERROR = "validator_error"

REASON_NEW_FINDINGS = "ruff_new_findings"
REASON_VALIDATOR_ERROR = "ruff_validator_error"
REASON_VALIDATOR_UNAVAILABLE = "ruff_validator_unavailable"

#: Hard bound on one ruff run. Ruff lints a single file in milliseconds; a run
#: that takes this long is broken, and a broken validator refuses.
TIMEOUT_SECONDS = 10

#: The process-audit fingerprint of this spawn (spawn_authority.SPAWN_SITES),
#: exported for tests. ``_spawn_ruff`` passes the same value as a literal (the
#: seal reads the AST), and its reason literal "ruff-edit-gate" is registered
#: in spawn_authority.
SPAWN_FINGERPRINT = ("ruff_edit_gate.py", "_spawn_ruff", "subprocess.run")

#: Per-directory config precedence, as Ruff applies it.
_CONFIG_NAMES = (".ruff.toml", "ruff.toml")

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: How many new findings a refusal message spells out before summarising.
_MAX_LISTED = 20


@dataclass(frozen=True)
class RuffFinding:
    code: str
    message: str
    line: int
    col: int


@dataclass(frozen=True)
class RuffGateResult:
    status: str
    new_findings: tuple[RuffFinding, ...] = ()
    reason: str = ""
    detail: str = ""
    config_path: str = ""

    @property
    def refused(self) -> bool:
        return self.status in (STATUS_NEW_FINDINGS, STATUS_VALIDATOR_ERROR)

    def refusal_message(self) -> str:
        """Operator-facing refusal text: codes, messages, AFTER positions only."""
        if self.status == STATUS_NEW_FINDINGS:
            shown = self.new_findings[:_MAX_LISTED]
            lines = [f"  {f.line}:{f.col} {f.code} {f.message}" for f in shown]
            more = len(self.new_findings) - len(shown)
            if more > 0:
                lines.append(f"  ... and {more} more")
            return (
                f"[{REASON_NEW_FINDINGS}] Edit would introduce "
                f"{len(self.new_findings)} new Ruff finding(s) under "
                f"{self.config_path or 'the project Ruff config'}:\n" + "\n".join(lines)
            )
        if self.status == STATUS_VALIDATOR_ERROR:
            return (
                f"[{self.reason or REASON_VALIDATOR_ERROR}] Ruff could not validate this "
                f"Python edit; refusing to fail open: {self.detail}"
            )
        return ""


def _not_configured(why: str) -> RuffGateResult:
    return RuffGateResult(status=STATUS_NOT_CONFIGURED, reason=why)


def _error(reason: str, detail: str, config: Path | None) -> RuffGateResult:
    return RuffGateResult(
        status=STATUS_VALIDATOR_ERROR,
        reason=reason,
        detail=detail,
        config_path=str(config or ""),
    )


def _pyproject_has_tool_ruff(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    except tomllib.TOMLDecodeError:
        # Unparseable: Ruff itself would error on it if it names [tool.ruff],
        # so claim it as a config and let the run fail closed.
        try:
            return "[tool.ruff" in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
    tool = data.get("tool")
    return isinstance(tool, dict) and "ruff" in tool


def find_ruff_config(target: Path, project_root: Path) -> Path | None:
    """The Ruff config governing ``target``, found INSIDE ``project_root`` only."""
    root = Path(project_root).resolve()
    directory = Path(target).resolve().parent
    if directory != root and root not in directory.parents:
        return None
    while True:
        for name in _CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file() and _pyproject_has_tool_ruff(pyproject):
            return pyproject
        if directory == root or directory.parent == directory:
            return None
        directory = directory.parent


def ruff_executable() -> Path | None:
    """The ruff console script of the ACTIVE interpreter's environment, or None."""
    scripts = sysconfig.get_path("scripts")
    if not scripts:
        return None
    exe = Path(scripts) / ("ruff" + (sysconfig.get_config_var("EXE") or ""))
    return exe if exe.is_file() else None


def _spawn_ruff(argv, *, stdin_text, cwd, timeout):
    """ONE audited ruff spawn: fixed argv head, stdin source, no shell."""
    from .shell_egress_service import audited_run

    # fingerprint/reason stay LITERALS here: the spawn seal reads them off the
    # AST (test_spawn_surface_seal) and matches them to spawn_authority.
    return audited_run(
        argv,
        fingerprint=("ruff_edit_gate.py", "_spawn_ruff", "subprocess.run"),
        reason="ruff-edit-gate",
        run=lambda *a, **kw: subprocess.run(*a, **kw),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        timeout=timeout,
        creationflags=_WIN_NO_WINDOW,
    )


class _RuffRunError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _parse_findings(stdout: str) -> list[RuffFinding]:
    data = json.loads(stdout)
    if not isinstance(data, list):
        raise ValueError("ruff JSON output is not a list")
    out: list[RuffFinding] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("ruff JSON finding is not an object")
        loc = item.get("location") or {}
        out.append(
            RuffFinding(
                code=str(item.get("code") or item.get("name") or "invalid-syntax"),
                message=str(item.get("message") or ""),
                line=int(loc.get("row") or 0),
                col=int(loc.get("column") or 0),
            )
        )
    return out


def _run(exe: Path, rel_path: str, root: Path, source: str) -> list[RuffFinding]:
    argv = [
        str(exe),
        "check",
        "--stdin-filename",
        rel_path,
        "--force-exclude",
        "--no-fix",
        "--no-cache",
        "--output-format",
        "json",
        "-",
    ]
    try:
        proc = _spawn_ruff(argv, stdin_text=source, cwd=root, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise _RuffRunError(REASON_VALIDATOR_UNAVAILABLE, f"ruff not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise _RuffRunError(
            REASON_VALIDATOR_ERROR, f"ruff timed out after {TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise _RuffRunError(REASON_VALIDATOR_ERROR, f"ruff spawn failed: {exc}") from exc

    rc = proc.returncode
    stdout = proc.stdout or ""
    if rc not in (0, 1):
        stderr = (proc.stderr or "").strip().splitlines()
        tail = stderr[0][:200] if stderr else ""
        raise _RuffRunError(REASON_VALIDATOR_ERROR, f"ruff exited rc={rc} {tail}".strip())
    if rc == 0 and not stdout.strip():
        return []
    try:
        findings = _parse_findings(stdout)
    except (ValueError, TypeError) as exc:
        raise _RuffRunError(
            REASON_VALIDATOR_ERROR, f"ruff rc={rc} produced unparseable output: {exc}"
        ) from exc
    if rc == 1 and not findings:
        raise _RuffRunError(REASON_VALIDATOR_ERROR, "ruff rc=1 with no findings")
    return findings


def _new_findings(
    before: list[RuffFinding], after: list[RuffFinding]
) -> tuple[RuffFinding, ...]:
    """Multiset difference on (code, message); AFTER positions for reporting.

    When a key grew by N, the N reported occurrences prefer AFTER positions that
    no BEFORE occurrence of the same key held -- the likeliest new ones.
    """
    before_counts = Counter((f.code, f.message) for f in before)
    after_counts = Counter((f.code, f.message) for f in after)
    before_positions = {(f.code, f.message, f.line, f.col) for f in before}
    new: list[RuffFinding] = []
    for key, count in after_counts.items():
        grew = count - before_counts.get(key, 0)
        if grew <= 0:
            continue
        occurrences = [f for f in after if (f.code, f.message) == key]
        occurrences.sort(key=lambda f: ((f.code, f.message, f.line, f.col) in before_positions))
        new.extend(occurrences[:grew])
    new.sort(key=lambda f: (f.line, f.col, f.code))
    return tuple(new)


def check_edit(
    *,
    project_root: Path | str | None,
    target: Path | str,
    before: str,
    after: str,
) -> RuffGateResult:
    """Would replacing ``before`` with ``after`` at ``target`` add Ruff findings?

    ``before`` must be the SAME snapshot ``after`` was built from ("" for a
    create); the caller owns that, this function never re-reads the disk.

    Never raises: an unexpected internal failure on a ``.py`` target is itself
    a ``validator_error`` (fail closed), so no caller can swallow it into a
    generic "validator hiccup, continue" path.
    """
    try:
        return _check_edit(project_root=project_root, target=target, before=before, after=after)
    except Exception as exc:  # noqa: BLE001 -- converted to a fail-closed verdict
        if Path(str(target)).suffix.lower() != ".py":
            return _not_configured("not_python")
        return _error(REASON_VALIDATOR_ERROR, f"ruff gate internal error: {exc!r}", None)


def _check_edit(
    *,
    project_root: Path | str | None,
    target: Path | str,
    before: str,
    after: str,
) -> RuffGateResult:
    target_path = Path(target)
    if target_path.suffix.lower() != ".py":
        return _not_configured("not_python")
    if project_root is None:
        return _not_configured("no_project_root")
    root = Path(project_root).resolve()
    if not target_path.is_absolute():
        target_path = root / target_path
    target_path = target_path.resolve()
    try:
        rel_path = target_path.relative_to(root).as_posix()
    except ValueError:
        return _not_configured("outside_project")

    config = find_ruff_config(target_path, root)
    if config is None:
        return _not_configured("no_ruff_config")

    exe = ruff_executable()
    if exe is None:
        return _error(
            REASON_VALIDATOR_UNAVAILABLE,
            "no ruff executable in the active runtime environment "
            f"({sysconfig.get_path('scripts')}); ruff is a core runtime dependency",
            config,
        )

    try:
        before_findings = _run(exe, rel_path, root, before) if before else []
        after_findings = _run(exe, rel_path, root, after)
    except _RuffRunError as exc:
        return _error(exc.reason, exc.detail, config)

    new = _new_findings(before_findings, after_findings)
    if new:
        return RuffGateResult(
            status=STATUS_NEW_FINDINGS,
            new_findings=new,
            reason=REASON_NEW_FINDINGS,
            config_path=str(config),
        )
    return RuffGateResult(status=STATUS_CLEAN, config_path=str(config))
