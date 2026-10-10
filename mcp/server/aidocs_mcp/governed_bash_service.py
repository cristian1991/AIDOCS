"""Governed Bash — one operator-grade switch over the native-shell pilot.

The native-Bash pilot is governed by several low-level, security-sensitive
flags (shell_enforcement_live, native_shell_provider_enabled,
native_shell_readonly_enabled) plus identity config (trusted roots, the
provider path, an optional SHA-256 pin, an optional OS-signature
requirement). Flipping them one-by-one is error-prone and — worse — a UI
can show "enabled" while the real security posture is incomplete (e.g.
flags on but no trusted root, so nothing actually runs native; or a
trusted root set to a directory that does not contain a real bash).

This service is the single source of truth for that posture:

  * ``posture(project_root)`` RE-DERIVES the complete verified state from
    the store + filesystem every call — never a cached "enabled" bit. It
    runs every check (flags, trusted-root, provider identity, optional
    hash pin, optional OS signature, a bounded execution probe) and
    reports ``verified`` only when ALL required checks pass and no
    configured optional check fails. The dashboard shows ENABLED iff
    ``verified`` is true.

  * ``enable(...)`` validates the candidate provider FIRST (identity +
    probe + optional pins), then writes every setting atomically and
    READBACK-VERIFIES each one; if any readback fails it rolls the
    enforcement flags back off so the system never sits half-enabled.
    Returns the freshly re-derived posture.

  * ``disable(...)`` clears the enforcement flags (identity config is
    left in place, harmless without the flags).

Authority: this module is pure mechanism. It must only be reached behind
the operator-auth wall (the dashboard CLI gate). It never consults nor
relaxes the agent-editable boundary — agents cannot reach it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import shell_provider_identity as _spi

# The low-level enforcement flags Governed Bash owns (all must be True).
ENFORCEMENT_FLAGS = (
    "tools.shell_enforcement_live",
    "tools.native_shell_provider_enabled",
    "tools.native_shell_readonly_enabled",
)
KEY_PROVIDER_PATH = "tools.native_shell_provider_path"
KEY_TRUSTED_ROOTS = "tools.native_shell_trusted_roots"
KEY_SHA256 = "tools.native_shell_provider_sha256"
KEY_REQUIRE_SIG = "tools.native_shell_require_os_signature"

_PROBE_TOKEN = "aidocs-probe-ok"
_PROBE_TIMEOUT_S = 5

ProbeRunner = Callable[[str], bool]


# ── primitives ──────────────────────────────────────────────────────


def _get(setting_path: str, project_root: Path, default):
    try:
        from .config import get_setting

        return get_setting(setting_path, project_root=project_root, default=default)
    except Exception:
        return default


def _autodetect_provider() -> str:
    found = shutil.which("bash")
    return found or ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_os_signature(path: Path) -> bool:
    """Best-effort OS code-signature check. Windows: Authenticode 'Valid'.
    Any non-Windows platform / failure → False (caller fails closed when
    a signature is REQUIRED).

    2026-06 fix: invoke the CANONICAL system PowerShell (absolute System32
    path) — NOT a PATH-resolved ``"powershell"``. The MCP server process is
    spawned with a restricted env whose PATH need not contain powershell, so
    the old PATH-resolved call failed (FileNotFoundError → False) and wrongly
    reported a validly-signed provider as unsigned (the dashboard ✗). The
    provider path rides in an env var (literal-safe; never interpolated into
    the PS source, which also broke on paths containing quotes), and the
    timeout matches the attest probe (PowerShell cold-start can exceed 5s).
    """
    if sys.platform != "win32":
        return False
    try:
        from .governed_shell_attest import _canonical_powershell

        pwsh = _canonical_powershell()
        if not pwsh:
            return False
        import os as _os

        env = dict(_os.environ)
        env["AIDOCS_SIG_PATH"] = str(path)
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        out = audited_run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-AuthenticodeSignature -LiteralPath $env:AIDOCS_SIG_PATH).Status",
            ],
            fingerprint=("governed_bash_service.py", "_verify_os_signature", "subprocess.run"),
            reason="authenticode-os-signature",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return out.returncode == 0 and out.stdout.strip().lower() == "valid"
    except Exception:
        return False


def _default_probe(provider_path: str) -> bool:
    """Bounded execution probe: run the provider on a trivial, side-effect
    -free command and confirm it produced the expected token. Proves the
    pinned path is actually an executable shell, not just a file.
    """
    if not provider_path:
        return False
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        out = audited_run(
            [provider_path, "-c", f"printf {_PROBE_TOKEN}"],
            fingerprint=("governed_bash_service.py", "_default_probe", "subprocess.run"),
            reason="governed-bash-probe",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            **kwargs,
        )
        return out.returncode == 0 and _PROBE_TOKEN in (out.stdout or "")
    except Exception:
        return False


# ── posture (read-only; re-derived every call) ──────────────────────


def posture(
    project_root: Path,
    *,
    provider_path: str | None = None,
    probe_runner: ProbeRunner | None = None,
) -> dict:
    """Re-derive the complete Governed Bash security posture. Returns a
    structured dict; ``verified`` is True ONLY when every required check
    passes and no configured optional check fails.
    """
    runner = probe_runner or _default_probe
    flags = {
        name.split(".")[-1]: bool(_get(name, project_root, False)) for name in ENFORCEMENT_FLAGS
    }
    flags_ok = all(flags.values())

    path = (
        provider_path
        or str(_get(KEY_PROVIDER_PATH, project_root, "") or "").strip()
        or _autodetect_provider()
    )
    roots = _spi._trusted_roots(project_root)

    checks: dict[str, object] = {}
    checks["trusted_roots_configured"] = bool(roots)
    checks["provider_path_resolved"] = bool(path)

    if path:
        ident_ok, ident_reason = _spi._verify_executable_path(
            path,
            project_root,
            "bash",
        )
    else:
        ident_ok, ident_reason = False, "no provider path resolved"
    checks["provider_identity"] = ident_ok
    checks["provider_identity_reason"] = ident_reason

    pin = str(_get(KEY_SHA256, project_root, "") or "").strip().lower()
    if pin:
        try:
            checks["hash_pin"] = bool(path and Path(path).is_file() and _sha256(Path(path)) == pin)
        except Exception:
            checks["hash_pin"] = False
    else:
        checks["hash_pin"] = None  # skipped (not configured)

    require_sig = bool(_get(KEY_REQUIRE_SIG, project_root, False))
    if require_sig:
        checks["os_signature"] = bool(path and _verify_os_signature(Path(path)))
    else:
        checks["os_signature"] = None  # skipped (not required)

    checks["bounded_probe"] = bool(runner(path)) if path else False

    required = [
        flags_ok,
        checks["trusted_roots_configured"],
        checks["provider_path_resolved"],
        checks["provider_identity"],
        checks["bounded_probe"],
    ]
    optional_ok = checks["hash_pin"] in (None, True) and checks["os_signature"] in (None, True)
    verified = all(required) and optional_ok

    return {
        "verified": verified,
        "flags": flags,
        "checks": checks,
        "provider_path": path,
        "trusted_roots": [str(r) for r in roots],
        "hash_pinned": bool(pin),
        "os_signature_required": require_sig,
    }


# ── one operator-facing card (status + capability + route + repair) ──

# The single host/provider pair Governed Bash currently supports. The
# capability matrix is the authority on whether that pair can be driven
# natively (command visibility + PreToolUse hard-deny + output replace).
_CARD_HOST = "claude_code"
_CARD_PROVIDER = "bash"


def _repair_reason(post: dict) -> str:
    """The first unmet REQUIRED check, phrased as an operator next step.
    Empty string when the posture is fully verified.
    """
    if post.get("verified"):
        return ""
    checks = post.get("checks", {})
    if not all(post.get("flags", {}).values()):
        return "enforcement flags are off — run `aidocs governed-bash-enable`"
    if not checks.get("trusted_roots_configured"):
        return (
            "no trusted install root configured — enable with "
            "`--provider-path <abs path to a real bash>`"
        )
    if not checks.get("provider_path_resolved"):
        return "no provider path resolved — pass `--provider-path`"
    if not checks.get("provider_identity"):
        return str(checks.get("provider_identity_reason") or "provider identity not verified")
    if not checks.get("bounded_probe"):
        return "the provider failed the bounded execution probe"
    if checks.get("hash_pin") is False:
        return "provider SHA-256 does not match the configured pin"
    if checks.get("os_signature") is False:
        return "provider lacks a valid OS code signature"
    return "posture not verified"


def posture_card(
    project_root: Path,
    *,
    provider_path: str | None = None,
    probe_runner: ProbeRunner | None = None,
) -> dict:
    """The single, complete operator card for Governed Bash. Wraps the
    re-derived posture with the three things the raw posture omits:
    host capability, the route a command would actually take, and a
    one-line repair reason. ``status`` is derived SOLELY from
    ``posture.verified`` — the UI must never claim ENABLED on any other
    basis (so a half-enabled flag flip can never read as on).
    """
    post = posture(
        project_root,
        provider_path=provider_path,
        probe_runner=probe_runner,
    )
    verified = bool(post.get("verified"))

    # Host capability — independent of the operator's flags: can this
    # host/provider pair be driven natively at all?
    try:
        from . import shell_capability_matrix as _matrix

        cap = _matrix.lookup(_CARD_HOST, _CARD_PROVIDER)
        host_capability = {
            "host": _CARD_HOST,
            "provider": _CARD_PROVIDER,
            "native_safe": bool(_matrix.is_native_safe(_CARD_HOST, _CARD_PROVIDER)),
            "output_replacement": bool(cap and cap.posttooluse_output_replacement),
        }
    except Exception:
        host_capability = {
            "host": _CARD_HOST,
            "provider": _CARD_PROVIDER,
            "native_safe": False,
            "output_replacement": False,
        }

    # Selected route — where a governed command actually goes right now.
    # Native ONLY when the full posture verifies AND the pair is
    # capability-eligible; otherwise ai_run (the canonical fallback).
    try:
        from .shell_enforcement import native_route

        route = native_route(
            _CARD_HOST,
            _CARD_PROVIDER,
            native_enabled=verified,
            project_root=project_root,
            executable_path=post.get("provider_path") or None,
        )
    except Exception:
        route = "ai_run"

    # The [bash] COMMAND policy is independent of the native-provider posture:
    # a verified provider with an empty allow-table is still an unusable shell.
    # Surface the effective table + the recommended governed-but-usable profile
    # so the operator sees (and can close) that gap.
    try:
        from .governed_bash_profile import inventory as _bash_inventory

        bash_command_policy = _bash_inventory(project_root)
    except Exception:
        bash_command_policy = {}

    return {
        **post,
        "status": "enabled" if verified else "disabled",
        "host_capability": host_capability,
        "selected_route": route,
        "repair_reason": _repair_reason(post),
        "bash_command_policy": bash_command_policy,
    }


# ── candidate validation (pre-flight, no writes) ────────────────────


def _validate_candidate(
    p: Path,
    *,
    project_root: Path,
    hash_pin: str | None,
    require_os_signature: bool,
    probe_runner: ProbeRunner,
) -> dict:
    checks: dict[str, object] = {}
    # Run the SAME provider-identity SHAPE law posture uses: absolute,
    # NOT under project_root (no repo-local fake), basename matches the
    # provider family. This is what keeps enable() from accepting a
    # project-local/user-controlled bash and then minting its own parent
    # dir as a "trusted root" — which would defeat the entire identity
    # model. The under-trusted-root half of the law cannot be checked
    # pre-write (the root is what we are about to write); it is enforced
    # by the post-write posture re-derivation (with rollback) in enable().
    shape_ok, shape_reason = _spi._path_shape_sanity(
        str(p),
        project_root,
        "bash",
    )
    checks["path_shape"] = shape_ok
    checks["path_shape_reason"] = shape_reason
    checks["provider_is_file"] = p.is_file()
    if hash_pin:
        try:
            checks["hash_pin"] = bool(p.is_file() and _sha256(p) == hash_pin.strip().lower())
        except Exception:
            checks["hash_pin"] = False
    else:
        checks["hash_pin"] = None
    if require_os_signature:
        checks["os_signature"] = _verify_os_signature(p)
    else:
        checks["os_signature"] = None
    checks["bounded_probe"] = bool(probe_runner(str(p)))
    ok = (
        bool(checks["path_shape"])
        and bool(checks["provider_is_file"])
        and bool(checks["bounded_probe"])
        and checks["hash_pin"] in (None, True)
        and checks["os_signature"] in (None, True)
    )
    return {"ok": ok, "checks": checks}


# ── atomic write + readback ─────────────────────────────────────────


def _apply_and_verify(
    project_root: Path,
    writes: dict,
    *,
    scope: str,
) -> list[str]:
    """Write each setting then read it back at the same scope. Returns the
    list of keys whose readback did NOT match (empty = all verified).
    """
    from .config_store import ConfigStore

    store = ConfigStore()
    failed: list[str] = []
    for key, value in writes.items():
        try:
            store.set(project_root, key, value, scope=scope, scope_key="")
            back = store.get(project_root, key, scope=scope, scope_key="")
            if back != value:
                failed.append(key)
        except Exception:
            failed.append(key)
    return failed


def _set_flags(project_root: Path, value: bool, *, scope: str) -> None:
    from .config_store import ConfigStore

    store = ConfigStore()
    for name in ENFORCEMENT_FLAGS:
        try:
            store.set(project_root, name, value, scope=scope, scope_key="")
        except Exception:
            pass


# ── enable / disable ────────────────────────────────────────────────


def enable(
    project_root: Path,
    *,
    operator_authenticated: bool,
    provider_path: str | None = None,
    hash_pin: str | None = None,
    require_os_signature: bool = False,
    scope: str = "global",
    probe_runner: ProbeRunner | None = None,
) -> dict:
    """Validate the candidate provider, then atomically enable Governed
    Bash and readback-verify. Fails closed; never leaves a half-enabled
    state (rolls the enforcement flags off if any write fails to verify).
    Returns {ok, posture, ...}.
    """
    if not operator_authenticated:
        return {"ok": False, "reason": "unauthenticated", "blocked_by": "operator_auth"}
    runner = probe_runner or _default_probe

    path = (provider_path or "").strip() or _autodetect_provider()
    if not path:
        return {
            "ok": False,
            "reason": "provider_not_found",
            "message": "No bash provider path given and none on PATH.",
        }
    p = Path(path)
    trusted_root = str(p.parent)

    pre = _validate_candidate(
        p,
        project_root=project_root,
        hash_pin=hash_pin,
        require_os_signature=require_os_signature,
        probe_runner=runner,
    )
    if not pre["ok"]:
        return {
            "ok": False,
            "reason": "precheck_failed",
            "checks": pre["checks"],
            "message": "Provider failed pre-flight validation "
            "(project-local / non-absolute / wrong basename / "
            "probe / pin); no settings were changed.",
        }

    writes = {
        KEY_PROVIDER_PATH: str(p),
        KEY_TRUSTED_ROOTS: trusted_root,
        KEY_SHA256: (hash_pin or "").strip().lower(),
        KEY_REQUIRE_SIG: bool(require_os_signature),
        # enforcement flags last — identity config is in place first
        ENFORCEMENT_FLAGS[0]: True,
        ENFORCEMENT_FLAGS[1]: True,
        ENFORCEMENT_FLAGS[2]: True,
    }
    failed = _apply_and_verify(project_root, writes, scope=scope)
    if failed:
        _set_flags(project_root, False, scope=scope)  # never half-on
        return {
            "ok": False,
            "reason": "write_readback_failed",
            "failed": failed,
            "message": f"Readback failed for: {', '.join(failed)}. Enforcement rolled back.",
        }

    # Final gate: re-derive the FULL posture (this runs the complete
    # identity law, incl. provider-under-trusted-root, against what was
    # actually persisted). If it is not verified — even though every
    # individual write read back correctly — roll the enforcement flags
    # back off so we never sit half-enabled, and return the failed
    # posture. This catches any case the pre-check could not (e.g. the
    # persisted trusted root not actually covering the provider).
    post = posture(project_root, provider_path=str(p), probe_runner=runner)
    if not post["verified"]:
        _set_flags(project_root, False, scope=scope)
        post = posture(project_root, provider_path=str(p), probe_runner=runner)
        return {
            "ok": False,
            "reason": "posture_unverified_rolled_back",
            "posture": post,
            "message": "Final posture did not verify; enforcement flags rolled back off.",
        }
    return {"ok": True, "posture": post}


def disable(
    project_root: Path,
    *,
    operator_authenticated: bool,
    scope: str = "global",
    probe_runner: ProbeRunner | None = None,
) -> dict:
    """Turn Governed Bash off — clear the enforcement flags + readback.
    Identity config (trusted root / provider path / pins) is left intact;
    it is inert without the enforcement flags.
    """
    if not operator_authenticated:
        return {"ok": False, "reason": "unauthenticated", "blocked_by": "operator_auth"}
    failed = _apply_and_verify(
        project_root,
        dict.fromkeys(ENFORCEMENT_FLAGS, False),
        scope=scope,
    )
    post = posture(project_root, probe_runner=probe_runner)
    return {"ok": (not failed) and (not post["verified"]), "failed": failed, "posture": post}
