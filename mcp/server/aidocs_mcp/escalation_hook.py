"""Escalation hook — Layer 9 C-2 UserPromptSubmit integration.

Agent-initiated escalation is NOT supported: agents cannot ask the
user for gate access (they would spam it and block workflow). Only
the user opens gates, by typing the grant phrase themselves.

Two support paths remain:

1. Dashboard approval of ANY pending escalation row the user
   created (or admin tooling created). The dashboard calls
   rbac_approve_escalation → row flips to approved → on the next
   UserPromptSubmit, consume_approvals_for_session drains approved
   rows and issues the corresponding grants.

2. `deny:` chat command (non-secret, permissive). Admins type
   `deny: esc_xxx <reason>` to reject a pending request from chat.
   No authentication required because denial is the *refusal* of a
   grant, not the granting itself. Audit row carries
   approver_label='chat-operator'.

Passwords are NEVER accepted via chat — the host logs the typed
text to its transcript before hooks fire, so scrubbing in the agent
context doesn't remove the secret from scrollback. Dashboard modal
or SSO (C-3) are the only password surfaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .escalation_store import (
    EscalationRequest,
    EscalationStore,
)
from .identity_store import IdentityStore
from .rbac_store import RBACStore

# `deny: <request_id> optional reason` — lets operators reject from
# chat. Denial is the permissive action (refusing to grant), so no
# password authentication is required; dashboard-backed approvals
# are the only authenticated chat path.
#
# #447: `cancel` and `no` are accepted as synonyms of `deny`. Every
# verb resolves the row to STATUS_DENIED — an operator refusal.
# STATUS_CANCELLED stays reserved for system rollback (see the
# escalation_store docstring); the verb the operator typed is recorded
# in the audit payload, not in the status.
_DENY_PATTERN = re.compile(
    r"^\s*(?P<verb>deny|cancel|no)\s*[:=]\s*(?P<request_id>esc_[a-f0-9]+)\s*(?P<reason>.*)$",
    re.MULTILINE,
)

# #447 bare form: verb at prompt start, then EITHER end-of-string (bare
# "no" / "cancel" / "deny" → reason '') OR an EXPLICIT separator
# (: = , - – —) followed by the free-text REASON ("cancel — wrong fix,
# wanted speed"). Verb + bare whitespace + text ("no idea what this
# means", "cancel the meeting notes update") deliberately does NOT
# match — that is ordinary prose, not a refusal; doctrine §X: ambiguity
# → DROP, never infer, the escalation stays pending. Honored ONLY when
# exactly ONE escalation is pending for the session — with two or more
# pending, a bare verb is ambiguous and is refused (side effect
# `escalation.ambiguous_pending`; nothing resolved). The reason is inert
# text: it is persisted into the EXISTING rbac_escalations.reason column
# and the audit payload, and can never act as a confirm/approve token —
# this path only ever calls decide(approve=False).
_BARE_DENY_PATTERN = re.compile(
    r"^\s*(?P<verb>deny|cancel|no)\b"
    r"(?:\s*\Z|[ \t]*[:=,\-–—]\s*(?P<reason>.*)\Z)",
    re.DOTALL,
)


def _audit_chat_denial(
    project_root: Path,
    *,
    request_id: str,
    session_id: str | None,
    verb: str,
    reason: str,
    gate_permission: str | None,
    bare: bool,
) -> None:
    """Best-effort audit event for a chat-side escalation denial (#447).
    Carries the operator's free-text reason in the payload."""
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="escalation_denied",
            source_kind="escalation_hook",
            session_id=session_id,
            capability_name="scrub_and_process",
            action_kind="escalation",
            target_entity=gate_permission or request_id,
            status="denied",
            payload={
                "request_id": request_id,
                "verb": verb,
                "reason": reason,
                "bare_form": bare,
                "approver_label": "chat-operator",
            },
            permission_name=gate_permission,
        )
    except Exception:
        pass


@dataclass
class ScrubResult:
    """Result of chat-scan + credential scrub.

    rewritten_prompt: the original prompt with any credential lines
        replaced by `approve: <redacted>`. Safe to feed to the agent.
    side_effects: dicts describing what happened (grants issued,
        denials, auth failures). Caller logs these to audit and
        optionally surfaces them to the user in additionalContext.
    credentials_scrubbed: True iff the prompt contained any matched
        credential pattern (success OR failure). Caller should never
        log the raw prompt when this is True.
    """

    rewritten_prompt: str
    side_effects: list[dict[str, Any]]
    credentials_scrubbed: bool


def scrub_and_process(
    project_root: Path,
    prompt: str,
    *,
    session_id: str | None = None,
    identity: IdentityStore | None = None,
    rbac: RBACStore | None = None,
    escalations: EscalationStore | None = None,
) -> ScrubResult:
    """Scan a UserPromptSubmit for `deny:`/`cancel:`/`no:` lines and the
    #447 bare refusal form.

    `approve:` via chat is intentionally unsupported — the host
    records typed text to its transcript BEFORE hooks fire, so
    plaintext passwords persist in scrollback even if we mutate
    the agent's view. Authenticated approvals happen via the
    dashboard (session-token-backed rbac_approve_escalation MCP
    tool) or via AskUserQuestion-collected email with dashboard
    confirmation. `deny:` is safe because it's the permissive
    action (rejecting a request, not granting one).

    #447: `cancel` / `no` are deny synonyms. Two forms:
      * explicit — `<verb>: esc_xxx <reason>` targets a request by id;
      * bare — the prompt is the verb alone ("no" → reason ''), or the
        verb + an EXPLICIT separator (: = , - – —) + the free-text
        REASON ("cancel — wrong fix, wanted speed"). Verb + bare
        whitespace + prose ("no idea what this means") does NOT match —
        ambiguity drops, the escalation stays pending. Honored only
        when exactly ONE escalation is pending for the session;
        multi-pending is ambiguous and refuses to resolve.
    Either way the row resolves to STATUS_DENIED (operator refusal —
    `cancelled` stays a system-rollback status), the reason lands in
    the existing rbac_escalations.reason column, and an
    `escalation_denied` audit event carries it. The reason is inert
    text: this path only ever calls decide(approve=False), so no
    reason string can act as a confirm token.
    """
    # identity/rbac kept in signature for API stability; unused now.
    del identity, rbac

    if not prompt or not isinstance(prompt, str):
        return ScrubResult(prompt or "", [], False)

    escalations = escalations or EscalationStore()
    side_effects: list[dict[str, Any]] = []
    scrubbed = False
    rewritten = prompt

    def _deny(request_id: str, verb: str, reason: str, *, bare: bool) -> bool:
        # deny-from-chat doesn't require credentials (the user is
        # rejecting, not unlocking); but we still need SOME identity
        # to attribute the decision. Future: require a session-bound
        # user_id. For now, attribute to "chat-operator".
        decided = escalations.decide(
            project_root,
            request_id,
            approve=False,
            approver_user_id=None,
            approver_label="chat-operator",
            reason=reason,
        )
        side_effects.append(
            {
                "kind": "escalation.denied" if decided else "escalation.unknown_request",
                "request_id": request_id,
                "verb": verb,
                "reason": reason,
            },
        )
        if decided:
            _audit_chat_denial(
                project_root,
                request_id=request_id,
                session_id=session_id,
                verb=verb,
                reason=reason,
                gate_permission=decided.gate_permission,
                bare=bare,
            )
        return decided is not None

    explicit_matches = list(_DENY_PATTERN.finditer(prompt))
    for match in explicit_matches:
        scrubbed = True
        request_id = match.group("request_id")
        reason = (match.group("reason") or "").strip()
        _deny(
            request_id,
            match.group("verb"),
            reason or "denied via chat",
            bare=False,
        )
        rewritten = rewritten.replace(match.group(0), "")

    # #447 bare form — only when no explicit `<verb>: esc_xxx` matched
    # (the explicit pattern is a subset of the bare shape) and the
    # session is known. Zero pending → inert prompt, untouched. One
    # pending → unambiguous target, resolve it. Two-plus pending →
    # refuse (ambiguous); the operator must use the explicit id form.
    if not explicit_matches and session_id:
        bare = _BARE_DENY_PATTERN.match(prompt)
        if bare is not None:
            try:
                pending = escalations.list_pending(project_root, session_id)
            except Exception:
                pending = []
            if len(pending) == 1:
                reason = (bare.group("reason") or "").strip()
                # Prompt stays intact: a bare refusal carries no
                # credential, and its trailing text may hold follow-up
                # instructions the agent still needs to see.
                _deny(
                    pending[0].request_id,
                    bare.group("verb"),
                    reason,
                    bare=True,
                )
            elif len(pending) > 1:
                side_effects.append(
                    {
                        "kind": "escalation.ambiguous_pending",
                        "pending_count": len(pending),
                        "detail": (
                            "bare refusal matched but multiple escalations are "
                            "pending — use the explicit `deny: esc_<id> <reason>` "
                            "form to target one"
                        ),
                    },
                )

    return ScrubResult(
        rewritten_prompt=rewritten,
        side_effects=side_effects,
        credentials_scrubbed=scrubbed,
    )


def request_escalation(
    project_root: Path,
    *,
    gate_permission: str,
    gate_phrase: str,
    requester_label: str,
    session_id: str | None = None,
    task_id: str | None = None,
    plan_path: str | None = None,
    sticky: bool = False,
    requester_user_id: str | None = None,
    extra: dict[str, Any] | None = None,
    escalations: EscalationStore | None = None,
    machine_id: str | None = None,
    command_snippet: str | None = None,
) -> EscalationRequest:
    """Create a pending approval row. Called from the gate-deny
    path when the user's current identity can't unlock the gate
    themselves but an admin could approve.

    machine_id defaults to the current host's stable machine_id()
    (see host_concurrency_store). This binds approvals to the
    originating machine so a grant issued for machine A can't be
    replayed on machine B via a shared dashboard session.
    command_snippet is stored (truncated) so the admin sees what
    triggered the bubble without having to correlate by timestamp.
    """
    escalations = escalations or EscalationStore()
    if machine_id is None:
        try:
            from .host_concurrency_store import machine_id as _machine_id

            machine_id = _machine_id()
        except Exception:
            machine_id = ""
    req = escalations.create_request(
        project_root,
        requester_label=requester_label,
        gate_permission=gate_permission,
        gate_phrase=gate_phrase,
        requester_user_id=requester_user_id,
        session_id=session_id,
        task_id=task_id,
        plan_path=plan_path,
        sticky=sticky,
        extra=extra,
        machine_id=str(machine_id or ""),
        command_snippet=command_snippet,
    )
    # Audit gap fill (2026-04-21): escalation lifecycle.
    # Compliance-adjacent — who asked for what privilege, when, under
    # what session+task. Paired with consume_approvals_for_session
    # below to log approval-side outcomes. Best-effort.
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="escalation_requested",
            source_kind="escalation_hook",
            session_id=session_id,
            capability_name="request_escalation",
            action_kind="escalation",
            target_entity=gate_permission,
            status="pending",
            payload={
                "request_id": req.request_id,
                "gate_permission": gate_permission,
                "gate_phrase": gate_phrase,
                "requester_label": requester_label,
                "requester_user_id": requester_user_id,
                "sticky": bool(sticky),
                "task_id": task_id,
                "machine_id": str(machine_id or ""),
                "command_snippet": command_snippet,
            },
            permission_name=gate_permission,
        )
    except Exception:
        pass
    return req


def check_live_grant_or_bubble(
    project_root: Path,
    *,
    gate_permission: str,
    session_id: str,
    requester_user_id: str,
    machine_id: str | None = None,
    command_hash: str | None = None,
    escalations: EscalationStore | None = None,
) -> dict[str, Any]:
    """Gate-side entry: does this (user, machine, session, permission)
    already have a live admin-approved grant? If yes, consume one use
    and return {'ok': True, 'grant_id': ...}. If no, return
    {'ok': False} so the caller proceeds to bubble via
    request_escalation.

    Keeping the bubble OUT of this helper means callers can assemble
    the command_snippet + sticky + extras they want on the request
    row, and callers that don't need bubble semantics (dry runs,
    probes) can use this as a pure check.
    """
    escalations = escalations or EscalationStore()
    if machine_id is None:
        try:
            from .host_concurrency_store import machine_id as _machine_id

            machine_id = _machine_id()
        except Exception:
            machine_id = ""
    grant = escalations.find_live_grant(
        project_root,
        user_id=requester_user_id,
        machine_id=str(machine_id or ""),
        session_id=session_id,
        permission_name=gate_permission,
        command_hash=command_hash,
    )
    if grant is None:
        return {"ok": False, "reason": "no_live_grant"}
    consumed = escalations.consume_grant(project_root, grant.grant_id)
    if consumed is None:
        # Race: another caller consumed it between find and consume.
        # Treat as no-grant so the caller re-bubbles.
        return {"ok": False, "reason": "grant_race_lost"}
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="escalation_grant_consumed",
            source_kind="escalation_hook",
            session_id=session_id,
            capability_name="check_live_grant_or_bubble",
            action_kind="escalation",
            target_entity=gate_permission,
            status="consumed",
            payload={
                "grant_id": consumed.grant_id,
                "request_id": consumed.request_id,
                "user_id": consumed.user_id,
                "machine_id": consumed.machine_id,
                "permission_name": gate_permission,
                "command_hash": command_hash,
                "uses_consumed": consumed.uses_consumed,
                "max_uses": consumed.max_uses,
            },
            user_id=requester_user_id,
            permission_name=gate_permission,
        )
    except Exception:
        pass
    return {
        "ok": True,
        "grant_id": consumed.grant_id,
        "request_id": consumed.request_id,
        "uses_remaining": consumed.remaining_uses,
    }


def peek_approved_for_session(
    project_root: Path,
    session_id: str,
    *,
    escalations: EscalationStore | None = None,
) -> list[EscalationRequest]:
    """Non-consuming variant of consume_approvals_for_session. Returns
    the approved-but-not-yet-applied requests WITHOUT marking them
    consumed. Use when caller needs to know what's pending but may
    later discard (e.g. blocked prompt → don't consume).

    SEC-001 hotfix (2026-04-23): replaces the eager-consume path in
    the hook's UserPromptSubmit flow. Consumption is deferred until
    the matching action is attempted at the tool-call layer
    (SEC-003 proper fix).
    """
    escalations = escalations or EscalationStore()
    return escalations.poll_approved_for_session(project_root, session_id)


def consume_approvals_for_session(
    project_root: Path,
    session_id: str,
    *,
    escalations: EscalationStore | None = None,
) -> list[EscalationRequest]:
    """Pop approved-but-not-yet-applied requests. Claude hook calls
    this on UserPromptSubmit and for each returned request issues
    the corresponding gate grant (per-turn or sticky), then the
    store marks them consumed.

    NOTE (SEC-001 hotfix 2026-04-23): the UserPromptSubmit caller
    switched to `peek_approved_for_session` to avoid consuming on
    unrelated prompts. This function remains for the Phase-3 apply
    path (once SEC-001 full refactor lands) and for test fixtures.
    """
    escalations = escalations or EscalationStore()
    ready = escalations.poll_approved_for_session(project_root, session_id)
    consumed: list[EscalationRequest] = []
    for req in ready:
        result = escalations.consume(project_root, req.request_id)
        if result is not None:
            consumed.append(result)
            # Audit gap fill: escalation_approved_consumed event.
            # Completes the request→approve→consume lifecycle chain.
            try:
                from .execution_index_store import ExecutionIndexStore

                ExecutionIndexStore().record_event(
                    project_root,
                    event_kind="escalation_consumed",
                    source_kind="escalation_hook",
                    session_id=session_id,
                    capability_name="consume_approvals_for_session",
                    action_kind="escalation",
                    target_entity=result.gate_permission,
                    status="consumed",
                    payload={
                        "request_id": result.request_id,
                        "gate_permission": result.gate_permission,
                        "sticky": bool(result.sticky),
                    },
                    permission_name=result.gate_permission,
                )
            except Exception:
                pass
    return consumed
