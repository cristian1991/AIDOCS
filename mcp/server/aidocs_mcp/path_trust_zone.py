"""SEC-004 (2026-04-23) — external path trust zones.

Classifies a file-system target path into one of five zones. Used by
the orchestrator's check_tool to enforce zone-aware default policy
BEFORE other logic. Keeps zone semantics out of the orchestrator so
the classifier is testable in isolation.

Zones:
  project_internal             — inside project_root, NOT in .MEMORY/
  memory_internal              — inside <project_root>/.MEMORY/
  approved_external_workspace  — prefix-match on operator-configured
                                 security.approved_external_roots list
  blocked_sensitive_external   — inside a sensitive home subdir
                                 (.ssh, .aws, .config on Unix;
                                 .ssh, AppData on Windows). Match
                                 regardless of OS so a hand-moved
                                 .ssh on Windows still blocks.
  unknown_external             — anything else not inside project_root

Classification is HIERARCHICAL and greedy:
  1. Is the path inside approved_external_roots? → approved
     (checked BEFORE project_internal so an approved-root that
     overlaps with the project still classifies as approved — the
     spec wants explicit approval to win.)
  2. Is the path inside project_root/.MEMORY/? → memory_internal
  3. Is the path inside project_root? → project_internal
  4. Is the path inside any sensitive home subdir? → blocked
  5. Otherwise → unknown_external

Edge cases:
  - Empty path → project_internal (no external exposure).
  - Relative path → resolved against project_root first.
  - Symlink resolution is opt-in (default False) — following symlinks
    could mis-classify a sensitive target reached via a cute symlink
    sitting inside project_root. Caller passes follow_symlinks=True
    when they want resolved semantics.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import Enum
from pathlib import Path


class PathTrustZone(str, Enum):
    PROJECT_INTERNAL = "project_internal"
    MEMORY_INTERNAL = "memory_internal"
    APPROVED_EXTERNAL_WORKSPACE = "approved_external_workspace"
    BLOCKED_SENSITIVE_EXTERNAL = "blocked_sensitive_external"
    UNKNOWN_EXTERNAL = "unknown_external"


# Sensitive home subdirectories. Matched case-insensitively relative
# to home_dir so HOME=/home/alice + ".ssh" matches /home/alice/.ssh/*
# and HOME=C:\Users\Alice + "AppData" matches C:\Users\Alice\AppData\*.
# Keep this list minimal per SEC-004 scope — SEC-006/future can
# expand. Add a new entry only when the operator hits a real
# miss; don't pre-emptively bloat.
_SENSITIVE_HOME_SUBDIRS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gcloud",
    ".azure",
    ".config",  # unix catch-all for editor profiles, shell rc
    "appdata",  # windows per-user state
)


def _resolve(path: os.PathLike | str, follow_symlinks: bool = False) -> Path:
    """Return an absolute Path without resolving symlinks by default.
    follow_symlinks=True triggers .resolve() for callers that want
    the canonical target.
    """
    p = Path(path)
    if follow_symlinks:
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return p.absolute()
    return p.absolute()


def _path_contains(root: Path, candidate: Path) -> bool:
    """True iff candidate is root itself or inside root. Case-insensitive
    on Windows where the filesystem is case-insensitive; strict on Unix.
    """
    try:
        root_s = str(root).rstrip("\\/").lower() if os.name == "nt" else str(root).rstrip("/")
        cand_s = str(candidate).lower() if os.name == "nt" else str(candidate)
    except Exception:
        return False
    if cand_s == root_s:
        return True
    # Normalize trailing separators and require a boundary match.
    sep = os.sep
    return cand_s.startswith(root_s + sep) or cand_s.startswith(root_s + "/")


def _resolve_target(
    target: os.PathLike | str | None,
    project_root: Path,
) -> Path:
    """Resolve the caller's target. Empty/None → project_root itself
    (project_internal). Relative paths resolved against project_root.
    """
    if target is None:
        return Path(project_root).absolute()
    s = str(target).strip()
    if not s:
        return Path(project_root).absolute()
    p = Path(s)
    if not p.is_absolute():
        p = Path(project_root) / p
    return p.absolute()


def _classify_resolved(
    resolved: Path,
    *,
    root: Path,
    approved_external_roots: Iterable[str] | None = None,
    home_dir: os.PathLike | str | None = None,
) -> PathTrustZone:
    """Classify an ALREADY-resolved absolute Path.

    Internal helper. Both ``resolve_under_project`` and the public
    ``classify_path`` route through this so a single resolution
    feeds the classification — no risk of resolver/classifier drift.
    """
    # 1. Approved external (prefix match wins over project_internal).
    if approved_external_roots:
        for approved in approved_external_roots:
            if not approved:
                continue
            ap = Path(approved).absolute()
            if _path_contains(ap, resolved):
                return PathTrustZone.APPROVED_EXTERNAL_WORKSPACE

    # 2. memory_internal — inside project_root/.MEMORY/
    memory_root = root / ".MEMORY"
    if _path_contains(memory_root, resolved):
        return PathTrustZone.MEMORY_INTERNAL

    # 3. project_internal — inside project_root
    if _path_contains(root, resolved):
        return PathTrustZone.PROJECT_INTERNAL

    # 4. blocked sensitive external — inside a sensitive home subdir
    home = Path(home_dir).absolute() if home_dir else Path.home()
    for sub in _SENSITIVE_HOME_SUBDIRS:
        sensitive_root = home / sub
        if _path_contains(sensitive_root, resolved):
            return PathTrustZone.BLOCKED_SENSITIVE_EXTERNAL

    # 5. unknown external fallback
    return PathTrustZone.UNKNOWN_EXTERNAL


def resolve_under_project(
    target: os.PathLike | str | None,
    *,
    project_root: os.PathLike | str,
    approved_external_roots: Iterable[str] | None = None,
    home_dir: os.PathLike | str | None = None,
    follow_symlinks: bool = False,
) -> tuple[Path, PathTrustZone]:
    """Single canonical resolver for read-pipeline gates.

    Returns ``(absolute_resolved_path, zone)``. Relative input is
    resolved against ``project_root`` — never against process cwd.
    Resolution happens ONCE; the same resolved Path is then
    classified via ``_classify_resolved`` so the resolver and
    classifier cannot drift apart.
    """
    root = Path(project_root).absolute()
    resolved = _resolve_target(target, root)
    if follow_symlinks:
        try:
            resolved = resolved.resolve()
        except (OSError, RuntimeError):
            pass
    zone = _classify_resolved(
        resolved,
        root=root,
        approved_external_roots=approved_external_roots,
        home_dir=home_dir,
    )
    return resolved, zone


def classify_path(
    target: os.PathLike | str | None,
    *,
    project_root: os.PathLike | str,
    approved_external_roots: Iterable[str] | None = None,
    home_dir: os.PathLike | str | None = None,
    follow_symlinks: bool = False,
) -> PathTrustZone:
    """Return the trust zone for target. See module docstring for the
    decision hierarchy.

    Public compatibility wrapper. Routes through the same shared
    resolver/classifier as ``resolve_under_project`` so callers that
    only need the zone get the same classification path.
    """
    root = Path(project_root).absolute()
    resolved = _resolve_target(target, root)
    if follow_symlinks:
        try:
            resolved = resolved.resolve()
        except (OSError, RuntimeError):
            pass
    return _classify_resolved(
        resolved,
        root=root,
        approved_external_roots=approved_external_roots,
        home_dir=home_dir,
    )
