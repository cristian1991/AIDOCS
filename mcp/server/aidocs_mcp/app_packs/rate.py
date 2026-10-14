"""R-f semantic rate + concurrency -- RFC 0003 v4.6.1 §3.6 R-f, §12.5 (build plan S10).

Authoritative application counters, one set per key:

    per seat       60 calls/min, burst 15;  in-flight 4    key (tenant_id, sub, toolspace_id)
    per toolspace  600 calls/min, burst 100; in-flight 32  key (tenant_id, toolspace_id)

(§12.5 names the second row "per org" and keys it ``(tenant_id, toolspace_id)``;
that key is used verbatim.)

* **Keys are AppScope members only.** :func:`_seat_key` / :func:`_toolspace_key`
  read ``tenant_id``, ``sub`` and ``toolspace_id`` and nothing else. The
  conversation (``host_conversation_ref``), ``binding_id``, ``binding_version``
  and ``connector_id`` are evidence (§3.6) and never reach a key, so a rebind, a
  recreated / second connector or a new conversation SHARES the counters and
  cannot launder them.
* **Each limit is three checks**, all of which must pass: a sliding 60 s window
  (at most ``per_minute`` admissions in any 60 s), a token bucket of capacity
  ``burst`` refilled at ``per_minute / 60`` per second, and an in-flight cap.
  Admission is atomic across the seat AND the toolspace counters: both pass and
  both are charged, or neither is charged.
* **No queue.** A call over any limit is refused at once with
  :class:`AppRateLimited` (``app_rate_limited``, §16.3) carrying an integer
  ``retry_after`` in ``[1, MAX_RETRY_AFTER_SECONDS]``. A refusal consumes no
  capacity. An in-flight refusal cannot know when a slot frees, so it reports
  the minimum bound (1 s).
* **Policy (R-h).** Effective limits come from ``limits_for(tenant_id,
  toolspace_id)`` (the S11 ``AppPolicySource.rate_limits``); whatever it
  returns is clamped per field to the §12.5 ceilings, so no source can loosen
  them. A source failure fails closed (:class:`AppRateAdmissionUnavailable`,
  ``app_internal``).
* The per-IP limiter and the process-wide semaphore in the gate transport stay
  defense in depth only; this module never reads or replaces them.

Storage: in-process, thread-safe (one lock). Counters are per gate process; a
multi-process gate needs a shared counter store [FLAGGED: the same production
HOLD as the in-memory binding store].
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from .scope import AppScope, ConnectionContext

__all__ = [
    "CEILINGS",
    "MAX_RETRY_AFTER_SECONDS",
    "SEAT_CEILING",
    "TOOLSPACE_CEILING",
    "AppRateAdmissionUnavailable",
    "AppRateLimited",
    "AppRateLimiter",
    "RateLimit",
    "RateLimits",
    "RatePermit",
    "clamp_to_ceilings",
]

WINDOW_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 60
MIN_RETRY_AFTER_SECONDS = 1

Subject = Union[AppScope, ConnectionContext]


def _positive_int(name: str, value: Any) -> None:
    # ``type(...) is int``: a bool is never an int here.
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class RateLimit:
    per_minute: int
    burst: int
    in_flight: int

    def __post_init__(self) -> None:
        _positive_int("per_minute", self.per_minute)
        _positive_int("burst", self.burst)
        _positive_int("in_flight", self.in_flight)

    def clamp(self, ceiling: "RateLimit") -> "RateLimit":
        return RateLimit(
            per_minute=min(self.per_minute, ceiling.per_minute),
            burst=min(self.burst, ceiling.burst),
            in_flight=min(self.in_flight, ceiling.in_flight),
        )

    def within(self, ceiling: "RateLimit") -> bool:
        """True iff every field is at or below ``ceiling`` (a tightening or equal)."""
        return (
            self.per_minute <= ceiling.per_minute
            and self.burst <= ceiling.burst
            and self.in_flight <= ceiling.in_flight
        )


@dataclass(frozen=True, slots=True)
class RateLimits:
    """The effective limits for one ``(tenant_id, toolspace_id)``."""

    seat: RateLimit
    toolspace: RateLimit

    def __post_init__(self) -> None:
        if type(self.seat) is not RateLimit or type(self.toolspace) is not RateLimit:
            raise ValueError("seat and toolspace must be RateLimit values")


#: §12.5 platform hard ceilings. Tenant or pack policy may tighten, never loosen.
SEAT_CEILING = RateLimit(per_minute=60, burst=15, in_flight=4)
TOOLSPACE_CEILING = RateLimit(per_minute=600, burst=100, in_flight=32)
CEILINGS = RateLimits(seat=SEAT_CEILING, toolspace=TOOLSPACE_CEILING)


def clamp_to_ceilings(limits: RateLimits) -> RateLimits:
    return RateLimits(seat=limits.seat.clamp(SEAT_CEILING), toolspace=limits.toolspace.clamp(TOOLSPACE_CEILING))


class AppRateLimited(Exception):
    """``app_rate_limited`` (§12.5, §16.3): refused now, never queued."""

    code = "app_rate_limited"

    def __init__(self, *, retry_after: int, level: str, reason: str) -> None:
        super().__init__(f"{self.code}: {level} {reason}; retry after {retry_after}s")
        self.retry_after = retry_after
        self.level = level  # "seat" | "toolspace"
        self.reason = reason  # "rate" | "burst" | "in_flight"


class AppRateAdmissionUnavailable(Exception):
    """The limits could not be resolved; the call is refused (fail closed)."""

    code = "app_internal"


def _scope_of(subject: Any) -> AppScope:
    if type(subject) is AppScope:
        return subject
    if type(subject) is ConnectionContext:
        return subject.app_scope
    raise TypeError("rate admission takes an AppScope or a ConnectionContext")


def _seat_key(subject: Subject) -> tuple:
    """(tenant_id, sub, toolspace_id) -- AppScope members only (§12.5, §3.6 R-f)."""
    scope = _scope_of(subject)
    return ("seat", scope.tenant_id, scope.sub, scope.toolspace_id)


def _toolspace_key(subject: Subject) -> tuple:
    """(tenant_id, toolspace_id) -- AppScope members only (§12.5)."""
    scope = _scope_of(subject)
    return ("toolspace", scope.tenant_id, scope.toolspace_id)


def _bounded_retry_after(seconds: float) -> int:
    return max(MIN_RETRY_AFTER_SECONDS, min(MAX_RETRY_AFTER_SECONDS, math.ceil(seconds - 1e-9)))


@dataclass
class _Counter:
    window: deque = field(default_factory=deque)  # admission times inside the 60 s window
    tokens: Optional[float] = None  # None = a full bucket on first use
    refilled_at: float = 0.0
    in_flight: int = 0

    def refresh(self, now: float, limit: RateLimit) -> None:
        cutoff = now - WINDOW_SECONDS
        while self.window and self.window[0] <= cutoff:
            self.window.popleft()
        if self.tokens is None:
            self.tokens = float(limit.burst)
        else:
            elapsed = max(0.0, now - self.refilled_at)
            self.tokens += elapsed * (limit.per_minute / WINDOW_SECONDS)
        self.tokens = min(self.tokens, float(limit.burst))  # a tightened burst binds at once
        self.refilled_at = now

    def refusal(self, now: float, limit: RateLimit) -> Optional[tuple[str, float]]:
        """``(reason, seconds until it could pass)``, or None when admissible."""
        if self.in_flight >= limit.in_flight:
            return "in_flight", 0.0
        if len(self.window) >= limit.per_minute:
            # the (len - per_minute + 1)-th oldest admission must leave the window
            return "rate", self.window[len(self.window) - limit.per_minute] + WINDOW_SECONDS - now
        tokens = self.tokens if self.tokens is not None else float(limit.burst)
        if tokens < 1.0:
            return "burst", (1.0 - tokens) / (limit.per_minute / WINDOW_SECONDS)
        return None

    def charge(self, now: float) -> None:
        self.window.append(now)
        self.tokens = (self.tokens if self.tokens is not None else 0.0) - 1.0
        self.in_flight += 1


class RatePermit:
    """One admitted in-flight call. :meth:`release` frees the slot (idempotent)."""

    __slots__ = ("_limiter", "_keys", "_released", "_lock")

    def __init__(self, limiter: "AppRateLimiter", keys: tuple[tuple, tuple]) -> None:
        self._limiter = limiter
        self._keys = keys
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self._keys)

    def __enter__(self) -> "RatePermit":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class AppRateLimiter:
    """The authoritative R-f admission for application calls (see module doc)."""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        limits_for: Optional[Callable[[str, str], RateLimits]] = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._limits_for = limits_for
        self._counters: dict[tuple, _Counter] = {}
        self._lock = threading.Lock()

    def _limits(self, scope: AppScope) -> RateLimits:
        if self._limits_for is None:
            return CEILINGS
        try:
            limits = self._limits_for(scope.tenant_id, scope.toolspace_id)
            if type(limits) is not RateLimits:
                raise TypeError("limits source returned a non-RateLimits value")
        except Exception as exc:  # noqa: BLE001 -- policy failure fails closed
            raise AppRateAdmissionUnavailable("rate limits unavailable") from exc
        return clamp_to_ceilings(limits)

    def admit(self, subject: Subject) -> RatePermit:
        """Admit one call now or raise :class:`AppRateLimited`. Never waits."""
        scope = _scope_of(subject)
        keys = (_seat_key(subject), _toolspace_key(subject))
        limits = self._limits(scope)
        checks = (("seat", keys[0], limits.seat), ("toolspace", keys[1], limits.toolspace))
        with self._lock:
            now = float(self._clock())
            staged = []
            refusals = []
            for level, key, limit in checks:
                counter = self._counters.get(key) or _Counter()
                counter.refresh(now, limit)
                staged.append((key, counter))
                hit = counter.refusal(now, limit)
                if hit is not None:
                    refusals.append((level, hit[0], hit[1]))
            if refusals:
                # Nothing is charged; a brand-new counter is not even stored.
                level, reason, _ = refusals[0]
                wait = max(seconds for _, _, seconds in refusals)
                raise AppRateLimited(retry_after=_bounded_retry_after(wait), level=level, reason=reason)
            for key, counter in staged:
                counter.charge(now)
                self._counters[key] = counter
        return RatePermit(self, keys)

    def _release(self, keys: tuple[tuple, tuple]) -> None:
        with self._lock:
            for key in keys:
                counter = self._counters.get(key)
                if counter is not None and counter.in_flight > 0:
                    counter.in_flight -= 1

    def in_flight(self, scope: AppScope) -> int:
        """The seat's current in-flight count (observability / tests)."""
        with self._lock:
            counter = self._counters.get(_seat_key(scope))
            return counter.in_flight if counter is not None else 0
