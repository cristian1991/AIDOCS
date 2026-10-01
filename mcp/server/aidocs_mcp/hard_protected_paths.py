"""Hard-protected project-internal data files (dev_mode-replacement tier).

WHY THIS IS NOT ai_protect/DNT
------------------------------
``ai_protect`` writes a DNT (Do-Not-Touch) header + a SQL registry row. That
protection is SOFT: an agent declares it, and the same identity can remove it.
It makes editors careful and fences subagents, but it is not a hard fence.

This module is the HARD fence for a narrow, dangerous set: project-internal
*data / state* files that the AIDOCS tooling owns and an agent must never
hand-write — the sqlite databases (and their -wal/-shm/-journal sidecars), the
compiled AIDOCS index, and the gate-state JSON files. Corrupting any of these
breaks the gate or the index silently. There is no legitimate "task" whose
scope is "hand-edit kg.sqlite3", so unlike protected *source* (gate code,
handled by the intent-authorized infrastructure tier) there is NO NLP
auto-unlock here — the only door is an explicit user confirmation.

COMPOSITION (Empire directive 2026-06-13)
---------------------------------------
A non-overridable hardcoded CORE floor + operator-configurable EXTRA globs
from aidocs.toml ``[gate].hard_protected``. Config can only ADD to the floor;
it can never shrink it. ``is_hard_protected`` is the single predicate the
write gate, read pipeline, and judge consult so the classification cannot
drift across surfaces.

Matching is path-shape only (basename + posix-glob), case-insensitive, and
OS-agnostic (back/forward slashes normalize). No filesystem access — the
predicate stays cheap enough to call per tool event.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable

# The immutable floor. Config EXTRA globs are unioned on top; they can never
# remove an entry here. Globs are matched against the normalized (forward-
# slash, lower-cased) path with PurePosixPath.match semantics, which honors
# `**` for any-depth and `*` for a single segment.
CORE_HARD_PROTECTED_GLOBS: tuple[str, ...] = (
    # sqlite databases + every sidecar, at any depth in the project.
    "**/*.sqlite3",
    "**/*.sqlite3-wal",
    "**/*.sqlite3-shm",
    "**/*.sqlite3-journal",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.sqlite3-journal",
    # The compiled AIDOCS index marker/store.
    "**/.aidocs/index.aidocs",
    ".aidocs/index.aidocs",
    # Gate / conductor state JSON — these drive execution behavior.
    "**/query-gate.json",
    "**/aidocs-managed.json",
    "**/conductor-state.json",
    "**/plan_conductor_state.json",
    "query-gate.json",
    "aidocs-managed.json",
    "conductor-state.json",
    "plan_conductor_state.json",
)


def _normalize(path: str) -> str:
    """Forward-slash, lower-case, strip a leading ``./`` prefix only.

    Pure string op. We deliberately do NOT strip leading dots — ``.aidocs``
    and ``.MEMORY`` are dot-prefixed directories that must survive.
    """
    p = (path or "").replace("\\", "/").lower()
    while p.startswith("./"):
        p = p[2:]
    return p


def _matches(normalized: str, patterns: Iterable[str]) -> bool:
    """fnmatch-based glob match. fnmatch is NOT path-segment-aware — its ``*``
    crosses ``/`` — which is exactly what these patterns want (``**/*.sqlite3``
    and ``infra/**`` should match at any depth). A bare basename pattern (no
    ``/``) is also tried against the path's final segment so an absolute path
    classifies by filename.
    """
    base = normalized.rsplit("/", 1)[-1]
    for pat in patterns:
        if not pat:
            continue
        cand = pat.replace("\\", "/").lower()
        if fnmatch.fnmatch(normalized, cand):
            return True
        if "/" not in cand and fnmatch.fnmatch(base, cand):
            return True
    return False


def is_hard_protected(path: str, *, extra_patterns: Iterable[str] = ()) -> bool:
    """True iff ``path`` is a hard-protected project-internal data file.

    Consults the immutable CORE floor first, then any operator-configured
    EXTRA globs. Config can only widen the set.
    """
    normalized = _normalize(path)
    if not normalized:
        return False
    if _matches(normalized, CORE_HARD_PROTECTED_GLOBS):
        return True
    return _matches(normalized, tuple(extra_patterns))


def hard_protected_reason(path: str, *, extra_patterns: Iterable[str] = ()) -> str | None:
    """Return a human-readable reason iff the path is hard-protected, else None.

    The reason is surfaced in the gate refusal so the agent learns WHY the
    write is fenced and that the only door is user confirmation.
    """
    if not is_hard_protected(path, extra_patterns=extra_patterns):
        return None
    return (
        f"'{path}' is a hard-protected project-internal data file (sqlite DB, "
        f"AIDOCS index, or gate-state). The tooling owns it; agents must never "
        f"hand-write it. Editing requires the RBAC-resolved hard-protected-edit "
        f"grant (role check + escalation)."
    )
