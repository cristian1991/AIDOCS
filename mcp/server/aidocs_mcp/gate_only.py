"""The typed "gate_only" answer -- a neutral public module.

Design decision (2026-10-29): no public module imports a gate file. Where a
public caller has a branch that genuinely needs gate authority (org admin,
gate project binding, OAuth clearance, gate git credentials), that branch
fails CLOSED locally with this one typed result instead of importing the
gate. The gate re-enters those branches through the gate-hooks seam
(gate_hooks.py), registering its implementations at gate startup; with no
hook registered the answer is an honest refusal, never a guess.

Also holds the one shared surface-kind constant a public caller compares
against (``SURFACE_GATE_ONLY``); the gate's OAuth store uses the same value.
"""

from __future__ import annotations

from typing import Any

#: blocked_by / _error code of every gate-only refusal.
GATE_ONLY = "gate_only"

#: The OAuth client surface kind the gate stamps for a client it registered as
#: gate-only. A plain string the gate and the barrier both compare against.
SURFACE_GATE_ONLY = "gate_only"


def gate_only_reason(capability: str) -> str:
    return (
        f"{capability} needs the gate's authority, which this build does not "
        "consult from here; refused (fail closed)."
    )


def gate_only_refusal(capability: str, **extra: Any) -> dict[str, Any]:
    """``{"ok": False, "blocked": True, "blocked_by": "gate_only", ...}``."""
    out: dict[str, Any] = {
        "ok": False,
        "blocked": True,
        "blocked_by": GATE_ONLY,
        "_error": GATE_ONLY,
        "capability": str(capability),
        "reason": gate_only_reason(capability),
    }
    out.update(extra)
    return out
