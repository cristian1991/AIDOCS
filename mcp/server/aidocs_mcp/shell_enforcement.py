"""ShellPolicy enforcement / contract layer (Batch 1.7).

Turns a ShellPolicy verdict into an enforcement action — and, for the
confirmable case, mints a REAL freeze / exact-approval via the canonical
``freeze_service.build_freeze_response`` (the same path ai_run and the
claude_hook PreToolUse use). This is enforcement/contract only:

  * Shadow NEVER mints freezes (shadow uses a replay delegate + records;
    it never calls this module). Minting lives here, in enforcement.
  * Approval does NOT imply native execution. The post-approval route is
    computed up front and attached to the result:
        - native execution disabled              → route = ai_run
        - native enabled but host/provider/output
          mode not eligible                      → route = ai_run
        - only an ELIGIBLE native provider       → route = native
  * Batch 1.7 does NOT execute anything. ``action="execute_native"`` is a
    contract marker for Batch 2.0; no adapter calls this layer for live
    execution yet. ai_run remains canonical/fallback.

Eligibility for native execution (all required):
  * native_shell_provider_enabled (config / caller override)
  * capability matrix proves command-visibility + PreToolUse hard-deny
  * PostToolUse output replacement proven (output_guard_mode would be
    host_replace, not pre_exec_strict) — Batch 1 doctrine: a strict
    pre-exec output mode refuses native execution until a concrete strict
    rule exists, so it routes to ai_run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import shell_capability_matrix as _matrix
from . import shell_policy as _sp
from .shell_envelope import TRANSPORT_AI_RUN, ShellCommandEnvelope

# enforcement actions
ACTION_ALLOW = "allow"  # ai_run transport, allowed
ACTION_DENY = "deny"
ACTION_FREEZE = "freeze_pending_approval"  # confirmable → real freeze minted
ACTION_FREEZE_FAILED = "freeze_failed"  # confirmable but mint failed → blocked
ACTION_FALLBACK = "fallback_to_ai_run"
ACTION_EXECUTE_NATIVE = "execute_native"  # Batch 2.0 contract marker

# post-approval / post-allow routes
ROUTE_NATIVE = "native"
ROUTE_AI_RUN = "ai_run"


@dataclass(frozen=True)
class ShellEnforcementResult:
    action: str
    native_route: str
    reason: str = ""
    blocked_by: str = ""
    freeze_state: dict[str, Any] | None = None
    verdict: _sp.ShellVerdict | None = None
    # Provider-aware fallback semantics. ai_run is bash-canonical: for a
    # bash command, "fallback_to_ai_run" CAN run the same command. For a
    # PowerShell/cmd command it CANNOT — the same command is not
    # bash-executable, so fallback means native is blocked and the agent
    # must use an AIDOCS-managed / ai_run-compatible equivalent.
    fallback_same_command: bool = False


class _Decision:
    """EnforceResult-shaped, .refusal only. Carries NO freeze side effects."""

    def __init__(self, refusal: dict[str, Any] | None) -> None:
        self.refusal = refusal


def _nonminting_core_law(hub: Any, runtime: Any):
    """A DECISION-ONLY core-law delegate for ShellPolicy. It maps
    AgentOrchestrator.check_tool's ToolDecision into a refusal shape WITHOUT
    ever minting a freeze (no build_freeze_response, no enforce_tool_call).

    This is mandatory for enforcement: ShellEnforcement._mint_freeze is the
    SINGLE place a real freeze is minted for a ShellPolicy confirmable, so
    the core law must not mint a second one. Fails CLOSED (deny) on any
    orchestrator error.
    """

    def _delegate(envelope: ShellCommandEnvelope) -> Any:
        try:
            from .agent_orchestrator import AgentOrchestrator

            decision = AgentOrchestrator(runtime).check_tool(
                envelope.project_root,
                "ai_run",
                {"command": envelope.command},
            )
        except Exception as exc:
            return _Decision(
                {
                    "blocked_by": "core_law_error",
                    "reason": f"core law decision failed: {exc}",
                },
            )
        if getattr(decision, "allowed", False):
            return _Decision(None)
        if getattr(decision, "needs_confirmation", False):
            # "confirm" in blocked_by signals confirmable to ShellPolicy
            # WITHOUT a freeze_state — the real freeze is minted later by
            # ShellEnforcement, exactly once.
            return _Decision(
                {
                    "blocked_by": "judge_confirm_required",
                    "reason": str(getattr(decision, "reason", "") or "confirm"),
                },
            )
        return _Decision(
            {
                "blocked_by": str(getattr(decision, "blocked_by", "") or "denied"),
                "reason": str(getattr(decision, "reason", "") or "denied"),
            },
        )

    return _delegate


def _resolve_native_enabled(project_root: Path, override: bool | None) -> bool:
    if override is not None:
        return override
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tools.native_shell_provider_enabled",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def _fallback_same_command(provider: str) -> bool:
    """True only when the same command can be re-run via ai_run. ai_run is
    bash-canonical, so only bash commands qualify; PowerShell/cmd commands
    are not bash-executable and must NOT be auto-run via ai_run.
    """
    from .shell_envelope import PROVIDER_BASH

    return provider == PROVIDER_BASH


def _fallback_reason(provider: str, base: str) -> str:
    if _fallback_same_command(provider):
        return base or "routing to ai_run (same bash command)"
    return (
        f"native {provider} blocked — the {provider} command is NOT "
        f"bash-executable via ai_run; use an AIDOCS-managed equivalent or "
        f"an ai_run-compatible (bash) command. " + (base or "")
    ).strip()


def native_route(
    host: str,
    provider: str,
    *,
    native_enabled: bool,
    project_root: Path | None = None,
    executable_path: str | None = None,
) -> str:
    """Where an APPROVED / ALLOWED command should run. Native ONLY when:
      * native execution is enabled, AND
      * the host/provider is capability-safe (hard-deny seam proven), AND
      * PostToolUse output replacement is proven, AND
      * provider IDENTITY is proven (Batch 2.0-B) — tool name is not
        identity; requires a host-validated executable path or a static
        trusted contract.
    Otherwise → ai_run fallback. Single eligibility rule shared by the
    allow path and the post-approval path.
    """
    if not native_enabled:
        return ROUTE_AI_RUN
    if not _matrix.is_native_safe(host, provider):
        return ROUTE_AI_RUN
    cap = _matrix.lookup(host, provider)
    if cap is None or not cap.posttooluse_output_replacement:
        # Output mode not eligible (pre_exec_strict) → refuse native.
        return ROUTE_AI_RUN
    from .shell_provider_identity import verify_native_provider_identity

    identity_ok, _reason = verify_native_provider_identity(
        host,
        provider,
        project_root=project_root,
        executable_path=executable_path,
    )
    if not identity_ok:
        # Provider identity unproven — the "Bash" tool may be costume.
        return ROUTE_AI_RUN
    return ROUTE_NATIVE


def resolve_shell_enforcement(
    envelope: ShellCommandEnvelope,
    *,
    hub: Any = None,
    runtime: Any = None,
    native_enabled: bool | None = None,
    law_delegate: Any = None,
    gate_state: dict[str, Any] | None = None,
) -> ShellEnforcementResult:
    """Evaluate + enforce. Mints a real freeze for confirmable verdicts.

    NOTE: enforcement runs the REAL law (side effects allowed) — this is
    not shadow. Callers that only want to observe must use
    shell_policy_shadow, which never reaches this module.
    """
    enabled = _resolve_native_enabled(envelope.project_root, native_enabled)
    # Default to the NON-MINTING core-law delegate so ShellEnforcement is the
    # single freeze minter (no double-mint). Tests may inject a stub.
    if law_delegate is None:
        law_delegate = _nonminting_core_law(hub, runtime)
    policy = _sp.ShellPolicy(law_delegate=law_delegate)
    verdict = policy.evaluate(
        envelope,
        hub=hub,
        runtime=runtime,
        gate_state=gate_state,
        native_enabled=enabled,
    )
    route = native_route(
        envelope.host,
        envelope.provider,
        native_enabled=enabled,
        project_root=envelope.project_root,
        executable_path=envelope.provider_executable_path,
    )

    if verdict.decision == _sp.DECISION_DENY:
        return ShellEnforcementResult(
            action=ACTION_DENY,
            native_route=route,
            reason=verdict.reason,
            blocked_by=verdict.blocked_by,
            verdict=verdict,
        )

    if verdict.decision == _sp.DECISION_CONFIRMABLE:
        # Enforcement mints a REAL freeze / exact-approval. Approval does
        # NOT imply native execution — native_route says where the
        # approved command goes (native only if eligible, else ai_run).
        #
        # Freeze mint success is MANDATORY: a confirmable verdict whose
        # approval cannot be created must stay BLOCKED, never returned as a
        # false "pending approval". No fake pending state, no fallback
        # execution.
        session_id = (envelope.managed_session_id or envelope.host_session_id or "").strip()
        if not session_id:
            return ShellEnforcementResult(
                action=ACTION_FREEZE_FAILED,
                native_route=ROUTE_AI_RUN,
                blocked_by="freeze_no_session",
                reason=(
                    "operator approval could not be created: no session "
                    "bound to mint a freeze. Execution remains BLOCKED."
                ),
                verdict=verdict,
            )
        freeze_state, mint_err = _mint_freeze(envelope, verdict, session_id)
        if not freeze_state or not freeze_state.get("request_id"):
            return ShellEnforcementResult(
                action=ACTION_FREEZE_FAILED,
                native_route=ROUTE_AI_RUN,
                blocked_by="freeze_mint_failed",
                reason=(
                    "operator approval could not be created "
                    f"({mint_err or 'no freeze state returned'}). "
                    "Execution remains BLOCKED — no pending approval exists."
                ),
                verdict=verdict,
            )
        return ShellEnforcementResult(
            action=ACTION_FREEZE,
            native_route=route,
            reason=verdict.reason,
            blocked_by=verdict.blocked_by or "judge_confirm_required",
            freeze_state=freeze_state,
            verdict=verdict,
        )

    if verdict.decision in (
        _sp.DECISION_FALLBACK,
        _sp.DECISION_CAPABILITY_UNSUPPORTED,
    ):
        return ShellEnforcementResult(
            action=ACTION_FALLBACK,
            native_route=ROUTE_AI_RUN,
            reason=_fallback_reason(envelope.provider, verdict.reason),
            blocked_by=verdict.blocked_by,
            verdict=verdict,
            fallback_same_command=_fallback_same_command(envelope.provider),
        )

    # decision == allow
    if verdict.transport == TRANSPORT_AI_RUN:
        return ShellEnforcementResult(
            action=ACTION_ALLOW,
            native_route=ROUTE_AI_RUN,
            reason=verdict.reason,
            verdict=verdict,
        )
    # native allow: only execute natively when fully eligible; otherwise
    # (e.g. output mode pre_exec_strict) fall back to ai_run.
    if route == ROUTE_NATIVE:
        return ShellEnforcementResult(
            action=ACTION_EXECUTE_NATIVE,
            native_route=ROUTE_NATIVE,
            reason=verdict.reason,
            verdict=verdict,
        )
    return ShellEnforcementResult(
        action=ACTION_FALLBACK,
        native_route=ROUTE_AI_RUN,
        reason=_fallback_reason(
            envelope.provider,
            "native allowed but not execution-eligible (output mode / capability)",
        ),
        verdict=verdict,
        fallback_same_command=_fallback_same_command(envelope.provider),
    )


def _mint_freeze(
    envelope: ShellCommandEnvelope,
    verdict: _sp.ShellVerdict,
    session_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Mint a real freeze. Returns (freeze_state, error). On any failure
    the caller MUST treat it as freeze-not-created (never ACTION_FREEZE).
    """
    try:
        from .freeze_service import build_freeze_response

        resp = build_freeze_response(
            envelope.project_root,
            session_id,
            tool_name=envelope.tool_name,
            tool_input={"command": envelope.command},
            judge_summary=verdict.reason or "operator confirmation required",
        )
        return resp.get("freeze_state"), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
