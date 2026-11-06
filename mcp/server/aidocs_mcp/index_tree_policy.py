"""ONE pruned-directory policy for project discovery and main-index admission.

MEASURED 2026-10-17 (read-only audit): with ~30 git worktrees under ``.tmp/``
the DNT walk read 16 KB of ~124k files on every ``ai_index_sync`` of a
5k-file project, the schema walk listed (and sorted) every path including
``.git`` before filtering, the index sitter treated every worktree edit as a
MAIN-index change, and edits made through AIDOCS tools inside a worktree wrote
``.tmp/wt-*`` rows into the main code index. Each walker had its own fixed
skip list and none of them knew about worktrees.

This module is the single answer to "is this directory part of the project
the MAIN index describes?". It is used in two places:

* DESCENT: :func:`walk` wraps ``os.walk`` and prunes a directory before it is
  ever listed (code index walk, schema discovery, the manual DNT walk);
* ADMISSION: :func:`excluded_rel_reason` refuses a single project-relative
  path (scoped post-edit reindex, the watcher, batch-write reverification),
  so a direct edit inside ``.tmp/`` can never create or update a main-index
  row even though no walk was involved.

What is pruned (directories strictly BELOW the project root):

* ``.git`` and ``.tmp`` (any case, ``.TMP`` included);
* ``site-packages`` / ``dist-packages`` (vendored interpreters under scratch);
* dot-directories, as the code index already did;
* nested git checkouts, named apart: a ``.git`` file pointing into
  ``.git/modules`` (``submodule``), any other ``.git`` file (linked worktree,
  ``nested_worktree``) or a ``.git`` directory (``nested_clone``);
* a symlink / junction whose target lies in an excluded part of the root;
* a symlink / junction (directory OR file) whose REAL target lies outside
  the root (``link_outside_root``): real paths are resolved before anything
  is admitted, and what cannot be resolved is refused, never followed.

Walker-specific skip lists (``node_modules``, ``migrations`` ...) stay with
their walkers; this policy is the COMMON floor, not a replacement.

Nothing is excluded silently: :class:`PruneReport` counts every pruned
directory by reason, keeps a few examples, and renders one explicit
``diagnostics`` entry per reason (what, how many, WHY - :data:`REASON_WHY`)
plus the rule itself (:data:`EXCLUSION_RULE`); walkers and the scoped
admission check surface it in the sync result.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

# Lower-cased directory name -> reason. Compared case-insensitively because the
# Windows filesystem is: `.TMP` is the same directory as `.tmp`.
PRUNE_NAMES: dict[str, str] = {
    ".git": "git",
    ".tmp": "tmp",
    "site-packages": "site_packages",
    "dist-packages": "site_packages",
}

_EXAMPLES_PER_REASON = 5

# `.git` marker probes are cached briefly for the ADMISSION hot path: the
# watcher can ask about the same ancestors thousands of times a second during
# a burst. Staleness is bounded (r0b2 re-review 3):
#   * walks never use the cache (dir_prune_reason probes fresh), so a full
#     sync right after a checkout appears prunes it;
#   * every watcher event drops the cached probe of its own path and parent
#     (invalidate_marker, called by project_index_sitter.event_relevant), so
#     the event that creates a `.git` closes the window immediately;
#   * what remains: a scoped admission (post-edit reindex) of a file in a
#     checkout created < _MARKER_TTL_S ago with no watcher event in between.
#     Accepted - the main index is a search index, not a security boundary
#     (edit and DNT gates read disk), and the next full walk purges the row.
# Inside the TTL a new worktree is otherwise seen at most _MARKER_TTL_S late; `.tmp/` worktrees are
# excluded by name regardless of the cache.
_MARKER_TTL_S = 5.0
_MARKER_CACHE_MAX = 50_000
_MARKER_CACHE: dict[str, tuple[float, str | None]] = {}
_MARKER_LOCK = threading.Lock()

# EXCLUSION RULE (r0b2 finding 4) - what the MAIN index never describes, and
# why. Surfaced verbatim in every report so an exclusion is never silent.
EXCLUSION_RULE = (
    "The main index describes only files of THIS checkout that lie inside the "
    "root. Pruned below the root: .git, .tmp (any case), site-packages / "
    "dist-packages, dot-directories, every nested git checkout (submodule, "
    "linked worktree, nested clone - each is its own project with its own "
    "index), and any symlink or junction whose real target is outside the "
    "root, unresolvable, a cycle, or inside a pruned directory."
)

REASON_WHY: dict[str, str] = {
    "git": "git metadata directory, not source",
    "tmp": ".tmp scratch / worktree area; worktrees index themselves",
    "site_packages": "vendored interpreter packages, not project source",
    "dot_dir": "dot-directory (tool / editor / cache state), not project source",
    "submodule": "git submodule (its .git file points into .git/modules); a separate repository",
    "nested_worktree": "linked git worktree or other nested checkout (.git file); a separate checkout",
    "nested_clone": "nested git clone (.git directory); a separate repository",
    "link_outside_root": "symlink/junction whose real target is outside the project root (refused, not followed)",
    "link_unresolvable": "symlink/junction whose target cannot be resolved (refused, not followed)",
    "link_cycle": "symlink/junction that loops back to an ancestor",
    "outside_root": "path is not inside the project root",
}


def explain_reason(reason: str) -> str:
    """Human explanation of an exclusion reason (``link_into_<r>`` included)."""
    if reason.startswith("link_into_"):
        inner = reason[len("link_into_") :]
        return f"symlink/junction into an excluded directory ({inner}: {explain_reason(inner)})"
    return REASON_WHY.get(reason, f"excluded by the main-tree policy ({reason})")


def diagnostics(counts: dict[str, int], examples: dict[str, list[str]]) -> list[dict[str, object]]:
    """One explicit entry per reason: what was excluded, how many, and why."""
    return [
        {"reason": r, "count": counts[r], "why": explain_reason(r), "examples": list(examples.get(r, []))}
        for r in sorted(counts)
    ]


class PruneReport:
    """Counts (and a few examples) of what a walk pruned, by reason."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.examples: dict[str, list[str]] = {}

    def add(self, reason: str, rel: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        bucket = self.examples.setdefault(reason, [])
        if len(bucket) < _EXAMPLES_PER_REASON:
            bucket.append(rel)

    def as_dict(self) -> dict[str, object]:
        return {
            "pruned_dirs": sum(self.counts.values()),
            "reasons": dict(self.counts),
            "examples": {k: list(v) for k, v in self.examples.items()},
            "diagnostics": diagnostics(self.counts, self.examples),
            "rule": EXCLUSION_RULE,
        }


def _marker_cache_clear() -> None:
    """Drop cached `.git` probes (tests)."""
    with _MARKER_LOCK:
        _MARKER_CACHE.clear()


def _classify_git_marker(marker: str) -> str | None:
    """Reason for a directory holding ``marker`` (its ``.git``), or None.
    Any ``.git`` entry excludes the directory; the kind only names why."""
    if not os.path.lexists(marker):
        return None
    if os.path.isdir(marker) and not _is_link(marker):
        return "nested_clone"
    try:
        with open(marker, encoding="utf-8", errors="replace") as fh:
            first = fh.read(1024)
    except OSError:
        return "nested_worktree"  # present but unreadable: still a nested checkout
    gitdir = first.replace("\\", "/").lower()
    if gitdir.startswith("gitdir:") and "/modules/" in gitdir:
        return "submodule"
    return "nested_worktree"


def invalidate_marker(dir_abs: str) -> None:
    """Drop the cached `.git` probe of one directory (watcher events)."""
    key = os.path.normcase(os.path.abspath(dir_abs))
    with _MARKER_LOCK:
        _MARKER_CACHE.pop(key, None)


def _git_marker_kind(dir_abs: str, *, use_cache: bool = True) -> str | None:
    if not use_cache:
        try:
            return _classify_git_marker(os.path.join(dir_abs, ".git"))
        except (OSError, ValueError):
            return None
    key = os.path.normcase(os.path.abspath(dir_abs))
    now = time.monotonic()
    with _MARKER_LOCK:
        hit = _MARKER_CACHE.get(key)
        if hit is not None and now - hit[0] < _MARKER_TTL_S:
            return hit[1]
    try:
        kind = _classify_git_marker(os.path.join(dir_abs, ".git"))
    except (OSError, ValueError):
        kind = None
    with _MARKER_LOCK:
        if len(_MARKER_CACHE) >= _MARKER_CACHE_MAX:
            _MARKER_CACHE.clear()
        _MARKER_CACHE[key] = (now, kind)
    return kind


def _is_link(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except (OSError, ValueError):
        return False


def _rel_inside(root_abs: str, target_abs: str) -> str | None:
    """Project-relative posix path of target, or None when outside root."""
    try:
        rel = os.path.relpath(target_abs, root_abs)
    except ValueError:  # different drive on Windows
        return None
    rel = rel.replace("\\", "/")
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None if rel != "." else ""
    return rel


def name_prune_reason(name: str, *, prune_dot_dirs: bool = True) -> str | None:
    """Reason a directory NAME alone is pruned (no filesystem access)."""
    reason = PRUNE_NAMES.get(name.lower())
    if reason:
        return reason
    if prune_dot_dirs and name.startswith("."):
        return "dot_dir"
    return None


def dir_prune_reason(
    root_abs: str,
    parent_abs: str,
    name: str,
    *,
    prune_dot_dirs: bool = True,
) -> str | None:
    """Why the directory ``parent_abs/name`` (below ``root_abs``) is pruned,
    or None to descend."""
    reason = name_prune_reason(name, prune_dot_dirs=prune_dot_dirs)
    if reason:
        return reason
    full = os.path.join(parent_abs, name)
    kind = _git_marker_kind(full, use_cache=False)  # walks probe fresh
    if kind:
        return kind
    if _is_link(full):
        if not os.path.exists(full):
            return "link_unresolvable"  # dangling: target does not exist
        try:
            real = os.path.realpath(full)
        except (OSError, ValueError):
            return "link_unresolvable"
        real_root = os.path.realpath(root_abs)
        rel = _rel_inside(real_root, real)
        if rel is None:
            # FAIL CLOSED (r0b2 finding 3): a link whose REAL target is
            # outside the root is never descended (os.walk follows a
            # Windows junction even with followlinks=False).
            return "link_outside_root"
        if rel == "":
            return "link_cycle"
        inner = excluded_rel_reason(real_root, rel + "/x", _follow_links=False)
        if inner:
            return f"link_into_{inner}"
        # a link back to an ancestor of itself would loop
        parent_real = os.path.realpath(parent_abs)
        if os.path.normcase(parent_real).startswith(os.path.normcase(real) + os.sep) or (
            os.path.normcase(parent_real) == os.path.normcase(real)
        ):
            return "link_cycle"
    return None


def walk(
    root: str | Path,
    *,
    prune_dot_dirs: bool = True,
    report: PruneReport | None = None,
) -> Iterator[tuple[str, list[str], list[str]]]:
    """``os.walk(root)`` with the policy applied at descent.

    Pruned directories are removed from ``dirnames`` BEFORE the caller sees
    them, so the caller's own in-place pruning composes on top and an excluded
    subtree is never listed."""
    root_abs = str(root)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        keep: list[str] = []
        for d in dirnames:
            reason = dir_prune_reason(root_abs, dirpath, d, prune_dot_dirs=prune_dot_dirs)
            if reason is None:
                keep.append(d)
            elif report is not None:
                rel = _rel_inside(root_abs, os.path.join(dirpath, d)) or d
                report.add(reason, rel)
        dirnames[:] = keep
        # A FILE link (or a dangling junction, which os.walk lists as a file)
        # is judged by the SAME admission check the scoped sync uses, so the
        # full walk and excluded_rel_reason can never disagree: outside the
        # root, into an excluded in-root dir, or dangling -> dropped. One
        # lstat per file; only links pay the realpath.
        files = [f for f in filenames if not _file_link_refused(root_abs, dirpath, f, prune_dot_dirs, report)]
        filenames[:] = files
        yield dirpath, dirnames, filenames


def _file_link_refused(
    root_abs: str, dirpath: str, name: str, prune_dot_dirs: bool, report: PruneReport | None
) -> bool:
    full = os.path.join(dirpath, name)
    if not _is_link(full):
        return False
    rel = _rel_inside(root_abs, full)
    reason = "outside_root" if not rel else excluded_rel_reason(root_abs, rel, prune_dot_dirs=prune_dot_dirs)
    if reason and report is not None:
        report.add(reason, rel or name)
    return reason is not None


def _dangling_link_on_path(root_abs: str, parts: list[str]) -> bool:
    """True when some prefix of ``parts`` is an entry that exists as a link
    (lexists) but whose target does not (not exists). Only called for paths
    that do not exist, so the hot path never pays it."""
    cur = root_abs
    for seg in parts:
        cur = os.path.join(cur, seg)
        if not os.path.lexists(cur):
            return False
        if not os.path.exists(cur):
            return True
    return False


def _normalize_rel(rel: str) -> str | None:
    text = str(rel or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    norm = os.path.normpath(text).replace("\\", "/")
    if norm == ".." or norm.startswith("../"):
        return None
    return norm


def excluded_rel_reason(
    project_root: str | Path,
    rel: str,
    *,
    prune_dot_dirs: bool = True,
    _follow_links: bool = True,
) -> str | None:
    """ADMISSION check for one project-relative FILE path.

    Returns the reason the path is outside the main index's tree (same reasons
    as :func:`walk`, plus ``outside_root``), or None to admit. Separators and
    case are normalised (``.TMP\\wt\\a.py`` is ``.tmp/wt/a.py``); a path that
    reaches an excluded directory through a symlink or junction is refused on
    its resolved location."""
    norm = _normalize_rel(rel)
    if norm is None:
        return "outside_root"
    parts = norm.split("/")
    root_abs = str(project_root)
    cur = root_abs
    for seg in parts[:-1]:
        reason = name_prune_reason(seg, prune_dot_dirs=prune_dot_dirs)
        if reason:
            return reason
        cur = os.path.join(cur, seg)
        kind = _git_marker_kind(cur)
        if kind:
            return kind
    if _follow_links:
        full = os.path.join(root_abs, *parts)
        # DANGLING: realpath(strict=False) "resolves" a link whose target is
        # gone, so test it: a missing path with a link on the way whose
        # target does not exist is unresolvable (a plain deletion is not).
        if not os.path.exists(full) and _dangling_link_on_path(root_abs, parts):
            return "link_unresolvable"
        try:
            real = os.path.realpath(full)
            real_root = os.path.realpath(root_abs)
        except (OSError, ValueError):
            return "link_unresolvable"  # fail closed: never admit what cannot be resolved
        real_rel = _rel_inside(real_root, real)
        if real_rel is None:
            # The path lies in the root but resolves OUTSIDE it (symlink or
            # junction somewhere on the way): refused, never followed.
            return "link_outside_root"
        if real_rel and os.path.normcase(real_rel) != os.path.normcase(norm):
            inner = excluded_rel_reason(
                real_root, real_rel, prune_dot_dirs=prune_dot_dirs, _follow_links=False
            )
            if inner:
                return f"link_into_{inner}"
    return None
