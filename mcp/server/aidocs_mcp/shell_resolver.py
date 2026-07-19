"""ai_run shell provider resolver — Batch A (canonical 2026-04-29).

Resolves which executable ai_run dispatches commands through. Bash-
compatible only. cmd.exe permanently refused. PowerShell observable-
but-inert until Batch C (the flag is detected and audited; verdict is
"rejected" with rejection_reason="powershell provider not implemented").

This module is REPORT-ONLY in Batch A. It does not change spawn
behavior. Existing spawn sites wrap calls to ``resolve_shell()`` in a
try block (Lock 1) and emit ``shell_provider_resolved`` audit events
with no command payload (Lock 2). Spawn site behavior is untouched —
the existing ``shell=True`` Popen still runs.

Doctrine: see ``.MEMORY/system/security-gates.md`` invariant
"AIDOCS shell provider lock" + §6 override taxonomy + §7A absolutes
+ §8 audit emission.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Probe constants ─────────────────────────────────────────────────

# Sentinel echoed by the sanity probe. Catches imposter `bash.exe`
# files that pass `--version` but don't actually implement Bash.
_PROBE_SENTINEL: str = "aidocs-ok"

# Probe timeout in seconds. Generous enough for slow boxes / cold-
# disk hits, tight enough that a hung binary doesn't stall the gate.
_PROBE_TIMEOUT: float = 2.0

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ── Standard install paths ──────────────────────────────────────────

# Order matters: prefer the developer-tools subset (`bin/bash.exe`)
# over the busybox-flavored `usr/bin/bash.exe` so Git Bash users get
# the same behavior the operator gets when they double-click "Git
# Bash" from the Start Menu.
_WINDOWS_GIT_BASH_PATHS: tuple[str, ...] = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

_POSIX_BASH_PATHS: tuple[str, ...] = (
    "/bin/bash",
    "/usr/bin/bash",
)


# ── Forbidden basenames ─────────────────────────────────────────────

# cmd.exe NEVER. No flag lifts this — the doctrine is absolute.
_FORBIDDEN_BASENAMES_ALWAYS: frozenset[str] = frozenset(
    {
        "cmd.exe",
    },
)

# PowerShell binaries — only relevant when the superadmin flag is
# true. Even then, Batch A produces verdict="rejected" because no
# PowerShell provider implementation exists yet (Batch C).
_POWERSHELL_BASENAMES: frozenset[str] = frozenset(
    {
        "powershell.exe",
        "pwsh.exe",
    },
)


# ── Result type ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedShell:
    """Resolution outcome.

    backend
        One of: ``"git_bash"``, ``"native_bash"``, ``"configured_bash"``,
        ``"powershell_superadmin"``, ``"unavailable"``.

    path
        Absolute path to the executable. Empty string when unavailable.

    argv_template
        List of strings forming the argv for ``Popen(shell=False)``.
        Use ``[*argv_template[:-1], command]`` to build the final argv
        — the trailing ``"{command}"`` placeholder is a marker for
        readability; callers replace the last element with the literal
        command string.

        Shape is ``[bash_path, "-c", "{command}"]``. We use ``-c`` and
        NOT ``-lc`` on purpose: AIDOCS should not load login-shell
        startup files (no ``.bash_profile`` / ``.bashrc`` chain, no
        user profile PATH mutation, no aliases/functions leaking into
        controlled execution). If PATH/env needs fixing for ai_run,
        that's an explicit shape passed via ``env=`` — not implicit
        login-shell sourcing.

    version
        First-line output from ``<path> --version``. Empty when the
        probe failed or the resolver chose ``"unavailable"``.

    resolution_source
        Trace of which resolver step picked this result. One of:
        ``"dev_session_override"``, ``"standard_git_bash_path"``,
        ``"standard_posix_path"``, ``"path_lookup_verified"``,
        ``"powershell_superadmin"``, ``"unavailable"``.

    platform
        ``sys.platform`` value at resolution time.

    verdict
        ``"usable"`` — caller may dispatch.
        ``"unavailable"`` — no provider found.
        ``"rejected"`` — provider detected but refused (e.g. cmd.exe,
            PowerShell pre-Batch-C, probe failure on every candidate).

    rejection_reason
        Human-readable text. Empty when verdict is ``"usable"``.

    audit_payload
        Dict ready to drop into ``execution_events.payload`` for the
        ``shell_provider_resolved`` event_kind. Lock 2: NEVER carries
        the raw command, argv, or any prompt content. The caller adds
        session_id / project_root / managed_mode at emission time.
    """

    backend: str
    path: str
    argv_template: list[str]
    version: str
    resolution_source: str
    platform: str
    verdict: str
    rejection_reason: str
    audit_payload: dict = field(default_factory=dict)


# ── Helpers ─────────────────────────────────────────────────────────


def _basename_lower(path: str) -> str:
    try:
        return Path(path).name.lower()
    except (OSError, ValueError):
        return ""


def _is_under(candidate: Path, root: Path) -> bool:
    """Return True if ``candidate`` (resolved) is under ``root``
    (resolved). Returns False on resolution failure (safer to allow
    a path we can't resolve than refuse the resolver entirely).
    """
    try:
        c = candidate.resolve()
        r = root.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    try:
        return c == r or r in c.parents
    except (OSError, ValueError):
        return False


def _safe_run(
    argv: list[str],
    *,
    timeout: float,
) -> tuple[int | None, str, str, str]:
    """Defensive subprocess wrapper. Returns
    ``(returncode_or_None, stdout, stderr, error_reason)``.

    Hardening over ``subprocess.run``:
      - ``stdin=DEVNULL`` so the child cannot block on a stdin
        pipe inherited from the MCP server's stdio transport.
      - explicit Popen + communicate(timeout=...) so the timeout
        is enforced by communicate's reader thread, not by
        ``run``'s wrapper which has been observed to wedge on
        Windows with capture_output=True.
      - on TimeoutExpired: kill, then best-effort communicate to
        drain pipes, then return error_reason="probe timed out".
      - all OSError / ValueError / generic Exception paths return
        a failure tuple — never raises.

    Used by ``_probe`` so a single hung candidate cannot block
    resolver beyond ``timeout`` seconds.
    """
    try:
        # #345: routed through audited_popen (ledger row per spawn).
        # Passthrough lambda IS the registered AST callsite; kwargs UNCHANGED.
        from .shell_egress_service import audited_popen, terminate_process_tree

        popen_platform_kwargs = (
            {
                "creationflags": _WIN_NO_WINDOW
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            }
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        proc = audited_popen(
            argv,
            fingerprint=("shell_resolver.py", "_safe_run", "subprocess.Popen"),
            reason="shell-resolver-probe",
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_platform_kwargs,
        )
    except (OSError, ValueError) as exc:
        return (None, "", "", f"spawn failed: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return (None, "", "", f"spawn failed: {exc!r}")

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return (proc.returncode, stdout or "", stderr or "", "")
    except subprocess.TimeoutExpired:
        try:
            terminate_process_tree(proc)
        except Exception:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
        except Exception:
            stdout, stderr = "", ""
        return (
            None,
            stdout or "",
            stderr or "",
            f"probe timed out after {timeout}s",
        )
    except Exception as exc:  # pragma: no cover - defensive
        try:
            terminate_process_tree(proc)
        except Exception:
            pass
        return (None, "", "", f"communicate failed: {exc!r}")


def _probe(path: str) -> tuple[bool, str, str]:
    """Probe a candidate executable. Returns
    ``(ok, version_first_line, rejection_reason)``.

    Three-step probe:
      1. existence + executable bit
      2. ``--version`` exits 0
      3. ``-c 'printf aidocs-ok'`` stdout contains the sentinel

    Bounded by ``_PROBE_TIMEOUT`` per step. A hung candidate
    cannot stall the resolver — if either step times out, returns
    a failure tuple and the resolver moves to the next candidate.
    """
    if not path:
        return (False, "", "empty path")
    p = Path(path)
    if not p.is_file():
        return (False, "", f"not a file: {path}")
    if not os.access(path, os.X_OK):
        return (False, "", f"not executable: {path}")

    # Step 2: --version
    rc, stdout, stderr, err = _safe_run(
        [path, "--version"],
        timeout=_PROBE_TIMEOUT,
    )
    if err:
        return (False, "", f"--version {err}")
    if rc != 0:
        return (False, "", f"--version exit {rc}")
    version_line = (stdout or stderr or "").splitlines()
    version = version_line[0].strip() if version_line else ""

    # Step 3: sanity sentinel. Uses ``-c`` (not ``-lc``) so the
    # probe doesn't source login-shell startup files — same reason
    # callers use ``-c`` for actual dispatch.
    rc, stdout, _stderr, err = _safe_run(
        [path, "-c", f"printf {_PROBE_SENTINEL}"],
        timeout=_PROBE_TIMEOUT,
    )
    if err:
        return (False, version, f"sentinel {err}")
    if rc != 0:
        return (False, version, f"sentinel exit {rc}")
    if _PROBE_SENTINEL not in (stdout or ""):
        return (
            False,
            version,
            f"sentinel mismatch (got {stdout!r})",
        )

    return (True, version, "")


def _build_audit_payload(
    *,
    backend: str,
    path: str,
    version: str,
    resolution_source: str,
    platform: str,
    verdict: str,
    rejection_reason: str,
) -> dict:
    """Assemble the audit_payload dict. Lock 2: never includes
    command / argv / prompt content.
    """
    return {
        "backend": backend,
        "path": path,
        "version": version,
        "resolution_source": resolution_source,
        "platform": platform,
        "verdict": verdict,
        "rejection_reason": rejection_reason,
    }


def _make_unavailable(
    *,
    platform: str,
    rejection_reason: str,
    resolution_source: str = "unavailable",
) -> ResolvedShell:
    return ResolvedShell(
        backend="unavailable",
        path="",
        argv_template=[],
        version="",
        resolution_source=resolution_source,
        platform=platform,
        verdict="unavailable",
        rejection_reason=rejection_reason,
        audit_payload=_build_audit_payload(
            backend="unavailable",
            path="",
            version="",
            resolution_source=resolution_source,
            platform=platform,
            verdict="unavailable",
            rejection_reason=rejection_reason,
        ),
    )


def _make_usable_bash(
    *,
    backend: str,
    path: str,
    version: str,
    resolution_source: str,
    platform: str,
) -> ResolvedShell:
    return ResolvedShell(
        backend=backend,
        path=path,
        argv_template=[path, "-c", "{command}"],
        version=version,
        resolution_source=resolution_source,
        platform=platform,
        verdict="usable",
        rejection_reason="",
        audit_payload=_build_audit_payload(
            backend=backend,
            path=path,
            version=version,
            resolution_source=resolution_source,
            platform=platform,
            verdict="usable",
            rejection_reason="",
        ),
    )


def _make_dev_override_rejected(
    *,
    path: str,
    platform: str,
    reason: str,
) -> ResolvedShell:
    """Dev session override failed validation or probe. Loud refusal,
    NOT fall-through to standard auto-detect: the override is an
    explicit session pin, so a broken pin must surface in audit
    rather than silently use a different provider.
    """
    return ResolvedShell(
        backend="unavailable",
        path=path,
        argv_template=[],
        version="",
        resolution_source="dev_session_override",
        platform=platform,
        verdict="rejected",
        rejection_reason=reason,
        audit_payload=_build_audit_payload(
            backend="unavailable",
            path=path,
            version="",
            resolution_source="dev_session_override",
            platform=platform,
            verdict="rejected",
            rejection_reason=reason,
        ),
    )


def _make_rejected_powershell(
    *,
    path: str,
    platform: str,
    reason: str,
) -> ResolvedShell:
    """PowerShell observable-but-inert. backend records the
    superadmin tag for audit; verdict is rejected; argv is empty so
    a Batch B caller that mishandles the result still can't dispatch.
    """
    return ResolvedShell(
        backend="powershell_superadmin",
        path=path,
        argv_template=[],
        version="",
        resolution_source="powershell_superadmin",
        platform=platform,
        verdict="rejected",
        rejection_reason=reason,
        audit_payload=_build_audit_payload(
            backend="powershell_superadmin",
            path=path,
            version="",
            resolution_source="powershell_superadmin",
            platform=platform,
            verdict="rejected",
            rejection_reason=reason,
        ),
    )


# ── Path validation (universal rejection rules) ─────────────────────


def _validate_path(
    candidate: str,
    project_root: Path,
    *,
    powershell_flag_active: bool,
) -> tuple[bool, str]:
    """Apply universal rejection rules. Returns ``(ok, reason)``.

    Rejections (caller continues to next resolver step on failure):
      - empty string
      - relative path
      - canonical path under project_root (closes repo-local fake-bash)
      - basename is cmd.exe — ALWAYS rejected, no flag override
      - basename is pwsh.exe / powershell.exe — rejected unless the
        superadmin flag is active (caller still records the audit
        with verdict="rejected" because Batch C isn't implemented)
    """
    if not candidate:
        return (False, "empty path")
    p = Path(candidate)
    if not p.is_absolute():
        return (False, f"not absolute: {candidate}")
    if _is_under(p, project_root):
        return (False, f"under project_root: {candidate}")

    base = _basename_lower(candidate)
    if base in _FORBIDDEN_BASENAMES_ALWAYS:
        return (False, "cmd.exe is permanently refused as ai_run provider")
    if base in _POWERSHELL_BASENAMES and not powershell_flag_active:
        return (False, "powershell requires superadmin flag")

    return (True, "")


# ── Flavor + override readers ───────────────────────────────────────


def _read_flavor(project_root: Path) -> str:
    """Read distribution.flavor. Defaults to ``"solo"`` on any
    failure — safer to assume non-dev than to grant the dev override
    on a config-read exception.
    """
    try:
        from .config import get_setting

        raw = get_setting(
            "distribution.flavor",
            project_root=project_root,
            default="solo",
        )
        return str(raw or "solo").strip().lower()
    except Exception:
        return "solo"


def _read_powershell_flag(project_root: Path) -> bool:
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "security.superadmin_allow_powershell_ai_run_backend",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def _read_dev_session_override(
    project_root: Path,
    session_id: str | None,
) -> str:
    """#404: the dev-flavor session shell override is retired. Always
    returns "" — resolution proceeds straight to the standard paths.
    Kept as a seam so the resolver order (R2) and its trace stay stable.
    """
    del project_root, session_id
    return ""


# ── Resolver ────────────────────────────────────────────────────────


def resolve_shell(
    project_root: Path,
    session_id: str | None = None,
) -> ResolvedShell:
    """Resolve which Bash-compatible executable ai_run should dispatch
    through.

    Order (Windows):
      1. Dev-flavor session override (`dev_ai_run_bash_path` in
         SessionQueryGateStore, only honored when
         distribution.flavor=="dev").
      2. Standard Git Bash paths (Program Files install).
      3. ``shutil.which("bash")`` with under-project-root rejection.
      4. PowerShell — recorded for audit when the superadmin flag is
         true, but verdict="rejected" until Batch C ships the
         provider implementation.
      5. Refuse.

    Order (Linux/macOS):
      1. Dev-flavor session override.
      2. Standard POSIX paths (/bin/bash, /usr/bin/bash).
      3. ``shutil.which("bash")``.
      4. Refuse. /bin/sh and dash are NOT acceptable defaults.

    Returns a frozen ``ResolvedShell``. Callers wrap this call in a
    try block (Lock 1) — resolver exceptions must never break the
    spawn path. Callers emit ``shell_provider_resolved`` events with
    the returned ``audit_payload`` plus session/project context (Lock
    2: no command payload).
    """
    # ai_run boundary trace (dev.runtime.ai_run_trace, dev flavor only).
    from ._dev_trace import trace as _trace_r

    def _bc_r(marker: str) -> None:
        _trace_r(project_root, marker)

    _bc_r("R0 enter resolve_shell")
    platform = sys.platform
    is_windows = platform == "win32"
    _bc_r(f"R0a platform={platform} is_windows={is_windows}")
    powershell_flag = _read_powershell_flag(project_root)
    _bc_r(f"R1 after _read_powershell_flag flag={powershell_flag}")

    # ── Step 1: dev-flavor session override ─────────────────────────
    # Contract: dev.runtime.ai_run_bash_path is an EXPLICIT session
    # pin, not a soft preference. If the operator set it, AIDOCS must
    # either use exactly that validated provider OR refuse loudly —
    # silent fall-through to auto-detect would make the knob
    # ambiguous ("did it use my override or ignore it?") which is
    # bad for dev debugging and audit clarity.
    #
    # Rule:
    #   empty       → continue normal auto-detect
    #   non-empty + valid + probe passes → use it, resolution_source
    #                                       ="dev_session_override"
    #   non-empty + invalid OR probe fails → verdict="rejected",
    #     rejection_reason carries the specific failure. Do NOT
    #     continue to standard Git Bash / native bash detection.
    override = _read_dev_session_override(project_root, session_id)
    _bc_r(f"R2 after _read_dev_session_override has_override={bool(override)}")
    if override:
        ok_validate, reason_validate = _validate_path(
            override,
            project_root,
            powershell_flag_active=powershell_flag,
        )
        if not ok_validate:
            return _make_dev_override_rejected(
                path=override,
                platform=platform,
                reason=(f"dev session override failed validation: {reason_validate}"),
            )
        ok_probe, version, reason_probe = _probe(override)
        if not ok_probe:
            return _make_dev_override_rejected(
                path=override,
                platform=platform,
                reason=(f"dev session override failed probe: {reason_probe}"),
            )
        return _make_usable_bash(
            backend="configured_bash",
            path=override,
            version=version,
            resolution_source="dev_session_override",
            platform=platform,
        )

    # ── Step 2 (Windows) / Step 2 (POSIX): standard install paths ───
    standard_paths = _WINDOWS_GIT_BASH_PATHS if is_windows else _POSIX_BASH_PATHS
    import time as _bc_time_mod

    for candidate in standard_paths:
        from pathlib import Path as _BC_P_R

        _bc_r(f"R3a candidate={candidate} exists={_BC_P_R(candidate).is_file()}")
        ok_validate, vreason = _validate_path(
            candidate,
            project_root,
            powershell_flag_active=powershell_flag,
        )
        _bc_r(f"R3b validate={ok_validate} reason={vreason!r}")
        if not ok_validate:
            # Should never happen for hardcoded standard paths, but
            # belt-and-braces — if a malicious actor symlinks a
            # standard path under project_root, the validator catches.
            continue
        _bc_r("R3c before _probe")
        _bc_t0 = _bc_time_mod.time()
        ok_probe, version, preason = _probe(candidate)
        _bc_dt_ms = int((_bc_time_mod.time() - _bc_t0) * 1000)
        _bc_r(f"R3d after _probe ok={ok_probe} ms={_bc_dt_ms} reason={preason!r}")
        if ok_probe:
            backend = "git_bash" if is_windows else "native_bash"
            source = "standard_git_bash_path" if is_windows else "standard_posix_path"
            return _make_usable_bash(
                backend=backend,
                path=candidate,
                version=version,
                resolution_source=source,
                platform=platform,
            )

    # ── Step 3: PATH lookup with rejection rules ────────────────────
    which_path = shutil.which("bash")
    _bc_r(f"R4 after shutil.which(bash) result={which_path!r}")
    if which_path:
        ok_validate, vreason = _validate_path(
            which_path,
            project_root,
            powershell_flag_active=powershell_flag,
        )
        _bc_r(f"R5a which validate={ok_validate} reason={vreason!r}")
        if ok_validate:
            _bc_r("R5b before _probe(which_path)")
            _bc_t0 = _bc_time_mod.time()
            ok_probe, version, preason = _probe(which_path)
            _bc_dt_ms = int((_bc_time_mod.time() - _bc_t0) * 1000)
            _bc_r(f"R5c after _probe ok={ok_probe} ms={_bc_dt_ms} reason={preason!r}")
            if ok_probe:
                backend = "git_bash" if is_windows else "native_bash"
                return _make_usable_bash(
                    backend=backend,
                    path=which_path,
                    version=version,
                    resolution_source="path_lookup_verified",
                    platform=platform,
                )

    # ── Step 4 (Windows only): PowerShell observable-but-inert ──────
    if is_windows and powershell_flag:
        ps_path = shutil.which("pwsh") or shutil.which("powershell")
        if ps_path:
            return _make_rejected_powershell(
                path=ps_path,
                platform=platform,
                reason="powershell provider not implemented (Batch C)",
            )

    # ── Step 5: refuse ──────────────────────────────────────────────
    if is_windows:
        msg = (
            "no Bash-compatible provider available. Install Git for "
            "Windows (https://git-scm.com/download/win) — Git Bash is "
            "the supported provider. cmd.exe is not a valid "
            "ai_run provider."
        )
    else:
        msg = (
            "no Bash-compatible provider available. Install bash "
            "(/bin/bash or /usr/bin/bash) — /bin/sh and dash are not "
            "acceptable defaults."
        )
    _bc_r("R6 before unavailable return")
    return _make_unavailable(
        platform=platform,
        rejection_reason=msg,
        resolution_source="unavailable",
    )


# ── Report-only helper for spawn-site insertion ─────────────────────


def report_shell_resolution(
    project_root: Path,
    session_id: str | None,
    *,
    source_kind: str,
    capability_name: str = "ai_run",
) -> None:
    """Resolve and emit a `shell_provider_resolved` audit event from a
    spawn-site caller. Batch A report-only entrypoint.

    The shape of this helper is the load-bearing safety contract:

    - Lock 1: every internal failure path is caught and absorbed. The
      caller's spawn behavior is never affected by a resolver bug or
      audit-store hiccup. If the resolver raises, an audit row with
      `verdict="error"` is recorded (best-effort) and we return
      cleanly. If the audit store itself raises, we return cleanly.
      This function does not raise to its caller, period.

    - Lock 2: the audit payload contains only resolver-derived fields
      plus session/project/managed_mode context. Never carries the
      raw command, argv, or any prompt content. Callers MUST NOT pass
      command bytes here.

    Caller pattern:

        from .shell_resolver import report_shell_resolution
        report_shell_resolution(
            project_root,
            session_id,
            source_kind="<module>.<function>",
        )
        # ... existing shell=True spawn unchanged ...

    The status field on the recorded event:
      - "allowed" when verdict="usable" (provider available)
      - "observed" when verdict="rejected" or "unavailable"
      - "error" when the resolver itself raised
    """
    try:
        resolved = resolve_shell(project_root, session_id=session_id)
    except Exception:
        # Resolver internal failure. Lock 1: absorb and emit the
        # error sentinel.
        _emit_resolution_event(
            project_root=project_root,
            session_id=session_id,
            source_kind=source_kind,
            capability_name=capability_name,
            status="error",
            payload={
                "verdict": "error",
                "rejection_reason": "resolver internal error",
            },
        )
        return

    status = "allowed" if resolved.verdict == "usable" else "observed"
    _emit_resolution_event(
        project_root=project_root,
        session_id=session_id,
        source_kind=source_kind,
        capability_name=capability_name,
        status=status,
        payload=dict(resolved.audit_payload),
    )


def _emit_resolution_event(
    *,
    project_root: Path,
    session_id: str | None,
    source_kind: str,
    capability_name: str,
    status: str,
    payload: dict,
) -> None:
    """Emit one `shell_provider_resolved` row. Lock 1: never raises.
    Lock 2: caller-side payload is augmented with session/project/
    managed_mode context only — no command content.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        full_payload = {
            **payload,
            "session_id": session_id or "",
            "project_root": str(project_root),
            "managed_mode": bool(session_id),
        }
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="shell_provider_resolved",
            source_kind=source_kind,
            session_id=session_id or None,
            capability_name=capability_name,
            action_kind="resolve_shell",
            target_entity="",
            status=status,
            payload=full_payload,
        )
    except Exception:
        pass
