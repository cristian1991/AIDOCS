"""Rate limiting for the human link flow -- RFC 0003 v4.6.1 §8.2 ("rate-limited;
failures audited without secrets"), r0b2 B2.

A bounded sliding window per key. Keys are SERVER-DERIVED axes only (the
client address the gate observed, the authenticated dashboard user), never
anything the model or the application's request body supplies. The route
consults it BEFORE the link service, so a refused attempt consumes no ticket,
no one-time code and no jti.

A refused attempt is not recorded (it never extends the window), and the key
space is bounded: when full, the least recently used key is evicted.
"""
from __future__ import annotations

import collections
import math
import threading
import time
from collections.abc import Callable, Hashable

__all__ = ["LinkRateLimiter"]


class LinkRateLimiter:
    """``admit(key) -> (ok, retry_after_seconds)`` over a sliding window."""

    def __init__(
        self,
        *,
        limit: int,
        window_s: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        for name, value in (("limit", limit), ("window_s", window_s), ("max_keys", max_keys)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._hits: "collections.OrderedDict[Hashable, collections.deque]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def admit(self, key: Hashable) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = collections.deque()
                self._hits[key] = hits
            else:
                self._hits.move_to_end(key)
            while hits and hits[0] <= now - self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                retry = max(1, math.ceil(hits[0] + self._window - now))
                return False, retry
            hits.append(now)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
            return True, 0

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)
