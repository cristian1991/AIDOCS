"""Rate admission for the §5.8 application-pairing ceremony.

RFC 0003 requires pairing-secret issuance and redemption to be rate-limited but
leaves the numeric runtime policy to deployment (§22). This module therefore
contains mechanics, not policy: callers provide explicit issue/redeem limits.

The S18 transport MUST pass only server-derived axes (for example the
authenticated control principal/service and the trusted edge client address).
No binding id, tenant id, pairing secret, JTI or other request-body field is a
rate identity.

Admission across the supplied axes is atomic. Every bucket is checked first;
only when ALL admit is the timestamp appended to any bucket. This matters for
shared axes: an attacker already exhausted on its actor bucket cannot burn a
fresh IP/service bucket merely by causing refusals.
"""
from __future__ import annotations

import collections
import math
import threading
import time
from collections.abc import Callable, Hashable, Iterable

__all__ = ["PairingRateLimiter"]

_OPERATIONS = frozenset({"issue", "redeem"})


class PairingRateLimiter:
    """Bounded sliding-window admission for pairing issue/redeem operations.

    admit(operation, axes) returns (ok, retry_after_seconds).

    axes is one or more hashable, server-derived identity keys. The same key
    has independent issue and redeem buckets, including independent bounded
    key capacity. Live buckets are never evicted to admit a fresh identity.
    On refusal, no supplied bucket is charged. retry_after is the time until
    all currently blocking conditions could admit.
    """

    def __init__(
        self,
        *,
        issue_limit: int,
        redeem_limit: int,
        window_s: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        for name, value in (
            ("issue_limit", issue_limit),
            ("redeem_limit", redeem_limit),
            ("window_s", window_s),
            ("max_keys", max_keys),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._limits = {"issue": issue_limit, "redeem": redeem_limit}
        self._window = window_s
        self._clock = clock
        # Capacity is PER operation. Untrusted bootstrap redemption pressure
        # must never evict or starve the authenticated platform issue budget.
        self._max_keys = max_keys
        self._hits: dict[str, "collections.OrderedDict[Hashable, collections.deque[float]]"] = {
            op: collections.OrderedDict() for op in _OPERATIONS
        }
        self._lock = threading.Lock()

    @staticmethod
    def _axes(value: Iterable[Hashable] | None) -> tuple[Hashable, ...]:
        if value is None:
            raise ValueError("at least one server-derived rate axis is required")
        try:
            raw = tuple(value)
        except TypeError as exc:
            raise ValueError("rate axes must be an iterable of hashable keys") from exc
        if not raw:
            raise ValueError("at least one server-derived rate axis is required")

        seen: set[Hashable] = set()
        out: list[Hashable] = []
        for key in raw:
            try:
                hash(key)
            except TypeError as exc:
                raise ValueError("rate axes must be hashable") from exc
            if key not in seen:
                seen.add(key)
                out.append(key)
        return tuple(out)

    def admit(
        self,
        operation: str,
        axes: Iterable[Hashable] | None,
    ) -> tuple[bool, int]:
        if operation not in _OPERATIONS:
            raise ValueError("pairing rate operation must be 'issue' or 'redeem'")
        keys = self._axes(axes)
        now = self._clock()
        limit = self._limits[operation]

        with self._lock:
            buckets = self._hits[operation]
            cutoff = now - self._window

            # Ordered by the last SUCCESSFUL admission. If the oldest bucket's
            # newest hit has expired, the entire bucket is dead and can be
            # reclaimed. Once the oldest live bucket is reached, every later
            # bucket is live too.
            while buckets:
                _oldest_key, oldest_hits = next(iter(buckets.items()))
                if oldest_hits and oldest_hits[-1] > cutoff:
                    break
                buckets.popitem(last=False)

            refusals: list[int] = []
            resolved: list[tuple[Hashable, collections.deque[float] | None]] = []
            missing = 0
            for key in keys:
                hits = buckets.get(key)
                if hits is None:
                    missing += 1
                else:
                    while hits and hits[0] <= cutoff:
                        hits.popleft()
                    if len(hits) >= limit:
                        refusals.append(max(1, math.ceil(hits[0] + self._window - now)))
                resolved.append((key, hits))

            # Capacity is security state, not a cache: NEVER evict a live bucket
            # to admit a fresh identity. Refusal mutates nothing.
            if len(buckets) + missing > self._max_keys:
                if buckets:
                    oldest_hits = next(iter(buckets.values()))
                    capacity_retry = max(
                        1,
                        math.ceil(oldest_hits[-1] + self._window - now),
                    )
                else:
                    capacity_retry = self._window
                refusals.append(capacity_retry)

            if refusals:
                return False, max(refusals)

            for key, hits in resolved:
                if hits is None:
                    hits = collections.deque()
                    buckets[key] = hits
                else:
                    buckets.move_to_end(key)
                hits.append(now)

            return True, 0

    def tracked_keys(self) -> int:
        with self._lock:
            return sum(len(buckets) for buckets in self._hits.values())
