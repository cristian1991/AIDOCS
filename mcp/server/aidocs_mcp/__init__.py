"""AIDOCS MCP file-backed services."""

from __future__ import annotations

import sys
from pathlib import Path

# RFC-4: vendored mempalace lives under <repo>/third_party/mempalace.
# Prepend it to sys.path so ``import mempalace`` resolves to the
# bundled copy rather than any externally installed wheel. AIDOCS is
# the Empire — MemPalace is the in-repo Palace engine, not an external
# dependency. This runs at first import of aidocs_mcp, before any
# downstream code triggers ``import mempalace``.
_VENDOR_ROOT = Path(__file__).resolve().parents[3] / "third_party" / "mempalace"
if _VENDOR_ROOT.is_dir() and str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))


def _version_from_pyproject() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        return "0.0.0"

    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        version = project.get("version")
        return str(version) if version else "0.0.0"
    except Exception:
        return "0.0.0"


def _version_from_release_manifest() -> str:
    """Read version from the signed release manifest shipped with the
    deployed package. This is the authoritative version source on the
    server, where pyproject.toml does not ship (only aidocs_mcp/ does).
    Until 2026-05-27 the footer on the live login page rendered '0.0.0'
    because pyproject was missing and the fallback below didn't know
    about the manifest — the manifest is the canonical reply now.
    """
    import json

    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version")
        return str(v) if v else "0.0.0"
    except Exception:
        return "0.0.0"


def release_build_info() -> dict:
    """The deployed build's identity, read directly from the signed release manifest (pure display
    accessor, no crypto verification): build_number (monotonic, incremented per --deploy), commit,
    version, created_at, builder. A missing manifest / field degrades to safe defaults. Powers the
    gate's serverInfo.version stamp + the ai_version tool so the operator can see, at a glance,
    exactly which build is live on webmcp."""
    import json

    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    info = {"build_number": 0, "commit": "", "version": "", "created_at": "", "builder": "", "fingerprint": ""}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return info
    if not isinstance(data, dict):
        return info
    bn = data.get("build_number")
    try:
        info["build_number"] = int(bn) if bn is not None else 0
    except (TypeError, ValueError):
        info["build_number"] = 0
    info["commit"] = str(data.get("commit") or "")
    info["version"] = str(data.get("version") or "")
    info["created_at"] = str(data.get("created_at") or "")
    info["builder"] = str(data.get("builder") or "")
    info["fingerprint"] = str(data.get("fingerprint") or "")
    return info


def _git_head_commit(repo_root: Path) -> str:
    """Best-effort git HEAD sha by reading .git files directly (no subprocess in
    the import path). Handles loose refs, packed-refs, and detached HEAD; empty
    string on a deployed package with no .git."""
    try:
        git_dir = repo_root / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:40]  # detached HEAD holds the sha directly
        ref = head.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:40]
        packed = git_dir / "packed-refs"  # packed-refs fallback
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith(("#", "^")) and line.endswith(ref):
                    return line.split(" ", 1)[0].strip()[:40]
    except Exception:
        pass
    return ""


# NOT SOURCE. Excluded from the drift answer — and the exclusion list is itself
# a place noise can creep back in, so keep it PRINCIPLED and short:
#   mcp/.deploy-reports — the crown gate GENERATES these during a run and commits
#     them AFTER promoting (doctrine XVI), so a deploy structurally ends with
#     local HEAD one commit ahead of the commit it just shipped. The deploy's own
#     bookkeeping can never be "unshipped code".
#   .MEMORY — memory/data, not code: backlog rows, session journals, sync events.
#     Every ai_backlog write mints an event file, so counting these would make
#     source_in_sync permanently false — which would recreate the exact defect
#     this function exists to kill (a signal that always says "different" cannot
#     be read, and real drift hides inside the permanent noise).
# The question this answers is narrow ON PURPOSE: "is the CODE I am running the
# CODE that is deployed?"
_NON_SOURCE_PATHS = ("mcp/.deploy-reports", ".MEMORY")


def _source_drift(repo_root: Path, deployed_commit: str) -> dict:
    """Does the LOCAL SOURCE differ from what is DEPLOYED? (Not: do the SHAs.)

    THE BUG THIS FIXES (operator, 2026-07-13: "why are local and deploy commit
    sha different? this is a bug no?"). Comparing raw SHAs is USELESS here,
    because the act of deploying CREATES a commit: the gate promotes commit X,
    then commits its own reports + session journal as X+1 (doctrine XVI —
    generated reports are committed before success is declared). So local !=
    deploy after EVERY successful deploy, forever, by construction.

    A signal that ALWAYS says "different" cannot be read — and real drift
    (genuinely unshipped source) becomes invisible inside the permanent noise.
    That is the same disease as an alarm that never stops: you stop hearing it.
    The operator hit exactly this — two SHAs, no way to tell which case it was.

    So answer the question he ACTUALLY has — "is the code I am running the code
    that is deployed?" — by diffing SOURCE, excluding the deploy's own
    bookkeeping, and counting UNCOMMITTED work too (an uncommitted edit is
    unshipped by definition, and is the case that actually bites).

    Fail-quiet, never fail-green: no git / no deployed commit / a broken compare
    -> in_sync is None ("cannot tell"), NEVER a fabricated True. Unknown is not
    a pass.
    """
    out: dict = {"in_sync": None, "unshipped": [], "note": ""}
    if not deployed_commit or not (repo_root / ".git").exists():
        out["note"] = "no git checkout or no deployed commit on record — cannot compare"
        return out
    try:
        # Reuse the ONE audited git helper (Article XXII): it is already
        # fingerprinted for the spawn-surface seal and windowless on win32.
        # A second git callsite here would just be a new untracked tunnel.
        from .git_helpers import run_git_sync

        ex = [f":(exclude){p}" for p in _NON_SOURCE_PATHS]
        root = str(repo_root)
        committed_out = run_git_sync(
            root, "diff", "--name-only", f"{deployed_commit}..HEAD", "--", ".", *ex, timeout=20
        )
        uncommitted_out = run_git_sync(
            root, "status", "--porcelain", "--", ".", *ex, timeout=20
        )
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so; never assume clean
        out["note"] = f"could not compare source with the deployed commit: {type(exc).__name__}"
        return out

    shipped_delta = [f for f in committed_out.splitlines() if f.strip()]
    # porcelain v1 is exactly TWO status chars then whitespace then the path —
    # slice at 2 and strip, never 3: a staged entry ("M  path") and an unstaged
    # one (" M path") pad differently, and a fixed 3-slice silently eats the
    # first character of the filename in one of them (observed: 'cp/server/...').
    dirty = [ln[2:].strip() for ln in uncommitted_out.splitlines() if ln.strip()]
    unshipped = sorted({*shipped_delta, *dirty})
    out["unshipped"] = unshipped[:20]
    out["in_sync"] = not unshipped
    if not unshipped:
        out["note"] = (
            "IN SYNC — the running source is exactly what is deployed. Local HEAD is "
            "ahead only by the deploy's OWN bookkeeping commit (reports + session "
            "journal), which every successful deploy creates by construction."
        )
    else:
        out["note"] = (
            f"UNSHIPPED SOURCE — {len(unshipped)} file(s) differ from the deployed "
            f"commit (committed-but-not-deployed, or uncommitted). This is REAL "
            f"drift, not deploy bookkeeping."
        )
    return out


def local_build_info() -> dict:
    """The build ACTUALLY RUNNING on this process — pyproject version + live git
    HEAD from the source checkout. On a dev box (editable install) the signed
    release manifest lags real code (only `--deploy` rewrites it) while the
    marker-poll watchdog has already reloaded newer code, so this reports the
    live truth. builder='live@source' + build_number 0 flag an unsealed run.

    Carries `deployed_commit` + `source_in_sync` so the SHA difference from
    mode=deploy is READABLE instead of alarming: see _source_drift."""
    repo_root = Path(__file__).resolve().parents[3]
    deployed = deploy_build_info().get("commit") or ""
    drift = _source_drift(repo_root, deployed)
    return {
        "mode": "local",
        "build_number": 0,
        "commit": _git_head_commit(repo_root),
        "version": _version_from_pyproject(),
        "created_at": "",
        "builder": "live@source",
        "source_root": str(Path(__file__).resolve().parents[2]),
        "deployed_commit": deployed,
        "source_in_sync": drift["in_sync"],
        "sync_note": drift["note"],
        "unshipped": drift["unshipped"],
    }


def deploy_build_info() -> dict:
    """The RUNNING deployed build + whether the live bytes match their signature.

    Two surfaces, one honest answer:
      • a dev box that ran `--deploy`: the seal under mcp/.deploy-reports/
        (crown.reports-head = truth-sealed tested commit, status.json = the
        test tally).
      • the SERVED gate (webmcp / VPS): .deploy-reports does NOT ship inside the
        deployed package, so those reads come back empty — the historical
        `mode=deploy` blank-payload bug. There, derive commit + fingerprint from
        the signed release the gate is actually running and RE-VERIFY the live
        code against it (release_trust.verify_release). `running_verified` is the
        load-bearing "is the deployed code the sealed code?" signal — compare it
        + `commit` + `fingerprint` against mode=local (dev HEAD) and mode=release
        (signed manifest) to confirm local == deployed == release.
    """
    import json

    reports = Path(__file__).resolve().parents[2] / ".deploy-reports"
    info = {
        "mode": "deploy",
        "commit": "",
        "tests": "",
        "status": "",
        "fingerprint": "",
        "running_verified": None,
    }
    try:
        info["commit"] = (reports / "crown.reports-head").read_text(encoding="utf-8").strip()[:40]
    except Exception:
        pass
    try:
        st = json.loads((reports / "status.json").read_text(encoding="utf-8"))
        info["tests"] = str(st.get("message") or "")
        info["status"] = str(st.get("label") or "")
    except Exception:
        pass
    # Served-surface fallback: ONLY when .deploy-reports gave nothing (i.e. the
    # deployed package, which never ships that dir) — never on a dev checkout,
    # where the seal above is the truth and a lingering local manifest would make
    # verify_release compare against stale bytes. There the gate runs the SIGNED
    # release, so recompute the running fingerprint and verify it against the
    # shipped manifest: `running_verified` answers "is the live code the sealed
    # code?", and commit/fingerprint let you compare against mode=local/release.
    if not info["commit"]:
        try:
            from .release_trust import verify_release

            rt = verify_release()
            if not str(getattr(rt, "reason", "")).startswith("unsigned"):
                info["commit"] = str(getattr(rt, "commit", "") or "")[:40]
                info["fingerprint"] = str(getattr(rt, "fingerprint", "") or "")
                info["running_verified"] = bool(getattr(rt, "ok", False))
                info["status"] = "verified" if rt.ok else f"UNVERIFIED: {rt.reason}"
                info["tests"] = str(getattr(rt, "reason", "") or "")
        except Exception:
            pass
    return info


def _has_source_checkout() -> bool:
    """True only when a REAL source tree is present (pyproject readable AND a
    resolvable git HEAD). The DEPLOYED package ships aidocs_mcp/ ONLY — no
    pyproject.toml, no .git — so mode=local there would report version 0.0.0 /
    commit '' (garbage). This is the guard that keeps the default honest on
    both surfaces."""
    return _version_from_pyproject() != "0.0.0" and bool(
        _git_head_commit(Path(__file__).resolve().parents[3]),
    )


def build_info(mode: str = "") -> dict:
    """Dispatch ai_version by truth-source:
      '' / 'auto' (DEFAULT) — the honest answer for THIS surface: 'local' on a
          source checkout (dev box: the manifest lags the marker-poll reload),
          'release' on a deployed server (webmcp: pyproject/.git do not ship,
          the signed manifest IS the truth). Fixes the 2026-07-13 regression
          where a hard 'local' default made the GATE's ai_version return
          version=0.0.0 / commit='' on mcp.codenexus.cloud.
      'local'   — force the live running source (honest empty on a server).
      'deploy'  — what the last local --deploy sealed (.deploy-reports).
      'release' — the signed package manifest.
    Unknown modes fall back to 'release' (the historical accessor)."""
    m = (mode or "").strip().lower()
    if m in ("", "auto"):
        m = "local" if _has_source_checkout() else "release"
    if m == "local":
        return local_build_info()
    if m == "deploy":
        return deploy_build_info()
    info = release_build_info()
    info["mode"] = "release"
    return info


def get_version() -> str:
    # Dev-box source of truth: pyproject.toml (always current at build).
    version = _version_from_pyproject()
    if version != "0.0.0":
        return version
    # Server source of truth: the signed release manifest.
    version = _version_from_release_manifest()
    if version != "0.0.0":
        return version
    # Lazy: importlib.metadata costs ~100 ms to import and is only needed in this
    # rare fallback (neither pyproject nor manifest readable). Importing it at
    # module top made every claude_hook process spawn pay ~100 ms for nothing.
    # Deferred here keeps __version__ identical while removing that cost from
    # the hot hook path.
    try:
        from importlib import metadata

        return metadata.version("aidocs-mcp")
    except Exception:
        return "0.0.0"


def __getattr__(name: str):
    """PEP 562 lazy ``__version__`` (#342 thin-hook budget): computing the
    version means reading pyproject.toml / the release manifest / .git — file
    I/O every ``python -m aidocs_mcp.claude_hook`` spawn paid at package import
    even though the hook never looks at the version. ``from aidocs_mcp import
    __version__`` and ``aidocs_mcp.__version__`` still work — they just pay on
    first access instead of at import.
    """
    if name == "__version__":
        return get_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
