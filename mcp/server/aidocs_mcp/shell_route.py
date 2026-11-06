"""Shell route clarity — four orthogonal facts about one shell verdict.

"native allowed but routed to ai_run" was a confusing single string. It
conflated four distinct facts. This module splits them so the dashboard,
CLI, and audit can state exactly what happened, and so "policy says yes"
is never mistaken for "it ran natively":

  policy_allowed          — the shell LAW permits the command (bash_policy
                            + judge + read-only family). Independent of
                            where/whether it runs.
  capability_eligible     — the HOST can actually run it natively (proven
                            provider identity + output replacement + native
                            enabled). Missing capability ⇒ not eligible.
  selected_route          — "native" | "ai_run" | "none". Where the
                            governor chose to send it.
  final_execution_surface — "native_bash" | "ai_run" | "none". Where it
                            ACTUALLY runs. Never "raw"/unreceipted: native
                            is receipted; ai_run is the governed shell;
                            none means denied.

LAW: native execution happens ONLY when policy_allowed AND
capability_eligible AND native_enabled. Otherwise a policy-allowed command
degrades to the governed ai_run surface — clearly, never silently.
"""

from __future__ import annotations

ROUTE_NATIVE = "native"
ROUTE_AI_RUN = "ai_run"
ROUTE_NONE = "none"

SURFACE_NATIVE = "native_bash"  # host bash, receipted via PostToolUse
SURFACE_AI_RUN = "ai_run"  # AIDOCS-managed shell (x-ray+receipt+audit)
SURFACE_NONE = "none"  # denied — nothing runs


def classify_route(
    *,
    policy_allowed: bool,
    capability_eligible: bool,
    native_enabled: bool,
) -> dict:
    """Return the four route-clarity facts + a non-misleading message.

    Pure: no IO. Callers pass the already-computed law verdict
    (policy_allowed), the host capability verdict (capability_eligible),
    and whether native execution is enabled by config.
    """
    policy_allowed = bool(policy_allowed)
    capability_eligible = bool(capability_eligible)
    native_enabled = bool(native_enabled)

    if not policy_allowed:
        route, surface = ROUTE_NONE, SURFACE_NONE
        message = "Denied by shell policy — not executed."
    elif native_enabled and capability_eligible:
        route, surface = ROUTE_NATIVE, SURFACE_NATIVE
        message = "Policy-allowed and native-eligible — runs natively (receipted + output-guarded)."
    else:
        route, surface = ROUTE_AI_RUN, SURFACE_AI_RUN
        if not native_enabled:
            why = "native execution is disabled"
        else:
            why = "host capability is not eligible (no proven native provider / output replacement)"
        message = (
            f"Policy-allowed, but {why} — runs via the governed ai_run "
            "shell (x-ray + receipt + output-guard + audit). Native stays "
            "disabled; this is NOT native execution."
        )

    return {
        "policy_allowed": policy_allowed,
        "capability_eligible": capability_eligible,
        "native_enabled": native_enabled,
        "selected_route": route,
        "final_execution_surface": surface,
        "message": message,
    }


def is_raw_bypass(route: dict) -> bool:
    """True iff a verdict would run on an ungoverned/raw surface. Always
    False for any classify_route output — the surfaces are exhaustively
    native_bash (receipted) / ai_run (governed) / none. Used by tests to
    assert no raw shell bypass exists.
    """
    return route.get("final_execution_surface") not in (
        SURFACE_NATIVE,
        SURFACE_AI_RUN,
        SURFACE_NONE,
    )
