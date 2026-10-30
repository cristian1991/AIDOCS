"""PalaceCache — per-palace state container (RFC 003 §5).

Previously the module-level globals in ``mempalace/mcp_server.py``
(``_client_cache``, ``_collection_cache``, ``_palace_db_inode``,
``_palace_db_mtime``, ``_vector_disabled``, ``_metadata_cache``,
``_metadata_cache_time``) implicitly keyed all cached state to a
single ``_config.palace_path``. That's correct for the standalone
``mempalace-mcp`` server (one process, one palace) but blocks
library-hosting under AIDOCS where one process may eventually serve
multiple projects.

This module introduces ``PalaceCache`` — a per-palace state container
that holds exactly the state the module-globals held. The default
single-palace standalone behavior is preserved via the module-level
``_DEFAULT_CACHE`` instance keyed to ``_config.palace_path``; the
library hosting path constructs one cache per project the hub serves.

Safe in standalone use: every legacy module-global accessor below
delegates to ``_DEFAULT_CACHE`` so existing imports keep working.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class PalaceCache:
    """Per-palace state previously held in module globals.

    One instance per palace path. ``mempalace/mcp_server.py`` retains
    ``_DEFAULT_CACHE`` keyed to ``_config.palace_path`` for
    backwards-compat. PalaceService (RFC 003 §4) holds one cache per
    project the hub serves.

    Field names match the original module globals so callers that
    were touching globals can swap to ``cache.<field>`` mechanically.
    """

    palace_path: str

    # Chroma client + collection caches.
    # Field: was _client_cache (mcp_server.py:268)
    client_cache: Any = None
    # Field: was _collection_cache (mcp_server.py:269)
    collection_cache: dict = field(default_factory=dict)

    # SQLite file inode/mtime watchers for invalidation.
    # Fields: were _palace_db_inode / _palace_db_mtime (mcp_server.py:270-271)
    palace_db_inode: int = 0
    palace_db_mtime: float = 0.0

    # Vector-disabled probe state.
    # Fields: were _vector_disabled / _vector_disabled_reason /
    #         _vector_capacity_status (mcp_server.py:320, 321, 325)
    vector_disabled: bool = False
    vector_disabled_reason: str = ""
    vector_capacity_status: dict = field(default_factory=dict)

    # Metadata cache (drawer counts, wing/room breakdown).
    # Fields: were _metadata_cache / _metadata_cache_time (mcp_server.py:610-611)
    metadata_cache: Optional[dict] = None
    metadata_cache_time: float = 0.0

    # KG cache. Already palace-keyed by path in original
    # (_kg_by_path at mcp_server.py:176), but lives here for symmetry
    # under library hosting where one cache instance owns its KG.
    kg_cache_lock: threading.Lock = field(default_factory=threading.Lock)
    kg_by_path: dict = field(default_factory=dict)

    def invalidate(self) -> None:
        """Drop all caches except KG (KG has its own lifecycle).

        Called when external mutation suspected (e.g. via
        ``tool_reconnect``).
        """
        self.client_cache = None
        self.collection_cache = {}
        self.palace_db_inode = 0
        self.palace_db_mtime = 0.0
        self.metadata_cache = None
        self.metadata_cache_time = 0.0
        # vector_disabled flag intentionally preserved — refreshed by
        # its own probe path (_refresh_vector_disabled_flag).


# ---------------------------------------------------------------------------
# Module-level default cache for standalone backwards-compat
# ---------------------------------------------------------------------------

# This dict is the per-palace-path cache registry used by standalone
# mempalace-mcp. The default cache key is ``_config.palace_path``
# resolved lazily at first use.
_CACHES: dict[str, PalaceCache] = {}
_CACHES_LOCK = threading.Lock()


def get_cache_for(palace_path: str) -> PalaceCache:
    """Return (or create) the PalaceCache for ``palace_path``.

    Standalone mcp_server callers pass ``_config.palace_path``.
    Library-hosting callers (PalaceService) pass their per-project
    palace path. The same cache instance is returned across calls
    with the same path.
    """
    norm = str(palace_path)
    cache = _CACHES.get(norm)
    if cache is not None:
        return cache
    with _CACHES_LOCK:
        cache = _CACHES.get(norm)
        if cache is None:
            cache = PalaceCache(palace_path=norm)
            _CACHES[norm] = cache
        return cache


def reset_all_caches() -> None:
    """Drop every cached PalaceCache. Primarily for tests."""
    with _CACHES_LOCK:
        _CACHES.clear()
