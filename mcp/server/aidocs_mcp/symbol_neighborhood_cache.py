"""Symbol-neighborhood cache — Layer 4 cross-project index slice 3.

`code_find_symbol` + `related_project_symbol_bundle` do the same work
repeatedly: given symbol X, walk the edges to callers/callees/siblings,
fetch their snippets, return. When an operator investigates a module
they hit the same symbols over and over; the edge walk is expensive
when it spans 50+ projects.

This cache memoizes the neighborhood: (project_id, symbol_id) →
{callers, callees, siblings, last_refreshed}. The cache is advisory —
callers can force-refresh or treat entries older than a TTL as stale.

Per-process in-memory store. Phase 2 promotes to sqlite (shared
across MCP workers). API stays identical.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_DEFAULT_TTL_SECONDS = 600.0  # 10 minutes — shorter than a typical
# coding session but long enough that a refactor touching many
# neighbors doesn't re-walk the graph on every call.


@dataclass
class Neighborhood:
    """One cached symbol neighborhood.

    project_id: the project that owns the symbol. Carried in the key
        so cross-project symbols with the same name don't collide.
    symbol_id: stable identifier — (path, name) tuple serialized, or
        whatever the caller's symbol store uses.
    callers: symbols that reference this one.
    callees: symbols this one references.
    siblings: symbols defined in the same file/class/module.
    fetched_at: monotonic timestamp of the last refresh.
    """

    project_id: str
    symbol_id: str
    callers: tuple[str, ...] = ()
    callees: tuple[str, ...] = ()
    siblings: tuple[str, ...] = ()
    fetched_at: float = 0.0


class SymbolNeighborhoodCache:
    """In-memory TTL cache for cross-project symbol neighborhoods.

    Thread-safe on the simple side: Python dict ops are atomic enough
    for this store, and the TTL read path is a single lookup. If
    Phase 2 hits a concurrent-writer problem the sqlite migration
    adds real locking.
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._cache: dict[tuple[str, str], Neighborhood] = {}

    def set(
        self,
        project_id: str,
        symbol_id: str,
        callers: tuple[str, ...] = (),
        callees: tuple[str, ...] = (),
        siblings: tuple[str, ...] = (),
    ) -> Neighborhood:
        """Record a fresh neighborhood. Replaces any existing entry."""
        key = self._key(project_id, symbol_id)
        entry = Neighborhood(
            project_id=key[0],
            symbol_id=key[1],
            callers=tuple(callers),
            callees=tuple(callees),
            siblings=tuple(siblings),
            fetched_at=time.monotonic(),
        )
        self._cache[key] = entry
        return entry

    def get(
        self,
        project_id: str,
        symbol_id: str,
        *,
        allow_stale: bool = False,
    ) -> Neighborhood | None:
        """Look up a cached neighborhood.

        allow_stale=False → return None when the entry is older than
            TTL (caller should re-walk and set() the fresh result).
        allow_stale=True → return the entry regardless; useful for
            "best-effort" surfaces like the dashboard.
        """
        key = self._key(project_id, symbol_id)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if not allow_stale and self._is_stale(entry):
            return None
        return entry

    def invalidate(self, project_id: str, symbol_id: str) -> bool:
        """Force-drop one cache entry. Returns True iff present."""
        return self._cache.pop(self._key(project_id, symbol_id), None) is not None

    def invalidate_project(self, project_id: str) -> int:
        """Drop every entry for a project (e.g. after a re-index).
        Returns count of dropped entries.
        """
        pid = str(project_id or "").strip()
        if not pid:
            return 0
        to_drop = [k for k in self._cache if k[0] == pid]
        for k in to_drop:
            self._cache.pop(k, None)
        return len(to_drop)

    def clear(self) -> None:
        """Wipe every entry. Test-only helper."""
        self._cache.clear()

    def size(self) -> int:
        """Current cache size (fresh + stale combined)."""
        return len(self._cache)

    def fresh_size(self) -> int:
        """Count of entries still within TTL — useful for dashboard
        cache-hit-rate surfaces.
        """
        return sum(1 for entry in self._cache.values() if not self._is_stale(entry))

    def ttl_seconds(self) -> float:
        """Current TTL. Exposed for tests + diagnostics."""
        return self._ttl

    # ── internals ──

    @staticmethod
    def _key(project_id: str, symbol_id: str) -> tuple[str, str]:
        pid = str(project_id or "").strip()
        sid = str(symbol_id or "").strip()
        if not pid or not sid:
            raise ValueError("project_id and symbol_id must both be non-empty")
        return (pid, sid)

    def _is_stale(self, entry: Neighborhood) -> bool:
        if self._ttl <= 0:
            return True  # TTL=0 means "always stale" — acts like off-switch.
        age = time.monotonic() - entry.fetched_at
        return age > self._ttl
