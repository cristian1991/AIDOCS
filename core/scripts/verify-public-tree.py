from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


FORBIDDEN_PATHS = (
    "mcp/tests/",
    "tests/",
)

FORBIDDEN_GLOBS = (
    "archive/roadmaps/**",
    "**/ROADMAP*.md",
    "**/*ROADMAP*.md",
)
ALLOWED_GLOB_EXCEPTIONS = (
    "PUBLIC_ROADMAP.md",
)


def _repo_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to resolve git root").strip())
    return Path(result.stdout.strip())


def _working_tree_entries(root: Path) -> list[str]:
    entries: list[str] = []
    for forbidden in FORBIDDEN_PATHS:
        target = root / forbidden.rstrip("/")
        if target.exists():
            if target.is_dir():
                for child in target.rglob("*"):
                    if child.is_file():
                        entries.append(child.relative_to(root).as_posix())
            else:
                entries.append(target.relative_to(root).as_posix())
    for pattern in FORBIDDEN_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(rel.endswith(exc) for exc in ALLOWED_GLOB_EXCEPTIONS):
                continue
            entries.append(rel)
    return sorted(entries)


def _ref_entries(root: Path, ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"failed to list tree for {ref}").strip())
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    blocked = []
    for path in paths:
        for forbidden in FORBIDDEN_PATHS:
            if forbidden.endswith("/"):
                if path.startswith(forbidden):
                    blocked.append(path)
                    break
            elif path == forbidden:
                blocked.append(path)
                break
        else:
            for pattern in FORBIDDEN_GLOBS:
                if Path(path).match(pattern) and not any(path.endswith(exc) for exc in ALLOWED_GLOB_EXCEPTIONS):
                    blocked.append(path)
                    break
    return sorted(blocked)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that public tree/release content excludes private paths.")
    parser.add_argument("--repo-root", default=".", help="Path inside the git repo (default: current directory)")
    parser.add_argument("--ref", default="", help="Git ref to verify instead of the working tree")
    args = parser.parse_args()

    root = _repo_root(Path(args.repo_root).resolve())
    blocked = _ref_entries(root, args.ref) if args.ref else _working_tree_entries(root)
    if blocked:
        print("Public packaging verification failed. Forbidden paths are present:", file=sys.stderr)
        for path in blocked:
            print(f"- {path}", file=sys.stderr)
        return 1

    subject = args.ref if args.ref else "working tree"
    print(f"Public packaging verification passed for {subject}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
