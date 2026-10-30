"""The law for caching expensive PROOFS (proof-cost governance doctrine, Art.
VII). A proof result may be reused ONLY when a CONTENT fingerprint of its inputs
matches exactly. This makes a cache verdict-identical rather than a staleness
risk: a stale proof can never become authority because the cache key IS the
content hash of the inputs the proof depends on.

Rules, all fail-closed:
  * An empty/None fingerprint ⇒ NEVER serve a cached value — recompute. A proof
    whose inputs cannot be fingerprinted is not safe to cache.
  * A different fingerprint ⇒ recompute AND overwrite, so the previous value is
    dropped and can never be served again (single-slot, latest-content-wins).
  * Only an exact fingerprint match returns the cached value.

This is deliberately NOT a general LRU/TTL cache. TTLs let stale proofs survive;
content fingerprints do not. Time may not vouch for authority — only content may.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FingerprintCache:
    """Single-slot, content-fingerprint-keyed proof cache. Latest content wins;
    a non-matching or absent key always recomputes (fail-closed).
    """

    def __init__(self, name: str = "") -> None:
        self._name = name
        self._key: str | None = None
        self._value: Any = None
        self._has_value = False
        self.hits = 0
        self.misses = 0

    def get_or_compute(self, fingerprint: str | None, compute: Callable[[], Any]) -> Any:
        # Fail-closed: a proof whose inputs cannot be fingerprinted is never
        # served from cache — recompute every time, cache nothing.
        if not fingerprint:
            self.misses += 1
            return compute()
        if self._has_value and fingerprint == self._key:
            self.hits += 1
            return self._value
        # Miss: recompute and OVERWRITE. The prior value is dropped so a stale
        # proof cannot be served once the content fingerprint has moved on.
        value = compute()
        self._key = fingerprint
        self._value = value
        self._has_value = True
        self.misses += 1
        return value

    def invalidate(self) -> None:
        self._key = None
        self._value = None
        self._has_value = False

    def stats(self) -> dict:
        return {
            "name": self._name,
            "hits": self.hits,
            "misses": self.misses,
            "cached_key": self._key,
            "has_value": self._has_value,
        }
