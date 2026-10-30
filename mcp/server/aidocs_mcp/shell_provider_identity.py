"""Native-shell provider IDENTITY verification (Batch 2.0-B precondition).

Tool name is costume, not identity. A malicious or compromised host could
expose a tool named "Bash"/"Cmd"/"PowerShell" while running a different
executable or wrapper. Native EXECUTION must therefore prove provider
identity, not trust the tool name + capability seams.

Native-ELIGIBLE identity (per shell_capability_matrix.provider_identity):
  A. IDENTITY_HOST_VERIFIED_PATH — the host exposes the provider's
     executable path AND it passes REAL identity proof:
       * absolute, not under project_root (no repo-local fake)
       * exists and is a file
       * sits under a configured TRUSTED install root
         (tools.native_shell_trusted_roots) — NOT user/project-controlled
       * basename matches the provider family
     (provider probe / hash / signature is a future optional layer.)
  B. IDENTITY_STATIC_TRUSTED — a static/signed capability contract declares
     the provider identity host-owned and non-spoofable. Accepted because
     the host is the enforcement substrate; if it lies about its own Bash
     tool, the PreToolUse gate AIDOCS runs inside is already compromised.

NOT eligible:
  * IDENTITY_HOST_REPORTED_PATH_SANITY — passes only SHAPE sanity (absolute,
    not under project_root, basename match). Shape is NOT identity: a
    renamed bash.exe outside the project passes shape and proves nothing.
    Diagnostic tier only — never native-eligible.
  * IDENTITY_NONE — unproven → ai_run.

This module makes NO assertion that any current host is trusted — every
seeded matrix entry is IDENTITY_NONE, so native execution stays disabled
until an operator/owner establishes a contract AND configures trusted
roots. ai_run remains the canonical, path-validated provider regardless.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import shell_capability_matrix as _matrix

# Acceptable EXECUTION-IDENTITY basenames per provider (lower-cased).
#
# Detection is intentionally BROADER than execution identity. shell_envelope
# maps sh/zsh/wsl → provider=bash so they are intercepted as shell-like
# surfaces (gated/denied/fallbacked), but only REAL bash/bash.exe may
# satisfy bash NATIVE-EXECUTION identity: the judge / bash_policy vet
# commands under bash parsing semantics, which sh/zsh do not share, and wsl
# is a launcher, not a shell. If zsh/sh native execution is ever wanted, add
# separate provider semantics + law rather than widening this set.
_PROVIDER_BASENAMES: dict[str, frozenset[str]] = {
    "bash": frozenset({"bash", "bash.exe"}),
    "powershell": frozenset(
        {
            "powershell",
            "pwsh",
            "powershell.exe",
            "pwsh.exe",
        },
    ),
    "cmd": frozenset({"cmd", "cmd.exe"}),
}


def _is_under(candidate: Path, root: Path) -> bool:
    try:
        c = candidate.resolve()
        r = root.resolve()
        return c == r or r in c.parents
    except (OSError, ValueError, RuntimeError):
        # Can't resolve → treat as under (fail closed for identity).
        return True


def _trusted_roots(project_root: Path | None) -> list[Path]:
    """Configured trusted install roots (tools.native_shell_trusted_roots).
    Empty by default → nothing verifies → fail closed.
    """
    try:
        from .config import get_setting

        raw = str(
            get_setting(
                "tools.native_shell_trusted_roots",
                project_root=project_root,
                default="",
            )
            or "",
        ).strip()
    except Exception:
        return []
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(";", os.pathsep).replace(",", os.pathsep).split(os.pathsep):
        c = chunk.strip()
        if c:
            parts.append(c)
    roots: list[Path] = []
    for c in parts:
        p = Path(c)
        if p.is_absolute():
            roots.append(p)
    return roots


def _path_shape_sanity(
    executable_path: str,
    project_root: Path | None,
    provider: str,
) -> tuple[bool, str]:
    """SHAPE-only checks: absolute, not under project_root, basename match.
    NOT identity proof on its own.
    """
    if not executable_path:
        return False, "no executable path exposed by host"
    p = Path(executable_path)
    if not p.is_absolute():
        return False, f"executable path not absolute: {executable_path}"
    if project_root is not None and _is_under(p, project_root):
        return False, (f"executable under project_root (repo-local fake): {executable_path}")
    base = p.name.lower()
    expected = _PROVIDER_BASENAMES.get(provider, frozenset())
    if base not in expected:
        return False, (
            f"basename {base!r} does not match provider {provider!r} "
            f"(tool name is costume, not identity)"
        )
    return True, "path shape sane"


def _verify_executable_path(
    executable_path: str,
    project_root: Path | None,
    provider: str,
) -> tuple[bool, str]:
    """REAL identity proof for a host-reported path: shape sanity PLUS the
    file exists, is a file, and lives under a configured trusted root that
    is NOT user/project-controlled.
    """
    ok, reason = _path_shape_sanity(executable_path, project_root, provider)
    if not ok:
        return False, reason
    p = Path(executable_path)
    if not p.is_file():
        return False, f"executable path does not exist / not a file: {p}"
    roots = _trusted_roots(project_root)
    if not roots:
        return False, (
            "no trusted install roots configured "
            "(tools.native_shell_trusted_roots is empty) — cannot prove the "
            "executable is not user/project-controlled"
        )
    if not any(_is_under(p, root) for root in roots):
        return False, (
            f"executable not under any trusted install root: {p} "
            f"(a renamed binary outside a trusted root is not identity)"
        )
    return True, f"verified executable under trusted root for {provider}"


def verify_native_provider_identity(
    host: str,
    provider: str,
    *,
    project_root: Path | None = None,
    executable_path: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). ok=True ONLY for a static-trusted contract or a
    host-reported path that passes REAL verification (exists + trusted root
    + basename). The path-SANITY tier is explicitly NOT eligible. Unknown /
    unproven → (False, reason) → caller routes to ai_run.
    """
    contract = _matrix.provider_identity(host, provider)
    if contract == _matrix.IDENTITY_STATIC_TRUSTED:
        return True, "host-owned identity (static trusted contract)"
    if contract == _matrix.IDENTITY_HOST_VERIFIED_PATH:
        path = executable_path or ""
        if not path and host == _matrix.HOST_CLAUDE_CODE:
            # SCOPED to the PROVEN Claude Code host adapter only (2026-06-06).
            # The host (e.g. Claude Code's Bash tool) did not echo the
            # executable it will run. Fall back to the OPERATOR-PINNED
            # provider path (tools.native_shell_provider_path — dashboard
            # wizard, not agent-editable). It is then subjected to the SAME
            # real verification as a host-reported path (exists + under a
            # trusted install root + basename); the live execution posture
            # separately re-proves FS-authority + current SHA-256 +
            # provenance for that path before any ALLOW. With no operator
            # pin (or no trusted roots) this stays empty → fail closed.
            try:
                from .config import get_setting

                path = str(
                    get_setting(
                        "tools.native_shell_provider_path",
                        project_root=project_root,
                        default="",
                    )
                    or "",
                )
            except Exception:
                path = ""
        return _verify_executable_path(
            path,
            project_root,
            provider,
        )
    if contract == _matrix.IDENTITY_HOST_REPORTED_PATH_SANITY:
        # Diagnostic tier: shape may be sane, but shape is not identity.
        return False, (
            "host-reported path is a SANITY tier only (shape != identity); "
            "not native-eligible — needs a verified-path or static-trusted "
            "contract"
        )
    return False, (
        "provider identity not proven — tool name is not identity "
        "(Batch 2.0-B precondition); routing to ai_run"
    )
