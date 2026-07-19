"""Shared MCP-direct tool-call gate cascade.

Slice 1 (canonical 2026-04-29). Extracted from the working
``_run_shell_unified`` body so every MCP tool surface can route
through the same managed-mode/freeze/orchestrator/needs_confirmation
cascade WITHOUT depending on the host's PreToolUse hook.

Single entry point:

    enforce_tool_call(
        hub, project_root, tool_name, tool_input,
        *, fail_closed=True, include_freeze=True,
    ) -> EnforceResult

    EnforceResult.refusal is None  → caller proceeds with the tool.
    EnforceResult.refusal is dict  → caller returns it verbatim. The
    refusal carries blocked_by + reason + (optional) freeze_state.

Behavior parity with ``_run_shell_unified`` (lines 36–189 of
``server_run_tools.py``):

  1. Resolve session_id once via ManagedModeService.get_mode.
  2. If include_freeze and an existing freeze row exists → return
     the existing-freeze envelope.
  3. Run AgentOrchestrator.check_tool. The cascade inside check_tool
     covers AccessGate.check_raw_shell/raw_tool/lane_tool +
     tool_policy.evaluate_tool + heuristic_judge.evaluate_tool_call +
     bash_policy via the orchestrator.
  4. needs_confirmation → mint a fresh freeze via
     ``freeze_service.build_freeze_response`` and return that envelope.
  5. ``check_tool`` raises → record an audit event and refuse
     (fail_closed=True).

Callers that already bypass the freeze layer (e.g. legacy tools that
must not block on operator confirmation) can pass include_freeze=False.
fail_closed=False is reserved for callers that genuinely want the
older fail-open behavior — none today; default stays True.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _strike_actor_and_lane(project_root: Path) -> tuple[str, str]:
    """(actor, lane_id) for a security strike, from the identity seam only.

    #360: the strike scope key is a security ledger axis, so both facts
    must come from the authenticated chain (spawn-path env stamp, resolved
    principal type, or the #217 lane registry via
    ``task_actor_identity.resolve_task_actor``) — never from agent-supplied
    tool arguments. Non-worker callers (operator seat, conductor) resolve
    as the plain "agent" actor with no lane, exactly the pre-#360 shape.
    Fail-safe: any resolver error degrades to ("agent", "") — attribution
    softens, the block itself never does.
    """
    try:
        from .task_actor_identity import resolve_task_actor

        _actor_id, lane_id, is_worker = resolve_task_actor(project_root)
    except Exception:
        return "agent", ""
    if not is_worker:
        return "agent", ""
    return ("lane_worker", lane_id) if lane_id else ("subagent", "")


def _judge_taxonomy_precheck(
    hub: Any,
    project_root: Path,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    session_id: str,
) -> EnforceResult | None:
    """Run the explicit rule_id→class taxonomy decision and SHORT-CIRCUIT
    on malicious_forbidden with a durable strike. Returns None when the
    decision is ALLOW or ASK so the legacy cascade keeps owning those
    paths (defensive fallback).

    Inserted before the freeze short-circuit AND the orchestrator
    cascade so the decision happens at one single point that covers
    both inner-host paths (Claude/Codex/OpenCode calling ai_run /
    str_replace / etc.) AND the outer gate (ChatGPT through
    outer_gate.executor → srv.call_tool('ai_run') → enforce_tool_call).

    Returns:
      - EnforceResult(refusal=...) when DECISION_BLOCK_STRIKE — block,
        record a durable strike via SecurityViolationService (which
        escalates to a repeated_security_violation freeze at the
        configured threshold). Family = "judge_malicious_forbidden",
        actor + lane_id resolved from the identity seam
        (``_strike_actor_and_lane``), never from tool_input.
      - None when the decision is ALLOW or ASK — caller falls
        through to the existing cascade.

    """
    # Lazy imports — keep gate_tool's import footprint stable.
    from .heuristic_judge import evaluate_tool_call as _judge_eval
    from .intent_grant_detector import detect_destructive_intent
    from .judge_taxonomy import (
        DECISION_ALLOW,
        DECISION_ASK_CONFIRM,
        DECISION_BLOCK_FREEZE_NO_CONFIRM,
        DECISION_BLOCK_STRIKE,
    )

    judge = _judge_eval(tool_name, tool_input, project_root=project_root)
    if not judge.verdicts:
        return None  # Nothing for the taxonomy to decide on.

    # Operator destructive intent — resolved from session-bound user
    # prompt tokens (claude_hook populates this on UserPromptSubmit;
    # the outer-gate ChatGPT path NEVER populates it because remote
    # MCP clients have no UPS surface — which is exactly the design,
    # remote clients can't fake operator intent).
    operator_intent = False
    if session_id:
        try:
            query_gate = getattr(hub, "query_gate", None)
            if query_gate is not None:
                operator_intent = detect_destructive_intent(
                    query_gate,
                    project_root,
                    session_id,
                )
        except Exception:
            operator_intent = False

    decision = judge.decide(operator_destructive_intent=operator_intent)
    if decision.decision in (DECISION_ALLOW, DECISION_ASK_CONFIRM):
        # Defer to the legacy cascade. ALLOW lets the cascade run other
        # checks (path discovery, freeze, RBAC, etc.); ASK_CONFIRM lets
        # the cascade build the freeze response with its existing
        # confirm machinery — we do NOT duplicate freeze minting here.
        return None
    if decision.decision == DECISION_BLOCK_FREEZE_NO_CONFIRM:
        # MACHINE-ENFORCED no-intent refusal (2026-05-26 split).
        # confirmable_destructive WITHOUT matching operator destructive
        # intent → block with NO confirm path. This was previously
        # inherited from the legacy cascade's reason-string inspection;
        # now the precheck returns a structured refusal so a downstream
        # caller cannot mis-route it to the confirm machinery.
        triggering_rule = decision.triggering_rule_id
        blocked_by = "judge_confirmable_no_intent"
        reason = (
            f"refused: confirmable_destructive rule {triggering_rule!r} "
            f"fired without matching operator destructive intent. "
            f"{decision.reason} (blocked_by={blocked_by})"
        )
        try:
            hub.execution.record_event(
                project_root,
                event_kind="judge_confirmable_no_intent_block",
                source_kind="gate_tool.judge_taxonomy_precheck",
                session_id=session_id or None,
                capability_name=tool_name,
                action_kind="block",
                target_entity=triggering_rule[:200],
                status="blocked",
                payload={
                    "tool_name": tool_name,
                    "rule_id": triggering_rule,
                    "verdict_class": decision.triggering_class,
                    "operator_destructive_intent": operator_intent,
                },
            )
        except Exception:
            pass
        return EnforceResult(
            refusal={
                "ok": False,
                "blocked_by": blocked_by,
                "reason": reason,
            },
            session_id=session_id,
            decision_blocked_by=blocked_by,
            decision_reason=reason,
        )
    if decision.decision != DECISION_BLOCK_STRIKE:
        # Defensive — unknown decision shape, fall through.
        return None

    # BLOCK_STRIKE — durable strike via SecurityViolationService keyed
    # by the correct isolation boundary:
    #   - Inner hosts: session_id (managed mode) + lane_id when known.
    #   - Outer gate: session_id is the outer-gate-minted session
    #     (e.g. "ogr_remote"-prefixed), which the executor binds per
    #     token + host_session_id pair. Same scope path; isolation is
    #     enforced by the outer gate at session-mint time.
    # #360: actor + lane come from the AUTHENTICATED identity seam
    # (task_actor_identity.resolve_task_actor — env stamp / principal /
    # #217 registry chain), NEVER from tool_input. The old
    # tool_input["lane_id"] read let the CALLER choose the strike scope
    # key — an agent-supplied string steering a security ledger axis.
    actor, lane_id = _strike_actor_and_lane(project_root)
    triggering_rule = decision.triggering_rule_id
    msg_extra = ""
    try:
        from .security_violation_service import SecurityViolationService

        sv = SecurityViolationService(hub)
        outcome = sv.record_and_escalate(
            project_root,
            session_id=session_id,
            family="judge_malicious_forbidden",
            actor=actor,
            lane_id=lane_id,
            target=triggering_rule,
            tool_name=tool_name,
        )
        if outcome.message:
            msg_extra = "\n\n" + outcome.message
    except Exception:
        # Strike persistence failure must NOT downgrade the block —
        # the refusal itself is the load-bearing safety property.
        pass

    blocked_by = "judge_malicious_forbidden"
    reason = (
        f"refused: malicious_forbidden rule {triggering_rule!r} "
        f"({decision.reason}) — no confirm path, security strike "
        f"recorded (blocked_by={blocked_by})"
    ) + msg_extra
    # #371 (WAR U): every refusal carries the file-it-as-FP affordance —
    # additive footer only, the block itself is untouched. Best-effort: a
    # footer failure must never soften the refusal.
    try:
        from .tool_gate_service import refusal_with_affordance

        reason = refusal_with_affordance(
            reason, f"judge.{triggering_rule}", project_root=project_root
        )
    except Exception:
        pass
    try:
        hub.execution.record_event(
            project_root,
            event_kind="judge_malicious_forbidden_block",
            source_kind="gate_tool.judge_taxonomy_precheck",
            session_id=session_id or None,
            capability_name=tool_name,
            action_kind="block",
            target_entity=triggering_rule[:200],
            status="blocked",
            payload={
                "tool_name": tool_name,
                "rule_id": triggering_rule,
                "verdict_class": decision.triggering_class,
                "operator_destructive_intent": operator_intent,
            },
        )
    except Exception:
        pass

    return EnforceResult(
        refusal={
            "ok": False,
            "blocked_by": blocked_by,
            "reason": reason,
        },
        session_id=session_id,
        decision_blocked_by=blocked_by,
        decision_reason=reason,
    )


@dataclass
class EnforceResult:
    """Outcome of ``enforce_tool_call``.

    Either ``refusal`` is None (caller proceeds) or it is a dict the
    caller returns verbatim. ``session_id`` is exposed so callers
    that need it for downstream audit/scoping (e.g. ai_run, the
    pilot caller) don't have to re-resolve managed mode.
    """

    refusal: dict[str, Any] | None
    session_id: str = ""
    decision_blocked_by: str = ""
    decision_reason: str = ""


def enforce_tool_call(
    hub: Any,
    project_root: Path,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    fail_closed: bool = True,
    include_freeze: bool = True,
    runtime: Any = None,
) -> EnforceResult:
    """Public entry point — wraps the gate cascade in the
    transport-safety boundary (lane 1.6, 2026-05-04).

    Any unhandled exception inside the cascade is caught here and
    returned as a structured refusal with `degraded_state`, so a
    gate raise NEVER propagates to the MCP transport. The transport
    stays alive across 100 sequential gate exceptions; legitimate
    tool calls afterwards succeed.

    See module docstring for the cascade behavior. Internal pipeline
    lives in `_enforce_tool_call_inner`.
    """
    try:
        return _enforce_tool_call_inner(
            hub,
            project_root,
            tool_name,
            tool_input,
            fail_closed=fail_closed,
            include_freeze=include_freeze,
            runtime=runtime,
        )
    except Exception as exc:  # noqa: BLE001
        # Q3-A: latch the degraded gate (Empire-rendered 2026-05-04).
        try:
            from .enforcement_pkg.degraded_latch import DegradedLatch

            DegradedLatch().latch(
                gate_name="gate_cascade",
                dependency_name=type(exc).__name__,
                error=str(exc)[:500],
            )
        except Exception:
            pass
        try:
            hub.execution.record_event(
                project_root,
                event_kind="gate_degraded",
                source_kind="transport_safety_boundary",
                capability_name=tool_name,
                action_kind="evaluate",
                target_entity=str(tool_name)[:200],
                status="degraded",
                payload={
                    "exc": type(exc).__name__,
                    "exc_msg": str(exc)[:500],
                    "tool_name": tool_name,
                },
            )
        except Exception:
            pass
        return EnforceResult(
            refusal={
                "ok": False,
                "blocked_by": "gate_degraded",
                "reason": (
                    f"Gate cascade raised an unhandled exception "
                    f"({type(exc).__name__}); refusing safely. "
                    f"Transport stays alive."
                ),
                "degraded_state": {
                    "gate": "transport_safety_boundary",
                    "exc_type": type(exc).__name__,
                },
            },
            session_id="",
            decision_blocked_by="gate_degraded",
            decision_reason=("gate cascade raised; transport-safety boundary."),
        )


def _enforce_tool_call_inner(
    hub: Any,
    project_root: Path,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    fail_closed: bool = True,
    include_freeze: bool = True,
    runtime: Any = None,
) -> EnforceResult:
    """Inner cascade body — DO NOT call directly. Use the public
    `enforce_tool_call` wrapper, which adds the transport-safety
    boundary (lane 1.6, 2026-05-04) so a gate raise never propagates
    to the MCP transport.

    Run the shared MCP-direct tool-call gate cascade.

    See module docstring for behavior parity with
    ``_run_shell_unified``. ``hub`` must expose ``execution`` for the
    fail-closed audit emit; everything else is resolved via the
    public services.

    Runtime resolution (canonical 2026-04-30):
      - Caller-passed `runtime=` wins (production: mcp_server's
        register_run_tools/code_edit_tools should pass the warm
        RuntimeService instance to avoid per-call object churn).
      - Else `getattr(hub, "runtime", None)` if anyone hangs the
        runtime off hub (no current caller does, kept for forward
        compat).
      - Else fresh `RuntimeService(hub)` constructed inline (test
        fixtures and any non-mcp_server caller). Functionally
        equivalent for AgentOrchestrator.check_tool because
        AgentOrchestrator only reads runtime.hub +
        runtime.effective_config + a few hub-delegating sub-services
        — none of those carry per-RuntimeService state across calls.
    """
    tool_input_dict: dict[str, Any] = dict(tool_input or {})

    # Late imports keep the module import cheap and avoid circulars
    # with agent_orchestrator (which imports access_gate, which
    # imports config, which transitively imports freeze_service).
    from .agent_orchestrator import AgentOrchestrator
    from .freeze_service import (
        build_existing_freeze_response,
        build_freeze_response,
        get_existing_freeze,
    )
    from .managed_mode_service import ManagedModeService

    # 1. Resolve session_id. Failure here is benign — the orchestrator
    #    cascade still runs; only the freeze layer needs sid.
    session_id = ""
    try:
        managed = ManagedModeService().get_mode(project_root)
        if managed.get("active"):
            session_id = str(managed.get("session_id") or "").strip()
    except Exception:
        session_id = ""

    # 1b. CONFIRMATION-WAR SPLIT (2026-05-26): explicit rule_id→class
    #     taxonomy precheck. Runs the same heuristic_judge that the
    #     legacy cascade runs and consults the explicit decision via
    #     JudgeResult.decide(operator_destructive_intent=...). When
    #     the decision is BLOCK_STRIKE (malicious_forbidden), we
    #     short-circuit here with a durable strike (via
    #     SecurityViolationService — NOT the in-memory taxonomy
    #     counter) and refuse with NO confirm path. ALLOW and ASK
    #     fall through to the existing AgentOrchestrator.check_tool
    #     cascade, which keeps owning credential / freeze / confirm
    #     semantics (defensive fallback). This is the single insertion
    #     point that makes inner (Claude/Codex/OpenCode → ai_run) and
    #     outer (ChatGPT → outer_gate.executor → srv.call_tool('ai_run')
    #     → enforce_tool_call) paths consume the same taxonomy
    #     decision, byte-for-byte.
    try:
        _precheck = _judge_taxonomy_precheck(
            hub,
            project_root,
            tool_name,
            tool_input_dict,
            session_id=session_id,
        )
    except Exception:
        _precheck = None
    if _precheck is not None:
        return _precheck

    # 2. Existing-freeze short-circuit.
    #    Doctrine 2026-05-04: operations classified as
    #    `remedial_freeze_management` or `destructive_cleanup` SKIP
    #    the freeze gate by their hardcoded operation_class — they
    #    are how the operator EXITS a freeze. The bypass is
    #    registry-bound, not caller-controlled (a caller cannot
    #    escalate by passing include_freeze=False).
    from .operation_classes import bypasses_freeze_gate

    _is_remedial = bypasses_freeze_gate(tool_name)
    if include_freeze and session_id and not _is_remedial:
        existing = get_existing_freeze(project_root, session_id)
        if existing is not None:
            env = build_existing_freeze_response(existing, project_root)
            return EnforceResult(
                refusal={
                    "ok": False,
                    "blocked_by": env.get("blocked_by", "session_frozen"),
                    "reason": env["permissionDecisionReason"],
                    "freeze_state": env.get("freeze_state"),
                },
                session_id=session_id,
                decision_blocked_by=str(env.get("blocked_by", "session_frozen")),
                decision_reason=str(env["permissionDecisionReason"]),
            )

    # 3. Orchestrator cascade. Runtime resolution per the precedence
    #    documented in the docstring: caller-passed > hub.runtime >
    #    fresh RuntimeService(hub). The fresh-construction fallback
    #    is required because AgentOrchestrator dereferences
    #    self.runtime.hub immediately.
    try:
        if runtime is None:
            runtime = getattr(hub, "runtime", None)
        if runtime is None:
            from .runtime_service import RuntimeService

            runtime = RuntimeService(hub)
        decision = AgentOrchestrator(runtime).check_tool(
            project_root,
            tool_name,
            tool_input_dict,
        )
    except Exception as exc:
        # 5. Fail-closed audit + refusal. Mirrors lines 128–154 of
        #    _run_shell_unified.
        if fail_closed:
            try:
                hub.execution.record_event(
                    project_root,
                    event_kind="evaluate_tool_action_failed",
                    source_kind="gate_tool.enforce_tool_call",
                    session_id=session_id or None,
                    capability_name=tool_name,
                    action_kind="evaluate",
                    target_entity=str(tool_name)[:200],
                    status="error",
                    payload={
                        "tool_name": tool_name,
                        "exc": type(exc).__name__,
                    },
                )
            except Exception:
                pass
            return EnforceResult(
                refusal={
                    "ok": False,
                    "blocked_by": "evaluator_exception",
                    "reason": ("gate cascade evaluation failed; refusing."),
                },
                session_id=session_id,
                decision_blocked_by="evaluator_exception",
                decision_reason="gate cascade evaluation failed; refusing.",
            )
        # fail_closed=False is currently unused by any caller; kept
        # for explicit opt-out symmetry. Behaves like the legacy
        # fail-open path: caller proceeds.
        return EnforceResult(refusal=None, session_id=session_id)

    if decision.allowed:
        return EnforceResult(refusal=None, session_id=session_id)

    # 4a. needs_confirmation but no session → structured blocker, not flat deny.
    if include_freeze and getattr(decision, "needs_confirmation", False) and not session_id:
        return EnforceResult(
            refusal={
                "ok": False,
                "blocked_by": "no_managed_session",
                "reason": (
                    "action requires operator approval but no active managed "
                    "session exists; run /aidocs first"
                ),
            },
            session_id="",
            decision_blocked_by="no_managed_session",
            decision_reason="no managed session for confirmation",
        )

    # 4b. needs_confirmation + session_id → mint a fresh freeze.
    if include_freeze and getattr(decision, "needs_confirmation", False) and session_id:
        # Freeze mint must succeed before we present any approval prompt.
        # If build_freeze_response raises (FreezeMintError or otherwise),
        # hard-block with a truthful reason — never a hollow "Type exactly"
        # prompt that resolves against nothing.
        try:
            env = build_freeze_response(
                project_root,
                session_id,
                tool_name=tool_name,
                tool_input=tool_input_dict,
                judge_summary=str(decision.reason or ""),
                risk_class=(str(getattr(decision, "risk_class", "") or "") or "destructive_action"),
                jurisdiction=(str(getattr(decision, "jurisdiction", "") or "") or "in"),
            )
        except Exception:
            return EnforceResult(
                refusal={
                    "ok": False,
                    "blocked_by": "freeze_mint_failed",
                    "reason": (
                        "operator approval could not be created; action "
                        "remains blocked; retry or contact admin"
                    ),
                },
                session_id=session_id,
                decision_blocked_by="freeze_mint_failed",
                decision_reason="freeze mint failed",
            )
        return EnforceResult(
            refusal={
                "ok": False,
                "blocked_by": env.get("blocked_by", "judge_confirm_required"),
                "reason": env["permissionDecisionReason"],
                "freeze_state": env.get("freeze_state"),
            },
            session_id=session_id,
            decision_blocked_by=str(env.get("blocked_by", "judge_confirm_required")),
            decision_reason=str(env["permissionDecisionReason"]),
        )

    # Flat deny.
    blocked_by = str(getattr(decision, "blocked_by", "") or "denied")
    reason = str(getattr(decision, "reason", "") or "tool refused")
    return EnforceResult(
        refusal={
            "ok": False,
            "blocked_by": blocked_by,
            "reason": (f"refused: {reason} (blocked_by={blocked_by})"),
        },
        session_id=session_id,
        decision_blocked_by=blocked_by,
        decision_reason=reason,
    )
