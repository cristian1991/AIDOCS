"""Per-gate degraded-state latch.

Q3-A doctrine (Empire-rendered 2026-05-04):

> Every enforcement gate fails closed on dependency failure. The
> transport boundary catches the exception and returns a structured
> refusal. Degraded state is **latched** and surfaced in
> dashboard/status. Nothing bypasses degraded gates (#404: the kill switch is excised).

Latch semantics:

- Process-wide singleton (in-memory). MCP boots fresh; a previous
  process's latch state does NOT persist across restart. That is
  intentional — the next boot's gate evaluation is the un-latch
  signal. (If we needed cross-restart persistence we'd add a
  sqlite mirror, but that risks a corrupt latch surviving a
  legitimate fix.)
- `latch(gate_name, dependency_name, error)` marks a gate degraded.
- `is_degraded(gate_name)` returns True until cleared.
- `clear(gate_name)` un-latches (called by a successful gate
  evaluation; future work).
- `snapshot()` returns the full state for dashboard / status
  exposure.

Telemetry-criticality split lives elsewhere (`audit_critical.py`)
because it has different shape semantics.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class DegradedEntry:
    gate_name: str
    dependency_name: str
    error: str
    latched_at: float


class DegradedLatch:
    """Process-wide singleton via shared class attribute. The
    actual storage lives on the class itself; instances are thin
    handles.
    """

    _lock: threading.Lock = threading.Lock()
    _entries: dict[str, DegradedEntry] = {}

    def latch(
        self,
        *,
        gate_name: str,
        dependency_name: str,
        error: str = "",
    ) -> None:
        """Mark a gate degraded. Idempotent — re-latching the same
        gate updates the entry's error/timestamp but does not
        unlatch.
        """
        if not gate_name.strip():
            return
        with type(self)._lock:
            type(self)._entries[gate_name] = DegradedEntry(
                gate_name=gate_name,
                dependency_name=dependency_name,
                error=error,
                latched_at=time.time(),
            )

    def is_degraded(self, gate_name: str) -> bool:
        with type(self)._lock:
            return gate_name in type(self)._entries

    def clear(self, gate_name: str) -> bool:
        """Un-latch. Returns True if a row was removed."""
        with type(self)._lock:
            return type(self)._entries.pop(gate_name, None) is not None

    def snapshot(self) -> list[DegradedEntry]:
        """Full latch state for dashboard / status output. Caller
        must NOT mutate the returned list — it's a defensive copy.
        """
        with type(self)._lock:
            return list(type(self)._entries.values())

    def reset_for_tests(self) -> None:
        """Clear all entries. Test-only — used to isolate cases."""
        with type(self)._lock:
            type(self)._entries.clear()
