"""AIDOCS package integrity — the trusted-code boundary.

The runtime decision (which interpreter) is only half the trust story; the OTHER
half is which CODE that interpreter runs. This module fingerprints the installed
``aidocs_mcp`` law package, records it in the runtime manifest, and lets MCP/hook
startup REFUSE when the installed code has drifted from the verified fingerprint.

Provenance is reported truthfully:
  * ``dev_editable`` — installed ``-e`` / from a source tree. MUTABLE by design,
    so integrity is NOT pinned and the package is NEVER remote-trustworthy.
  * ``official_wheel`` — installed from a wheel (dist-info RECORD, not editable).
    Fingerprint is pinned; drift → refuse; remote-trustworthy when matched.
  * ``custom`` — installed but neither editable nor a clean wheel.

Fail-closed model: an editable/unverified install is allowed to RUN locally
(else dev/first-run bricks) but is never remote-trustworthy; a NON-editable
install whose fingerprint OR version drifts from the manifest is REFUSED at
startup (re-record after a legit upgrade). The version string is folded into the
fingerprint, so version-string spoofing changes the fingerprint → drift.

All inputs are injectable so the law is unit/fuzz-testable offline.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# PROV_OFFICIAL = a non-editable wheel install is *present*. It does NOT mean
# the wheel is an official/trusted CodeNexus release — that requires a trusted
# basis (signature / release manifest), see has_trusted_basis(). Provenance is
# a detection fact; TRUST is a separate, narrower judgment.
PROV_OFFICIAL = "official_wheel"
PROV_CUSTOM = "custom"
PROV_EDITABLE = "dev_editable"

# Files whose CONTENT is part of the trusted-code fingerprint: code + the
# control/seed/data files that change behavior. Explicit EXCLUSIONS (NOT
# fingerprinted, by design): compiled artifacts (*.pyc/*.pyo), __pycache__/,
# and anything not shipped inside the package dir (tests/docs live outside it).
# Broadening past *.py closes the "tamper a bundled .json/.sql seed" gap.
_FP_INCLUDE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",  # code
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",  # control/config
        ".sql",
        ".txt",
        ".csv",
        ".tsv",
        ".sqlite",
        ".db",  # seed/data
    },
)
# ``trust`` holds the signed-release artifacts (manifest/sig/pubkey). They are
# EXCLUDED so the manifest can certify the code's fingerprint without the
# fingerprint depending on the manifest that contains it (self-reference).
_FP_EXCLUDE_DIRS = frozenset({"__pycache__", "trust"})
_FP_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})


def package_dir() -> Path:
    import aidocs_mcp

    return Path(aidocs_mcp.__file__).resolve().parent


def package_version() -> str | None:
    try:
        import aidocs_mcp

        return getattr(aidocs_mcp, "__version__", None)
    except Exception:
        return None


def compute_package_fingerprint(
    pkg_dir: Path | str | None = None,
    *,
    version: str | None = None,
) -> dict:
    """Deterministic sha256 over the package's code AND control/seed/data files
    (sorted relpath + per-file content digest) with the version string folded
    in. A tampered law file, a tampered bundled data/seed/control file, OR a
    spoofed version string all change the fingerprint. See _FP_INCLUDE_SUFFIXES
    for the included set and the documented exclusions.
    """
    base = Path(pkg_dir) if pkg_dir else package_dir()
    ver = version if version is not None else package_version()
    h = hashlib.sha256()
    h.update(b"aidocs-pkg-fp-v2\0")  # domain-separate from v1
    h.update((ver or "").encode("utf-8"))
    h.update(b"\0")
    code_n = 0
    data_n = 0
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _FP_EXCLUDE_DIRS for part in f.parts):
            continue
        suf = f.suffix.lower()
        if suf in _FP_EXCLUDE_SUFFIXES or suf not in _FP_INCLUDE_SUFFIXES:
            continue
        rel = f.relative_to(base).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            data = f.read_bytes()
        except OSError:
            data = b""
        h.update(hashlib.sha256(data).digest())
        if suf in (".py", ".pyi"):
            code_n += 1
        else:
            data_n += 1
    return {
        "fingerprint": "sha256:" + h.hexdigest(),
        "file_count": code_n + data_n,
        "code_files": code_n,
        "data_files": data_n,
        "version": ver,
        "method": "module_digest_v2",
    }


def _read_direct_url_editable(dist_name: str = "aidocs_mcp") -> bool | None:
    try:
        import importlib.metadata as md

        du = md.distribution(dist_name).read_text("direct_url.json")
        if not du:
            return None
        return bool(json.loads(du).get("dir_info", {}).get("editable"))
    except Exception:
        return None


def _has_record(dist_name: str = "aidocs_mcp") -> bool:
    try:
        import importlib.metadata as md

        return bool(md.distribution(dist_name).files)
    except Exception:
        return False


def detect_provenance(
    pkg_dir: Path | str | None = None,
    *,
    editable: bool | None = None,
    has_record: bool | None = None,
    pyproject_present: bool | None = None,
) -> str:
    """official_wheel | custom | dev_editable (truthful). Editable wins — it is
    the only mutable, untrusted case.
    """
    ed = editable if editable is not None else _read_direct_url_editable()
    if ed:
        return PROV_EDITABLE
    # source-tree fallback: a pyproject within a few parents ⟹ editable/source.
    if pyproject_present is None:
        base = Path(pkg_dir) if pkg_dir else package_dir()
        pyproject_present = any(
            (up / "pyproject.toml").is_file() for up in (base, *list(base.parents)[:4])
        )
    if pyproject_present:
        # A pyproject within a few parents is the normal dev-checkout signal — BUT
        # a DEPLOYED SIGNED RELEASE is also a copied source tree that ships
        # pyproject (for its build) and is run from source (e.g. PYTHONPATH).
        # Per this module's doctrine the root of trust is the SIGNED RELEASE, not
        # the install method (see status_for_provenance), so a source tree whose
        # signed manifest VERIFIES against the running code is NOT a mutable dev
        # checkout. A TRUE `pip install -e` marker already returned above; this
        # only relaxes the heuristic case. Cheap gate first — a dev checkout ships
        # no signed manifest, so it stays editable WITHOUT paying verify_release
        # (preserving the startup/hook budget); only a present manifest is verified.
        _base = Path(pkg_dir) if pkg_dir else package_dir()
        _signed = (_base / "trust" / "release_manifest.json").is_file() and has_trusted_basis(
            pkg_dir=pkg_dir,
        )
        if not _signed:
            return PROV_EDITABLE
    rec = has_record if has_record is not None else _has_record()
    return PROV_OFFICIAL if rec else PROV_CUSTOM


def has_trusted_basis(
    *,
    signature: str | None = None,
    release_manifest: str | None = None,
    pkg_dir: Path | str | None = None,
) -> bool:
    """Whether a NON-editable install has a real basis to be trusted as an
    official CodeNexus release. The root of trust is a SIGNED RELEASE: a pinned
    Ed25519 public key + a builder-signed manifest whose fingerprint matches the
    running code (see ``release_trust.verify_release``). Mere wheel presence
    never auto-blesses; an unsigned / tampered / stale / wrong-key install
    yields False — the deliberate "remote stays suspicious" floor.

    An explicit ``signature``/``release_manifest`` argument is an injected basis
    used by tests/callers that have already verified out-of-band; it short-
    circuits to True without re-reading the on-disk artifacts.
    """
    if signature or release_manifest:
        return True
    try:
        from .release_trust import verify_release

        return bool(verify_release(pkg_dir).ok)
    except Exception:
        # Verification machinery itself unavailable/erroring ⟹ no basis. Fail
        # closed: an error never becomes trust.
        return False


def status_for_provenance(prov: str, *, trusted_basis: bool = False) -> str:
    """Trust status. Provenance is detection; status is trust. The root of trust
    is the signed release (``trusted_basis``), NOT the pip install method — so a
    NON-editable install with a verified signed basis is `blessed` whether it is
    a wheel or a custom/copied tree, while a wheel WITHOUT a basis stays
    `wheel_unverified`. Editable is always `dev` (mutable, never trustable).
    """
    if prov == PROV_EDITABLE:
        return "dev"
    if trusted_basis:
        return "blessed"
    return "wheel_unverified" if prov == PROV_OFFICIAL else "custom"


def _interpreter_fingerprint() -> str:
    """Executable-bytes fingerprint of the RUNNING interpreter, via the existing
    provisioner hash (sha256 of the binary + size), so a swapped/tampered
    interpreter is detectable against the recorded DB trust row. Falls back to a
    clearly-weaker path+build hash only if the executable is unreadable.
    """
    import sys

    try:
        from .runtime_provisioner import runtime_fingerprint

        fp = runtime_fingerprint(sys.executable)
        if fp:
            return fp
    except Exception:
        pass
    h = hashlib.sha256()
    h.update((sys.executable or "").encode("utf-8"))
    h.update(b"\0")
    h.update((sys.version or "").encode("utf-8"))
    return "sha256-weak:" + h.hexdigest()


def _resolve_trust(
    home: Path | str | None,
    *,
    manifest: dict | None,
    trust: dict | None,
    trust_source: str | None,
) -> tuple[str | None, str | None, str, bool, dict | None]:
    """Resolve the authoritative stored trust DB-FIRST, with runtime.json as a
    degraded projection only. Returns
    (stored_fingerprint, stored_version, source, stale_projection, record).

    source ∈ {"injected", "db", "runtime_json_degraded", "none"}. When the DB
    and the runtime.json projection disagree, the DB wins and stale_projection
    is True (the file is a stale cache, truthfully reported).
    """
    if trust is not None:
        return (
            trust.get("package_fingerprint"),
            trust.get("package_version"),
            trust_source or "injected",
            False,
            trust,
        )
    db_rec = None
    if home is not None:
        try:
            from .runtime_trust_store import RuntimeTrustStore

            db_rec = RuntimeTrustStore(home).current()
        except Exception:
            db_rec = None
    if manifest is not None:
        json_m = manifest
    elif home is not None:
        from .runtime_provisioner import read_manifest

        json_m = read_manifest(home) or {}
    else:
        json_m = {}
    json_fp = json_m.get("package_fingerprint")
    json_ver = json_m.get("package_version")
    if db_rec and db_rec.get("package_fingerprint"):
        stale = bool(
            json_fp
            and (
                json_fp != db_rec["package_fingerprint"]
                or str(json_ver) != str(db_rec.get("package_version"))
            ),
        )
        return (db_rec["package_fingerprint"], db_rec.get("package_version"), "db", stale, db_rec)
    if json_fp:
        return (json_fp, json_ver, "runtime_json_degraded", False, None)
    return (None, None, "none", False, None)


def verify_package_integrity(
    home: Path | str | None = None,
    *,
    manifest: dict | None = None,
    current: dict | None = None,
    provenance: str | None = None,
    trust: dict | None = None,
    trust_source: str | None = None,
    interpreter_fp: str | None = None,
) -> dict:
    """Compare the CURRENT installed package + interpreter against the
    AUTHORITATIVE recorded trust. The canonical SQLite DB is the ONLY source
    that can grant remote trust; runtime.json is a projection (allows local
    startup, never remote). Returns {ok, provenance, status, mutable, drifted,
    interpreter_drift, unverified, remote_trustworthy, version, trust_source,
    stale_projection, reason}.
    """
    prov = provenance if provenance is not None else detect_provenance()

    # Editable is mutable by definition — short-circuit BEFORE the (relatively
    # expensive) full-package hash AND before any trust lookup, so dev file
    # edits never lock out local MCP/hooks and the hook budget is honored.
    # Doctrine: dev stays alive; remote stays suspicious.
    if prov == PROV_EDITABLE:
        return {
            "provenance": prov,
            "status": "dev",
            "version": package_version(),
            "ok": True,
            "mutable": True,
            "drifted": False,
            "interpreter_drift": False,
            "unverified": False,
            "remote_trustworthy": False,
            "trust_source": "live_provenance",
            "stale_projection": False,
            "reason": (
                "editable/dev install — MUTABLE; integrity not pinned; never remote-trustworthy"
            ),
        }
    # Resolve the stored trust FIRST (cheap: a SQLite read / file read) before
    # computing the expensive full-package fingerprint. When nothing is stored
    # to compare against, the current fingerprint cannot change the verdict
    # (unverified ⇒ local-ok, remote-False either way), so we skip the hash
    # entirely. This is law-preserving — the verdict is byte-identical — and it
    # makes the Outer Gate refusal path (which calls verify without a home, so
    # never has a stored row) and every first-run/degraded check faster.
    # Doctrine: faster refusal is good; this never turns unknown into allowed.
    stored, stored_ver, source, stale, rec = _resolve_trust(
        home,
        manifest=manifest,
        trust=trust,
        trust_source=trust_source,
    )
    if not stored:
        ver = (current or {}).get("version") if current is not None else package_version()
        return {
            "provenance": prov,
            "status": status_for_provenance(prov),
            "version": ver,
            "ok": True,
            "mutable": False,
            "drifted": False,
            "interpreter_drift": False,
            "unverified": True,
            "remote_trustworthy": False,
            "trust_source": source,
            "stale_projection": stale,
            "reason": (
                "no recorded package trust — unverified; run `aidocs runtime --record-package`"
            ),
        }
    # Something IS stored → we must hash the package to compare for drift.
    cur = current if current is not None else compute_package_fingerprint()
    base = {
        "provenance": prov,
        "status": status_for_provenance(prov),
        "version": cur.get("version"),
    }
    pkg_drift = cur["fingerprint"] != stored or str(cur.get("version")) != str(stored_ver)
    # Interpreter drift: only meaningful when the authoritative row carries an
    # interpreter fingerprint. Computed lazily so we never hash the interpreter
    # binary when there is nothing to compare against (hook-budget friendly).
    stored_interp = (rec or {}).get("interpreter_fingerprint")
    interp_drift = False
    if stored_interp:
        cur_interp = interpreter_fp if interpreter_fp is not None else _interpreter_fingerprint()
        interp_drift = bool(cur_interp and cur_interp != stored_interp)
    drifted = pkg_drift or interp_drift
    # ONLY the DB (source=="db") can grant remote trust, and only by an explicit
    # remote_trustworthy flag on the matched row. runtime.json (degraded) and
    # any drift can never make a package remote-trustworthy.
    remote = source == "db" and not drifted and bool((rec or {}).get("remote_trustworthy"))
    src_note = (
        "the canonical trust DB"
        if source == "db"
        else "the runtime.json projection (DB unavailable — degraded)"
    )
    if pkg_drift:
        reason = (
            f"package drift: installed law/data/version differ from "
            f"{src_note} — refusing (re-record via `aidocs runtime "
            f"--record-package` after a legitimate upgrade)"
        )
    elif interp_drift:
        reason = (
            "interpreter drift: running interpreter executable differs "
            "from the recorded trust row — refusing (swapped/tampered "
            "interpreter)"
        )
    elif stale:
        reason = "stale runtime.json projection (DB is authoritative)"
    else:
        reason = ""
    return {
        **base,
        "ok": (not drifted),
        "mutable": False,
        "drifted": drifted,
        "interpreter_drift": interp_drift,
        "unverified": False,
        "remote_trustworthy": remote,
        "trust_source": source,
        "stale_projection": stale,
        "reason": reason,
    }


def startup_integrity_gate(home: Path | str | None = None, **kw) -> dict:
    """The fail-closed check MCP/hook startup calls. ok=False ONLY on proven
    drift of a non-editable install; editable/unverified are allowed to run
    locally (but are not remote-trustworthy).
    """
    v = verify_package_integrity(home, **kw)
    return {**v, "ok": (not v.get("drifted"))}


def package_integrity_fields() -> dict:
    """The projection fields written into runtime.json (a CACHE — never the
    authority). The canonical copy lives in the runtime_trust DB. remote trust
    is only granted with a trusted basis (signature/release manifest), which we
    do not have yet — so a plain wheel records remote_trustworthy=False.
    """
    cur = compute_package_fingerprint()
    prov = detect_provenance()
    basis = has_trusted_basis()
    return {
        "package_fingerprint": cur["fingerprint"],
        "package_version": cur.get("version"),
        "package_provenance": prov,
        "package_status": status_for_provenance(prov, trusted_basis=basis),
        # The signed release is the root of trust, not the pip install method: a
        # NON-editable install whose signed manifest matches the running code is
        # remote-trustworthy (wheel OR copied tree). Editable is never trusted.
        "package_remote_trustworthy": (prov != PROV_EDITABLE and basis),
        "package_interpreter_fingerprint": _interpreter_fingerprint(),
        "package_files": cur["file_count"],
        "package_data_files": cur.get("data_files", 0),
        "package_mutable": (prov == PROV_EDITABLE),
        "package_recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def record_package_integrity(
    home: Path | str | None = None,
    *,
    source: str = "cli",
    actor: str | None = None,
    note: str | None = None,
) -> dict:
    """Record the current package/interpreter trust. CANONICAL write goes to the
    runtime_trust SQLite DB (an immutable, audited row); runtime.json is then
    refreshed as a PROJECTION/cache only. Returns the projection dict (for the
    CLI display + back-compat) augmented with the canonical record metadata.
    """
    from .runtime_provisioner import _write_manifest, read_manifest

    base = Path(home) if home else Path.home()
    fields = package_integrity_fields()

    # 1) Canonical authority: append an audited trust row to SQLite.
    rec: dict | None = None
    try:
        from .runtime_trust_store import RuntimeTrustStore

        rec = RuntimeTrustStore(base).record(
            {
                "interpreter_fingerprint": fields["package_interpreter_fingerprint"],
                "package_fingerprint": fields["package_fingerprint"],
                "package_version": fields["package_version"],
                "provenance": fields["package_provenance"],
                "status": fields["package_status"],
                "remote_trustworthy": fields["package_remote_trustworthy"],
            },
            source=source,
            actor=actor,
            note=note,
            payload={"files": fields["package_files"]},
        )
    except Exception:
        rec = None

    # 2) Projection/cache: runtime.json. Truthful about whether the canonical DB
    # write actually happened: if the DB write FAILED, the file is labelled
    # `projection_failed` (NOT `projection_of_db`) so nothing downstream mistakes
    # an authority-less file for a real recorded trust. Doctrine: a file may
    # project authority, it does not become authority.
    m = read_manifest(base) or {}
    m.update(fields)
    if rec is not None:
        m["package_trust_source"] = "projection_of_db"
        m["package_trust_db_recorded_at"] = rec.get("recorded_at")
        m["package_trust_db_id"] = rec.get("id")
    else:
        m["package_trust_source"] = "projection_failed"
        m["package_trust_db_recorded_at"] = None
        m["package_trust_db_id"] = None
        m["package_status"] = "unverified"  # no DB row ⇒ not recorded trust
    try:
        _write_manifest(base, m)
    except Exception:
        pass
    m["canonical_record"] = rec
    m["recorded"] = rec is not None
    return m


def _default_subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    import subprocess

    p = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    return p.returncode, p.stdout or "", p.stderr or ""


def _same_interpreter(a: str | None, b: str | None) -> bool:
    import os

    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return str(a) == str(b)


def record_selected_interpreter_trust(
    home: Path | str | None,
    python_path: str | None,
    *,
    source: str = "setup",
    runner=None,
) -> dict:
    """Record trust for the SELECTED interpreter that host adapters (.mcp.json /
    Claude hooks / Codex hooks) will actually launch — NOT necessarily the
    Python process running setup. This closes the setup split-brain that left a
    trust row fingerprinting setup's ambient interpreter while the hosts launch
    a different owned runtime (→ interpreter_drift on first MCP start).

    * selected == current process → record in-process.
    * selected != current process → invoke the SELECTED runtime to record ITS
      OWN interpreter+package (`python -m aidocs_mcp.cli runtime
      --record-package --source <source>`), then CONFIRM a row exists in the
      authoritative DB (we trust the DB, not the subprocess's word).

    Returns {recorded: bool, method, reason}. Never raises. On failure the
    caller marks setup trust truthfully UNRECORDED (local startup still works,
    remote stays refused) — it does not fabricate a row.
    """
    import sys

    base = Path(home) if home else Path.home()
    if _same_interpreter(python_path, sys.executable) or not python_path:
        try:
            m = record_package_integrity(base, source=source)
            return {
                "recorded": bool(m.get("recorded")),
                "method": "in_process",
                "reason": "" if m.get("recorded") else "db_write_failed",
            }
        except Exception as exc:
            return {"recorded": False, "method": "in_process", "reason": repr(exc)}
    argv = [
        str(python_path),
        "-m",
        "aidocs_mcp.cli",
        "runtime",
        "--record-package",
        "--source",
        source,
    ]
    run = runner or _default_subprocess_runner
    try:
        code, out, err = run(argv)
    except Exception as exc:
        return {
            "recorded": False,
            "method": "selected_runtime",
            "reason": f"could not launch selected runtime: {exc!r}",
        }
    if code != 0:
        return {
            "recorded": False,
            "method": "selected_runtime",
            "reason": (f"selected runtime record exited {code}: {(err or out)[:200]}"),
        }
    # Confirm against the authoritative DB — the subprocess CLAIMING success is
    # not enough; only a real row counts.
    try:
        from .runtime_trust_store import RuntimeTrustStore

        rec = RuntimeTrustStore(base).current()
    except Exception:
        rec = None
    if not rec:
        return {
            "recorded": False,
            "method": "selected_runtime",
            "reason": "selected runtime ran but no trust row was recorded",
        }
    return {"recorded": True, "method": "selected_runtime", "reason": ""}


# ── end-to-end trust-chain proof (setup report + doctor) ─────────────────
def _norm_path(p) -> str | None:
    return str(p).replace("\\", "/") if p else None


def _read_json_file(path) -> dict | None:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except Exception:
        return None


def _interp_from_hook_cmd(cmd: str | None) -> str | None:
    """`<interp> -m aidocs_mcp.claude_hook` → `<interp>`."""
    if not cmd:
        return None
    i = cmd.find(" -m aidocs_mcp")
    return (cmd[:i] if i > 0 else cmd).strip()


def _mcp_command_from(project_root) -> str | None:
    if not project_root:
        return None
    d = _read_json_file(Path(project_root) / ".mcp.json")
    if not isinstance(d, dict):  # missing/unreadable .mcp.json
        return None
    try:
        return d["mcpServers"]["aidocs"]["command"]
    except Exception:
        return None


def _hook_command_in(obj) -> str | None:
    """Walk an arbitrary hooks JSON tree for the first command that launches the
    aidocs claude_hook.
    """
    if isinstance(obj, dict):
        c = obj.get("command")
        if isinstance(c, str) and "aidocs_mcp.claude_hook" in c:
            return c
        for v in obj.values():
            r = _hook_command_in(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _hook_command_in(v)
            if r:
                return r
    return None


def _check_selected_import(python_path, *, expected_version=None, runner=None) -> dict:
    """Confirm the SELECTED interpreter can import aidocs_mcp at the expected
    version. In-process when it IS the current interpreter, else a subprocess.
    """
    import sys

    if not python_path or _same_interpreter(python_path, sys.executable):
        return {"ok": True, "version": package_version(), "method": "in_process"}
    code_s = (
        "import json,sys;import aidocs_mcp;"
        "sys.stdout.write(json.dumps({'version': "
        "getattr(aidocs_mcp,'__version__',None)}))"
    )
    run = runner or _default_subprocess_runner
    try:
        code, out, err = run([str(python_path), "-c", code_s])
    except Exception as exc:
        return {"ok": False, "version": None, "reason": repr(exc)}
    if code != 0:
        return {"ok": False, "version": None, "reason": (err or out)[:200]}
    try:
        return {"ok": True, "version": json.loads(out).get("version"), "method": "subprocess"}
    except Exception:
        return {"ok": False, "version": None, "reason": "bad import output"}


def prove_trust_chain(
    home=None,
    project_root=None,
    *,
    python_path=None,
    expected_version=None,
    mcp_command=None,
    claude_command=None,
    codex_command=None,
    interpreter_fp=None,
    import_result=None,
    runner=None,
) -> dict:
    """Prove the selected-runtime trust chain end-to-end:
      1. selected python == .mcp.json == Claude hook == Codex hook interpreter;
      2. the selected runtime imports the expected aidocs_mcp version;
      3. the runtime_trust DB row's interpreter fingerprint == the selected
         executable's fingerprint;
      4. runtime.json is only a labelled projection (projection_of_db), never
         authority;
      5. first MCP startup under the selected runtime does NOT hit
         interpreter_drift (nor package drift).
    If trust was never recorded (no DB row) or the projection is
    projection_failed, the proof is truthfully `degraded`/`trust_unrecorded` —
    it never claims a chain it cannot show.
    """
    from .runtime_provisioner import read_manifest, runtime_fingerprint

    base = Path(home) if home else Path.home()
    sel_norm = _norm_path(python_path)

    # 1) host-config interpreter equality
    if mcp_command is None:
        mcp_command = _mcp_command_from(project_root)
    if claude_command is None:
        claude_command = _hook_command_in(_read_json_file(base / ".claude" / "settings.json") or {})
    if codex_command is None:
        codex_command = _hook_command_in(_read_json_file(base / ".codex" / "hooks.json") or {})
    adapters = {
        "mcp": _norm_path(mcp_command),  # IS the interp
        "claude": _norm_path(_interp_from_hook_cmd(claude_command)),
        "codex": _norm_path(_interp_from_hook_cmd(codex_command)),
    }
    present = {k: v for k, v in adapters.items() if v}
    mismatches = {k: v for k, v in present.items() if v != sel_norm}
    # Per-adapter status so a display can show matched / absent / mismatched
    # rather than a single check that could imply an absent adapter was verified.
    statuses = {
        k: ("matched" if v == sel_norm else "mismatched") if v else "absent"
        for k, v in adapters.items()
    }
    path_eq = {
        "ok": (not mismatches),
        "proven": bool(present),
        "selected": sel_norm,
        "statuses": statuses,
        **adapters,
        "mismatches": mismatches,
    }

    # 2) selected runtime imports expected aidocs_mcp version
    if import_result is None:
        import_result = _check_selected_import(
            python_path,
            expected_version=expected_version,
            runner=runner,
        )
    imp_ok = bool(import_result.get("ok")) and (
        expected_version is None or str(import_result.get("version")) == str(expected_version)
    )
    imports = {
        "ok": imp_ok,
        "version": import_result.get("version"),
        "expected": expected_version,
        "reason": import_result.get("reason", ""),
    }

    # 3) DB row interpreter fingerprint == selected executable fingerprint
    row = None
    try:
        from .runtime_trust_store import RuntimeTrustStore

        row = RuntimeTrustStore(base).current()
    except Exception:
        row = None
    trust_recorded = row is not None
    sel_fp = interpreter_fp if interpreter_fp is not None else runtime_fingerprint(python_path)
    db_fp = (row or {}).get("interpreter_fingerprint")
    db_match = bool(trust_recorded and db_fp and sel_fp and db_fp == sel_fp)
    db_interp = {"ok": db_match, "db_fp": db_fp, "selected_fp": sel_fp}

    # 4) runtime.json is a labelled projection only
    man = read_manifest(base) or {}
    label = man.get("package_trust_source")
    proj = {
        "ok": (label == "projection_of_db"),
        "label": label,
        "degraded": (label == "projection_failed"),
    }

    # 5) first MCP startup under the selected runtime: no interpreter/package drift
    sim = (
        verify_package_integrity(
            base,
            current={
                "fingerprint": (row or {}).get("package_fingerprint"),
                "version": (row or {}).get("package_version"),
            },
            provenance=(row or {}).get("provenance"),
            interpreter_fp=sel_fp,
        )
        if trust_recorded
        else {"interpreter_drift": False, "drifted": True}
    )
    no_drift = {
        "ok": (not sim.get("interpreter_drift") and not sim.get("drifted")),
        "interpreter_drift": bool(sim.get("interpreter_drift")),
    }

    degraded = (not trust_recorded) or proj["degraded"]
    ok = bool(
        path_eq["ok"]
        and imports["ok"]
        and db_interp["ok"]
        and proj["ok"]
        and no_drift["ok"]
        and trust_recorded,
    )
    if not trust_recorded:
        reason = "trust_unrecorded: no runtime_trust DB row for the selected runtime"
    elif proj["degraded"]:
        reason = "degraded: runtime.json is projection_failed (DB write failed)"
    elif not ok:
        reason = "trust chain incomplete (see per-check results)"
    else:
        reason = ""
    return {
        "ok": ok,
        "trust_recorded": trust_recorded,
        "degraded": degraded,
        "reason": reason,
        "checks": {
            "path_equality": path_eq,
            "selected_imports_expected": imports,
            "db_interpreter_matches": db_interp,
            "runtime_json_is_projection": proj,
            "no_interpreter_drift": no_drift,
        },
    }
