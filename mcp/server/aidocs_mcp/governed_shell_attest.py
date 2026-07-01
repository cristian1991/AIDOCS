"""Automatic governed-shell attestation — the one-control backend.

King re-seal 2026-05-30: replace the multi-toggle / manual-allowlist
ceremony (operator hand-types --provider-path + --hash-pin) with ONE
operator control — "Allow agent shell tools validated and supported by
AIDOCS" — backed by Governed Bash (governed_bash_service) as the sole
service-managed authority.

This module is the discovery + auto-enrollment layer ON TOP of
governed_bash_service.enable(). It does NOT re-implement identity /
posture / atomic-rollback — those stay in governed_bash_service. It
adds exactly one thing: turn "the operator flipped one switch" into a
fully-attested enable of the CANONICAL provider, with the SHA-256 pin
GENERATED (not hand-typed), or an operator approval card when the only
candidate is an unknown/PATH provider.

Attestation tiers (in order, strongest first):
  1. KNOWN-ROOT auto-enroll — the provider lives under a canonical
     Windows Git-for-Windows install root (not PATH). basename match +
     bounded probe + expected-publisher OS signature where available +
     a freshly generated SHA-256 pin. This is the ONLY path that
     auto-enrolls without an operator approval card.
  2. UNKNOWN / PATH provider — discovered only via PATH (shutil.which),
     not under a known root. Returns an approval card; the operator
     must explicitly approve the exact path. NEVER auto-enrolls.

Anti-self-enroll: a malicious PATH-shadowed bash.exe (placed earlier on
PATH than the real Git Bash) is, by construction, NOT under a known
install root, so tier 1 never selects it; it can only ever reach the
tier-2 approval card, which an agent cannot satisfy. Discovery scans
known roots DIRECTLY (filesystem), never PATH, for the auto-enroll
decision — so PATH ordering cannot influence what auto-enrolls.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import governed_bash_service as _gbs
from . import governed_shell_approval_store as _store

# Canonical Git-for-Windows install roots, in preference order. These
# are FILESYSTEM locations, deliberately NOT PATH — PATH ordering must
# never influence what auto-enrolls (the anti-self-enroll floor). Each
# entry is a directory that, if present, is expected to contain a real
# Git Bash at bin/bash.exe or usr/bin/bash.exe.
def _system_owned_roots() -> list[Path]:
    """Canonical Git-for-Windows install roots that are SYSTEM-OWNED and
    NOT user-writable on a default Windows ACL (Program Files / Program
    Files (x86) — modifying them requires Administrator). On POSIX:
    root-owned system bin dirs. These are the ONLY roots eligible for
    AUTO-ENROLLMENT — a provider here cannot have been planted by the
    unprivileged user (or an agent running as that user).
    """
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [Path(pf) / "Git", Path(pf86) / "Git"]
    # POSIX system bin dirs (root-owned on a normal install).
    return [Path("/usr"), Path("/")]


def _user_writable_roots() -> list[Path]:
    """Canonical Git locations that live UNDER a user-writable tree
    (LOCALAPPDATA per-user install, scoop under the home dir). A provider
    here is real Git Bash for many operators, but the user (or a
    compromised agent running as the user) can REPLACE the binary, so it
    is NEVER auto-enrolled — only surfaced on the approval card for an
    explicit, exact operator decision.
    """
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    out: list[Path] = []
    if local:
        out.append(Path(local) / "Programs" / "Git")
    if userprofile:
        out.append(Path(userprofile) / "scoop" / "apps" / "git" / "current")
    return out


def _bash_in_root(root: Path) -> list[Path]:
    """Concrete bash provider paths under a Git install root."""
    if os.name == "nt":
        return [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    # POSIX: bin/bash under the system root.
    return [root / "bin" / "bash"]


def _system_owned_candidates() -> list[Path]:
    """bash providers under SYSTEM-OWNED roots — the auto-enroll set."""
    out: list[Path] = []
    for r in _system_owned_roots():
        out.extend(_bash_in_root(r))
    return _dedupe_paths(out)


def _user_writable_candidates() -> list[Path]:
    """bash providers under USER-WRITABLE roots — approval-card only."""
    out: list[Path] = []
    for r in _user_writable_roots():
        out.extend(_bash_in_root(r))
    return _dedupe_paths(out)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _is_user_writable_location(path: Path) -> bool:
    """Best-effort: is `path` under a user-writable tree (home / LOCALAPPDATA
    / scoop)? A user-writable provider is never auto-enrolled even if it
    somehow appears under a 'known' name. Location-based floor; defends
    against a planted binary the unprivileged user can overwrite.
    """
    try:
        rp = str(path.resolve()).lower()
    except Exception:
        rp = str(path).lower()
    markers = []
    for env_key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME"):
        v = os.environ.get(env_key, "")
        if v:
            markers.append(v.lower())
    return any(m and rp.startswith(m) for m in markers)


# Expected Authenticode subject substrings for a genuine Git for Windows
# bash.exe. Checked ONLY in addition to a VALID signature status — a
# matching subject on an UNSIGNED / INVALID-status binary is worthless
# (anyone can self-sign with a chosen subject).
_EXPECTED_PUBLISHER_SUBSTRINGS = (
    "git for windows",
    "johannes schindelin",
)

def _system_directory() -> str | None:
    """The OS system directory from the Win32 GetSystemDirectoryW API — NOT
    from %SystemRoot%/%windir%/%SystemDrive% (operator-mutable env vars). The
    kernel returns the real `…\\System32`; a forged env cannot influence it.
    Returns None off-Windows or on any API failure (caller fails closed)."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)  # type: ignore[attr-defined]
        if n == 0 or n > 260:
            return None
        val = buf.value
        return val or None
    except Exception:
        return None


def _canonical_powershell() -> str | None:
    """The CANONICAL, system-owned Windows PowerShell executable — resolved
    from the OS system-directory API, NEVER from PATH or mutable env vars.

    Attestation must not trust a PATH-resolved `powershell` or an env-derived
    path: a malicious powershell.exe earlier on PATH, or a forged
    SystemRoot/SystemDrive, could forge a CLEAN verdict. We take the kernel's
    GetSystemDirectoryW result and require the helper to (a) be a real file
    and (b) RESOLVE (symlinks included) to a path STILL under that system
    directory — proving the helper itself is under system authority. Any
    failure / off-system resolution / user-writable location → None (the
    caller fails closed; no PATH fallback)."""
    try:
        sysdir = _system_directory()
        if not sysdir:
            return None
        sysroot = Path(sysdir)
        cand = sysroot / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not cand.is_file():
            return None
        # Prove the helper is UNDER system authority: its real (symlink-
        # resolved) path must stay within the API system directory, and must
        # not sit under a user-writable tree.
        if _is_user_writable_location(cand):
            return None
        try:
            real = cand.resolve()
            base = sysroot.resolve()
            real.relative_to(base)  # raises if the helper resolves outside System32
        except Exception:
            return None
        # NON-CIRCULAR helper authority: prove the helper + its parent chain
        # up to the system dir are system-owned via the native Win32 owner-SID
        # check (NOT the PowerShell effective-access probe — that would be
        # circular for proving PowerShell itself). A user-owned / ACL-weakened
        # helper or ancestor → not authoritative → None.
        if not _helper_authority_ok(cand, sysroot):
            return None
        return str(cand)
    except Exception:
        # e.g. a POSIX host with os.name faked to 'nt' in a test, or any
        # path/stat error → no canonical helper (caller fails closed).
        return None


def _helper_authority_ok(helper: Path, sysroot: Path) -> bool:
    """Prove the PowerShell helper AND its subpath chain through the OS-derived
    System32 trust anchor (INCLUSIVE: powershell.exe → v1.0 → WindowsPowerShell
    → System32) are NOT writable by the current effective token, using the
    READ-ONLY native effective-access proof
    (governed_shell_approval_store.effective_access_writable — Win32
    AccessCheck, no writes, NEVER PowerShell so it is non-circular). The chain
    is anchored at System32 and NEVER walks to the filesystem root. Off-Windows
    not applicable (POSIX governed shell uses bash) → True. Fail closed (False)
    the moment any entry is token-writable or undecidable."""
    if os.name != "nt":
        return True
    try:
        chain = _store.chain_inclusive(helper, sysroot)
    except Exception:
        return False
    for entry in chain:
        writable = _store.effective_access_writable(entry)
        if writable is None or writable:
            return False
    return True


# PowerShell that reads the target path from an ENVIRONMENT VARIABLE
# (never interpolated into the script text) and emits two lines:
# the signature Status, then the signer subject. -LiteralPath avoids
# glob/wildcard interpretation. This is the literal-safe probe: no
# operator/discovery path is ever spliced into PowerShell source, so a
# path containing quotes/`;`/`$(...)` cannot inject.
_AUTHENTICODE_PS = (
    "$ErrorActionPreference='Stop';"
    "$p=$env:AIDOCS_ATTEST_PATH;"
    "$s=Get-AuthenticodeSignature -LiteralPath $p;"
    "Write-Output $s.Status;"
    "Write-Output ($s.SignerCertificate.Subject)"
)


def _publisher_ok(path: Path) -> tuple[bool | None, str]:
    """Windows Authenticode validity + expected-publisher check.

    Returns (ok, reason):
      * (True, ...)  — signature Status == Valid AND subject matches an
        expected Git-for-Windows publisher.
      * (None, ...)  — non-Windows (no Authenticode to check) OR the
        probe itself was unavailable (treated as 'unverifiable', which
        the caller routes to operator approval — NOT auto-enroll).
      * (False, ...) — a signature exists but its Status is NOT Valid,
        OR the publisher subject is unexpected. Hard refusal.

    Literal-safe: the path is passed via the AIDOCS_ATTEST_PATH env var,
    never interpolated into the PowerShell source.
    """
    if os.name != "nt":
        return (None, "non-windows: no Authenticode publisher to check")
    pwsh = _canonical_powershell()
    if not pwsh:
        # No canonical system PowerShell → unverifiable. We refuse to fall
        # back to a PATH-resolved powershell that could be shadowed.
        return (None, "canonical system PowerShell not found (refusing PATH fallback)")
    try:
        import subprocess

        # Bounded (timeout=15), fixed argv, operator-local attestation
        # probe invoked by ABSOLUTE canonical path (never PATH-resolved).
        # The provider path rides in the environment (literal-safe; no
        # interpolation into PowerShell source). -LiteralPath blocks
        # glob interpretation. Not routed through ShellEgressService
        # because its output (signature status + cert subject) is
        # attestation evidence the output-guard fail-closed would
        # withhold. Baselined via the two-layer law (semgrep file-exclude
        # + LEGACY_SUBPROCESS_FINGERPRINTS row).
        env = dict(os.environ)
        env["AIDOCS_ATTEST_PATH"] = str(path)
        cp = subprocess.run(  # noqa: S603
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _AUTHENTICODE_PS,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        lines = [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return (None, "no Authenticode output (unsigned or unreadable)")
        status = lines[0].strip().lower()
        subject = (lines[1].strip().lower() if len(lines) > 1 else "")
        # Status MUST be Valid — a present-but-NotSigned/HashMismatch/
        # UnknownError signature is a refusal, not a skip. Subject text
        # alone is never sufficient.
        if status != "valid":
            return (False, f"Authenticode status not Valid: {status!r}")
        for needle in _EXPECTED_PUBLISHER_SUBSTRINGS:
            if needle in subject:
                return (True, f"valid signature; publisher matches {needle!r}")
        return (False, f"valid signature but unexpected publisher: {subject[:120]!r}")
    except Exception as exc:
        # Probe error → UNVERIFIABLE. Caller must route to approval, not
        # auto-enroll (we could not prove the signature is valid).
        return (None, f"publisher check unavailable: {type(exc).__name__}")


def _fs_authority_ok(provider_path: Path) -> tuple[bool, str]:
    """REAL non-user-writable filesystem-authority check on the provider
    file AND its relevant parent chain.

    POSIX: every entry from the provider up to the filesystem root must
    be root-owned (uid 0) and NOT group/other-writable — i.e. an
    unprivileged user cannot replace the binary or any ancestor dir
    (a writable ancestor lets an attacker swap the file via rename).
    Windows: a fail-closed EFFECTIVE-ACCESS proof on every entry —
    AIDOCS reads the current process token's ENABLED SIDs (the user SID +
    every enabled, non-deny-only group it actually belongs to) and the
    path's ACL, translates each write-granting Allow ACE to a SID, and
    refuses if any such SID is one the current user effectively holds.
    It does NOT guess by hardcoded principal NAME (Users/Everyone/…) and
    does NOT silently ignore an ACE or membership it cannot resolve: an
    unresolvable ACE SID, an unreadable token, or an unreadable ACL is an
    HONEST REFUSAL, never a pass. Location is used only as a fast NEGATIVE
    pre-filter (a per-user tree is refused without even reading the ACL);
    it is never, on its own, a positive grant of authority.

    Returns (ok, reason). Fail-CLOSED on any stat/resolve error.
    """
    try:
        p = provider_path.resolve()
    except Exception as exc:
        return (False, f"cannot resolve provider path: {type(exc).__name__}")

    chain: list[Path] = [p]
    for parent in p.parents:
        chain.append(parent)

    if os.name != "nt":
        import stat as _stat

        for entry in chain:
            try:
                st = entry.stat()
            except OSError as exc:
                return (False, f"cannot stat {entry}: {type(exc).__name__}")
            if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
                return (False, f"{entry} is group/other-writable (mode-weakened)")
            uid = getattr(st, "st_uid", None)
            if uid is not None and uid != 0:
                return (False, f"{entry} owned by non-root uid {uid} (user-writable)")
        return (True, "non-user-writable: root-owned, no group/other write on provider + parent chain")

    # Windows: ONE LAW — the same READ-ONLY native AccessCheck used for the
    # control-plane store and the PowerShell helper
    # (governed_shell_approval_store.effective_access_writable). No
    # PowerShell-per-ancestor spawn (faster + one consistent authority). Each
    # entry on the provider + parent chain must NOT be writable by the current
    # effective token; undecidable → fail closed.
    for entry in chain:
        # Fast NEGATIVE pre-filter: a per-user tree is refused outright (never
        # a positive grant on its own).
        if _is_user_writable_location(entry):
            return (False, f"{entry} is under a user-writable tree")
        w = _store.effective_access_writable(entry)
        if w is None:
            return (False, f"{entry}: effective-access undecidable")
        if w:
            return (False, f"{entry}: current token can write (effective-access)")
    return (True, "non-user-writable: native AccessCheck clean on provider + parent chain")


def _same_executable(a: str | None, b: str | None) -> bool:
    """Path-equality for executable identity: resolve both and compare,
    case-insensitively on Windows. Fail-CLOSED (False) on any error or a
    missing side."""
    if not a or not b:
        return False
    try:
        ra = Path(a).resolve()
        rb = Path(b).resolve()
    except Exception:
        return False
    sa, sb = str(ra), str(rb)
    if os.name == "nt":
        return sa.lower() == sb.lower()
    return sa == sb


def identity_binding_ok(
    host_exposed_path: str | None,
    posture: dict[str, Any],
    *,
    enrolled_self_bind_ok: bool = False,
) -> tuple[bool, str]:
    """THE host-native ALLOW identity binding (king re-seal 2026-05-30).

    AIDOCS may render a RAW host-native ALLOW only when the HOST EXPOSES
    (or cryptographically binds) the ACTUAL executable it will run AND
    that exact path equals the freshly-attested enrolled provider carried
    in `posture`. When the host exposes nothing, AIDOCS does NOT fabricate
    the identity from the enrolled config — it fails closed so the SAME
    command routes through ai_run, where AIDOCS owns the spawn.

    DEFAULT behaviour is unchanged (fail-closed on a missing host-exposed
    path) so the anti-costume guarantee holds for every caller that does
    not explicitly opt in.

    `enrolled_self_bind_ok` (2026-06-05, operator-authorised, read-only
    scope): a NARROW opt-in for a host whose PreToolUse payload
    structurally cannot echo its executable (e.g. Claude Code's Bash tool)
    AND where the caller has independently established that the command is
    in the governed READ-ONLY class against an OPERATOR-PINNED provider.
    Only then, with `posture.ok` already proven (which means the enrolled
    provider was freshly re-attested THIS call: exact path + FS-authority
    over provider+parents + current SHA-256 equality + provenance), AIDOCS
    binds to that enrolled provider. This is NOT bare-config fabrication:
    the provider is operator-pinned (dashboard wizard, not agent-editable)
    and cryptographically re-proven every call; AIDOCS runs INSIDE the
    host's PreToolUse gate, so trusting the host to invoke its own pinned
    bash is the SAME trust boundary AIDOCS already depends on. Writes /
    network / nested-shell never reach here (they are not read-only) and
    still fall back to ai_run.

    Returns (ok, reason). Fail-CLOSED on a not-ok posture, a missing
    attested enrolled path, a missing host-exposed path (unless the
    read-only self-bind opt-in is set), or a mismatch.
    """
    if not posture.get("ok"):
        return (False, "live execution posture not ok")
    enrolled = str(posture.get("checks", {}).get("enrolled_provider_path") or "").strip()
    if not enrolled:
        return (False, "no attested enrolled provider path in posture")
    host_exposed = str(host_exposed_path or "").strip()
    if not host_exposed:
        if enrolled_self_bind_ok:
            return (
                True,
                "host exposed no path; read-only self-bind to the operator-pinned, "
                "freshly re-attested enrolled provider",
            )
        return (
            False,
            "host did not expose the executable it will run; AIDOCS will not "
            "fabricate identity from enrolled config — routing to ai_run",
        )
    if not _same_executable(host_exposed, enrolled):
        return (
            False,
            f"host-exposed executable {host_exposed!r} != attested enrolled "
            f"provider {enrolled!r}",
        )
    return (True, "host-exposed executable equals the freshly-attested enrolled provider")


def claude_code_native_self_bind_ok(env: Any, posture: dict[str, Any]) -> bool:
    """HOST-ADAPTER CONTRACT (2026-06-06): may THIS host self-bind to the
    operator-pinned provider when it exposes no executable path?

    Granted ONLY when ALL hold:
      * host == claude_code (the one PROVEN local native-shell adapter), AND
      * provider == bash, AND
      * posture.ok — the live execution posture just re-proved THIS call: the
        operator enabled Governed Bash and pinned a system-authority provider,
        and AIDOCS freshly re-attested that EXACT provider (path + FS-authority
        over provider+parents + current SHA-256 + provenance), AND
      * the host is path-silent (exposes no executable to bind strictly).

    DELIBERATELY scoped: unknown hosts, OpenCode (until separately proven), and
    remote MCP / web execution (mcp.codenexus.cloud / ChatGPT) NEVER self-bind.
    A path-silent call from any of those fails identity binding and routes to
    ai_run, where AIDOCS owns the spawn. This is the host-owned binding
    contract that authorises path-silent WRITE/build/test work on Claude Code
    without fabricating identity from bare config.
    """
    try:
        from .shell_capability_matrix import HOST_CLAUDE_CODE, PROVIDER_BASH
    except Exception:
        return False
    host = getattr(env, "host", "") or ""
    provider = getattr(env, "provider", "") or ""
    path_silent = not str(getattr(env, "provider_executable_path", "") or "").strip()
    return bool(
        host == HOST_CLAUDE_CODE
        and provider == PROVIDER_BASH
        and posture.get("ok")
        and path_silent
    )


def live_execution_posture(
    project_root: Path,
    *,
    probe_runner: _gbs.ProbeRunner | None = None,
) -> dict[str, Any]:
    """THE single canonical, freshly RE-DERIVED governed-shell execution
    posture. Every prospective native host ALLOW must consume this; it is
    the authority that unifies dashboard posture, provider identity, and
    PreToolUse execution into one proof computed AT EXECUTION TIME.

    Proves, fresh, with NO cached/flag-only/basename-only/trusted-root-
    only/monkeypatched-static shortcut:
      1. service-managed enablement (enforcement flags on)
      2. exact enrolled provider path (tools.native_shell_provider_path)
         is set and is a real file
      3. non-user-writable FS authority (provider + parent chain)
      4. valid platform provenance where required (Windows Authenticode
         Valid + expected publisher)
      5. CURRENT SHA-256 equality — re-hash the enrolled provider NOW and
         compare to the enrolled pin (swapped bytes ⇒ deny)
      6. bounded execution probe (it still runs)
      7. host capability (matrix native-safe + output replacement)

    Returns {ok, route, reason, repair, checks}. route is 'native' ONLY
    when ok; otherwise 'ai_run' with an operator repair string. Never
    raises — any error fails closed to ai_run.
    """
    runner = probe_runner or _gbs._default_probe
    checks: dict[str, Any] = {}
    try:
        from .config import get_setting

        # 1. enablement (service-managed flags).
        flags_on = all(
            bool(
                get_setting(name, project_root=project_root, default=False),
            )
            for name in _gbs.ENFORCEMENT_FLAGS
        )
        checks["service_managed_enabled"] = flags_on
        if not flags_on:
            return _posture_fail(
                checks, "governed bash not enabled", "run the supported-provider enable action"
            )

        # 2. exact enrolled provider path.
        enrolled = str(
            get_setting(_gbs.KEY_PROVIDER_PATH, project_root=project_root, default="") or "",
        ).strip()
        checks["enrolled_provider_path"] = enrolled
        if not enrolled or not Path(enrolled).is_file():
            return _posture_fail(
                checks, "no enrolled provider path / file missing",
                "re-run the supported-provider enable action",
            )
        prov = Path(enrolled)

        # 3. non-user-writable FS authority (provider + parent chain).
        fs_ok, fs_reason = _fs_authority_ok(prov)
        checks["fs_authority"] = fs_ok
        checks["fs_authority_reason"] = fs_reason
        if not fs_ok:
            return _posture_fail(
                checks, f"provider not under system FS authority: {fs_reason}",
                "the enrolled provider or a parent dir is user-writable; "
                "re-enroll a system-owned provider",
            )

        # 4. valid provenance where required (Windows).
        pub_ok, pub_reason = _publisher_ok(prov)
        checks["provenance"] = pub_ok
        checks["provenance_reason"] = pub_reason
        if os.name == "nt" and pub_ok is not True:
            return _posture_fail(
                checks, f"platform provenance not valid: {pub_reason}",
                "re-enroll a validly-signed provider",
            )
        if pub_ok is False:
            return _posture_fail(
                checks, f"provenance invalid: {pub_reason}", "re-enroll a trusted provider"
            )

        # 5. CURRENT SHA-256 equality (re-hash NOW; swapped bytes ⇒ deny).
        pin = str(
            get_setting(_gbs.KEY_SHA256, project_root=project_root, default="") or "",
        ).strip().lower()
        checks["enrolled_sha256"] = pin
        if not pin:
            return _posture_fail(
                checks, "no enrolled SHA-256 tamper pin", "re-enroll the provider"
            )
        try:
            current = _gbs._sha256(prov)
        except Exception as exc:
            return _posture_fail(
                checks, f"cannot hash provider: {type(exc).__name__}", "re-enroll the provider"
            )
        checks["current_sha256"] = current
        if current != pin:
            return _posture_fail(
                checks, "PROVIDER BYTES DRIFTED — current SHA-256 != enrolled pin",
                "provider was modified/swapped since enrollment; re-enroll after verifying",
            )

        # 6. bounded execution probe.
        probe_ok = bool(runner(str(prov)))
        checks["bounded_probe"] = probe_ok
        if not probe_ok:
            return _posture_fail(
                checks, "provider failed the bounded execution probe", "re-enroll the provider"
            )

        # 7. host capability (matrix).
        try:
            from . import shell_capability_matrix as _matrix

            cap_ok = bool(_matrix.is_native_safe("claude_code", "bash"))
            cap = _matrix.lookup("claude_code", "bash")
            out_ok = bool(cap and cap.posttooluse_output_replacement)
        except Exception:
            cap_ok, out_ok = False, False
        checks["host_capability_native_safe"] = cap_ok
        checks["host_output_replacement"] = out_ok
        if not (cap_ok and out_ok):
            return _posture_fail(
                checks, "host/provider not capability-eligible for native execution",
                "no operator repair — capability matrix governs this honestly",
            )

        return {
            "ok": True,
            "route": "native",
            "reason": "live governed-shell execution posture verified",
            "repair": "",
            "checks": checks,
        }
    except Exception as exc:
        return _posture_fail(
            {"error": type(exc).__name__}, "posture derivation error", "retry / re-enroll"
        )


def _posture_fail(checks: dict[str, Any], reason: str, repair: str) -> dict[str, Any]:
    """Any failed live-posture check → route to ai_run + operator repair."""
    return {
        "ok": False,
        "route": "ai_run",
        "reason": reason,
        "repair": repair,
        "checks": checks,
    }


def discover_supported_provider(
    *,
    probe_runner: _gbs.ProbeRunner | None = None,
) -> dict[str, Any]:
    """Scan KNOWN install roots (never PATH) for a canonical, attestable
    bash. Returns the strongest candidate with a generated pin, or a
    not-found result.

    Result shape:
      {
        "found": bool,
        "provider_path": str,          # the attested provider, if found
        "sha256": str,                 # generated pin, if found
        "publisher": {"ok": bool|None, "reason": str},
        "checks": {...},               # basename/probe/etc per candidate
        "scanned": [str, ...],         # known-root paths examined
      }
    """
    runner = probe_runner or _gbs._default_probe
    scanned: list[str] = []
    for cand in _system_owned_candidates():
        scanned.append(str(cand))
        if not cand.is_file():
            continue
        # basename family check (real bash only — sh/zsh/wsl excluded by
        # the identity law's _PROVIDER_BASENAMES).
        if cand.name.lower() not in {"bash", "bash.exe"}:
            continue
        # Trust floor: refuse to auto-enroll anything under a user-writable
        # tree even if it surfaced via a system-owned root symlink. The
        # unprivileged user must not be able to overwrite an auto-enrolled
        # provider.
        if _is_user_writable_location(cand):
            continue
        # REAL FS-authority: provider file + parent chain must be non-user-
        # writable (root-owned + no group/other write on POSIX; outside
        # user trees on Windows). An ACL/mode-weakened system root (e.g. a
        # Program Files dir with a loosened ACL, or a chmod-777'd /usr/bin)
        # REFUSES auto-enroll here.
        fs_ok, _fs_reason = _fs_authority_ok(cand)
        if not fs_ok:
            continue
        # bounded execution probe (proves it runs)
        if not runner(str(cand)):
            continue
        # Platform-valid provenance. On Windows, REQUIRE a Valid
        # Authenticode status + expected publisher (not subject text
        # alone). pub_ok None = unverifiable → NOT auto-enrolled (route
        # to approval); pub_ok False = invalid/wrong → refuse.
        pub_ok, pub_reason = _publisher_ok(cand)
        if os.name == "nt" and pub_ok is not True:
            # Windows auto-enroll DEMANDS a proven-valid signature.
            continue
        if pub_ok is False:
            continue
        # SHA-256 is generated ONLY AFTER attestation passes (system-owned
        # + probe + valid publisher). It is a post-attestation TAMPER PIN
        # — recorded so a later run detects a swapped binary — never a
        # source of trust on its own.
        try:
            pin = _gbs._sha256(cand)
        except Exception:
            continue
        return {
            "found": True,
            "provider_path": str(cand),
            "sha256": pin,
            "publisher": {"ok": pub_ok, "reason": pub_reason},
            "checks": {
                "basename": True,
                "bounded_probe": True,
                "system_owned_root": True,
                "user_writable": False,
                "provenance_valid": (pub_ok is True) or (os.name != "nt"),
            },
            "scanned": scanned,
        }
    return {
        "found": False,
        "provider_path": "",
        "sha256": "",
        "publisher": {"ok": None, "reason": "no system-owned provider with valid provenance"},
        "checks": {},
        "scanned": scanned,
    }


def enable_supported(
    project_root: Path,
    *,
    operator_authenticated: bool,
    scope: str = "global",
    probe_runner: _gbs.ProbeRunner | None = None,
) -> dict[str, Any]:
    """THE one-control backend. Auto-discover + auto-enroll the canonical
    Git Bash from known roots with a generated pin, or return an operator
    approval card when no known-root provider attests.

    This is what "Allow agent shell tools validated and supported by
    AIDOCS" calls. It NEVER asks the operator to type a path or a pin;
    it NEVER auto-enrolls a PATH-only provider.
    """
    if not operator_authenticated:
        return {"ok": False, "reason": "unauthenticated", "blocked_by": "operator_auth"}

    runner = probe_runner or _gbs._default_probe
    disc = discover_supported_provider(probe_runner=runner)

    if disc["found"]:
        # Auto-enroll the attested known-root provider with the GENERATED
        # pin. governed_bash_service.enable performs the atomic write +
        # readback + final posture re-derive (rollback if unverified).
        # require_os_signature stays False here — publisher is enforced
        # in discovery (present-but-wrong already excluded), and a
        # genuine-but-unsigned build in a known root must not be blocked.
        res = _gbs.enable(
            project_root,
            operator_authenticated=True,
            provider_path=disc["provider_path"],
            hash_pin=disc["sha256"],
            require_os_signature=False,
            scope=scope,
            probe_runner=runner,
        )
        res["attestation"] = {
            "auto_enrolled": bool(res.get("ok")),
            "provider_path": disc["provider_path"],
            "sha256": disc["sha256"],
            "publisher": disc["publisher"],
            "source": "known_install_root",
        }
        return res

    # No system-owned, valid-provenance provider auto-enrolled. Build an
    # EXACT-PATH approval card listing the candidates the operator may
    # explicitly approve: user-writable Git installs (real but
    # overwritable) and the PATH-discovered bash. NONE of these
    # auto-enroll — a PATH-shadowed or user-writable bash can never
    # self-enroll; the operator approves an exact path via
    # governed-bash-enable --provider-path.
    import shutil

    user_writable = [str(p) for p in _user_writable_candidates() if p.is_file()]
    path_candidate = shutil.which("bash") or ""
    # Issue a SIGNED card per exact candidate the operator may approve. Each
    # card binds path + current SHA-256 + nonce + expiry, so approval cannot
    # be inherited by a recomputed candidate set or swapped bytes.
    issued_cards = [
        c for c in (issue_candidate_card(project_root, p) for p in _issued_candidate_paths()) if c
    ]
    return {
        "ok": False,
        "reason": "no_system_owned_provider_with_valid_provenance",
        "requires_operator_approval": True,
        "approval_card": {
            "title": "Unknown / unproven shell provider — operator approval required",
            "detail": (
                "No system-owned (non-user-writable) Git Bash with valid "
                "platform provenance was found. AIDOCS auto-enrolls ONLY a "
                "provider under a system-owned root (Program Files) with a "
                "Valid Authenticode signature from the expected publisher. "
                "User-writable installs (per-user / scoop), unsigned or "
                "invalid-signature binaries, and PATH-only providers are "
                "never auto-enrolled — the operator must APPROVE one of the "
                "signed candidate cards below (each bound to a path + hash + "
                "nonce + expiry)."
            ),
            "user_writable_candidates": user_writable,
            "path_candidate": path_candidate,
            "scanned_system_roots": disc["scanned"],
            # The signed, exact-path approval offers. Approval must echo one
            # of these cards back (verify_candidate_card).
            "candidate_cards": issued_cards,
        },
    }


# ── Approval cards via the PRIVILEGED BROKER ────────────────────────
# A card is a signed, single-use offer to enroll ONE exact candidate. The
# signing key + pending + consumed state live ONLY in the privileged broker
# (a separate service) — the agent never holds the secret and cannot mint or
# forge a card. The agent does the checks it CAN do without the secret
# (file exists, still a server-issued candidate, CURRENT bytes == pinned
# hash) and delegates signing + single-use nonce consumption to the broker.
# With NO broker connected, approvals are UNAVAILABLE (fail closed).
CARD_TTL_SECONDS = 300


def issue_candidate_card(
    project_root: Path, candidate_path: str, *, now: float | None = None
) -> dict[str, Any] | None:
    """Ask the privileged broker to mint a signed, single-use card for one
    exact candidate. Returns None (fail closed) if the candidate is not a
    real file OR no broker is connected (the agent cannot sign on its own)."""
    try:
        p = Path(candidate_path)
        if not p.is_file():
            return None
        resolved = str(p.resolve())
        sha = _gbs._sha256(p)
    except Exception:
        return None
    broker = _store.get_broker()
    if broker is None:
        return None  # fail closed: no privileged broker → approvals unavailable
    try:
        return broker.issue_card(resolved, sha, CARD_TTL_SECONDS)
    except Exception:
        return None


def verify_candidate_card(
    project_root: Path, card: dict[str, Any], *, now: float | None = None
) -> tuple[bool, str]:
    """Verify + single-use-consume an approval card. The agent independently
    re-checks what it can WITHOUT the secret — still a server-issued
    candidate, the file exists, CURRENT bytes == the card's pinned hash
    (swapped bytes ⇒ reject) — then delegates signature + single-use nonce
    consumption to the privileged broker. Fail closed with no broker."""
    if not isinstance(card, dict):
        return (False, "malformed card")
    path = str(card.get("provider_path") or "")
    sha = str(card.get("sha256") or "").lower()
    if not (path and sha):
        return (False, "incomplete card")
    broker = _store.get_broker()
    if broker is None:
        return (False, "no privileged approval broker (fail closed)")
    # Agent-side checks that need no secret:
    issued = (
        {c.lower() for c in _issued_candidate_paths()}
        if os.name == "nt"
        else set(_issued_candidate_paths())
    )
    probe = path.lower() if os.name == "nt" else path
    if probe not in issued:
        return (False, "card path is no longer a server-issued candidate")
    if not Path(path).is_file():
        return (False, "card path missing")
    try:
        current = _gbs._sha256(Path(path)).lower()
    except Exception as exc:
        return (False, f"cannot hash candidate: {type(exc).__name__}")
    if current != sha:
        return (False, "provider bytes changed since the card was issued (swapped bytes)")
    # Broker owns the secret + single-use nonce ledger: it checks the
    # signature, expiry, and consumes the nonce EXACTLY ONCE (replay → refuse).
    try:
        ok, why = broker.verify_and_consume(card)
    except Exception as exc:
        return (False, f"broker verification error: {type(exc).__name__}")
    return (ok, why)


def _issued_candidate_paths() -> list[str]:
    """The EXACT paths the server would offer on an approval card — the
    user-writable Git installs that exist plus the PATH-discovered bash.
    Exact-path approval is bound to THIS server-issued set: the operator
    can only approve a path AIDOCS itself surfaced, never an arbitrary
    typed path."""
    import shutil

    out = [str(p.resolve()) for p in _user_writable_candidates() if p.is_file()]
    which = shutil.which("bash")
    if which:
        try:
            out.append(str(Path(which).resolve()))
        except Exception:
            out.append(which)
    return out


def approve_exact_path(
    project_root: Path,
    provider_path: str,
    *,
    operator_authenticated: bool,
    scope: str = "global",
    card: dict[str, Any] | None = None,
    probe_runner: _gbs.ProbeRunner | None = None,
) -> dict[str, Any]:
    """Approve ONE exact provider via a SIGNED, single-use control-plane
    approval card. A valid `card` is REQUIRED — the legacy path-only side
    door is gone: there is no way to approve a bare operator-typed path. The
    card is verified end-to-end (control-plane signature + expiry +
    still-a-candidate + CURRENT bytes == pinned hash) AND consumed exactly
    once (replay refused), then the SHA-256 pin is GENERATED from the
    approved file and governed_bash_service.enable does the atomic write +
    readback + posture re-derive (rollback if unverified)."""
    if not operator_authenticated:
        return {"ok": False, "reason": "unauthenticated", "blocked_by": "operator_auth"}
    runner = probe_runner or _gbs._default_probe

    # No card → no approval. The path-only side door is removed.
    if card is None:
        return {
            "ok": False,
            "reason": "approval_card_required",
            "blocked_by": "exact_path_approval",
            "message": (
                "exact-path approval requires a signed, single-use control-plane "
                "approval card. Re-run the supported-shell action to obtain a "
                "candidate card, then approve that exact card."
            ),
        }
    ok, why = verify_candidate_card(project_root, card)
    if not ok:
        return {
            "ok": False,
            "reason": "invalid_approval_card",
            "blocked_by": "exact_path_approval",
            "message": f"approval card rejected: {why}",
        }
    provider_path = str(card.get("provider_path") or provider_path)

    try:
        resolved = str(Path(provider_path).resolve())
    except Exception:
        return {"ok": False, "reason": "unresolvable_path", "blocked_by": "exact_path_approval"}
    issued = {c.lower() for c in _issued_candidate_paths()} if os.name == "nt" else set(
        _issued_candidate_paths()
    )
    probe = resolved.lower() if os.name == "nt" else resolved
    if probe not in issued:
        return {
            "ok": False,
            "reason": "not_a_server_issued_candidate",
            "blocked_by": "exact_path_approval",
            "message": (
                "exact-path approval is bound to the server-issued candidate "
                "card; AIDOCS will not enroll an arbitrary typed path. Re-run "
                "the supported-shell action to obtain the candidate card, then "
                "approve one of its listed paths."
            ),
            "issued_candidates": sorted(issued),
        }
    if not Path(resolved).is_file():
        return {"ok": False, "reason": "candidate_missing", "blocked_by": "exact_path_approval"}
    try:
        pin = _gbs._sha256(Path(resolved))
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"cannot_hash_candidate:{type(exc).__name__}",
            "blocked_by": "exact_path_approval",
        }
    res = _gbs.enable(
        project_root,
        operator_authenticated=True,
        provider_path=resolved,
        hash_pin=pin,
        require_os_signature=False,
        scope=scope,
        probe_runner=runner,
    )
    res["attestation"] = {
        "auto_enrolled": False,
        "operator_approved_exact_path": bool(res.get("ok")),
        "provider_path": resolved,
        "sha256": pin,
        "source": "server_issued_candidate_card",
    }
    return res
