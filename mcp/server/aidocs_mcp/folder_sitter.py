"""Folder-sitter: self-write markers + retired watcher shims (2026-04-21).

ORIGINALLY a dashboard-toggled watchdog watcher for external file drops. That
watcher is RETIRED (2026-05-24): ProjectIndexSitter (`project_index_sitter.py`)
is now the SINGLE owner of external-file freshness — it does watchdog
acceleration AND always-on polling reconcile AND delete detection AND known-stale
truth, so a second parallel watcher here would only double-index. `ensure_watcher`
/ `stop_watcher` / `stop_all_watchers` remain as thin back-compat shims that
delegate to ProjectIndexSitter.

What stays NATIVE here is the SELF-WRITE SUPPRESSION machinery: AIDOCS's own edit
tools (ai_str_replace, ai_edit_lines, ai_create_file, etc.) stamp a short-TTL
marker via `mark_self_write` BEFORE they hit the filesystem; the freshness owner
consults `_is_self_write_recent` and skips matching events, so an AIDOCS-initiated
write (already reindexed on the pull path) is never double-indexed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

# (path_norm, mtime_ns) tuples recently written by AIDOCS-initiated
# tools. Marker set with 60-second TTL. The watcher checks here before
# triggering a reindex — events that match are suppressed.
_SELF_WRITE_MARKERS: dict[tuple[str, int], float] = {}
_MARKERS_LOCK = threading.Lock()
_MARKER_TTL_SECONDS = 60.0


def mark_self_write(path: str | Path, *, mtime_ns: int | None = None) -> None:
    """Mark an AIDOCS-initiated write so the folder-sitter skips its event.

    Called from the edit tools BEFORE the write lands. `mtime_ns=None`
    means "stamp whatever mtime the file ends up with at watcher-check
    time" — for atomic writes this is usually close enough that the
    watcher's resolution coalesces the match.
    """
    norm = str(path).replace("\\", "/")
    now = time.time()
    with _MARKERS_LOCK:
        # Opportunistic GC of stale markers on every insert.
        expired = [k for k, t in _SELF_WRITE_MARKERS.items() if now - t > _MARKER_TTL_SECONDS]
        for k in expired:
            _SELF_WRITE_MARKERS.pop(k, None)
        key = (norm, int(mtime_ns) if mtime_ns is not None else -1)
        _SELF_WRITE_MARKERS[key] = now


def _is_self_write_recent(path_norm: str, mtime_ns: int) -> bool:
    """True when a recent mark_self_write covers this (path, mtime) pair."""
    now = time.time()
    with _MARKERS_LOCK:
        # Exact match on (path, mtime)
        if (path_norm, mtime_ns) in _SELF_WRITE_MARKERS:
            ts = _SELF_WRITE_MARKERS[(path_norm, mtime_ns)]
            return (now - ts) <= _MARKER_TTL_SECONDS
        # Path-only match (mtime_ns=-1 sentinel) — AIDOCS wrote without
        # knowing the final mtime; honor the marker for any recent event.
        if (path_norm, -1) in _SELF_WRITE_MARKERS:
            ts = _SELF_WRITE_MARKERS[(path_norm, -1)]
            return (now - ts) <= _MARKER_TTL_SECONDS
    return False


# ── watcher API: RETIRED, delegates to ProjectIndexSitter ─────────────────
# The standalone watchdog watcher this module used to own is superseded by
# ProjectIndexSitter, which is now the SINGLE owner of external-file freshness
# (watchdog acceleration + always-on polling reconcile + delete detection +
# known-stale truth). These functions remain as thin back-compat shims that
# delegate to it, so any caller gets exactly one owner instead of two parallel
# watchers double-indexing. Only the self-write marker machinery above is still
# native to this module (ProjectIndexSitter reuses `_is_self_write_recent`, the
# edit pipeline calls `mark_self_write`).


def ensure_watcher(project_root: Path, hub: Any) -> bool:
    """RETIRED shim → ProjectIndexSitter. External-file freshness now has one
    owner; this delegates so legacy callers converge on it. Returns whether the
    index sitter is running.
    """
    try:
        from .project_index_sitter import ensure_index_sitter

        return bool(ensure_index_sitter(project_root, hub))
    except Exception:
        return False


def stop_watcher(project_root: Path) -> None:
    """RETIRED shim → ProjectIndexSitter.stop_index_sitter."""
    try:
        from .project_index_sitter import stop_index_sitter

        stop_index_sitter(project_root)
    except Exception:
        pass


def stop_all_watchers() -> None:
    """RETIRED shim → ProjectIndexSitter.stop_all_index_sitters."""
    try:
        from .project_index_sitter import stop_all_index_sitters

        stop_all_index_sitters()
    except Exception:
        pass
