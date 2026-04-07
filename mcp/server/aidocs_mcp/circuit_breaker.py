"""Circuit breaker for MCP server connections.

Tracks consecutive failures per MCP server. After N failures, the circuit
opens and subsequent calls are rejected immediately for a cooldown period.
Uses exponential backoff with jitter.

States:
    CLOSED  — normal operation, calls pass through
    OPEN    — too many failures, calls rejected immediately
    HALF    — cooldown expired, next call is a probe (if it passes → CLOSED, if it fails → OPEN)
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class BreakerState:
    server_id: str
    state: str  # "closed", "open", "half_open"
    consecutive_failures: int
    last_failure_at: float  # monotonic time
    cooldown_until: float  # monotonic time — when to transition from open → half_open
    total_failures: int
    total_successes: int

    def to_dict(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "server_id": self.server_id,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_remaining_seconds": max(0, round(self.cooldown_until - now, 1)) if self.state == "open" else 0,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
        }


class CircuitBreaker:
    """Per-server circuit breaker with exponential backoff."""

    def __init__(
        self,
        failure_threshold: int = 3,
        base_cooldown: float = 30.0,
        max_cooldown: float = 300.0,
        jitter_factor: float = 0.2,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._jitter_factor = jitter_factor
        self._states: dict[str, BreakerState] = {}
        self._lock = threading.Lock()

    def _get_state(self, server_id: str) -> BreakerState:
        if server_id not in self._states:
            self._states[server_id] = BreakerState(
                server_id=server_id,
                state="closed",
                consecutive_failures=0,
                last_failure_at=0.0,
                cooldown_until=0.0,
                total_failures=0,
                total_successes=0,
            )
        return self._states[server_id]

    def can_execute(self, server_id: str) -> tuple[bool, str]:
        """Check if a call to this server is allowed.

        Returns:
            (allowed, reason) — reason is empty if allowed.
        """
        with self._lock:
            state = self._get_state(server_id)
            now = time.monotonic()

            if state.state == "closed":
                return True, ""

            if state.state == "open":
                if now >= state.cooldown_until:
                    # Transition to half-open — allow one probe
                    state.state = "half_open"
                    return True, ""
                remaining = round(state.cooldown_until - now, 1)
                return False, (
                    f"Circuit open for '{server_id}': {state.consecutive_failures} consecutive failures. "
                    f"Retry in {remaining}s."
                )

            # half_open — allow the probe call
            return True, ""

    def record_success(self, server_id: str) -> None:
        """Record a successful call — resets the circuit."""
        with self._lock:
            state = self._get_state(server_id)
            state.state = "closed"
            state.consecutive_failures = 0
            state.total_successes += 1

    def record_failure(self, server_id: str) -> None:
        """Record a failed call — may trip the circuit."""
        with self._lock:
            state = self._get_state(server_id)
            state.consecutive_failures += 1
            state.total_failures += 1
            state.last_failure_at = time.monotonic()

            if state.consecutive_failures >= self._failure_threshold:
                state.state = "open"
                # Exponential backoff: base * 2^(failures - threshold)
                exponent = state.consecutive_failures - self._failure_threshold
                cooldown = min(
                    self._base_cooldown * (2 ** exponent),
                    self._max_cooldown,
                )
                # Add jitter
                jitter = cooldown * self._jitter_factor * (random.random() * 2 - 1)
                state.cooldown_until = time.monotonic() + cooldown + jitter

    def reset(self, server_id: str) -> None:
        """Manually reset a circuit."""
        with self._lock:
            if server_id in self._states:
                self._states[server_id].state = "closed"
                self._states[server_id].consecutive_failures = 0

    def get_all_states(self) -> list[dict[str, object]]:
        """Get all breaker states for monitoring."""
        with self._lock:
            return [state.to_dict() for state in self._states.values()]

    def get_state(self, server_id: str) -> dict[str, object]:
        """Get a specific breaker state."""
        with self._lock:
            return self._get_state(server_id).to_dict()


# Singleton
_BREAKER: CircuitBreaker | None = None
_BREAKER_LOCK = threading.Lock()


def get_breaker() -> CircuitBreaker:
    global _BREAKER
    if _BREAKER is None:
        with _BREAKER_LOCK:
            if _BREAKER is None:
                _BREAKER = CircuitBreaker()
    return _BREAKER
