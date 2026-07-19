"""Host-native shell adapter — the LOCAL half of the two-transport shell
architecture (behind tools.shell_enforcement_live).

TWO TRANSPORTS:
  * LOCAL agent hosts with a proven native seam (today: Claude Code) use their
    built-in Bash tool as the PRIMARY governed developer shell — this adapter.
  * REMOTE / web agents (GPT via mcp.codenexus.cloud) have no host-native Bash
    surface and use MCP ai_run as their server-side shell. ai_run is also the
    canonical law reference + the capability fallback for genuinely unsupported
    native shapes.

Single authority for native shell tools:
  1. structural gates run first (managed-mode, reconnect, active freeze) via
     ToolGate.structural_block_native_shell — terminal block is honored.
  2. ShellEnforcement owns the verdict (single freeze minter; core law is
     decision-only / non-minting): DENY → deny, FREEZE → freeze (once),
     EXECUTE_NATIVE → eligible for a host ALLOW, otherwise → ai_run.
  3. On EXECUTE_NATIVE there is NO read-only/pilot authorisation cage: AIDOCS
     renders a host ALLOW for EVERY law-permitted local command (reads,
     project-local writes, git, builds, tests), gated ONLY by genuine
     CAPABILITY — output-replaceable receipt, fresh provider posture, a proven
     host↔provider identity, and no host-unsafe shape (command substitution /
     unbounded follow). shell_readonly / shell_family are
     telemetry / capability EVIDENCE only, never a second authorisation gate.

HOST-ADAPTER IDENTITY CONTRACT (governed_shell_attest.claude_code_native_self_
bind_ok): when the operator enabled Governed Bash and pinned a system-authority
provider, the live posture freshly re-attests that EXACT provider before each
ALLOW, and the PROVEN Claude Code adapter may self-bind to it when path-silent.
Unknown hosts, OpenCode (until separately proven), and remote MCP/web execution
NEVER self-bind → ai_run.

Renders a Claude-Code-style PreToolUse hookSpecificOutput envelope. Fails
CLOSED: any internal error for a flag-on native-shell call returns a deny,
never a fall-through that could permit the host to run the process.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import shell_enforcement as _enf
from .shell_envelope import (
    TRANSPORT_HOST_NATIVE,
    detect_provider_and_transport,
    normalize,
)

# Monitor decision (2.0-A): TRANSPORT_MONITOR is a READ/STATUS surface — it
# tails an already-gated background run (started via ai_run), it does not
# spawn a process. So it is EXCLUDED from 2.0-A native-execution
# enforcement; revisit in Batch 2.0-C if Monitor ever gains a spawn path.
# 2.0-A intercepts only TRANSPORT_HOST_NATIVE.

_AI_RUN_GUIDANCE = " Use ai_run(command=...) — the AIDOCS-managed shell — instead."


def _fallback_same_command_for(env: Any) -> bool:
    """True when the SAME command is bash-executable via ai_run (bash
    provider). PowerShell/cmd forms are not.
    """
    try:
        return _enf._fallback_same_command(env.provider)
    except Exception:
        return False


def _native_continuation(command: str, *, same_command: bool = True) -> str:
    """The EXACT, usable ai_run continuation for a denied native call.

    Governed Bash is never a dead end: every denied native path tells the
    operator how to run the SAME work the governed way. ai_run is the
    AIDOCS-managed shell — it applies the full law (future-sight x-ray,
    receipt, output-guard, audit). No raw unreceipted Bash is unlocked.
    """
    cmd = (command or "").strip()
    if same_command and cmd:
        return f'ai_run(command="{cmd[:300]}")'
    # PowerShell/cmd forms are not bash-executable via ai_run.
    return "ai_run(command=<bash equivalent>)"


def _continuation_text(command: str, *, same_command: bool = True) -> str:
    cont = _native_continuation(command, same_command=same_command)
    if same_command and (command or "").strip():
        return (
            f" ↪ Governed continuation — run the SAME command via the "
            f"AIDOCS-managed shell: {cont}. ai_run applies the full law "
            f"(x-ray, receipt, output-guard, audit); no raw unreceipted "
            f"Bash is unlocked."
        )
    return (
        " ↪ Governed continuation: re-issue this as a bash command via "
        "ai_run(command=...). AIDOCS governs every run; no raw "
        "unreceipted Bash is unlocked."
    )


def _flag_on(project_root: Path) -> bool:
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tools.shell_enforcement_live",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def is_native_shell_enforced(project_root: Path, tool_name: str) -> bool:
    """True when this is a host-native shell tool AND shell-enforcement is
    live — i.e. a call 2.0-A must own and must NEVER let fall through.
    Monitor is excluded (read/status surface).
    """
    try:
        _p, transport = detect_provider_and_transport(tool_name)
        if transport != TRANSPORT_HOST_NATIVE:
            return False
        return _flag_on(project_root)
    except Exception:
        # Can't prove it's safe to fall through → treat as enforced so the
        # caller fails closed.
        return True


def fail_closed_deny_envelope(detail: str = "") -> dict[str, Any]:
    """The canonical 2.0-A fail-closed deny envelope. Exported so the host
    hook can render it without falling through to the normal pipeline.
    """
    reason = "native shell enforcement failed; action remains blocked." + _AI_RUN_GUIDANCE
    if detail:
        reason += f" ({detail})"
    return _deny_env(reason, "shell_enforcement_error")


def _managed_session_id(runtime: Any, project_root: Path, host_session_id: str) -> str:
    try:
        managed = runtime.hub.managed_mode.get_mode(
            project_root,
            host_session_id=host_session_id,
        )
        if isinstance(managed, dict) and managed.get("active"):
            return str(managed.get("session_id") or "").strip()
    except Exception:
        pass
    return ""


def _deny_env(
    reason: str,
    blocked_by: str,
    *,
    continuation: str = "",
    family: str = "",
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "blocked_by": blocked_by,
    }
    # Structured, machine-usable continuation for hosts/dashboards — the
    # exact ai_run call that runs this work the governed way.
    if continuation:
        out["ai_run_continuation"] = continuation
    if family:
        out["command_family"] = family
    # Route-clarity fields (policy_allowed / capability_eligible /
    # selected_route / final_execution_surface) and any other structured
    # extras — surfaced so the dashboard/CLI show exactly what happened.
    for k, v in extra.items():
        if v is not None:
            out[k] = v
    return {"hookSpecificOutput": out}


def _allow_env(reason: str) -> dict[str, Any]:
    # Affirmative host ALLOW — CC runs the native tool. Only used for the
    # 2.0-B0 pilot-safe command class after every gate passed.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        },
    }


def _freeze_env(reason: str, blocked_by: str, freeze_state: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "blocked_by": blocked_by or "judge_confirm_required",
    }
    if freeze_state:
        out["freeze_state"] = freeze_state
    return {"hookSpecificOutput": out}


def _render_structural(result: Any) -> dict[str, Any]:
    # managed-mode / reconnect / freeze terminal block → CC deny envelope.
    why = result.why or ()
    blocked_by = ""
    if "session_frozen" in why or any("freeze" in w for w in why):
        blocked_by = "session_frozen"
    return _deny_env(result.reason or "blocked", blocked_by)


def _render_enforcement(res: Any, command: str = "") -> dict[str, Any]:
    action = res.action
    if action == _enf.ACTION_FREEZE:
        # Confirmable family (package/build/test/network/dangerous form) →
        # canonical APPROVAL card (built by freeze_service).
        return _freeze_env(res.reason, res.blocked_by, res.freeze_state)
    if action == _enf.ACTION_FREEZE_FAILED:
        return _deny_env(res.reason, res.blocked_by or "freeze_mint_failed")
    if action == _enf.ACTION_DENY:
        # Hard deny still offers the governed continuation (the command may
        # be allowable via ai_run's own gates) — never a dead end.
        same = getattr(res, "fallback_same_command", True)
        return _deny_env(
            res.reason + _continuation_text(command, same_command=same),
            res.blocked_by or "denied",
            continuation=_native_continuation(command, same_command=same),
        )
    if action == _enf.ACTION_EXECUTE_NATIVE:
        return _deny_env(
            "native shell execution is disabled (Batch 2.0-A renders "
            "verdicts only; no native process runs)." + _continuation_text(command),
            "native_execution_disabled_2a",
            continuation=_native_continuation(command),
        )
    # ACTION_FALLBACK (or any residual allow): native blocked, route ai_run.
    reason = res.reason or "native shell blocked"
    same = getattr(res, "fallback_same_command", False)
    return _deny_env(
        reason + _continuation_text(command, same_command=same),
        res.blocked_by or "fallback_to_ai_run",
        continuation=_native_continuation(command, same_command=same),
    )


def _record(
    project_root,
    host,
    tool_name,
    env,
    res,
    status,
    *,
    native_execution_allowed: bool = False,
    intended_native_allowed: bool = False,
    tool_use_id: str = "",
    family: str = "",
    family_disposition: str = "",
    **route_extra: Any,
) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        _payload_extra = {k: v for k, v in route_extra.items() if v is not None}
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="shell_policy_enforced",
            source_kind="shell_adapter.native_2a",
            session_id=env.host_session_id or None,
            capability_name=tool_name,
            action_kind="enforce",
            target_entity="shell_policy",
            status=status,
            payload={
                "host": host,
                "tool_name": tool_name,
                "provider": env.provider,
                "transport": env.transport,
                # Per-call correlation key (binds this allow to its own
                # PostToolUse completion — no stale/cross-run correlation).
                "tool_use_id": tool_use_id,
                "command_hash": hashlib.sha256(env.command.encode("utf-8")).hexdigest(),
                "command_bytes": len(env.command.encode("utf-8")),
                "action": getattr(res, "action", ""),
                "law_decision": getattr(getattr(res, "verdict", None), "law_decision", ""),
                "native_route": getattr(res, "native_route", ""),
                "blocked_by": getattr(res, "blocked_by", ""),
                # Truthful native-execution accounting (2.0-B0):
                #   intended_native_allowed — verdict was execute_native
                #   native_execution_allowed — AIDOCS rendered a host ALLOW
                #       (the host MAY run it). NOT a completion — completion
                #       is a separate PostToolUse receipt (native_completed).
                "intended_native_allowed": intended_native_allowed,
                "native_execution_allowed": native_execution_allowed,
                # Command-family policy verdict (read/write/build/test/
                # package/network/unknown) + its native disposition.
                "command_family": family,
                "family_disposition": family_disposition,
                # Route-clarity facts (when provided by the caller).
                **_payload_extra,
            },
        )
    except Exception:
        pass


import re as _re

_NATIVE_SUBST_RE = _re.compile(r"\$\(|`|<\(|>\(")  # command/process substitution


def _native_capability_safe(command: str) -> tuple[bool, str]:
    """NARROW capability gate for the host-native surface (NOT authorization —
    the canonical law already decided allow/deny/freeze). The host runs the RAW
    command string with full shell interpretation, so two shapes are not
    host-native-safe and must route to ai_run (where AIDOCS owns the spawn and
    x-rays the full string):

      * command/process substitution ($(...), `...`, <(...), >(...)) — hides
        execution that bash_policy (per-segment) and the judge do NOT expand,
        so `cat $(rm x)` would otherwise run rm on the host. AIDOCS cannot
        x-ray inside a substitution post-hoc.
      * an unbounded follow/stream (tail -f / -F / --follow) — never returns,
        so no correlated PostToolUse completion receipt can ever close it.

    Simple redirection ('>') stays native (it is a bounded write the receipt
    captures). Returns (safe, reason)."""
    c = command or ""
    if _NATIVE_SUBST_RE.search(c):
        return (
            False,
            "command/process substitution is not host-native-safe (hidden execution)",
        )
    if _re.search(r"\btail\b", c) and _re.search(r"(^|\s)(-[A-Za-z]*[fF]\b|--follow\b)", c):
        return (False, "follow/stream has no bounded completion receipt")
    return (True, "")


def native_shell_2a_pretool(
    *,
    runtime: Any,
    hub: Any,
    project_root: Path,
    host: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    host_session_id: str,
    tool_use_id: str = "",
) -> dict[str, Any] | None:
    """Return a CC PreToolUse envelope for a flag-on native-shell call, or
    None when this is not a flag-on native-shell call (caller proceeds with
    the normal pipeline). Never lets a native process run; fails closed.
    """
    _provider, transport = detect_provider_and_transport(tool_name)
    # Monitor (read/status) is excluded from 2.0-A — only host-native.
    if transport != TRANSPORT_HOST_NATIVE:
        return None
    if not _flag_on(project_root):
        return None
    try:
        from .tool_gate_service import ToolGate

        gate = ToolGate(runtime)
        structural = gate.structural_block_native_shell(
            tool_name=tool_name,
            host_session_id=host_session_id,
            project_root=project_root,
        )
        if structural.is_terminal:
            # Structural deny/ask (managed/reconnect/active freeze) stays
            # ABOVE ShellPolicy — honor it.
            return _render_structural(structural)

        env = normalize(
            tool_name=tool_name,
            tool_input=tool_input or {},
            project_root=project_root,
            host=host,
            host_session_id=host_session_id,
            managed_session_id=_managed_session_id(runtime, project_root, host_session_id),
        )
        res = _enf.resolve_shell_enforcement(env, hub=hub, runtime=runtime)

        # NORMAL GOVERNED SHELL (2026-06-05, operator-authorised): the
        # host-native Bash tool is the ordinary shell surface, not a
        # read-only annex. An execute_native verdict already means the
        # CANONICAL ai_run LAW authorised this exact command — ShellPolicy +
        # bash_policy + judge + anti-coup + lifecycle/future-sight ran in the
        # core-law delegate — AND native_route proved native CAPABILITY
        # (native-safe hard-deny seam + output replacement + provider
        # identity). So AIDOCS renders a host ALLOW for EVERY law-permitted
        # command, not just a read-only/pilot subset. There is NO second
        # authorisation cage: shell_family stays AUDIT/TELEMETRY +
        # capability-routing evidence only. Confirmable-destructive actions
        # never reach here (they return ACTION_FREEZE); forbidden actions
        # never reach here (ACTION_DENY). ai_run remains the canonical law
        # and a fallback EXECUTOR for concrete native-capability limits
        # (e.g. output not host-replaceable), never a surface the agent must
        # manually reissue routine permitted work through.
        if res.action == _enf.ACTION_EXECUTE_NATIVE:
            from .governed_shell_attest import (
                claude_code_native_self_bind_ok,
                identity_binding_ok,
                live_execution_posture,
            )
            from .shell_family import (
                FAMILY_READ,
                classify_family,
                native_family_disposition,
            )
            from .shell_policy import OUTPUT_HOST_REPLACE
            from .shell_readonly import _extra_commands, is_read_only_safe

            # NO AUTHORISATION CAGE. The canonical ai_run LAW already authorised
            # this exact command (ShellPolicy + bash_policy + judge + anti-coup
            # + lifecycle/future-sight). AIDOCS renders a host ALLOW for EVERY
            # law-permitted local command — reads, project-local writes, git,
            # builds, tests — gated ONLY by genuine capability: host-replaceable
            # receipt + fresh attestation + a proven host<->provider identity
            # contract. shell_family + the read-only catalog survive ONLY as
            # TELEMETRY / capability EVIDENCE (recorded in the audit), never a
            # second authorisation monarchy after the law already allowed.
            family = classify_family(env.command)
            read_validated = (
                family == FAMILY_READ
                and is_read_only_safe(env.command, _extra_commands(project_root))[0]
            )  # telemetry/evidence only — does NOT gate the decision
            fam = native_family_disposition(
                project_root,
                env.command,
                read_validated=read_validated,
            )

            # Receipted-output invariant: native output MUST be host-
            # replaceable so the PostToolUse completion receipt + output guard
            # are provable. If the host capability cannot guarantee it, THAT
            # is a real native-capability limit → governed ai_run fallback.
            receipted = (
                getattr(getattr(res, "verdict", None), "output_guard_mode", "")
                == OUTPUT_HOST_REPLACE
            )
            # FINAL canonical gate (Empire re-seal 2026-05-30): one freshly
            # re-derived live governed-shell execution posture re-proves
            # service-managed enablement + exact enrolled provider path +
            # non-user-writable FS authority (provider + parent chain) +
            # valid provenance + CURRENT SHA-256 equality (swapped bytes deny
            # HERE, before any host execution) + host capability.
            live = live_execution_posture(project_root)
            # HOST-ADAPTER IDENTITY CONTRACT (2026-06-06): strict path-equality
            # when the host EXPOSES its executable; the path-silent self-bind to
            # the operator-pinned + freshly re-attested provider is granted ONLY
            # to the PROVEN Claude Code host adapter (host==claude_code,
            # provider==bash, posture.ok). Unknown hosts, OpenCode (until
            # separately proven), and remote MCP/web execution NEVER self-bind —
            # a path-silent call there fails identity and routes to ai_run.
            self_bind_ok = claude_code_native_self_bind_ok(env, live)
            bound, bind_reason = identity_binding_ok(
                env.provider_executable_path,
                live,
                enrolled_self_bind_ok=self_bind_ok,
            )

            native_capable, _cap_reason = _native_capability_safe(env.command)
            # #319: local-solo machine-presence is an accepted native identity
            # in place of the provider-attestation chain (live posture + bound).
            from .governed_shell_attest import machine_presence_native_ok

            # machine-presence is the identity ONLY when provider attestation is
            # NOT enrolled. An ENROLLED provider whose live posture FAILED (e.g.
            # SHA / provenance drift = a tampering signal) is authoritative and
            # must NEVER be rescued by machine-presence — drift stays a hard
            # refusal regardless of local-solo standing.
            _provider_enrolled = _enf._resolve_native_enabled(project_root, None)
            _mp_raw, _mp_reason = machine_presence_native_ok(
                env.host, env.provider, project_root,
            )
            _mp_ok = (not _provider_enrolled) and _mp_raw
            _identity_ok = bool(live["ok"] and bound) or _mp_ok
            if receipted and _identity_ok and native_capable:
                _record(
                    project_root,
                    host,
                    tool_name,
                    env,
                    res,
                    "native_allow",
                    native_execution_allowed=True,
                    intended_native_allowed=True,
                    tool_use_id=tool_use_id,
                    family=fam.family,
                    family_disposition=fam.disposition,
                )
                return _allow_env(
                    f"AIDOCS native execution authorized (governed shell, "
                    f"{fam.family} family)",
                )

            # Genuine native-capability / attestation limits → governed ai_run
            # fallback. This is NOT a usability cage: the law already said
            # allow; only a concrete host-capability gap defers it.
            if not live["ok"] and not _mp_ok:
                _same = _fallback_same_command_for(env)
                _record(
                    project_root,
                    host,
                    tool_name,
                    env,
                    res,
                    "live_posture_denied",
                    native_execution_allowed=False,
                    intended_native_allowed=True,
                    tool_use_id=tool_use_id,
                    family=fam.family,
                    family_disposition=fam.disposition,
                )
                return _deny_env(
                    f"native execution refused by live governed-shell posture: "
                    f"{live['reason']}. Repair: {live['repair']}. Routing to ai_run."
                    + _continuation_text(env.command, same_command=_same),
                    "live_posture_refused",
                    continuation=_native_continuation(env.command, same_command=_same),
                    family=fam.family,
                )
            if not bound and not _mp_ok:
                _same = _fallback_same_command_for(env)
                _record(
                    project_root,
                    host,
                    tool_name,
                    env,
                    res,
                    "identity_unbound",
                    native_execution_allowed=False,
                    intended_native_allowed=True,
                    tool_use_id=tool_use_id,
                    family=fam.family,
                    family_disposition=fam.disposition,
                )
                return _deny_env(
                    f"native execution refused — host-native identity not bound: "
                    f"{bind_reason}. Routing to ai_run where AIDOCS owns the spawn."
                    + _continuation_text(env.command, same_command=_same),
                    "native_identity_unbound",
                    continuation=_native_continuation(env.command, same_command=_same),
                    family=fam.family,
                )
            if not native_capable:
                # Law-permitted, but the command shape is not host-native-safe
                # (command substitution / unbounded follow) → governed ai_run,
                # where AIDOCS owns the spawn and x-rays the full string.
                _same = _fallback_same_command_for(env)
                _record(
                    project_root,
                    host,
                    tool_name,
                    env,
                    res,
                    "native_capability_unsafe",
                    native_execution_allowed=False,
                    intended_native_allowed=True,
                    tool_use_id=tool_use_id,
                    family=fam.family,
                    family_disposition=fam.disposition,
                )
                return _deny_env(
                    f"native execution refused — {_cap_reason}. Routing to ai_run "
                    f"where AIDOCS owns the spawn and x-rays the full command."
                    + _continuation_text(env.command, same_command=_same),
                    "native_capability_unsafe",
                    continuation=_native_continuation(env.command, same_command=_same),
                    family=fam.family,
                )
            # receipted is False → host output is not replaceable → a real
            # native-capability limit → governed ai_run fallback.
            from .shell_route import classify_route

            route = classify_route(
                policy_allowed=True,
                capability_eligible=False,
                native_enabled=True,
            )
            _same = _fallback_same_command_for(env)
            _record(
                project_root,
                host,
                tool_name,
                env,
                res,
                "execute_native_denied",
                native_execution_allowed=False,
                intended_native_allowed=True,
                tool_use_id=tool_use_id,
                family=fam.family,
                family_disposition=fam.disposition,
                policy_allowed=route["policy_allowed"],
                capability_eligible=route["capability_eligible"],
                selected_route=route["selected_route"],
                final_execution_surface=route["final_execution_surface"],
            )
            return _deny_env(
                f"native execution deferred — host output is not "
                f"receipt-guaranteed for the '{fam.family}' family "
                f"(output-guard not host-replaceable). {route['message']}"
                + _continuation_text(env.command, same_command=_same),
                "native_output_not_replaceable",
                continuation=_native_continuation(env.command, same_command=_same),
                family=fam.family,
                policy_allowed=route["policy_allowed"],
                capability_eligible=route["capability_eligible"],
                selected_route=route["selected_route"],
                final_execution_surface=route["final_execution_surface"],
            )

        status = "freeze" if res.action == _enf.ACTION_FREEZE else "blocked"
        _record(project_root, host, tool_name, env, res, status, tool_use_id=tool_use_id)
        return _render_enforcement(res, env.command)
    except Exception as exc:
        # Fail closed: a flag-on native-shell call must NEVER fall through
        # to a path that could let the host run the process.
        return fail_closed_deny_envelope(type(exc).__name__)
