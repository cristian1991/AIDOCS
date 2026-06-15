"""AIDOCS-owned runtime provisioning — the enforcement-interpreter boundary.

A security-grade gate must NOT run under whatever ambient python (a project
venv, a system python on PATH, an ephemeral build env) happened to invoke
setup: that interpreter can vanish, be shadowed, or carry a different/again
patched ``aidocs_mcp``. Enforcement hooks must run under a STABLE, AIDOCS-owned
interpreter whose integrity we can vouch for.

Runtime tiers (best → worst), every one VERIFIED (must import ``aidocs_mcp``):

  * ``operator_pin`` — operator set ``AIDOCS_PYTHON`` explicitly. Owned by
    operator intent; distinguished from a runtime AIDOCS itself provisioned.
  * ``standalone``   — a pinned standalone CPython under ``~/.aidocs/runtime``
    installed from a pinned version/URL/SHA256 (or an offline archive with a
    pinned SHA256), atomically. The headline owned tier.
  * ``venv``         — a dedicated AIDOCS-managed venv built from a suitable
    base python. A TRUTHFUL DEGRADED Tier-1 path when no standalone pin is
    available for the platform.
  * ``ambient``      — ``sys.executable``. NOT owned. Hooks are NEVER installed
    against ambient silently — only under an explicit degraded/dev escape.

Everything that touches the network / filesystem extraction / pip is behind an
injected callable so the semantics (standalone preference, checksum-fail-closed,
ambient refusal, drift repair, idempotent + offline reinstall, truthful audit)
are provable without a real download. We ship NO unverified URLs/checksums: an
unconfigured platform honestly DEGRADES to venv rather than installing something
it cannot verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

RuntimeRunner = Callable[[list[str]], "tuple[int, str, str]"]

# A bare PEP 440-ish version token (e.g. "1.2.3", "0.4.0rc1"). Anything else in
# a package spec is treated as a local wheel/sdist/source path or a full pip
# requirement and installed verbatim.
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.\-+!_]*$")


def expected_aidocs_version() -> str | None:
    """The aidocs_mcp version THIS process is — the law/enforcement package an
    owned runtime must prove it carries. ``None`` only if our own import is
    somehow broken (then version enforcement is skipped, import is still
    required).
    """
    try:
        import aidocs_mcp

        return getattr(aidocs_mcp, "__version__", None)
    except Exception:
        return None


def _resolve_package(
    package_spec: str | None,
    expected_version: str | None,
) -> tuple[str, str | None]:
    """Map a package spec → (pip target, version to ENFORCE at verify).

    * ``None`` → pin to ``aidocs_mcp==<expected_version>`` (or bare aidocs_mcp if
      the expected version is unknown).
    * a bare version token → ``aidocs_mcp==<token>`` and enforce that version.
    * anything else (local wheel/sdist/source dir, full requirement) → install
      verbatim; we cannot predict its version, so we enforce none and record the
      actual version reported after install.
    """
    if not package_spec:
        if expected_version:
            return f"aidocs_mcp=={expected_version}", expected_version
        return "aidocs_mcp", None
    s = str(package_spec).strip()
    if _VERSION_RE.match(s):
        return f"aidocs_mcp=={s}", s
    return s, None


# ── locations ────────────────────────────────────────────────────────────
def runtime_root(home: Path | str) -> Path:
    return Path(home) / ".aidocs" / "runtime"


def manifest_path(home: Path | str) -> Path:
    return runtime_root(home) / "runtime.json"


def _standalone_dir(home: Path | str) -> Path:
    return runtime_root(home) / "cpython"


def _venv_dir(home: Path | str) -> Path:
    return runtime_root(home) / "venv"


def _python_in(base: Path) -> str | None:
    """First existing python executable under an install dir (OS-agnostic).

    Covers both a flat layout and the ``python/`` top-level dir that
    python-build-standalone ``install_only`` archives unpack into (windows:
    ``python/python.exe``; unix: ``python/bin/python3``).
    """
    for rel in (
        "python.exe",
        "python3.exe",
        Path("Scripts") / "python.exe",
        Path("bin") / "python",
        Path("bin") / "python3",
        "python",
        "python3",
        Path("python") / "python.exe",
        Path("python") / "bin" / "python3",
        Path("python") / "bin" / "python",
    ):
        cand = base / rel
        if cand.is_file():
            return str(cand)
    return None


def standalone_python(home: Path | str) -> str | None:
    return _python_in(_standalone_dir(home))


def venv_python(home: Path | str) -> str | None:
    return _python_in(_venv_dir(home))


def platform_key() -> str:
    """Stable key for selecting a pinned standalone build."""
    return f"{sys.platform}-{platform.machine().lower()}"


# Pinned standalone builds keyed by ``platform_key()`` (sys.platform + machine).
# These are python-build-standalone ``install_only`` archives; every SHA256 was
# transcribed byte-for-byte from the release's published SHA256SUMS (and matches
# the GitHub asset digest). A platform NOT listed here still degrades honestly to
# venv (or an operator --offline-archive + --sha256). To refresh: bump
# _PBS_RELEASE/_PBS_PY and replace each sha256 from the new release's SHA256SUMS
# — never hand-edit a single hex char.
_PBS_RELEASE = "20260510"
_PBS_PY = "3.12.13"

# AIDOCS blesses exactly ONE CPython per release as its official owned standalone
# runtime. Anything else an operator runs under is "custom" provenance — still
# usable (offline archive / sha manifest / venv / operator pin) but reported as
# NOT the blessed build, so the distinction is always visible in doctor/setup.
BLESSED_PYTHON = _PBS_PY  # 3.12.13
# Intentionally NOT blessed yet — this is policy, not an oversight. 3.13/3.14 are
# excluded until the toolchain + dependency surface are validated against them;
# operators may still run them explicitly via --offline-archive/--sha256 (custom).
NOT_BLESSED_YET = ("3.13", "3.14")


def is_blessed_version(version: str | None) -> bool:
    """True iff ``version`` is THE blessed CPython for this AIDOCS release."""
    return bool(version) and str(version) == BLESSED_PYTHON


_PBS_BASE = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{_PBS_RELEASE}/cpython-{_PBS_PY}%2B{_PBS_RELEASE}-"
)


def _pbs(triple: str, sha256: str) -> dict[str, str]:
    return {
        "version": _PBS_PY,
        "url": f"{_PBS_BASE}{triple}-install_only.tar.gz",
        "sha256": sha256,
    }


PINNED: dict[str, dict[str, str]] = {
    "win32-amd64": _pbs(
        "x86_64-pc-windows-msvc",
        "346dfbcb95171dd6d1275e6f8cb2e656cc15cb054c399ae54db57bfad4b1a60f",
    ),
    "linux-x86_64": _pbs(
        "x86_64-unknown-linux-gnu",
        "e7332b4b4bb85006deb48d251c786a04c14de104c9b3a006b33457a4a604b8bc",
    ),
    "linux-aarch64": _pbs(
        "aarch64-unknown-linux-gnu",
        "87097de12bc212e41ea8409efd0083fe06465d725e35d130e4007a4bf7e4f1c8",
    ),
    "darwin-arm64": _pbs(
        "aarch64-apple-darwin",
        "5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17",
    ),
    "darwin-x86_64": _pbs(
        "x86_64-apple-darwin",
        "cd369e76973c3179bc578230d8615ab621968ed758c5e32f636eecef4ad79894",
    ),
}


def pinned_spec(key: str | None = None) -> dict[str, str] | None:
    spec = PINNED.get(key or platform_key())
    return dict(spec) if spec else None


def _asset_filename(spec: dict) -> str:
    """The upstream asset filename a PINNED URL points at (decoding %2B → +)."""
    from urllib.parse import unquote

    return unquote(str(spec.get("url", "")).rsplit("/", 1)[-1])


def _parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``<sha256>  <filename>`` checksum file into {filename: sha}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            out[parts[-1]] = parts[0].lower()
    return out


def _fetch_sha256sums(release: str) -> str:
    import urllib.request

    url = (
        "https://github.com/astral-sh/python-build-standalone/releases/"
        f"download/{release}/SHA256SUMS"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read().decode("utf-8", "replace")


def verify_pinned_against_upstream(
    sums_text: str | None = None,
    *,
    fetch: Callable[[str], str] | None = None,
    release: str | None = None,
    version: str | None = None,
    pinned: dict[str, dict[str, str]] | None = None,
) -> dict:
    """MAINTENANCE/CI check (NOT part of the offline suite): confirm every PINNED
    entry still matches the upstream python-build-standalone release. For each
    entry it checks the asset's release+version+triple (via the exact filename),
    that the asset EXISTS in the release SHA256SUMS, and that our recorded SHA256
    matches upstream. Pass ``sums_text`` to run offline (unit tests); otherwise
    the SHA256SUMS for ``release`` is fetched. Returns {ok, release, checked,
    results:[{platform, asset, ok, problems, upstream_sha}]}; ok=False on ANY
    mismatch or missing asset.
    """
    rel = release or _PBS_RELEASE
    ver = version or _PBS_PY
    pins = PINNED if pinned is None else pinned
    if sums_text is None:
        sums_text = (fetch or _fetch_sha256sums)(rel)
    sums = _parse_sha256sums(sums_text)
    results: list[dict] = []
    ok = True
    for key, spec in pins.items():
        fname = _asset_filename(spec)
        problems: list[str] = []
        if f"cpython-{ver}+{rel}-" not in fname:
            problems.append("release_or_version_mismatch")
        if str(spec.get("version")) != str(ver):
            problems.append(f"spec_version!={ver}")
        upstream = sums.get(fname)
        if upstream is None:
            problems.append("missing_upstream_asset")
        elif upstream != str(spec.get("sha256", "")).lower():
            problems.append(f"sha_mismatch:upstream={upstream}")
        if problems:
            ok = False
        results.append(
            {
                "platform": key,
                "asset": fname,
                "ok": not problems,
                "problems": problems,
                "upstream_sha": upstream,
            },
        )
    return {"ok": ok, "release": rel, "version": ver, "checked": len(results), "results": results}


# ── hashing + manifest ───────────────────────────────────────────────────
def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def runtime_fingerprint(python_path: str | None) -> str | None:
    """A re-checkable fingerprint of the interpreter executable: sha256 of its
    bytes + size. Recorded by the provisioner at verified-install time and
    recomputed when a manifest shortcut is offered, so a swapped/tampered
    interpreter can't ride a stale manifest past verification. ``None`` if the
    executable is missing/unreadable (⇒ no shortcut, fresh verify).
    """
    if not python_path:
        return None
    p = Path(python_path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        return f"sha256:{sha256_file(p)}:{size}"
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(home: Path | str) -> dict | None:
    p = manifest_path(home)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_manifest(home: Path | str, data: dict) -> None:
    _atomic_write(manifest_path(home), json.dumps(data, indent=2) + "\n")


# ── verification ─────────────────────────────────────────────────────────
_VERIFY_SNIPPET = (
    "import json;"
    "import aidocs_mcp as m;"
    "print(json.dumps({'version': getattr(m, '__version__', None)}))"
)


def _default_runner(argv: list[str], timeout: int = 25) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:  # noqa: BLE001 — surfaced truthfully to caller
        return 127, "", repr(exc)


def verify_interpreter(
    python_path: str,
    *,
    runner: RuntimeRunner | None = None,
    expected_version: str | None = None,
) -> dict:
    """Run a tiny probe under ``python_path`` proving it can import
    ``aidocs_mcp`` and reporting its version. Fail-closed: any error → not ok.
    Returns {ok, imports, version, reason}.
    """
    run = runner or (lambda a: _default_runner(a))
    if not python_path:
        return {"ok": False, "imports": False, "version": None, "reason": "no_interpreter"}
    code, out, err = run([python_path, "-c", _VERIFY_SNIPPET])
    if code != 0:
        return {
            "ok": False,
            "imports": False,
            "version": None,
            "reason": f"import_failed:{(err or out).strip()[:200]}",
        }
    version = None
    try:
        version = (json.loads(out.strip().splitlines()[-1]) or {}).get("version")
    except Exception:
        return {"ok": False, "imports": True, "version": None, "reason": "probe_unparseable"}
    if expected_version is not None and str(version) != str(expected_version):
        return {
            "ok": False,
            "imports": True,
            "version": version,
            "reason": f"version_mismatch:{version}!={expected_version}",
        }
    return {"ok": True, "imports": True, "version": version, "reason": ""}


# ── resolution (tiered, verified, fail-closed) ───────────────────────────
_USABLE_OWNED = ("operator_pin", "standalone", "venv")


def resolve_runtime(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    runner: RuntimeRunner | None = None,
    verify: bool = True,
    allow_ambient: bool = False,
    expected_version: str | None = None,
) -> dict:
    """Resolve the interpreter enforcement should run under. Walks tiers best →
    worst, VERIFYING each (must import aidocs_mcp) and returning the first that
    passes. Ambient is returned only when ``allow_ambient`` (explicit
    degraded/dev escape); otherwise an unverifiable owned tier yields
    ``tier='none'`` so callers fail closed. Always truthful: ``owned`` is True
    only for operator_pin/standalone/venv, ``degraded`` flags the venv/ambient
    paths, ``checked`` records every tier we tried and why it was rejected.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    checked: list[dict] = []

    def _try(path: str | None, tier: str, source: str) -> dict | None:
        if not path or not Path(path).is_file():
            checked.append({"tier": tier, "source": source, "reason": "missing"})
            return None
        v = (
            verify_interpreter(path, runner=runner, expected_version=expected_version)
            if verify
            else {"ok": True, "imports": True, "version": None, "reason": ""}
        )
        if not v["ok"]:
            checked.append({"tier": tier, "source": source, "reason": v["reason"]})
            return None
        return {
            "path": path,
            "tier": tier,
            "source": source,
            "owned": tier in _USABLE_OWNED,
            "verified": bool(verify and v["ok"]),
            "degraded": tier in ("venv", "ambient"),
            "version": v.get("version"),
            "reason": "",
            "checked": checked,
        }

    pinned = str(e.get("AIDOCS_PYTHON") or "").strip()
    # A standalone lives under runtime/cpython (provision target); also accept a
    # python placed directly under ~/.aidocs/runtime as an owned standalone.
    sa = standalone_python(base) or _python_in(runtime_root(base))
    for path, tier, source in (
        (pinned or None, "operator_pin", "env"),
        (sa, "standalone", "owned_runtime"),
        (venv_python(base), "venv", "owned_runtime"),
    ):
        hit = _try(path, tier, source)
        if hit is not None:
            return hit

    if allow_ambient:
        v = (
            verify_interpreter(sys.executable, runner=runner)
            if verify
            else {"ok": True, "version": None}
        )
        return {
            "path": sys.executable,
            "tier": "ambient",
            "source": "ambient",
            "owned": False,
            "verified": bool(verify and v.get("ok")),
            "degraded": True,
            "version": v.get("version"),
            "reason": "ambient_escape",
            "checked": checked,
        }
    # Fail closed: no verified owned runtime, ambient not permitted.
    return {
        "path": None,
        "tier": "none",
        "source": "none",
        "owned": False,
        "verified": False,
        "degraded": False,
        "reason": "no_verified_owned_runtime",
        "checked": checked,
    }


# Fields the provisioner MUST have written for a manifest to vouch for a runtime
# without a fresh subprocess verify. A hand-written/partial manifest lacks these
# and is rejected → fresh verify.
_MANIFEST_REQUIRED = (
    "verified",
    "verified_at",
    "tier",
    "kind",
    "package",
    "expected_version",
    "python",
    "fingerprint",
)


def manifest_vouches(
    manifest: dict | None,
    python_path: str,
    expected_version: str | None,
) -> tuple[bool, str]:
    """A manifest may shortcut verification ONLY if the provisioner wrote it with
    explicit proof AND it still matches reality:
      * ``verified is True`` and all required provenance fields present,
      * recorded ``python`` is exactly this interpreter,
      * recorded ``version`` matches the expected law version (when one is
        required),
      * the recorded executable ``fingerprint`` still recomputes equal (no
        swap/tamper since the verified install).
    Returns (ok, reason). Anything missing/mismatched ⇒ (False, why).
    """
    if not isinstance(manifest, dict):
        return False, "no_manifest"
    if manifest.get("verified") is not True:
        return False, "manifest_not_verified"
    for key in _MANIFEST_REQUIRED:
        if manifest.get(key) in (None, ""):
            return False, f"manifest_missing:{key}"
    if str(manifest.get("python")) != str(python_path):
        return False, "manifest_python_mismatch"
    if expected_version is not None and str(manifest.get("version")) != str(expected_version):
        return False, "manifest_version_mismatch"
    fp = runtime_fingerprint(python_path)
    if not fp or fp != manifest.get("fingerprint"):
        return False, "fingerprint_mismatch"
    return True, ""


def owned_runtime_trust(
    python_path: str,
    home: Path | str,
    *,
    runner: RuntimeRunner | None = None,
    expected_version: str | None = None,
) -> dict:
    """Decide whether an OWNED-LOOKING interpreter may be trusted to carry
    enforcement. Trust requires EITHER a provisioner-written, still-valid
    verified manifest (see ``manifest_vouches`` — lets us skip a subprocess on
    the hot path) OR a fresh verification proving it imports the EXPECTED
    aidocs_mcp version (not just any aidocs_mcp). A hand-written, stale, or
    wrong-fingerprint manifest CANNOT bypass the fresh verify.
    Returns {ok, basis, version, reason}; ok=False ⇒ hooks must NOT pin to it.
    """
    m = read_manifest(home)
    vouch, why = manifest_vouches(m, python_path, expected_version)
    if vouch:
        return {"ok": True, "basis": "manifest", "version": m.get("version"), "reason": ""}
    v = verify_interpreter(python_path, runner=runner, expected_version=expected_version)
    if v["ok"]:
        return {"ok": True, "basis": "fresh_verify", "version": v.get("version"), "reason": ""}
    return {"ok": False, "basis": "none", "version": v.get("version"), "reason": v["reason"] or why}


# ── provisioning ─────────────────────────────────────────────────────────
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def provision_venv(
    home: Path | str,
    *,
    base_python: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Build/repair a dedicated AIDOCS-managed venv (degraded Tier-1) from a
    suitable base python and install the INTENDED aidocs_mcp version (or a local
    wheel/source) into it — never a floating ``--upgrade``. Verifies the
    EXPECTED version after install. Idempotent: a still-valid venv is a no-op
    unless ``force``. Fail-closed + truthful report.
    """
    base = Path(home)
    run = runner or (lambda a: _default_runner(a))
    target, enforce = _resolve_package(package_spec, expected_version)
    existing = venv_python(base)
    if existing and not force:
        v = verify_interpreter(existing, runner=runner, expected_version=enforce)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "venv",
                "action": "noop",
                "python": existing,
                "version": v.get("version"),
                "package": target,
                "degraded": True,
                "reason": "",
            }
    src = base_python or sys.executable
    code, out, err = run([src, "-m", "venv", str(_venv_dir(base))])
    if code != 0:
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": f"venv_create:{(err or out).strip()[:200]}",
            "degraded": True,
        }
    py = venv_python(base)
    if not py:
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": "venv_python_missing",
            "degraded": True,
        }
    # Pin the intended version/artifact — no floating --upgrade.
    code, out, err = run([py, "-m", "pip", "install", target])
    if code != 0:
        return {
            "ok": False,
            "tier": "venv",
            "action": "install_failed",
            "reason": f"pip_install:{(err or out).strip()[:200]}",
            "python": py,
            "package": target,
            "degraded": True,
        }
    v = verify_interpreter(py, runner=runner, expected_version=enforce)
    if not v["ok"]:
        return {
            "ok": False,
            "tier": "venv",
            "action": "verify_failed",
            "reason": v["reason"],
            "python": py,
            "package": target,
            "degraded": True,
        }
    _write_manifest(
        base,
        {
            "tier": "venv",
            "kind": "venv",
            "python": py,
            "version": v.get("version"),
            "expected_version": enforce or v.get("version"),
            "base_python": src,
            "package": target,
            # A venv is a degraded fallback over an arbitrary base python — never the
            # blessed standalone, so always CUSTOM provenance.
            "source": "managed_venv",
            "blessed": False,
            "provenance_class": "custom",
            "installed_at": _now(),
            "verified": True,
            "verified_at": _now(),
            "fingerprint": runtime_fingerprint(py),
        },
    )
    return {
        "ok": True,
        "tier": "venv",
        "action": "installed",
        "python": py,
        "version": v.get("version"),
        "package": target,
        "degraded": True,
        "blessed": False,
        "provenance_class": "custom",
        "reason": "",
    }


def _default_extractor(archive: Path, dest: Path) -> None:
    import shutil

    shutil.unpack_archive(str(archive), str(dest))


def provision_standalone(
    home: Path | str,
    *,
    spec: dict | None = None,
    offline_archive: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Install a pinned standalone CPython atomically and put aidocs_mcp in it.

    Order, all fail-closed:
      1. Resolve spec (arg → PINNED[platform]); none AND no offline archive →
         honest degrade (caller falls back to venv).
      2. Acquire the archive (offline path, else download the pinned URL).
      3. SHA256-verify against the pin. MISMATCH → abort, install NOTHING.
      4. Extract to a staging dir, then os.replace into place (atomic swap).
      5. Install aidocs_mcp into it; verify it imports. Failure → roll back the
         staged tree, leave any prior runtime untouched.
      6. Write the manifest (tier=standalone) only on full success.
    Idempotent: a verified standalone already present is a no-op unless force.
    """
    base = Path(home)
    operator_spec = spec is not None  # caller supplied their own pin
    spec = dict(spec) if spec else pinned_spec()
    pkg, enforce = _resolve_package(package_spec, expected_version)
    # OFFICIAL/BLESSED only when installed from OUR PINNED table at the blessed
    # version with no operator override. An operator offline archive or explicit
    # spec is preserved but recorded as CUSTOM provenance.
    blessed = bool(
        not operator_spec
        and not offline_archive
        and is_blessed_version((spec or {}).get("version")),
    )
    prov_class = "official" if blessed else "custom"
    existing = standalone_python(base)
    if existing and not force:
        v = verify_interpreter(existing, runner=runner, expected_version=enforce)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "standalone",
                "action": "noop",
                "python": existing,
                "version": v.get("version"),
                "package": pkg,
                "owned": True,
                "blessed": blessed,
                "provenance_class": prov_class,
                "reason": "",
            }
    expected_sha = str((spec or {}).get("sha256") or "").strip().lower()
    url = str((spec or {}).get("url") or "").strip()
    if not offline_archive and not url:
        return {
            "ok": False,
            "tier": "standalone",
            "action": "no_pin",
            "reason": "no_pin_for_platform",
            "degraded_to": "venv",
        }
    if not expected_sha:
        return {
            "ok": False,
            "tier": "standalone",
            "action": "refused",
            "reason": "no_pinned_sha256",
            "degraded_to": "venv",
        }

    # Stage on the SAME filesystem as the target so the os.replace swap below is
    # atomic (and not a cross-disk move). The system temp dir is often on a
    # different drive than ~/.aidocs/runtime (e.g. C: temp vs a D: home), which
    # makes os.replace(staging, target) raise WinError 17 / cross-device EXDEV.
    rroot = runtime_root(base)
    rroot.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".aidocs-rt-stage-", dir=rroot))
    try:
        archive = Path(offline_archive) if offline_archive else work / "dist"
        if offline_archive:
            if not archive.is_file():
                return {
                    "ok": False,
                    "tier": "standalone",
                    "action": "archive_missing",
                    "reason": f"offline_archive_not_found:{archive}",
                }
        else:
            dl = downloader or _default_download
            dl(url, archive)
        actual = sha256_file(archive)
        if actual != expected_sha:
            # FAIL CLOSED: never install an archive whose hash we can't vouch.
            return {
                "ok": False,
                "tier": "standalone",
                "action": "checksum_mismatch",
                "reason": f"sha256 {actual} != pinned {expected_sha}",
            }
        staging = work / "stage"
        staging.mkdir(parents=True, exist_ok=True)
        (extractor or _default_extractor)(archive, staging)
        target = _standalone_dir(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_name(f"cpython.old-{int(time.time())}")
            os.replace(target, backup)
        else:
            backup = None
        os.replace(staging, target)
        py = standalone_python(base)
        try:
            if installer is not None:
                ires = installer(py or "")
                if not (ires or {}).get("ok", True):
                    raise RuntimeError(str(ires.get("reason")))
            elif py:
                code, out, err = (runner or (lambda a: _default_runner(a)))(
                    [py, "-m", "pip", "install", pkg],
                )
                if code != 0:
                    raise RuntimeError(f"pip_install:{(err or out)[:200]}")
            v = verify_interpreter(py or "", runner=runner, expected_version=enforce)
            if not v["ok"]:
                raise RuntimeError(v["reason"])
        except Exception as exc:  # rollback the staged tree, restore prior
            try:
                if target.exists():
                    import shutil

                    shutil.rmtree(target, ignore_errors=True)
                if backup is not None:
                    os.replace(backup, target)
            except OSError:
                pass
            return {
                "ok": False,
                "tier": "standalone",
                "action": "verify_failed",
                "reason": str(exc),
                "python": py,
            }
        if backup is not None:
            import shutil

            shutil.rmtree(backup, ignore_errors=True)
        _write_manifest(
            base,
            {
                "tier": "standalone",
                "kind": "standalone",
                "python": py,
                "version": v.get("version"),
                "expected_version": enforce or v.get("version"),
                "url": url or "(offline)",
                "sha256": expected_sha,
                "package": pkg,
                "source": "offline_archive" if offline_archive else "download",
                "blessed": blessed,
                "provenance_class": prov_class,
                "installed_at": _now(),
                "verified": True,
                "verified_at": _now(),
                "fingerprint": runtime_fingerprint(py),
            },
        )
        return {
            "ok": True,
            "tier": "standalone",
            "action": "installed",
            "python": py,
            "version": v.get("version"),
            "package": pkg,
            "owned": True,
            "blessed": blessed,
            "provenance_class": prov_class,
            "reason": "",
        }
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def _default_download(url: str, dest: Path) -> None:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)


def spec_from_operator(
    *,
    offline_archive: str | None = None,
    sha256: str | None = None,
    url: str | None = None,
    manifest_file: str | None = None,
) -> dict | None:
    """Build a standalone install spec from explicit OPERATOR input for a
    platform PINNED has no entry for. Either a manifest JSON ({url?, sha256}) or
    an explicit --sha256 (+ optional --url). Returns None when nothing usable was
    given, so the caller still fails closed (no SHA ⇒ refused downstream).
    """
    if manifest_file:
        try:
            data = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict) and data.get("sha256"):
            return {
                "url": str(data.get("url") or url or "(offline)"),
                "sha256": str(data["sha256"]).strip().lower(),
            }
        return None
    if sha256:
        return {
            "url": str(url or "(offline)") if (url or offline_archive) else "",
            "sha256": str(sha256).strip().lower(),
        }
    return None


def ensure_runtime(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    spec: dict | None = None,
    offline_archive: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    base_python: str | None = None,
    allow_venv_fallback: bool = True,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Provision the best available owned runtime: standalone first, then a
    degraded venv. Honest: reports which tier was achieved and why standalone
    was skipped (no pin / checksum / verify). Operator-pinned AIDOCS_PYTHON is
    respected by resolution and never overwritten by provisioning.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    # Operator pin wins — don't provision over an explicit, verified pin.
    pin = str(e.get("AIDOCS_PYTHON") or "").strip()
    if pin and Path(pin).is_file():
        v = verify_interpreter(pin, runner=runner, expected_version=expected_version)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "operator_pin",
                "action": "respected",
                "python": pin,
                "version": v.get("version"),
                "owned": True,
                "reason": "",
            }
    sa = provision_standalone(
        base,
        spec=spec,
        offline_archive=offline_archive,
        package_spec=package_spec,
        expected_version=expected_version,
        downloader=downloader,
        extractor=extractor,
        installer=installer,
        runner=runner,
        force=force,
    )
    if sa.get("ok"):
        return sa
    if not allow_venv_fallback:
        return sa
    ve = provision_venv(
        base,
        base_python=base_python,
        package_spec=package_spec,
        expected_version=expected_version,
        runner=runner,
        force=force,
    )
    ve["standalone_skipped"] = sa.get("reason") or sa.get("action")
    return ve


def prepare_owned_runtime_for_setup(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    expected_version: str | None = "__current__",
    base_python: str | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
) -> dict:
    """Setup helper: resolve an owned runtime VERIFYING the EXPECTED aidocs_mcp
    law version, and PROVISION one (pinned to that version) when none verifies —
    so drift is caught at provision/resolve time, not left for hook verification.
    Never installs hooks and never escapes to ambient; the caller decides that.
    Returns {ok, tier, python, blessed, provenance_class, expected_version,
    provisioned, provision_result}.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    exp = expected_aidocs_version() if expected_version == "__current__" else expected_version
    rt = resolve_runtime(base, e, runner=runner, allow_ambient=False, expected_version=exp)
    provisioned: dict | None = None
    if rt.get("tier") == "none":
        provisioned = ensure_runtime(
            base,
            e,
            expected_version=exp,
            base_python=base_python,
            downloader=downloader,
            extractor=extractor,
            installer=installer,
            runner=runner,
        )
        rt = resolve_runtime(base, e, runner=runner, allow_ambient=False, expected_version=exp)
    manifest = read_manifest(base)
    blessed = bool(
        rt.get("tier") == "standalone"
        and rt.get("owned")
        and manifest
        and manifest.get("blessed") is True
        and str(manifest.get("python")) == str(rt.get("path"))
        and is_blessed_version(rt.get("version")),
    )
    return {
        "ok": rt.get("tier") != "none",
        "tier": rt.get("tier"),
        "python": rt.get("path"),
        "version": rt.get("version"),
        "blessed": blessed,
        "provenance_class": ("official" if blessed else "custom" if rt.get("owned") else "none"),
        "expected_version": exp,
        "provisioned": provisioned is not None,
        "provision_result": provisioned,
    }


# ── doctor / drift repair ────────────────────────────────────────────────
def _provenance(manifest: dict | None) -> dict:
    """A truthful, compact provenance view of how the runtime was obtained."""
    if not manifest:
        return {"source": "none", "kind": None, "version": None}
    return {
        "source": manifest.get("source"),
        "kind": manifest.get("kind"),
        "version": manifest.get("version"),
        "package": manifest.get("package"),
        "sha256": manifest.get("sha256"),
        "url": manifest.get("url"),
        "base_python": manifest.get("base_python"),
        "installed_at": manifest.get("installed_at"),
        "verified": bool(manifest.get("verified")),
        "verified_at": manifest.get("verified_at"),
        "fingerprint": manifest.get("fingerprint"),
        # official (blessed PINNED) vs operator-custom; default custom when an
        # older manifest predates the field.
        "blessed": bool(manifest.get("blessed")),
        "provenance_class": manifest.get("provenance_class")
        or ("official" if manifest.get("blessed") else "custom"),
    }


def doctor(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    fix: bool = False,
    rebuild: bool = False,
    offline_archive: str | None = None,
    base_python: str | None = None,
    spec: dict | None = None,
    sha256: str | None = None,
    url: str | None = None,
    manifest_file: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = "__current__",
    allow_ambient: bool = False,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
) -> dict:
    """Truthful runtime health + repair. ``--check`` (default) reports the
    resolved tier, manifest, drift, and PROVENANCE (source/version/sha/base).
    ``--fix`` provisions an owned runtime if none verifies; ``--rebuild`` forces
    a fresh standalone (then venv) install. Verification proves the EXPECTED
    aidocs_mcp version (this process's, unless overridden). When PINNED has no
    platform entry an operator supplies --offline-archive + --sha256 (or a
    manifest). Exit-code truth: ok iff a verified owned runtime resolves after.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    exp = expected_aidocs_version() if expected_version == "__current__" else expected_version
    if spec is None:
        spec = spec_from_operator(
            offline_archive=offline_archive,
            sha256=sha256,
            url=url,
            manifest_file=manifest_file,
        )
    manifest = read_manifest(base)
    before = resolve_runtime(
        base,
        e,
        runner=runner,
        allow_ambient=allow_ambient,
        expected_version=exp,
    )
    drift = bool(
        manifest
        and manifest.get("python")
        and not (
            verify_interpreter(str(manifest["python"]), runner=runner, expected_version=exp)["ok"]
        ),
    )

    actions: list[dict] = []
    if rebuild or (fix and before.get("tier") in ("none", "ambient")):
        res = ensure_runtime(
            base,
            e,
            spec=spec,
            offline_archive=offline_archive,
            package_spec=package_spec,
            expected_version=exp,
            base_python=base_python,
            downloader=downloader,
            extractor=extractor,
            installer=installer,
            runner=runner,
            force=bool(rebuild),
        )
        actions.append(res)

    after = resolve_runtime(
        base,
        e,
        runner=runner,
        allow_ambient=allow_ambient,
        expected_version=exp,
    )
    manifest = read_manifest(base)
    owned_ok = bool(after.get("owned") and after.get("verified"))
    # BLESSED only when the resolved interpreter IS the manifest's official
    # standalone. operator_pin/venv/offline/ambient are all operator-custom.
    blessed = bool(
        owned_ok
        and after.get("tier") == "standalone"
        and manifest
        and manifest.get("blessed") is True
        and str(manifest.get("python")) == str(after.get("path"))
        and is_blessed_version(after.get("version")),
    )
    provenance_class = "official" if blessed else ("custom" if after.get("owned") else "none")
    report = {
        "ok": owned_ok,
        "tier": after.get("tier"),
        "python": after.get("path"),
        "owned": bool(after.get("owned")),
        "verified": bool(after.get("verified")),
        "degraded": bool(after.get("degraded")),
        "drift_detected": drift,
        "expected_version": exp,
        "resolved_version": after.get("version"),
        "blessed_version": BLESSED_PYTHON,
        "blessed": blessed,
        "provenance_class": provenance_class,
        "provenance": _provenance(manifest),
        "manifest": manifest,
        "checked": after.get("checked", []),
        "actions": actions,
        "mode": "rebuild" if rebuild else ("fix" if fix else "check"),
    }
    # Trusted-code boundary: report the CANONICAL package trust (SQLite first,
    # runtime.json only as a degraded projection — truthfully labelled).
    try:
        from . import package_integrity as _pi

        v = _pi.verify_package_integrity(base)
        report["package_trust"] = {
            k: v.get(k)
            for k in (
                "ok",
                "provenance",
                "status",
                "mutable",
                "drifted",
                "unverified",
                "remote_trustworthy",
                "version",
                "trust_source",
                "stale_projection",
            )
        }
        # End-to-end selected-runtime trust-chain proof (home-side; no project
        # .mcp.json in the doctor context). Truthful trust_unrecorded/degraded.
        report["trust_chain"] = _pi.prove_trust_chain(
            base,
            project_root=None,
            python_path=after.get("path"),
            expected_version=exp,
        )
    except Exception as _exc:  # never break the doctor
        report["package_trust"] = {"ok": None, "error": repr(_exc)}
        report["trust_chain"] = {"ok": None, "error": repr(_exc)}
    if not owned_ok and after.get("tier") == "ambient":
        report["reason"] = (
            "Only ambient sys.executable resolves — enforcement hooks must not "
            "depend on it. Run `aidocs runtime --fix` (or --offline-archive) to "
            "provision an AIDOCS-owned runtime."
        )
    elif not owned_ok:
        report["reason"] = "No verified AIDOCS-owned runtime. Run `aidocs runtime --fix`."
    return report
