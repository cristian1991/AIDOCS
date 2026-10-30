"""Audit-criticality split — Q3-A doctrine (Empire-rendered 2026-05-04).

Two audit shapes:

- `record_audit_critical(...)` — for allow / bypass / mutation
  events. The action MUST be audited; if the audit write fails,
  the caller MUST refuse the action. Returns True on success, False
  on failure. Latches `audit_critical` on the degraded latch.

- `record_audit_best_effort(...)` — for refusal events and other
  non-mutation observability that the doctrine still classifies as
  audit (not pure telemetry). Returns True on success. Caller may
  continue regardless of return value. Latches `audit_best_effort`
  on degraded latch when failures happen.

The split exists so that:
  - We never perform an unaudited mutation (compliance contract).
  - A broken audit store doesn't ALSO block the kingdom from
    refusing — refusal-side audit is best-effort because the
    important thing (refusal) already happened.

Pure telemetry (metrics, traces, UI hints, optional summaries) is
NOT covered by this module. Those callers wrap their own try/except
and degrade silently — they're not audit, they're observability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .degraded_latch import DegradedLatch


def record_audit_critical(
    hub: Any,
    project_root: Path,
    **event_fields: Any,
) -> bool:
    """Critical audit. Returns True on success, False on failure.

    Caller contract: if False, the caller MUST refuse the action
    rather than perform it unaudited.
    """
    try:
        hub.execution.record_event(project_root, **event_fields)
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            DegradedLatch().latch(
                gate_name="audit_critical",
                dependency_name="ExecutionIndexStore",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        except Exception:
            pass
        return False


def record_audit_best_effort(
    hub: Any,
    project_root: Path,
    **event_fields: Any,
) -> bool:
    """Best-effort audit (refusals / observability). Returns True
    on success. Caller continues regardless.

    Failures latch `audit_best_effort` on the degraded latch so the
    dashboard sees the audit store is in trouble, but the in-flight
    action proceeds.
    """
    try:
        hub.execution.record_event(project_root, **event_fields)
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            DegradedLatch().latch(
                gate_name="audit_best_effort",
                dependency_name="ExecutionIndexStore",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        except Exception:
            pass
        return False
