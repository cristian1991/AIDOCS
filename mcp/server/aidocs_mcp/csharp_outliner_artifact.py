"""The TRUSTED Roslyn C# outliner artifact: install once, verify before every exec.

WHAT THIS REPLACES. ``csharp_roslyn_client`` used to find its DLL by walking up
from its own file to "a directory containing mcp/ and tools/" and taking
``tools/aidocs-csharp-outliner/bin/Release/net9.0/aidocs-csharp-outliner.dll``
if it existed — or whatever ``AIDOCS_CSHARP_ROSLYN_DLL`` named. That is
discovery from whatever checkout happens to be nearby: another worktree, a
tenant tree, a stale build, any bytes at all. And when nothing was there the
indexer quietly fell back to a regex extractor that misses one-line classes.

THE CONTRACT NOW.

  BUILT ONLY AT A TRUSTED INSTALL STEP. ``install_outliner`` is called by the
  runtime generation build (``runtime_provisioner._provision_venv``), by the
  operator CLI (``python -m aidocs_mcp.csharp_outliner_artifact install``), and
  by the test-session fixture. It copies the outliner SOURCE into a private
  staging dir, runs ``dotnet build``, hashes every output file, and moves the
  output into an immutable, content-addressed ``builds/<build_id>/`` dir. It is
  never reached from an indexing request.

  ONE MANIFEST, STORED APART FROM THE BYTES IT VOUCHES FOR. The manifest
  records the SHA-256 of every file in the build dir plus the source hash, the
  resolved Roslyn/Razor package versions, the target framework and the .NET SDK
  version and host path. Where it lives depends on the runtime shape:

    generation runtime  the generation's SEAL (``generation.complete.json``,
                        key ``csharp_outliner``) — written last, with the
                        generation, and the build sits inside that generation,
                        so rollback/collection move them together;
    no generation       ``<runtime_root>/csharp-outliner/manifest.json``
                        (written by the operator CLI install).

  VERIFIED IMMEDIATELY BEFORE EACH EXEC. ``resolve_trusted_outliner`` re-reads
  the manifest and re-hashes every file of the build dir each time it is called,
  refuses any file the manifest does not list (a planted DLL next to the
  outliner would be loaded by the runtime's probing), and returns the dotnet
  host recorded at build time rather than whatever ``dotnet`` PATH resolves to.
  The bridge calls it before spawning its daemon (and before each respawn) and
  before every single-shot run.

``AIDOCS_CSHARP_ROSLYN_DLL`` IS REMOVED, not kept-and-verified: a path from the
environment is a second discovery channel, and verifying it against the
manifest would make it redundant with the manifest's own location — so it could
only ever add a way to be wrong.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

SCHEMA = "aidocs.csharp-outliner.manifest/1"
DLL_NAME = "aidocs-csharp-outliner.dll"
CSPROJ_NAME = "AidocsCsharpOutliner.csproj"
#: The committed NuGet lock: the ONLY package set a trusted build may restore.
LOCKFILE_NAME = "packages.lock.json"
TARGET_FRAMEWORK = "net9.0"
STORE_DIRNAME = "csharp-outliner"
BUILDS_DIRNAME = "builds"
MANIFEST_FILENAME = "manifest.json"
#: Key inside a runtime generation's seal that holds the manifest.
SEAL_KEY = "csharp_outliner"
#: The served gate release's root-owned identity file, and the line in it that
#: seals the release's outliner manifest (sha256 of manifest.json's bytes).
RELEASE_IDENTITY = "release.identity"
RELEASE_SEAL_KEY = "csharp_outliner_manifest_sha256"

# One reason per cause — surfaced verbatim as the NOT_INDEXED reason.
REASON_NOT_INSTALLED = "outliner_not_installed"
REASON_MANIFEST_INVALID = "outliner_manifest_invalid"
REASON_FILE_MISSING = "outliner_file_missing"
REASON_HASH_MISMATCH = "outliner_hash_mismatch"
REASON_UNEXPECTED_FILE = "outliner_unexpected_file"
REASON_DOTNET_MISSING = "dotnet_not_available"
REASON_SOURCE_MISSING = "outliner_source_missing"
REASON_LOCKFILE_MISSING = "outliner_lockfile_missing"
REASON_BUILD_FAILED = "outliner_build_failed"
REASON_EXEC_FAILED = "outliner_exec_failed"
REASON_RUNTIME_UNRESOLVED = "runtime_generation_unresolved"

_BUILD_ID_SHAPE = re.compile(r"roslyn-[0-9a-f]{12}-[0-9a-f]{12}")
_BUILD_TIMEOUT_S = 900

Runner = Callable[[list[str]], "tuple[int, str, str]"]


class OutlinerUnavailable(RuntimeError):
    """The trusted outliner cannot be used. ``reason`` is one REASON_* code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class VerifiedOutliner:
    dll: Path
    dotnet: str
    build_id: str
    manifest_digest: str
    manifest: dict[str, Any]


# Test seam (code-only, never environment): the session fixture points the
# resolver at the store it just built through ``install_outliner``.
_test_root: Path | None = None


def set_trusted_root_for_tests(path: Path | str | None) -> None:
    global _test_root
    _test_root = Path(path) if path is not None else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def _tree_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


# ── resolution + verification ─────────────────────────────────────────────


def _standalone_store(home: Path) -> Path:
    from .runtime_generations import runtime_root

    return runtime_root(home) / STORE_DIRNAME


def release_root_for(module_file: Path | str) -> Path | None:
    """The served gate RELEASE that contains ``module_file``, or None.

    A release is ``.../releases/<id>/`` carrying a ``release.identity`` — the
    root-owned, signed-and-bound identity the deploy writes for every promoted
    release (deploy_aidocs_gate.sh Gate 4). The served package runs from that
    release's own ``.venv`` (or ``server/``), so this is the running code's
    OWN release, not a neighbouring checkout: a checkout, a worktree or a
    tenant tree has no ``releases/<id>/release.identity`` ancestor and never
    matches."""
    try:
        here = Path(module_file).resolve()
    except OSError:
        return None
    for parent in here.parents:
        if parent.parent.name == "releases" and (parent / RELEASE_IDENTITY).is_file():
            return parent
    return None


def _read_identity(release: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = (release / RELEASE_IDENTITY).read_text(encoding="utf-8")
    except OSError as exc:
        raise OutlinerUnavailable(REASON_MANIFEST_INVALID, f"release identity unreadable: {exc}") from exc
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _load_release_manifest(release: Path) -> tuple[dict[str, Any], Path]:
    """Release layout: the manifest must be SEALED by release.identity."""
    seal = _read_identity(release).get(RELEASE_SEAL_KEY, "")
    store = release / STORE_DIRNAME
    if not seal:
        raise OutlinerUnavailable(
            REASON_NOT_INSTALLED, f"release {release.name} was promoted without a C# outliner"
        )
    path = store / MANIFEST_FILENAME
    if not path.is_file():
        raise OutlinerUnavailable(REASON_NOT_INSTALLED, f"no manifest at {path}")
    if sha256_file(path) != seal:
        raise OutlinerUnavailable(
            REASON_MANIFEST_INVALID, "manifest is not the one release.identity seals"
        )
    return _read_manifest_file(path), store


def _load_manifest(module_file: Path | str | None = None) -> tuple[dict[str, Any], Path]:
    """(manifest, store_root) from the ONE trusted location for this process.

    Order: test seam; the runtime GENERATION these bytes were loaded from;
    the served gate RELEASE these bytes were loaded from; else the activation
    pointer's generation, or the no-generation standalone store."""
    if _test_root is not None and module_file is None:
        return _read_manifest_file(_test_root / MANIFEST_FILENAME), _test_root
    from . import runtime_generations as rg

    here = module_file or __file__
    home = Path.home()
    gid = rg.loaded_generation(here)
    if gid is None:
        release = release_root_for(here)
        if release is not None:
            return _load_release_manifest(release)
        gid, why = rg.read_pointer(home)
        if not gid and why != rg.REASON_LEGACY_TREE:
            raise OutlinerUnavailable(REASON_RUNTIME_UNRESOLVED, why)
    if not gid:
        store = _standalone_store(home)
        return _read_manifest_file(store / MANIFEST_FILENAME), store
    gdir = rg.generation_dir(home, gid)
    if gdir is None or not rg.is_complete(home, gid):
        raise OutlinerUnavailable(REASON_RUNTIME_UNRESOLVED, f"generation {gid} not sealed")
    try:
        seal = json.loads((gdir / rg.COMPLETE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OutlinerUnavailable(REASON_MANIFEST_INVALID, f"seal unreadable: {exc}") from exc
    manifest = seal.get(SEAL_KEY) if isinstance(seal, dict) else None
    if not isinstance(manifest, dict):
        raise OutlinerUnavailable(
            REASON_NOT_INSTALLED, f"generation {gid} was sealed without a C# outliner"
        )
    return manifest, gdir / STORE_DIRNAME


def _read_manifest_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OutlinerUnavailable(REASON_NOT_INSTALLED, f"no manifest at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OutlinerUnavailable(REASON_MANIFEST_INVALID, str(exc)) from exc
    if not isinstance(data, dict):
        raise OutlinerUnavailable(REASON_MANIFEST_INVALID, "manifest is not an object")
    return data


def manifest_digest(module_file: Path | str | None = None) -> str:
    """Cheap identity of the CURRENT trusted manifest (no file hashing).

    The bridge compares it per request so a running daemon is torn down and
    re-verified the moment the trusted manifest changes."""
    manifest, store = _load_manifest(module_file)
    return _digest_json({"store": str(store), "manifest": manifest})


def resolve_trusted_outliner(module_file: Path | str | None = None) -> VerifiedOutliner:
    """Verify every byte of the trusted build NOW; raise OutlinerUnavailable."""
    return _verify(*_load_manifest(module_file))


def verify_release(release: Path | str) -> VerifiedOutliner:
    """Verify the outliner sealed into a staged/served gate release."""
    return _verify(*_load_release_manifest(Path(release)))


def _verify(manifest: dict[str, Any], store: Path) -> VerifiedOutliner:
    if manifest.get("status") != "installed":
        raise OutlinerUnavailable(
            str(manifest.get("reason") or REASON_NOT_INSTALLED), str(manifest.get("detail") or "")
        )
    build_id = str(manifest.get("build_id") or "")
    files = manifest.get("files")
    dll_rel = str(manifest.get("dll") or "")
    if (
        manifest.get("schema") != SCHEMA
        or not _BUILD_ID_SHAPE.fullmatch(build_id)
        or not isinstance(files, dict)
        or dll_rel not in files
        or files.get(dll_rel) != manifest.get("dll_sha256")
    ):
        raise OutlinerUnavailable(REASON_MANIFEST_INVALID, "manifest fields malformed")
    build_dir = store / BUILDS_DIRNAME / build_id
    if not build_dir.is_dir():
        raise OutlinerUnavailable(REASON_FILE_MISSING, str(build_dir))
    present = {p.relative_to(build_dir).as_posix() for p in build_dir.rglob("*") if p.is_file()}
    extra = sorted(present - set(files))
    if extra:
        raise OutlinerUnavailable(REASON_UNEXPECTED_FILE, ", ".join(extra[:5]))
    for rel, want in files.items():
        p = build_dir / rel
        if rel not in present:
            raise OutlinerUnavailable(REASON_FILE_MISSING, rel)
        if sha256_file(p) != want:
            raise OutlinerUnavailable(REASON_HASH_MISMATCH, rel)
    dotnet = str(manifest.get("dotnet_path") or "")
    if not dotnet or not Path(dotnet).is_file():
        raise OutlinerUnavailable(REASON_DOTNET_MISSING, dotnet or "no dotnet host recorded")
    return VerifiedOutliner(
        dll=build_dir / dll_rel,
        dotnet=dotnet,
        build_id=build_id,
        manifest_digest=_digest_json({"store": str(store), "manifest": manifest}),
        manifest=manifest,
    )


# ── trusted install ───────────────────────────────────────────────────────


def _default_run(argv: list[str]) -> tuple[int, str, str]:
    from .runtime_provisioner import _default_runner

    return _default_runner(argv, timeout=_BUILD_TIMEOUT_S)


def _source_files(source_dir: Path) -> list[Path]:
    files = sorted(
        p
        for p in source_dir.iterdir()
        if p.is_file() and (p.suffix == ".cs" or p.name in (CSPROJ_NAME, LOCKFILE_NAME))
    )
    if not any(p.name == CSPROJ_NAME for p in files):
        raise OutlinerUnavailable(REASON_SOURCE_MISSING, f"{CSPROJ_NAME} not in {source_dir}")
    # A trusted build restores ONLY the pinned set: no lock file, no build.
    if not any(p.name == LOCKFILE_NAME for p in files):
        raise OutlinerUnavailable(REASON_LOCKFILE_MISSING, f"{LOCKFILE_NAME} not in {source_dir}")
    return files


def _resolved_packages(deps_json: Path) -> dict[str, str]:
    try:
        data = json.loads(deps_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for key in (data.get("libraries") or {}):
        name, _, version = str(key).partition("/")
        if name.startswith(("Microsoft.CodeAnalysis", "Microsoft.AspNetCore.Razor")) and version:
            out[name] = version
    return out


def install_outliner(
    source_dir: Path | str, store_root: Path | str, *, run: Runner | None = None
) -> dict[str, Any]:
    """Build the outliner from SOURCE into ``store_root/builds/<build_id>``.

    Returns the manifest (status "installed"); raises OutlinerUnavailable on
    any failure. Never overwrites an existing build dir: build ids are content
    addressed, so an existing dir must match byte for byte or the install is
    refused."""
    run = run or _default_run
    source_dir = Path(source_dir)
    store_root = Path(store_root)
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise OutlinerUnavailable(REASON_DOTNET_MISSING, "dotnet is not on PATH at install time")
    dotnet = str(Path(dotnet).resolve())
    if not source_dir.is_dir():
        raise OutlinerUnavailable(REASON_SOURCE_MISSING, str(source_dir))
    sources = _source_files(source_dir)
    src_hash = hashlib.sha256()
    for p in sources:
        src_hash.update(p.name.encode("utf-8") + b"\0" + p.read_bytes() + b"\0")
    source_sha = src_hash.hexdigest()
    builds = store_root / BUILDS_DIRNAME
    builds.mkdir(parents=True, exist_ok=True)
    code, sdk_out, sdk_err = run([dotnet, "--version"])
    if code != 0:
        raise OutlinerUnavailable(REASON_DOTNET_MISSING, (sdk_err or sdk_out).strip()[:300])
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=str(store_root)) as staging:
        src = Path(staging) / "src"
        out = Path(staging) / "out"
        src.mkdir()
        for p in sources:
            shutil.copy2(p, src / p.name)
        # LOCKED MODE: restore exactly packages.lock.json (content-hashed) or
        # fail — a drifted csproj (NU1004) or a cold cache with no reachable
        # feed (NU1101/NU1301) is a loud build failure, never a float.
        code, bout, berr = run(
            [
                dotnet,
                "build",
                str(src / CSPROJ_NAME),
                "-c",
                "Release",
                "-o",
                str(out),
                "--nologo",
                "-p:RestoreLockedMode=true",
            ]
        )
        if code != 0 or not (out / DLL_NAME).is_file():
            text = f"{bout or ''}\n{berr or ''}"
            codes = sorted(set(re.findall(r"\b(?:NU|CS|MSB)\d{4}\b", text)))
            tail = text.strip()[-600:]
            raise OutlinerUnavailable(
                REASON_BUILD_FAILED, f"rc={code} codes={','.join(codes) or '-'} {tail}"
            )
        files = _tree_files(out)
        build_id = f"roslyn-{source_sha[:12]}-{_digest_json(files)[:12]}"
        dest = builds / build_id
        if dest.exists():
            if _tree_files(dest) != files:
                raise OutlinerUnavailable(
                    REASON_HASH_MISMATCH, f"existing build {build_id} differs; refusing to overwrite"
                )
        else:
            os.replace(out, dest)
        packages = _resolved_packages(dest / "aidocs-csharp-outliner.deps.json")
    return {
        "schema": SCHEMA,
        "status": "installed",
        "build_id": build_id,
        "dll": DLL_NAME,
        "dll_sha256": files[DLL_NAME],
        "files": files,
        "source_sha256": source_sha,
        "lockfile_sha256": sha256_file(source_dir / LOCKFILE_NAME),
        "packages": packages,
        "target_framework": TARGET_FRAMEWORK,
        "dotnet_path": dotnet,
        "dotnet_sdk_version": sdk_out.strip(),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def install_or_reason(source_dir: Path | str, store_root: Path | str, *, run: Runner | None = None) -> dict:
    """``install_outliner`` for callers that must not fail (a generation build):
    an unavailable outliner is recorded as a status, never raised."""
    try:
        return install_outliner(source_dir, store_root, run=run)
    except OutlinerUnavailable as exc:
        return {"schema": SCHEMA, "status": "unavailable", "reason": exc.reason, "detail": exc.detail[:600]}


def write_standalone_manifest(store_root: Path | str, manifest: dict[str, Any]) -> Path:
    """Atomically write the no-generation manifest (LAST write of an install)."""
    dest = Path(store_root) / MANIFEST_FILENAME
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def seal_release(release: Path | str, manifest_path: Path | str) -> None:
    """Bind ``manifest_path``'s SHA-256 into ``<release>/release.identity``
    (replacing any earlier outliner seal line), atomically."""
    release = Path(release)
    ident = release / RELEASE_IDENTITY
    lines = [
        ln
        for ln in ident.read_text(encoding="utf-8").splitlines()
        if not ln.startswith(f"{RELEASE_SEAL_KEY}=")
    ]
    lines.append(f"{RELEASE_SEAL_KEY}={sha256_file(Path(manifest_path))}")
    fd, tmp = tempfile.mkstemp(prefix=".identity-", dir=str(release))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ident)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_release(source_dir: Path | str, release: Path | str) -> VerifiedOutliner:
    """Deploy step (served gate, PRE-swap, as root): build the outliner INTO
    the staged release, write its manifest, seal it into release.identity,
    then re-verify every byte through the served-runtime resolution path.
    Raises OutlinerUnavailable on any failure — release.identity is only
    touched after a successful build."""
    release = Path(release)
    if not (release / RELEASE_IDENTITY).is_file():
        raise OutlinerUnavailable(REASON_RUNTIME_UNRESOLVED, f"{release} has no {RELEASE_IDENTITY}")
    store = release / STORE_DIRNAME
    store.mkdir(parents=True, exist_ok=True)
    manifest = install_outliner(source_dir, store)
    path = write_standalone_manifest(store, manifest)
    seal_release(release, path)
    return verify_release(release)


def _emit(ok: bool, **fields: Any) -> int:
    print(json.dumps({"ok": ok, **fields}))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """Operator / deploy entry points.

    install         --source DIR [--root DIR]   standalone store (no generations)
    install-release --source DIR --release DIR  served gate release (deploy)
    verify          [--release DIR]             re-verify what would be used
    """
    import argparse

    ap = argparse.ArgumentParser(prog="python -m aidocs_mcp.csharp_outliner_artifact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("install", help="build + record the trusted outliner")
    ins.add_argument("--source", required=True)
    ins.add_argument("--root", default=None, help="store root (default: <runtime_root>/csharp-outliner)")
    rel = sub.add_parser("install-release", help="build + seal the outliner into a gate release")
    rel.add_argument("--source", required=True)
    rel.add_argument("--release", required=True)
    ver = sub.add_parser("verify", help="verify the trusted outliner")
    ver.add_argument("--release", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "verify":
            v = verify_release(args.release) if args.release else resolve_trusted_outliner()
            return _emit(True, dll=str(v.dll), build_id=v.build_id)
        if args.cmd == "install-release":
            v = install_release(args.source, args.release)
            return _emit(True, dll=str(v.dll), build_id=v.build_id)
        root = Path(args.root) if args.root else _standalone_store(Path.home())
        root.mkdir(parents=True, exist_ok=True)
        manifest = install_outliner(args.source, root)
        path = write_standalone_manifest(root, manifest)
        return _emit(True, manifest=str(path), build_id=manifest["build_id"])
    except OutlinerUnavailable as exc:
        return _emit(False, reason=exc.reason, detail=exc.detail)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
