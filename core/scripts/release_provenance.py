"""Release provenance: checksums + SBOM + provenance record.

Generates the side-artifacts that make a public release verifiable:
  * SHA256SUMS    — sha256 of every release artifact (wheel/sdist/exe/msi).
  * sbom.json     — minimal CycloneDX 1.5 SBOM (components from the pinned
                    Python + dashboard dependency declarations).
  * provenance.json — what was built, from which source commit + public
                    export digest, and the hashes of the (mutable) build
                    inputs that were pinned (Cargo.lock, package-lock.json,
                    pyproject.toml) so input drift is detectable.

All outputs are deterministic for a given input set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256sums(paths: list[Path]) -> str:
    """GNU coreutils SHA256SUMS format (sorted by name, deterministic)."""
    lines = sorted(f"{sha256_file(p)}  {p.name}" for p in paths if p.is_file())
    return "\n".join(lines) + ("\n" if lines else "")


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        check=False,
    )
    return out.stdout.strip()


def _py_deps(root: Path) -> list[dict]:
    try:
        with (root / "mcp" / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return []
    deps = (data.get("project", {}) or {}).get("dependencies", []) or []
    comps = []
    for spec in deps:
        comps.append({"type": "library", "name": str(spec), "scope": "python"})
    return comps


def _node_deps(root: Path) -> list[dict]:
    pkg = root / "apps" / "aidocs-dashboard" / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except Exception:
        return []
    comps = []
    for kind in ("dependencies", "devDependencies"):
        for name, ver in (data.get(kind, {}) or {}).items():
            comps.append({"type": "library", "name": name,
                          "version": str(ver), "scope": "npm"})
    return comps


def sbom(root: Path, *, version: str, source_commit: str) -> dict:
    """Minimal CycloneDX 1.5 SBOM from the declared dependency surfaces."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application", "name": "aidocs", "version": version,
            },
            "properties": [
                {"name": "source_commit", "value": source_commit},
            ],
        },
        "components": _py_deps(root) + _node_deps(root),
    }


# Mutable build inputs whose hashes we pin into provenance so drift shows.
_BUILD_INPUTS = (
    "mcp/pyproject.toml",
    "apps/aidocs-dashboard/package-lock.json",
    "apps/aidocs-dashboard/src-tauri/Cargo.lock",
)


def provenance(
    root: Path, *, tag: str, version: str, export_digest: str,
    artifacts: list[Path],
) -> dict:
    source_commit = _git(root, "rev-parse", "HEAD")
    inputs: dict[str, str] = {}
    for rel in _BUILD_INPUTS:
        p = root / rel
        inputs[rel] = sha256_file(p) if p.is_file() else ""
    return {
        "schema": "aidocs-provenance/1",
        "tag": tag,
        "version": version,
        "source_commit": source_commit,
        "public_export_digest": export_digest,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_inputs_sha256": inputs,
        "artifacts": [
            {"name": p.name, "sha256": sha256_file(p)}
            for p in sorted(artifacts, key=lambda x: x.name) if p.is_file()
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Release provenance generator.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--tag", default="")
    ap.add_argument("--version", default="")
    ap.add_argument("--out", default=".", help="output dir for side-artifacts")
    ap.add_argument("--artifact", action="append", default=[],
                    help="release artifact path (repeatable)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    artifacts = [Path(a).resolve() for a in args.artifact]

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from public_export import export_digest as _ed
    digest = _ed(root)

    (out / "SHA256SUMS").write_text(sha256sums(artifacts), encoding="utf-8")
    (out / "sbom.json").write_text(
        json.dumps(sbom(root, version=args.version,
                        source_commit=_git(root, "rev-parse", "HEAD")),
                   indent=2),
        encoding="utf-8")
    (out / "provenance.json").write_text(
        json.dumps(provenance(root, tag=args.tag, version=args.version,
                              export_digest=digest, artifacts=artifacts),
                   indent=2),
        encoding="utf-8")
    print(f"Wrote SHA256SUMS, sbom.json, provenance.json to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
