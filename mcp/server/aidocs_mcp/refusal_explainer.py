"""Refusal explainer — for WHAT KIND of block, WHAT HAPPENS (read-only).

WHY THIS EXISTS (operator, 2026-08-25). An agent ran a plain CSS append —
``printf '...' >> document-items.css`` — and the session froze under the
escalation permission ``run_destructive``; every subsequent tool call,
including reads, was refused until an operator decided the escalation. The
operator's question was not "why this one" but "for what kind of block does
what happen" — and no surface answered it. The answer was spread across six
modules, each owning one hop:

    heuristic_judge   which RULE fires for a command          (SHELL_WRITE_SOURCE)
    judge_taxonomy    which judge CLASS that rule is           (confirmable_destructive)
    intent detector   whether the operator's prompt flips it   (any of ~25 words)
    agent_orchestrator which ``blocked_by`` tier that becomes  (judge_confirm_required)
    verdict_class     which LADDER rung that tier lands on     (fail-closed -> security_freeze)
    freeze_service    what a rung costs: strike / freeze / who clears it

This module walks those hops IN THE ORDER THE GATE DOES, with the real
functions, and reports each one. It never mints, never strikes, never writes:
it is the map, not the territory. Everything it reports is computed by the
same code the gate runs, so it cannot drift from the gate the way prose can.

Three entry points:

  explain_refusal(...)   a refusal identity (blocked_by / matched_rule /
                         risk_class / intent) -> its consequences.
  explain_command(...)   a shell command -> the judge verdicts, the taxonomy
                         decision on BOTH intent branches, the blocked_by
                         each branch produces, and the consequences of each.
  refusal_matrix(...)    the whole table: every known confirm tier, every
                         flat-deny strike family, every judge rule, every
                         freeze kind, every freeze-exempt tool.

Honesty rules, inherited from ai_whoami (#859): every consequence names the
code that decides it; nothing here predicts what a live gate WILL do for a
live session (that depends on session state this module does not read —
strike counts, an already-active freeze, the session's stamped prompt tokens).
It states what the code maps each input to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ── The confirm-bearing tiers the orchestrator actually emits ─────────
#
# ``verdict_class`` classifies a ``blocked_by`` it recognises; anything else
# fails closed to the freeze rung. That is the right safety direction, but it
# means the tiers NOT in its tables are invisible unless someone lists them.
# These are the ``needs_confirmation=True`` emitters in
# ``AgentOrchestrator.check_tool`` as of 2026-08-25 (the only decisions that
# ever reach the ladder). A drift test pins each one to the orchestrator
# source so this list cannot silently go stale.
CONFIRMABLE_EMITTERS: dict[str, dict[str, str]] = {
    "bash_policy_ask": {
        "emitted_by": "AgentOrchestrator.check_tool — bash_policy verdict='ask' (default=ask, unlisted command)",
        "example": "pwd / dotnet restore with bash.default=ask",
    },
    "bash_policy_confirmable": {
        "emitted_by": (
            "AgentOrchestrator.check_tool — deny-table or destructive-default hit "
            "AND operator destructive intent detected"
        ),
        "example": "rm -rf ./build with deny={rm:['*']} and 'delete' in the prompt",
    },
    "judge_confirm_required": {
        "emitted_by": (
            "AgentOrchestrator.check_tool — heuristic_judge Class-C (confirmable_destructive) "
            "verdict AND operator destructive intent detected; matched_rule carries the rule_id"
        ),
        "example": "printf '...' >> app.css (SHELL_WRITE_SOURCE) with 'all' in the prompt",
    },
    "anticoup_<kind>": {
        "emitted_by": "AgentOrchestrator.check_tool — anti-coup decision='ask'; risk_class='control_plane:<kind>'",
        "example": "registering an MCP server / editing the hook config",
    },
}

# Flat-deny tiers (needs_confirmation=False) the orchestrator / precheck emit.
# They never reach the ladder; their cost is decided by the strike ledger.
FLAT_DENY_EMITTERS: dict[str, str] = {
    "heuristic_judge": "judge Class-C verdict WITHOUT operator intent (orchestrator path)",
    "judge_confirmable_no_intent": "judge confirmable_destructive WITHOUT operator intent (gate_tool precheck path)",
    "judge_malicious_forbidden": "judge malicious_forbidden (gate_tool precheck) — strike",
    "heuristic_judge_sensitive": "judge Class-B sensitive read hard floor — strike",
    "judge_credential_confirm": "credential token in payload matched the prompt — reply 'yes' flow, no freeze",
    "bash_policy": "bash_policy deny / dangerous_chain / unauthenticated host",
    "bash_policy_missing": "no declarative [bash] table configured",
    "tier0_raw_shell": "native Bash in a managed session (Invariant #38) — friction",
    "tier0_edit_redirect": "raw Edit/Write in a managed session — friction, redirect to ai_*",
    "raw_tool": "raw file tool on a managed file",
    "host_read": "host Read of a sensitive external path — strike",
    "command_read_intent": "read of a sensitive/external target — soft",
    "sensitive_path_blocked": "write/read of a sensitive external path — strike",
    "unknown_external_path": "path outside every known root — soft (strike for OS infra paths)",
    "path_input_conflict": "path/input shape mismatch",
    "infrastructure": ".github/.git config/deploy script without grant — friction",
    "foreground_long_running": "long runner in the foreground — friction",
    "test_retry": "same test re-run too often — friction",
    "tool_policy": "tool not allowed on this surface — friction",
    "lane_tool": "conductor-only tool from a non-conductor — friction",
    "managed_mode_inactive": "no session bound — friction",
    "todowrite_disabled": "TodoWrite is disabled on managed projects",
    "cross_agent_scope_conflict": "another agent owns the path",
    "cross_session_scratchpad_unestablished": "scratchpad not established for this session",
    "cross_session_scratchpad_laundered": "scratchpad path laundered across sessions",
    "anticoup_unavailable": "anti-coup evaluator unavailable — fail closed",
    "anticoup_<kind>": "anti-coup decision='deny'",
}


# ── helpers ────────────────────────────────────────────────────────────


def _ladder_match(blocked_by: str, matched_rule: str, risk_class: str, intent: bool) -> str:
    """Name WHICH branch of ``verdict_class.classify`` decided — the same
    order as the function, so the label is the function's own reasoning."""
    from . import verdict_class as vc

    bb = (blocked_by or "").strip().lower()
    mr = (matched_rule or "").strip().lower()
    rc = (risk_class or "").strip().lower()
    if bb in vc._FREEZE_BLOCKED_BY:
        return "blocked_by in _FREEZE_BLOCKED_BY (rung 1, capability)"
    if rc.startswith(vc._FREEZE_RISK_PREFIXES):
        return "risk_class prefix in _FREEZE_RISK_PREFIXES (rung 1, capability)"
    if intent:
        return "user_intent_detected -> no offence"
    if bb in vc._STRIKE_BLOCKED_BY:
        return "blocked_by in _STRIKE_BLOCKED_BY (rung 2, scope)"
    if bb in vc._WORKFLOW_BLOCKED_BY:
        return "blocked_by in _WORKFLOW_BLOCKED_BY (rung 3, workflow)"
    judge_rules = getattr(vc, "_WORKFLOW_JUDGE_RULES", frozenset())
    if bb == "judge_confirm_required" and mr.upper() in judge_rules:
        return "judge rule in _WORKFLOW_JUDGE_RULES (rung 3, tool-shape redirect)"
    if mr in vc._WORKFLOW_MATCHED_RULES:
        return "matched_rule in _WORKFLOW_MATCHED_RULES (rung 3, workflow)"
    if rc.startswith(vc._WORKFLOW_RISK_PREFIXES):
        return "risk_class prefix in _WORKFLOW_RISK_PREFIXES (rung 3, workflow)"
    return "UNRECOGNISED -> fail-closed to rung 1 (security_freeze)"


def _freeze_kind_facts(kind: str) -> dict[str, Any]:
    """What a freeze of this KIND costs and who may lift it — from the
    store's own constants and the card renderer's own branches."""
    from .session_freeze_store import (
        KIND_ADMIN_ESCALATION,
        KIND_HOSTILE_OPERATOR_PROMPT,
        KIND_REPEATED_SECURITY_VIOLATION,
        KIND_SELF_APPROVE,
    )

    table = {
        KIND_SELF_APPROVE: {
            "who_can_clear": (
                "an operator: approve the escalation (aidocs admin approve-escalation "
                "<request-id>) or clear the freeze (aidocs admin clear-freeze --freeze-id "
                "<request-id>); the 'confirm <verb>-<hash>' phrase is resolved at the "
                "operator's UserPromptSubmit — an agent cannot emit it"
            ),
            "self_approvable": True,
            "scope": "actor (the one agent whose action was refused)",
            "ttl": "expires on the deadline the card states (freeze_ttl_note)",
        },
        KIND_ADMIN_ESCALATION: {
            "who_can_clear": "an operator with rbac.admin_clear_freeze",
            "self_approvable": False,
            "scope": "actor",
            "ttl": "per card",
        },
        KIND_REPEATED_SECURITY_VIOLATION: {
            "who_can_clear": "ONLY an operator with rbac.admin_clear_freeze — never a UPS phrase",
            "self_approvable": False,
            "scope": "actor (strike ledger partition)",
            "ttl": "NO automatic TTL for security kinds",
        },
        KIND_HOSTILE_OPERATOR_PROMPT: {
            "who_can_clear": "ONLY an operator with rbac.admin_clear_freeze",
            "self_approvable": False,
            "scope": "the whole work session (every actor)",
            "ttl": "NO automatic TTL",
        },
    }
    return {"kind": kind, **table.get(kind, {"who_can_clear": "unknown kind", "self_approvable": False})}


def _strike_facts(blocked_by: str, target: str = "") -> dict[str, Any]:
    """Does this flat deny accrue a strike, and where does the ladder end?"""
    from . import security_violation_service as svs
    from . import violation_severity as vsev

    family = svs.FAMILY_BY_BLOCKED_BY.get(blocked_by, "")
    if not family:
        return {
            "strike": False,
            "family": "",
            "note": (
                "not in security_violation_service.FAMILY_BY_BLOCKED_BY -> no strike is "
                "recorded by the generic path (some emitters strike by family directly, "
                "e.g. tier0_raw_shell='raw_shell_t0', edit redirect='tier0_edit_redirect')"
            ),
        }
    severity = vsev.classify_severity(family, target=target or None)
    threshold = svs._DEFAULT_THRESHOLD
    sensitive_min = getattr(svs, "_SENSITIVE_READ_MIN_FREEZE_THRESHOLD", threshold)
    if family in getattr(svs, "_SENSITIVE_READ_FAMILIES", frozenset()):
        threshold = max(threshold, sensitive_min)
    out: dict[str, Any] = {
        "strike": severity not in (vsev.SEVERITY_FRICTION,),
        "family": family,
        "severity": severity,
        "freeze_threshold_default": threshold,
        "threshold_note": (
            "config security.* agent threshold overrides the default; 0 disables the "
            "freeze escalation (strikes are still recorded)"
        ),
    }
    if severity == vsev.SEVERITY_FRICTION:
        out["consequence"] = "friction: refused + audited, NO strike, NO freeze; " + (
            vsev.FRICTION_REDIRECTS.get(family, "retry via the governed path")
        )
    elif severity == vsev.SEVERITY_IMMEDIATE_FREEZE:
        out["consequence"] = "IMMEDIATE freeze (repeated_security_violation kind, admin-clear-only)"
        out["freeze"] = _freeze_kind_facts("repeated_security_violation")
    else:
        out["consequence"] = (
            f"{severity}: refused + strike recorded; the {threshold}th strike in this "
            "family partition freezes the actor (repeated_security_violation, admin-clear-only); "
            "a warning is issued one strike before"
        )
        out["freeze_at_threshold"] = _freeze_kind_facts("repeated_security_violation")
    return out


# ── 1. explain a refusal identity ──────────────────────────────────────


def explain_refusal(
    *,
    blocked_by: str = "",
    matched_rule: str = "",
    risk_class: str = "",
    user_intent_detected: bool = False,
    needs_confirmation: bool = True,
    target: str = "",
) -> dict[str, Any]:
    """A refusal identity -> its consequences, each naming the deciding code.

    ``needs_confirmation=True`` is the confirm-bearing path: the #571 ladder
    (``verdict_class.outcome_for``) decides allow / block / freeze.
    ``needs_confirmation=False`` is a flat deny: the ladder is never consulted;
    the cost is decided by the strike ledger
    (``security_violation_service`` + ``violation_severity``).
    """
    from . import verdict_class as vc

    inp = {
        "blocked_by": blocked_by,
        "matched_rule": matched_rule,
        "risk_class": risk_class,
        "user_intent_detected": bool(user_intent_detected),
        "needs_confirmation": bool(needs_confirmation),
    }
    if not needs_confirmation:
        facts = _strike_facts(blocked_by, target=target)
        return {
            "input": inp,
            "path": "flat_deny",
            "decided_by": "security_violation_service.record_and_escalate (strike ledger); the #571 ladder is not consulted",
            "outcome": "deny",
            "freezes_now": bool(facts.get("severity") == "immediate_freeze"),
            "latches_next_calls": bool(facts.get("severity") == "immediate_freeze"),
            "agent_cancellable": True,
            "strike": facts,
            "summary": facts.get("consequence", "refused"),
        }

    outcome, resolved = vc.outcome_for(
        blocked_by=blocked_by,
        matched_rule=matched_rule,
        risk_class=risk_class,
        user_intent_detected=user_intent_detected,
    )
    out: dict[str, Any] = {
        "input": inp,
        "path": "confirmable",
        "decided_by": "verdict_class.outcome_for (the ONE three-way router every mint site calls)",
        "ladder_match": _ladder_match(blocked_by, matched_rule, risk_class, user_intent_detected),
        "verdict_class": resolved,
        "outcome": outcome,
        "security_class": vc.is_security_class(resolved),
        "issues_strike": vc.issues_strike(resolved),
        "freezes_agent": vc.freezes_agent(resolved),
        "agent_cancellable": vc.agent_cancellable(resolved),
        "gate_permission": vc.gate_permission_for(resolved),
    }
    if outcome == vc.OUTCOME_ALLOW:
        out["latches_next_calls"] = False
        out["summary"] = "ALLOW — detected user-intent already covered the action; not even blocked"
    elif outcome == vc.OUTCOME_BLOCK:
        out["latches_next_calls"] = False
        out["writes"] = "nothing: no freeze row, no escalation request, no strike (freeze_service.build_workflow_block_response)"
        out["summary"] = (
            "WORKFLOW BLOCK — refused with a 'Do this:' redirect; nothing latched; "
            "the agent may drop the action and continue at no cost"
        )
    else:
        out["latches_next_calls"] = True
        out["writes"] = (
            "an EscalationStore request filed under gate_permission="
            f"{out['gate_permission']!r} + a session_freeze row (kind=self_approve, "
            "scope=actor) via freeze_service.build_freeze_response"
        )
        out["freeze"] = _freeze_kind_facts("self_approve")
        out["while_frozen"] = (
            "every tool call by this actor short-circuits at get_existing_freeze — "
            "reads included — except the freeze-exempt tools (see refusal_matrix()['freeze_exempt_tools'])"
        )
        if resolved == vc.CLASS_SECURITY_STRIKE:
            out["note"] = (
                "rung 2 is strike-bearing AND still routes to the freeze pipeline in this "
                "pass (#571 GAP A, deliberate) — the class metadata says 'strike, no "
                "freeze' but the enforcement freezes"
            )
        out["summary"] = (
            f"FREEZE — session frozen pending operator decision (filed as {out['gate_permission']}); "
            "next identical retry passes once after approval"
        )
    return out


# ── 2. dry-run a command through the real judge / taxonomy / ladder ──────


def _judge_branch(rule_ids: list[str], *, intent: bool, surface: str) -> dict[str, Any]:
    """What the taxonomy decides for these rule ids on one intent branch, and
    which blocked_by tier the gate turns that into. Mirrors, by name, the
    branches in gate_tool._judge_taxonomy_precheck and
    AgentOrchestrator.check_tool's judge block."""
    from . import judge_taxonomy as jt

    decision = jt.evaluate_verdicts(rule_ids, operator_destructive_intent=intent)
    d = decision.decision
    branch: dict[str, Any] = {
        "operator_destructive_intent": intent,
        "taxonomy_decision": d,
        "triggering_rule_id": decision.triggering_rule_id,
        "triggering_class": decision.triggering_class,
        "taxonomy_reason": decision.reason,
    }
    if d == jt.DECISION_ALLOW:
        branch["blocked_by"] = ""
        branch["gate_tier"] = "judge passes (advisory only); later gates may still refuse"
        branch["consequence"] = {"outcome": "allow (judge)"}
        return branch
    if d == jt.DECISION_BLOCK_STRIKE:
        bb = "judge_malicious_forbidden" if surface == "gate_tool" else "heuristic_judge_sensitive"
        branch["blocked_by"] = bb
        branch["gate_tier"] = (
            "gate_tool._judge_taxonomy_precheck -> flat refusal + durable strike "
            "(family judge_malicious_forbidden)"
            if surface == "gate_tool"
            else "orchestrator Class-B/forbidden floor -> flat refusal + strike"
        )
        branch["consequence"] = explain_refusal(blocked_by=bb, needs_confirmation=False)
        return branch
    if d == jt.DECISION_BLOCK_FREEZE_NO_CONFIRM:
        bb = "judge_confirmable_no_intent" if surface == "gate_tool" else "heuristic_judge"
        branch["blocked_by"] = bb
        branch["gate_tier"] = (
            "flat refusal, NO confirm path, NO freeze row (despite the decision's name); "
            "the refusal text carries the judge recommendation"
        )
        branch["consequence"] = explain_refusal(blocked_by=bb, needs_confirmation=False)
        return branch
    # DECISION_ASK_CONFIRM
    branch["blocked_by"] = "judge_confirm_required"
    branch["gate_tier"] = (
        "AgentOrchestrator.check_tool -> ToolDecision(needs_confirmation=True, "
        "blocked_by='judge_confirm_required', matched_rule=<rule_id>) -> #571 ladder"
    )
    branch["consequence"] = explain_refusal(
        blocked_by="judge_confirm_required",
        matched_rule=decision.triggering_rule_id,
        needs_confirmation=True,
    )
    return branch


def explain_command(
    command: str,
    *,
    tool_name: str = "ai_run",
    prompt: str = "",
    project_root: Path | None = None,
) -> dict[str, Any]:
    """A shell command -> every hop of the gate's decision, on BOTH intent
    branches, without minting anything.

    ``prompt`` (optional) is scanned with the real destructive-intent
    detector so the operator can see WHICH words in their message would have
    flipped the branch. The live gate reads those tokens from the session's
    stamped last prompt, which this function does not read.
    """
    from . import judge_taxonomy as jt
    from .heuristic_judge import evaluate_tool_call
    from .intent_grant_detector import (
        _DESTRUCTIVE_INTENT_TOKENS,
        detect_destructive_intent_in_text,
    )

    cmd = str(command or "")
    out: dict[str, Any] = {
        "command": cmd[:400],
        "tool_name": tool_name,
        "hops": [],
    }

    # Hop 0 — surface. Native Bash in a managed session never reaches the judge.
    bare = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    if bare in ("bash", "powershell", "pwsh", "cmd", "shell", "wsl", "monitor"):
        out["hops"].append({
            "hop": "surface",
            "decided_by": "AccessGate.check_raw_shell (Invariant #38)",
            "verdict": (
                "in a MANAGED session a native shell tool is T0-blocked BEFORE the judge: "
                "flat refusal, family raw_shell_t0 = friction (no strike, no freeze), "
                "'Use ai_run'. The hops below apply when the same command is sent via ai_run "
                "(or a native shell provider that delegates to the core law)."
            ),
        })

    # Hop 1 — bash_policy (operator allow/deny tables), when a policy is loadable.
    policy_hop: dict[str, Any] = {"hop": "bash_policy", "decided_by": "bash_policy.evaluate_bash_policy"}
    try:
        from .bash_policy import evaluate_bash_policy, load_canonical_bash_policy

        policy = load_canonical_bash_policy(project_root=project_root) if project_root else None
        if isinstance(policy, dict) and policy:
            pd = evaluate_bash_policy(cmd, policy, workspace_root=str(project_root))
            policy_hop["allowed"] = bool(pd.get("allowed"))
            policy_hop["matched_rule"] = str(pd.get("matched_rule") or "")
            policy_hop["reason"] = str(pd.get("reason") or "")
            if not pd.get("allowed"):
                if pd.get("verdict") == "ask":
                    policy_hop["blocked_by"] = "bash_policy_ask"
                    policy_hop["consequence"] = explain_refusal(
                        blocked_by="bash_policy_ask",
                        matched_rule=policy_hop["matched_rule"],
                        needs_confirmation=True,
                    )
                else:
                    policy_hop["blocked_by"] = "bash_policy (flat) — or bash_policy_confirmable when deny-table/destructive-default + operator intent"
                    policy_hop["consequence_flat"] = explain_refusal(blocked_by="bash_policy", needs_confirmation=False)
                    policy_hop["consequence_with_intent"] = explain_refusal(
                        blocked_by="bash_policy_confirmable", needs_confirmation=True,
                    )
        else:
            policy_hop["skipped"] = (
                "no canonical [bash] policy loadable for this project_root — the live gate "
                "FAILS CLOSED here (bash_policy_missing) when the table is absent"
            )
    except Exception as exc:  # noqa: BLE001 -- a diagnostic must never raise
        policy_hop["error"] = str(exc)
    out["hops"].append(policy_hop)

    # Hop 2 — the judge.
    judge = evaluate_tool_call(tool_name, {"command": cmd}, project_root=project_root)
    verdicts = [v.to_dict() for v in judge.verdicts]
    rule_ids = [v.rule_id for v in judge.verdicts if v.rule_id != "SEMANTIC_CONTEXT"]
    out["hops"].append({
        "hop": "heuristic_judge",
        "decided_by": "heuristic_judge.evaluate_tool_call",
        "verdicts": verdicts,
        "judge_classes": {rid: jt.classify(rid) for rid in rule_ids},
    })

    # Hop 3 — intent.
    matched_tokens = detect_destructive_intent_in_text(prompt) if prompt else []
    out["hops"].append({
        "hop": "operator_destructive_intent",
        "decided_by": "intent_grant_detector.detect_destructive_intent (session's stamped prompt tokens)",
        "prompt_supplied": bool(prompt),
        "matched_tokens_in_supplied_prompt": matched_tokens,
        "note": (
            "ANY token in the list below, anywhere in the operator's last prompt, unlocks the "
            "confirm branch for ANY confirmable_destructive rule — matching is not per-rule"
        ),
        "tokens": sorted(_DESTRUCTIVE_INTENT_TOKENS),
    })

    # Hop 4 — taxonomy decision + ladder, both branches, on the shared gate_tool
    # path (precheck) and the hook/tool_gate_service path (no precheck).
    if rule_ids:
        out["hops"].append({
            "hop": "judge_taxonomy + #571 ladder",
            "decided_by": "judge_taxonomy.evaluate_verdicts -> orchestrator blocked_by -> verdict_class.outcome_for",
            "without_intent": _judge_branch(rule_ids, intent=False, surface="gate_tool"),
            "with_intent": _judge_branch(rule_ids, intent=True, surface="gate_tool"),
            "hook_path_note": (
                "the Claude Code PreToolUse hook path (tool_gate_service.orchestrator_check) "
                "has no taxonomy precheck: the no-intent refusal there is blocked_by="
                "'heuristic_judge' (flat, same cost); the with-intent branch is identical"
            ),
        })
    if prompt:
        out["branch_for_supplied_prompt"] = "with_intent" if matched_tokens else "without_intent"
    return out


# ── 3. the whole table ─────────────────────────────────────────────────


def refusal_matrix(project_root: Path | None = None) -> dict[str, Any]:
    """Every kind of block the gate can produce, and what each one costs."""
    from . import judge_taxonomy as jt
    from . import operation_classes as oc
    from . import verdict_class as vc
    from .intent_grant_detector import _DESTRUCTIVE_INTENT_TOKENS
    from .session_freeze_store import VALID_KINDS

    # Confirm-bearing tiers: the ladder's own tables + the emitters it does
    # not recognise (which fail closed).
    confirm: dict[str, Any] = {}
    for bb in sorted(vc._FREEZE_BLOCKED_BY | vc._STRIKE_BLOCKED_BY | vc._WORKFLOW_BLOCKED_BY):
        confirm[bb] = explain_refusal(blocked_by=bb, needs_confirmation=True)
    for mr in sorted(vc._WORKFLOW_MATCHED_RULES):
        confirm[f"matched_rule={mr}"] = explain_refusal(matched_rule=mr, needs_confirmation=True)
    for rc in vc._FREEZE_RISK_PREFIXES + vc._WORKFLOW_RISK_PREFIXES:
        confirm[f"risk_class={rc}:*"] = explain_refusal(risk_class=f"{rc}:x", needs_confirmation=True)
    emitters: dict[str, Any] = {}
    for bb, meta in CONFIRMABLE_EMITTERS.items():
        probe = bb.replace("<kind>", "mcp_registry")
        rc = "control_plane:mcp_registry" if bb.startswith("anticoup") else ""
        emitters[bb] = {**meta, "consequence": explain_refusal(blocked_by=probe, risk_class=rc, needs_confirmation=True)}
    judge_rules_ws = sorted(getattr(vc, "_WORKFLOW_JUDGE_RULES", frozenset()))
    for rid in judge_rules_ws:
        emitters[f"judge_confirm_required + matched_rule={rid}"] = {
            "emitted_by": "judge Class-C verdict with operator intent, rule_id carried as matched_rule",
            "consequence": explain_refusal(
                blocked_by="judge_confirm_required", matched_rule=rid, needs_confirmation=True,
            ),
        }

    # Flat denies.
    flat: dict[str, Any] = {}
    for bb, desc in FLAT_DENY_EMITTERS.items():
        probe = bb.replace("<kind>", "mcp_registry")
        flat[bb] = {"emitted_by": desc, **explain_refusal(blocked_by=probe, needs_confirmation=False)}

    # Judge rules by taxonomy class, with what each class costs.
    by_class: dict[str, list[str]] = {}
    for rid, cls in sorted(jt.RULE_CLASS.items()):
        by_class.setdefault(cls, []).append(rid)
    try:
        from .heuristic_judge import list_judge_rules

        known = {str(r["rule_id"]) for r in list_judge_rules(project_root)}
    except Exception:  # noqa: BLE001
        known = set()
    unmapped = sorted(r for r in known if r not in jt.RULE_CLASS)
    class_cost = {
        jt.CLASS_SAFE_ADVISORY: "pass; recorded as advisory; never blocks by itself",
        jt.CLASS_CONFIRMABLE_DESTRUCTIVE: (
            "WITHOUT operator intent: flat refusal (judge_confirmable_no_intent / heuristic_judge), "
            "no strike, no freeze. WITH any destructive-intent token in the prompt: "
            "judge_confirm_required -> ladder -> "
            "workflow BLOCK for rules in verdict_class._WORKFLOW_JUDGE_RULES "
            f"({', '.join(judge_rules_ws) or 'none'}), otherwise "
            "security_freeze filed as run_destructive"
        ),
        jt.CLASS_MALICIOUS_FORBIDDEN: (
            "hard refusal, no confirm path, durable strike (family judge_malicious_forbidden, "
            "severity strike); the threshold-th strike freezes the actor admin-clear-only"
        ),
    }

    freeze_kinds = {k: _freeze_kind_facts(k) for k in sorted(VALID_KINDS)}

    exempt = {
        "bypass_freeze_gate_by_operation_class": sorted(
            name for name in oc._TOOL_OPERATION_CLASS if oc.bypasses_freeze_gate(name)
        ),
        "remedy_reachability_tools": sorted(oc._REMEDY_REACHABILITY_TOOLS),
        "report_mode_tools": {k: sorted(v) for k, v in oc._REPORT_MODE_TOOLS.items()},
        "disarm_only_tools": dict(oc._DISARM_ONLY_TOOLS),
        "note": (
            "everything else — ai_find, ai_get_lines, Bash, every edit tool — is refused "
            "while the actor is frozen (freeze_service.get_existing_freeze short-circuit)"
        ),
    }

    return {
        "ladder": {
            "rung_3_workflow_block": "refuse + redirect; no strike, no freeze, agent-cancellable for free",
            "rung_2_security_strike": "strike-bearing; STILL FREEZES in this pass (#571 GAP A)",
            "rung_1_security_freeze": "freeze; escalation filed as run_destructive; operator decides",
            "no_offence": "detected user-intent already covered the action; not blocked",
            "fail_closed": "an unrecognised blocked_by classifies to rung 1",
            "decided_by": "verdict_class.classify / outcome_for",
        },
        "confirm_tiers_in_ladder_tables": confirm,
        "confirm_tiers_emitted_by_orchestrator": emitters,
        "flat_deny_tiers": flat,
        "judge_rules_by_taxonomy_class": {
            "class_cost": class_cost,
            "rules": by_class,
            "unmapped_rules_default_to": jt.CLASS_CONFIRMABLE_DESTRUCTIVE,
            "unmapped_rules": unmapped,
        },
        "destructive_intent_tokens": sorted(_DESTRUCTIVE_INTENT_TOKENS),
        "freeze_kinds": freeze_kinds,
        "freeze_exempt_tools": exempt,
    }
