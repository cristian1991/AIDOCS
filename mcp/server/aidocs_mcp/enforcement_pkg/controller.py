"""EnforcementController — single doorway for safety law.

This file is SKELETON-ONLY. The pipeline runner is sketched but
gates are not yet wired; wiring lands after king resolutions on:

    Q1: kill_switch vs judge-forbidden — does the emergency key
        open every cell, or stop short of critical-class verdicts?
    Q2: pipeline order Freeze (5) before Grants (6) — confirm.
    Q3: degraded-state policy — fail-closed always, or
        runtime-configurable per gate?

Doctrine: see
    .MEMORY/sessions/2026-05-03-freeze-state-stickiness-firefighter/
    plans/enforcement-controller-doctrine.md

Until wired, adapters continue to call the legacy surfaces. The
controller is built ALONGSIDE existing code (migration step 2 of
doctrine § X) so parity tests can compare verdicts before any
legacy site is deleted.
"""

from __future__ import annotations

from .decision import Decision, GateStep, Verdict
from .surface import Request

# Canonical pipeline order (doctrine § VI). Names are stable; the
# matching gate module under enforcement_pkg/gates/ owns the logic.
CANONICAL_PIPELINE: tuple[str, ...] = (
    "normalize",
    "identity",
    "kill_switch",
    "managed_mode",
    "freeze",
    "grants",
    "path_extraction",
    "trust_zone",
    "policy",
    "judge",
    "shell_policy",
    "audit_envelope",
    "decision_format",
)


class EnforcementController:
    """Single public doorway. Build a Request, get a Decision.

    Skeleton today. Gates wire in incrementally; the order is
    canonical (CANONICAL_PIPELINE). Each gate evaluates against a
    state snapshot taken at step start (TOCTOU rule, doctrine § III.1).

    Gates that don't apply to a given Surface are skipped — e.g.
    UPS doesn't run shell_policy; run_kill skips freeze gate
    (remedial-operation doctrine, § IV.4).
    """

    def __init__(self) -> None:
        # Gate registry will be populated as gates land.
        # Map: gate_name -> Gate instance (must expose evaluate(state))
        self._gates: dict[str, object] = {}

    def enforce(self, request: Request) -> Decision:
        """SKELETON. Returns a permissive ALLOW with a single trace
        entry recording that the controller is not yet wired.

        Real implementation: walks CANONICAL_PIPELINE, evaluates each
        applicable gate, builds Decision from snapshots, returns.

        Until wired, this method MUST NOT be called by production
        code paths. Parity tests may call it to compare against
        legacy decisions; the comparator treats "not wired" as a
        skip, not a failure.

        Lane 2 phase 1 (2026-05-04): wired via `_legacy_facade`.
        Bridges to gate_tool.enforce_tool_call; replaced gate-by-gate
        in subsequent phases.
        """
        from ._legacy_facade import enforce_via_legacy

        return enforce_via_legacy(request)
        # Dead skeleton fallback, kept until full migration prunes it:
        return Decision(
            request_id=request.request_id,
            verdict=Verdict.ALLOW,
            reason_code="controller_not_wired",
            user_message=(
                "EnforcementController skeleton — not yet wired. "
                "Legacy enforcement remains authoritative."
            ),
            trace=[
                GateStep(
                    gate_name="controller_skeleton",
                    verdict="allow",
                    reason="skeleton phase — gates not yet wired",
                ),
            ],
            session_id=request.host_session_id,
        )
