"""Materiality gate — the §XXXII spend rule.

A server's RAM floor is fixed regardless of user LOC; 30 lines of
anything never earns a compiler. So we count (project × language) LOC
with a CHEAP extension walk (os.scandir, skipping the heavy dirs) and
only clear projects above the threshold. Below it the tree-sitter floor
is the CORRECT tool — refusing is not degradation.

Counting is memoized per (root, language) with a TTL so repeated door
calls in the same window don't re-walk the tree.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .domain import Language, MaterialityVerdict

# Directories that never count toward material LOC (vendored deps, VCS,
# build output). Cheap membership test during the scandir walk.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".aidocs-tmp",
    }
)

_CACHE_TTL_S = 300.0

# (root_str, language) -> (monotonic_deadline, loc)
_cache: dict[tuple[str, Language], tuple[float, int]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    """Forget all memoized LOC counts (tests + post-large-edit callers)."""
    with _cache_lock:
        _cache.clear()


def _count_uncached(project_root: Path, language: Language) -> int:
    exts = language.extensions
    total = 0
    stack: list[str] = [str(project_root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in _SKIP_DIRS:
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.lower().endswith(exts):
                                total += _count_lines(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def count_language_loc(project_root: Path, language: Language) -> int:
    """LOC for ``language`` under ``project_root`` (memoized, TTL 300s)."""
    key = (str(project_root), language)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    loc = _count_uncached(Path(project_root), language)
    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL_S, loc)
    return loc


def verdict(project_root: Path, language: Language, threshold: int) -> MaterialityVerdict:
    """Decide whether (project × language) clears the spend threshold."""
    loc = count_language_loc(project_root, language)
    material = loc >= threshold
    if material:
        reason = f"material: {loc} LOC >= threshold {threshold}"
    else:
        reason = f"below threshold: {loc} LOC < {threshold} — tree-sitter floor is correct"
    return MaterialityVerdict(material=material, loc=loc, threshold=threshold, reason=reason)
