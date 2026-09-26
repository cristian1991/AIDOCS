"""Observe-only ShellPolicy shadow (Batch 1.5).

Runs ShellPolicy alongside the LIVE PreToolUse cascade for native-shell
tools to gather real-traffic parity evidence — WITHOUT enforcing, without
enabling native execution, and CRUCIALLY without re-running the live gate
cascade.

The dangerous trap this module exists to avoid: ShellPolicy's default law
delegate calls ``gate_tool.enforce_tool_call`` again, which would duplicate
audits, freeze minting, sticky-grant checks, and degraded markers. So the
shadow injects a SIDE-EFFECT-FREE delegate built from the ALREADY-COMPUTED
live verdict. ShellPolicy's own pure stages (provider dialect, read-bypass
evaluation, empty-command validation) still run and can diverge from the
live verdict — that divergence is exactly the signal we want.

Hard guarantees:
  * never raises to the caller (best-effort; records shell_policy_shadow_error)
  * never calls enforce_tool_call / the live cascade
  * never calls ai_run
  * never mints freezes / writes grants
  * never blocks the live tool — caller returns the live verdict unchanged
  * default OFF (tools.shell_policy_shadow_enabled); config-read failure → off
  * audit payload carries hashes/sizes + rule IDs, never raw command text
  * NEVER shadows an UNGUARDABLE native path. Shadow runs only where the
    capability matrix proves command-visibility + PreToolUse hard-deny for
    (host, provider). An unguardable native path is recorded as
    status="skipped_unguardable" (would_block, fallback=ai_run) and its
    command is NOT evaluated — we do not observe a command we cannot
    intercept while it executes. The Batch-2 enforcement for such paths is
    block + ai_run fallback, not shadow.

Divergence is split:
  * law_diverged       — ShellPolicy's law verdict differs from live
                         (e.g. provider dialect caught a PowerShell form the
                         bash-centric cascade missed). A real signal.
  * transport_diverged — ShellPolicy downgraded an allowed call to
                         fallback_to_ai_run / capability_unsupported because
                         native is disabled or capability-gated. Healthy,
                         expected, NOT a law disagreement.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import shell_capability_matrix as _matrix
from . import shell_policy as _sp
from .shell_envelope import (
    TRANSPORT_HOST_NATIVE,
    TRANSPORT_MONITOR,
    detect_provider_and_transport,
    normalize,
)

# live verdict → ShellPolicy law decision vocabulary.
# "continue" is ToolGateResult's DEFAULT, non-terminal verdict — it means
# "all gates passed, proceed", i.e. the dominant ALLOW path for benign
# commands. Mapping it to allow keeps law_diverged honest; mapping it to
# deny would flag a false law divergence on nearly every benign native
# command and destroy the parity signal. Genuinely UNKNOWN strings still
# fall through to the conservative LAW_DENY default in _run().
_LIVE_TO_LAW = {
    "continue": _sp.LAW_ALLOW,
    "allow": _sp.LAW_ALLOW,
    "deny": _sp.LAW_DENY,
    "ask": _sp.LAW_CONFIRMABLE,
}


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _live_delegate(live_law_decision: str, live_blocked_by: str):
    """Build a side-effect-free LawDelegate that REPLAYS the already-
    computed live verdict instead of re-running enforce_tool_call.
    """
    if live_law_decision == _sp.LAW_ALLOW:
        refusal = None
    elif live_law_decision == _sp.LAW_CONFIRMABLE:
        # Truthy freeze_state marks the confirmable branch in _core_law
        # without minting anything (it is a synthetic replay marker).
        refusal = {
            "blocked_by": live_blocked_by or "confirmable",
            "reason": "live cascade verdict (shadow replay)",
            "freeze_state": {"shadow_replay": True},
        }
    else:  # deny
        refusal = {
            "blocked_by": live_blocked_by or "deny",
            "reason": "live cascade verdict (shadow replay)",
        }

    class _Replay:
        def __init__(self) -> None:
            self.refusal = refusal

    def _delegate(_envelope) -> Any:
        return _Replay()

    return _delegate


def _blocked_by_from_why(why: tuple) -> str:
    """Best-effort blocked_by extraction from the live result's why
    tuple. Not load-bearing — law divergence compares law decisions, not
    blocked_by — so an empty result is acceptable.
    """
    markers = (
        "orchestrator_deny",
        "agent_brief_blocked",
        "reconnect_required",
    )
    items = list(why or ())
    for i, m in enumerate(items):
        if m in markers and i + 1 < len(items):
            return str(items[i + 1])
    return ""


def _enabled(project_root: Path) -> bool:
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tools.shell_policy_shadow_enabled",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def _native_enabled(project_root: Path) -> bool:
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


def run_pretool_shadow(
    *,
    project_root: Path,
    host: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    host_session_id: str,
    live_verdict: str,
    live_why: tuple = (),
    managed_session_id: str = "",
    lane: str = "",
) -> None:
    """Best-effort observe-only shadow. Returns None always. The caller
    proceeds with its live verdict regardless of what happens here.
    """
    try:
        # Only native-shell surfaces; ai_run and non-shell tools skip.
        provider, transport = detect_provider_and_transport(tool_name)
        if transport not in (TRANSPORT_HOST_NATIVE, TRANSPORT_MONITOR):
            return
        if not _enabled(project_root):
            return
        # HARD RULE (Empire directive): never shadow an UNGUARDABLE native
        # shell path. If AIDOCS cannot prove it intercepts + hard-denies
        # the command before execution (capability matrix), we do NOT
        # observe it executing — we record that the path must be blocked
        # and routed to ai_run, and we run NO command policy evaluation.
        # "We don't watch dragons we can't shoot; we close the border."
        if not _matrix.is_native_safe(host, provider):
            _record_unguardable(
                project_root,
                tool_name,
                host,
                provider,
                transport,
            )
            return
        _run(
            project_root=project_root,
            host=host,
            tool_name=tool_name,
            tool_input=tool_input or {},
            host_session_id=host_session_id,
            live_verdict=live_verdict,
            live_why=live_why,
            managed_session_id=managed_session_id,
            lane=lane,
        )
    except Exception as exc:  # never propagate
        _record_error(project_root, tool_name, host, exc)


def _run(
    *,
    project_root,
    host,
    tool_name,
    tool_input,
    host_session_id,
    live_verdict,
    live_why,
    managed_session_id,
    lane,
) -> None:
    live_law = _LIVE_TO_LAW.get(live_verdict, _sp.LAW_DENY)
    live_blocked_by = _blocked_by_from_why(live_why)
    native_enabled = _native_enabled(project_root)

    envelope = normalize(
        tool_name=tool_name,
        tool_input=tool_input,
        project_root=project_root,
        host=host,
        host_session_id=host_session_id,
        managed_session_id=managed_session_id,
        lane=lane,
    )
    policy = _sp.ShellPolicy(
        law_delegate=_live_delegate(live_law, live_blocked_by),
    )
    verdict = policy.evaluate(envelope, native_enabled=native_enabled)

    law_diverged = verdict.law_decision != live_law
    transport_diverged = verdict.decision in (
        _sp.DECISION_FALLBACK,
        _sp.DECISION_CAPABILITY_UNSUPPORTED,
    )
    status = "diverged" if law_diverged else "observed"

    findings = [
        {"rule_id": f.rule_id, "risk": f.risk, "category": f.category} for f in verdict.findings
    ]
    cmd = envelope.command
    payload = {
        "host": host,
        "tool_name": tool_name,
        "provider": envelope.provider,
        "transport": envelope.transport,
        "command_hash": _hash(cmd),
        "command_bytes": len(cmd.encode("utf-8")),
        "cwd_hash": _hash(envelope.cwd) if envelope.cwd else "",
        "host_session_id": host_session_id,
        "managed_session_id": managed_session_id,
        "lane": lane,
        "live_decision": live_verdict,
        "live_law_decision": live_law,
        "live_blocked_by": live_blocked_by,
        "shadow_decision": verdict.decision,
        "shadow_law_decision": verdict.law_decision,
        "shadow_blocked_by": verdict.blocked_by,
        "shadow_output_guard_mode": verdict.output_guard_mode,
        "shadow_findings": findings,
        "law_diverged": law_diverged,
        "transport_diverged": transport_diverged,
        "native_enabled": native_enabled,
        "capability_safe": _matrix.is_native_safe(host, envelope.provider),
    }
    _record(project_root, tool_name, status, payload)


def _record(project_root, tool_name, status, payload) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="shell_policy_shadow",
            source_kind="claude_hook.pretool_shadow",
            capability_name=tool_name,
            action_kind="shadow",
            target_entity="shell_policy",
            status=status,
            payload=payload,
        )
    except Exception:
        pass


def _record_unguardable(
    project_root,
    tool_name,
    host,
    provider,
    transport,
) -> None:
    """Record that a native shell path is unguardable and must be
    blocked + routed to ai_run — WITHOUT evaluating the command (no
    dialect/read scan, no ShellPolicy run). Evidence only.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="shell_policy_shadow",
            source_kind="claude_hook.pretool_shadow",
            capability_name=tool_name,
            action_kind="shadow",
            target_entity="shell_policy",
            status="skipped_unguardable",
            payload={
                "host": host,
                "tool_name": tool_name,
                "provider": provider,
                "transport": transport,
                "capability_safe": False,
                "would_block": True,
                "fallback": "ai_run",
                "reason": (
                    "native shell path is unguardable (no proven "
                    "command-visibility / PreToolUse hard-deny) — must be "
                    "blocked and routed to ai_run; not shadowed"
                ),
            },
        )
    except Exception:
        pass


def _record_error(project_root, tool_name, host, exc) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="shell_policy_shadow_error",
            source_kind="claude_hook.pretool_shadow",
            capability_name=tool_name,
            action_kind="shadow",
            target_entity="shell_policy",
            status="shadow_error",
            payload={
                "host": host,
                "tool_name": tool_name,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )
    except Exception:
        pass
