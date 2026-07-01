"""In-process auth rate-limiter for the OAuth login form.

Doctrine: every OAuth /oauth/authorize POST is gated by TWO keys —
          one per source IP, one per target email. Each key allows
          exactly ONE attempt per AUTH_RATE_LIMIT_WINDOW seconds
          (default 10). Either key being throttled rejects the
          request with HTTP 429 + a Retry-After header.
Why:      fail2ban (added 2026-05-28) catches SSH brute-force at the
          OS level. The OAuth login form is a separate brute-force
          surface — a botnet with 10k IPs can credential-stuff one
          account if we only rate-limit by IP, and one IP can sweep
          an email list if we only rate-limit by account. Both keys
          are required to defeat both attacks. 10s is deliberately
          absurd from a usability standpoint (per king directive
          2026-05-28); the cost to a legitimate operator is one
          page-load worth of friction, the cost to an attacker is
          ~8,640 attempts/day/key — useless for brute-force.
Apply:    call try_consume(key) BEFORE calling IdentityStore.authenticate.
          Compose two keys per request:
            - f"ip:{client_ip}"
            - f"email:{email.strip().lower()}"
          Reject on first throttled key (don't leak which key was
          rate-limited — a probe could otherwise infer whether a
          given email is one a real user just attempted from).

Concurrency: a process-global dict guarded by a single lock. The
gate is single-process (one pm2 fork), so this is sufficient. If we
ever move to multi-process, swap the dict for shared memory or
Redis — the public API stays the same.

GC: the dict is opportunistically pruned when it exceeds 4096 keys;
entries older than 5 minutes are removed. Prevents unbounded growth
under a sustained scanning attack while keeping the steady-state
memory footprint tiny.
"""

from __future__ import annotations

import threading
import time

AUTH_RATE_LIMIT_WINDOW: float = 10.0  # seconds — per king directive 2026-05-28
_GC_THRESHOLD = 4096  # dict size that triggers pruning
_GC_OLDER_THAN = 300.0  # prune entries older than this (s)

_lock = threading.Lock()
_last_attempt: dict[str, float] = {}


def try_consume(key: str) -> tuple[bool, int]:
    """Atomically check + record an attempt.

    Returns:
        (True, 0)            — attempt allowed; key's clock starts now
        (False, seconds_left) — throttled; the integer is how many
                                seconds the operator must wait before
                                the next attempt. Always >= 1 when
                                blocked, so the UI countdown never
                                shows "0" while the lockout is active.

    """
    if not key:
        return True, 0
    now = time.monotonic()
    with _lock:
        prev = _last_attempt.get(key)
        if prev is not None:
            elapsed = now - prev
            if elapsed < AUTH_RATE_LIMIT_WINDOW:
                retry = max(1, int(AUTH_RATE_LIMIT_WINDOW - elapsed) + 1)
                return False, retry
        _last_attempt[key] = now
        if len(_last_attempt) > _GC_THRESHOLD:
            cutoff = now - _GC_OLDER_THAN
            stale = [k for k, ts in _last_attempt.items() if ts < cutoff]
            for k in stale:
                del _last_attempt[k]
    return True, 0


def reset() -> None:
    """Test-only — wipe rate-limit state between tests."""
    with _lock:
        _last_attempt.clear()
