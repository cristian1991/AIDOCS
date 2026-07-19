"""Shared freeze service — one contract for claude_hook + ai_run.

Backlog #39 Phase A landed the freeze pipeline inside claude_hook
directly. Phase 1.5 (ai_run unification) extracts the shared logic
into this module so any caller that needs to:

  - evaluate a shell command via the gate cascade
  - check whether a session is currently frozen
  - build a freeze envelope for a confirmable verdict

uses the SAME helpers. claude_hook PreToolUse and server_run_tools.
ai_run both consume this service. Adding a third caller (the OpenCode
plugin, the Codex adapter, a future MCP host) consumes the same.

Storage layer stays in session_freeze_store.py (SessionFreezeStore).
This module is the service layer: orchestrator delegation + envelope
construction + fingerprint generation.
"""

from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_freeze_store import SessionFreeze


# Permission name carried by self-approve confirm grants. Distinct from
# the generic ``run_destructive`` admin grants so the confirm-retry lift
# path (consume_confirm_grant_if_matching) never collides with SEC-003's
# additive-only match-and-consume, and a confirm grant can only ever lift
# the ONE exact action the operator confirmed.
CONFIRM_GRANT_PERMISSION = "self_approve_confirm"


class FreezeMintError(Exception):
    """A freeze / exact-approval could not be durably created.

    Raised by ``build_freeze_response`` when the escalation request or the
    session-freeze row cannot be persisted. Callers MUST treat this as a
    hard block — NEVER present a "Type exactly: <phrase>" approval prompt
    when minting failed, because the phrase would resolve against nothing
    (false pending approval).
    """


# ── Canonical approval card ──────────────────────────────────────────
# Every freeze / confirmable verdict renders through ONE card so the
# operator always sees the same nine things: minted request identity,
# exact scope, reason, action consequence, jurisdiction, risk class,
# approval phrase, cancel path, and audit linkage. No hollow prompts:
# the card is only ever rendered AFTER the freeze is durably minted
# (build_freeze_response) or for an already-minted freeze
# (build_existing_freeze_response). When minting fails, callers receive a
# FreezeMintError and emit NO approval text.


@dataclass
class ApprovalCard:
    request_id: str  # minted identity + audit linkage
    scope: str  # exact action awaiting approval
    reason: str  # why it was held
    consequence: str  # what approval will cause
    jurisdiction: str  # in | out (of AIDOCS control)
    risk_class: str  # destructive / control_plane / …
    approval_phrase: str  # exact text to approve (or "")
    cancel_words: tuple = ("cancel", "deny", "no")
    admin_paths: list = field(default_factory=list)
    self_approvable: bool = True  # False → admin-clear-only lockdown


_JURISDICTION_NOTE = {
    "in": "in — AIDOCS governs this action",
    "out": "OUT — the resulting tool/server runs OUTSIDE AIDOCS control",
}


def _render_compact(card: ApprovalCard) -> str:
    """One-line card for rows / reminders / audit summaries."""
    out = "out " if card.jurisdiction == "out" else ""
    bits = [
        f"🔒 {card.risk_class}",
        f"scope={card.scope}",
    ]
    if card.self_approvable and card.approval_phrase:
        bits.append(f"approve='{card.approval_phrase}'")
        bits.append(f"cancel={'/'.join(card.cancel_words)}")
    else:
        bits.append("admin-clear-only")
    bits.append(f"req={card.request_id}")
    if out:
        bits.append("jurisdiction=OUT")
    return " · ".join(bits)


def render_approval_card(card: ApprovalCard, mode: str = "verbose") -> str:
    """Render the canonical operator-facing approval card.

    mode='verbose' (default) → the full multi-line card for prompts /
    host adapters / TUI bodies. mode='compact' → a single line for
    dashboard rows, existing-freeze reminders, and audit summaries.
    Both modes derive from the SAME ApprovalCard — no surface invents its
    own confirmation text.
    """
    if mode == "compact":
        return _render_compact(card)
    jx = _JURISDICTION_NOTE.get(card.jurisdiction, card.jurisdiction or "in")
    lines = [
        f"🔒 AIDOCS APPROVAL REQUIRED — {card.risk_class}",
        "",
        f"Request:      {card.request_id}",
        f"Scope:        {card.scope}",
        f"Reason:       {card.reason}",
        f"Consequence:  {card.consequence}",
        f"Jurisdiction: {jx}",
        f"Risk class:   {card.risk_class}",
        "",
    ]
    if card.self_approvable and card.approval_phrase:
        cancel = " / ".join(card.cancel_words)
        lines += [
            "To APPROVE — Type exactly:",
            f"   {card.approval_phrase}",
            f"To CANCEL: reply {cancel}. Any other reply leaves the session frozen.",
        ]
    else:
        lines += [
            "This lock is ADMIN-CLEAR-ONLY — the agent's own prompts cannot lift it.",
        ]
    if card.admin_paths:
        lines.append("")
        lines.append("Admin escape (real, tested):")
        lines += [f"   {p}" for p in card.admin_paths]
    lines += [
        "",
        f"Audit: this verdict is recorded under escalation request {card.request_id}.",
    ]
    return "\n".join(lines)


def _card_state(card: ApprovalCard, kind: str) -> dict[str, Any]:
    """Structured mirror of the card for non-text consumers (dashboard /
    agent), alongside the legacy keys callers/tests already read.
    """
    return {
        "kind": kind,
        "request_id": card.request_id,
        "fingerprint_phrase": card.approval_phrase,
        "approval_card": {
            "request_id": card.request_id,
            "scope": card.scope,
            "reason": card.reason,
            "consequence": card.consequence,
            "jurisdiction": card.jurisdiction,
            "risk_class": card.risk_class,
            "approval_phrase": card.approval_phrase,
            "cancel_words": list(card.cancel_words),
            "admin_paths": list(card.admin_paths),
            "audit_ref": card.request_id,
            "self_approvable": card.self_approvable,
        },
    }


def _admin_paths(request_id: str) -> list[str]:
    return [
        f"aidocs admin approve-escalation {request_id} --approver-email <email> --reason <why>",
        f"aidocs admin clear-freeze --freeze-id {request_id} "
        f"--approver-email <email> --reason <why>",
    ]


# ── Fingerprint generation ───────────────────────────────────────────


def build_freeze_fingerprint(
    tool_name: str,
    tool_input: object,
) -> str:
    """Build a short, deterministic phrase the operator types to
    approve a confirmable destructive verdict.

    Format: ``confirm <verb>-<short-hash>`` — short enough to
    retype without error, unique enough that approvals don't
    collide between concurrent freezes on the same session.

    verb extraction:
      - Bash/ai_run: first non-flag token of `command`
      - Other tools: tool name lowercased
    Hash: first 8 hex chars of sha256(tool + json(input)).
    """
    verb = (tool_name or "tool").strip().lower()
    if isinstance(tool_input, dict):
        cmd = str(tool_input.get("command") or "").strip()
        if cmd:
            for token in cmd.split():
                if not token.startswith("-"):
                    verb = token.split("/")[-1].lower() or verb
                    break
    try:
        payload = _json.dumps(
            {"tool": tool_name, "input": tool_input},
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        payload = f"{tool_name}|{tool_input!r}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    verb_clean = "".join(c for c in verb if c.isalnum() or c in "-_")
    return f"confirm {verb_clean}-{digest}"


# ── Action fingerprint + single-use confirm grants ───────────────────
# The confirm flow does NOT execute the held action. Instead, an exact
# `confirm <phrase>` mints a single-use grant bound to the EXACT action
# (action fingerprint + tool + session + machine), and the next identical
# retry consumes that grant and passes the gate exactly once. A different
# command, or a second retry, finds no live grant and is refused — the
# operator must confirm again. This removes the old loop where confirm
# cleared the freeze but the retry re-froze (the grant was minted with the
# confirm PHRASE as its command_hash, so it could never match the real
# command).


def action_fingerprint(tool_name: str, tool_input: object) -> str:
    """Canonical fingerprint of an attempted action, byte-identical to
    ``AgentOrchestrator._sec003_match_and_consume`` so a grant minted here
    matches what the gate computes on the retry.

    command (bash/ai_run) → sha256(command)[:32]; otherwise the extracted
    path → sha256(path)[:32]; otherwise "" (uncbindable → no grant).
    """
    import hashlib

    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "").strip()
    if command:
        return hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()[:32]
    raw_path = ""
    if isinstance(tool_input, dict):
        try:
            from .access_gate import _extract_path

            raw_path = _extract_path(tool_input) or ""
        except Exception:
            raw_path = ""
    if raw_path:
        return hashlib.sha256(raw_path.encode("utf-8", "replace")).hexdigest()[:32]
    return ""


def normalize_tool_name(tool_name: str) -> str:
    """Lowercase + strip the MCP host prefixes so a confirm grant binds to
    a stable surface identity. ``mcp__aidocs__ai_run`` and ``ai_run`` are
    the same surface; ``Bash`` and ``ai_run`` are NOT — an ai_run approval
    must never be consumable by raw Bash.
    """
    name = (tool_name or "").strip().lower()
    for pfx in ("mcp__aidocs__", "mcp__"):
        if name.startswith(pfx):
            return name[len(pfx) :]
    return name


def _stable_machine_id() -> str:
    try:
        from .host_concurrency_store import machine_id as _mid

        return str(_mid() or "")
    except Exception:
        return ""


def build_confirm_binding(tool_name: str, tool_input: object) -> dict[str, str]:
    """The action-identity tuple stashed on the escalation request at
    freeze time so the UPS resolver can mint a correctly-bound grant
    without re-deriving it from a possibly-different surface.
    """
    return {
        "confirm_action_fingerprint": action_fingerprint(tool_name, tool_input),
        # Stored NORMALIZED so the consume side compares like-for-like.
        "confirm_tool_name": normalize_tool_name(tool_name),
        "confirm_machine_id": _stable_machine_id(),
        "confirm_user_id": "",
    }


def mint_confirm_grant(
    project_root: Path,
    request: Any,
    session_id: str,
) -> str | None:
    """Mint the single-use grant for an approved self-approve freeze.

    Reads the action binding the freeze stashed on the escalation request.
    Returns the grant_id, or None when the request carries no fingerprint
    (uncbindable → mint nothing, so a retry is refused rather than allowed
    by a permission-only grant).
    """
    extra = getattr(request, "extra", None) or {}
    fingerprint = str(extra.get("confirm_action_fingerprint") or "").strip()
    if not fingerprint:
        return None
    from .escalation_store import EscalationStore

    grant = EscalationStore().create_grant(
        project_root,
        request_id=getattr(request, "request_id", "") or "",
        user_id=str(extra.get("confirm_user_id") or ""),
        machine_id=str(extra.get("confirm_machine_id") or ""),
        session_id=session_id,
        permission_name=CONFIRM_GRANT_PERMISSION,
        approved_by_user_id="",
        ttl_seconds=300,
        max_uses=1,
        command_hash=fingerprint,
        tool_name=str(extra.get("confirm_tool_name") or ""),
        scope="once",
    )
    return getattr(grant, "grant_id", None)


def consume_confirm_grant_if_matching(
    project_root: Path,
    *,
    session_id: str,
    tool_name: str,
    tool_input: object,
) -> bool:
    """Gate-side lift: if a live single-use confirm grant binds to THIS
    exact action (same fingerprint + session + machine), consume one use
    and return True (the retry passes once). Otherwise return False — a
    different command or an already-spent grant finds nothing and the
    caller proceeds to the normal deny/freeze path. Never raises.
    """
    if not session_id:
        return False
    try:
        fingerprint = action_fingerprint(tool_name, tool_input)
        if not fingerprint:
            return False
        from .escalation_store import EscalationStore

        es = EscalationStore()
        grant = es.find_live_grant(
            project_root,
            user_id="",
            machine_id=_stable_machine_id(),
            session_id=session_id,
            permission_name=CONFIRM_GRANT_PERMISSION,
            command_hash=fingerprint,
            # Strict surface binding: an ai_run approval must not be
            # consumable by raw Bash (or any other surface), even for an
            # identical command string.
            tool_name=normalize_tool_name(tool_name),
        )
        if grant is None:
            return False
        return es.consume_grant(project_root, grant.grant_id) is not None
    except Exception:
        return False


# ── Existing-freeze lookup ───────────────────────────────────────────


def _resolve_freeze_actor_identity(
    host_session_id: str = "",
    host_kind: str = "",
) -> tuple[str, str]:
    """Resolve both host identity axes from the current request/process scope."""
    host = str(host_session_id or "").strip()
    kind = str(host_kind or "").strip()
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_kind,
            current_calling_host_session_id,
        )

        if not host:
            host = (current_calling_host_session_id() or "").strip()
        if not kind:
            kind = (current_calling_host_kind() or "").strip()
    except Exception:
        pass
    return host, kind or "unknown"


def get_existing_freeze(
    project_root: Path,
    session_id: str,
    host_session_id: str = "",
    host_kind: str = "",
) -> SessionFreeze | None:
    """Return the active freeze owned by the calling actor, if any."""
    try:
        from .session_freeze_store import SessionFreezeStore

        host, kind = _resolve_freeze_actor_identity(host_session_id, host_kind)
        operator_user_id = ""
        try:
            from .config import get_setting

            if get_setting(
                "security.freeze_all_sessions_on_malicious_intent",
                project_root=project_root,
                default=True,
            ):
                from .project_authority import _authenticated_uid

                operator_user_id = _authenticated_uid(project_root, host)
        except Exception:
            operator_user_id = ""
        return SessionFreezeStore().get_active_freeze(
            project_root,
            session_id,
            host,
            host_kind=kind,
            operator_user_id=operator_user_id,
        )
    except Exception:
        return None


# ── Envelope construction ────────────────────────────────────────────


def build_freeze_response(
    project_root: Path,
    session_id: str,
    *,
    tool_name: str,
    tool_input: object,
    judge_summary: str,
    admin_tier: bool = False,
    risk_class: str = "destructive_action",
    jurisdiction: str = "in",
    scope: str | None = None,
    consequence: str | None = None,
    host_session_id: str = "",
    host_kind: str = "",
) -> dict[str, Any]:
    """Create a freeze + escalation request, return a deny envelope
    dict that tells the operator how to approve.

    Shared by claude_hook PreToolUse and ai_run. Each caller wraps the
    returned dict in its own host-shaped envelope:
      - claude_hook returns it directly (CC consumes hookSpecificOutput)
      - ai_run wraps reason + freeze_state into its structured run-
        output shape so the agent sees the freeze contract

    Single-turn for self_approve (admin_tier=False). admin_tier=True
    is reserved for Phase B — currently always False; the kind is
    wired here for forward compatibility.
    """
    from .escalation_store import EscalationStore
    from .session_freeze_store import (
        KIND_ADMIN_ESCALATION,
        KIND_SELF_APPROVE,
        SessionFreezeStore,
    )

    host, resolved_host_kind = _resolve_freeze_actor_identity(
        host_session_id,
        host_kind,
    )
    if not host:
        raise FreezeMintError(
            "freeze mint refused: canonical calling host_session_id is unavailable",
        )
    fingerprint = build_freeze_fingerprint(tool_name, tool_input)
    kind = KIND_ADMIN_ESCALATION if admin_tier else KIND_SELF_APPROVE

    snippet = ""
    if isinstance(tool_input, dict):
        cmd = str(tool_input.get("command") or "").strip()
        if cmd:
            snippet = cmd[:200]
    if not snippet:
        snippet = f"{tool_name}(...)"

    # Mint a DURABLE freeze first. No "Type exactly" prompt is emitted
    # unless both the escalation request and the freeze row persist — a
    # hollow approval phrase (no request_id to resolve against) is a false
    # pending approval and is forbidden. Any failure raises FreezeMintError
    # so callers hard-block instead of presenting an unresolvable prompt.
    try:
        req = EscalationStore().create_request(
            project_root,
            requester_label="operator-self-approve",
            gate_permission="run_destructive",
            gate_phrase=fingerprint,
            session_id=session_id or None,
            command_snippet=snippet,
            # Stash the exact action identity so the UPS confirm resolver
            # can mint a grant the retry will actually match.
            extra=build_confirm_binding(tool_name, tool_input),
        )
    except Exception as exc:
        raise FreezeMintError(f"escalation request creation failed: {exc}") from exc

    request_id = getattr(req, "request_id", "") or ""
    if not request_id:
        raise FreezeMintError("escalation request returned no request_id")

    try:
        SessionFreezeStore().set_freeze(
            project_root,
            session_id=session_id,
            request_id=request_id,
            fingerprint_phrase=fingerprint,
            kind=kind,
            host_session_id=host,
            host_kind=resolved_host_kind,
        )
    except Exception as exc:
        # Compensation: the escalation request already persisted, but the
        # freeze row did not. Roll the request back so no orphan PENDING
        # request survives to be approved into a grant for an action that
        # never had a freeze. Best-effort — the hard block stands either way.
        try:
            EscalationStore().cancel_request(
                project_root,
                request_id,
                reason="freeze mint rollback: set_freeze failed",
            )
        except Exception:
            pass
        raise FreezeMintError(f"session freeze persistence failed: {exc}") from exc

    # Canonical card — rendered ONLY now that the freeze is durably minted.
    card = ApprovalCard(
        request_id=request_id,
        scope=snippet,
        reason=(judge_summary or "operator confirmation required").strip(),
        consequence=(
            consequence
            or "Approval authorizes the NEXT exact matching retry once — "
            "AIDOCS does not run it for you; re-issue the same action and "
            "it passes a single time. A different action, or a second "
            "retry, is refused. The session stays frozen until you "
            "approve or cancel."
        ),
        jurisdiction=jurisdiction,
        risk_class=risk_class,
        approval_phrase=fingerprint,
        admin_paths=_admin_paths(request_id),
        self_approvable=not admin_tier,
    )
    if scope:
        card.scope = scope
    kind = "awaiting_self_approve" if not admin_tier else "awaiting_admin_escalation"
    return {
        "permissionDecisionReason": render_approval_card(card),
        "blocked_by": "judge_confirm_required",
        "freeze_state": _card_state(card, kind),
    }


def _render_strike_trail(
    project_root: Path | None,
    session_id: str,
    *,
    agent_context_id: str = "",
    limit: int = 5,
) -> str:
    """Render only the strikes owned by the frozen actor."""
    if project_root is None or not session_id:
        return ""
    try:
        import json as _json

        from .execution_index_store import ExecutionIndexStore

        ex = ExecutionIndexStore()
        ex.init_db(project_root)
        where = (
            "WHERE event_kind = 'security_violation_strike' AND "
            "COALESCE(session_id, '') = COALESCE(?, '') "
        )
        params: list[object] = [session_id]
        if agent_context_id:
            where += "AND target_entity LIKE ? "
            params.append(f"%|{agent_context_id}")
        params.append(int(limit))
        with ex.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT capability_name, payload_json FROM execution_events "
                + where
                + "ORDER BY rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines: list[str] = ["", "  Recent attempts that led here:"]
    for row in rows:
        try:
            payload: dict[str, Any] = _json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        name = str(payload.get("tool_name") or "")
        target = str(payload.get("target") or "")
        family = str(row["capability_name"] or "")
        if len(target) > 120:
            target = target[:117] + "..."
        if name and target:
            lines.append(f"    • [{family}] {name} → {target}")
        elif target:
            lines.append(f"    • [{family}] {target}")
        elif name:
            lines.append(f"    • [{family}] {name}")
        else:
            lines.append(f"    • [{family}]")
    return "\n".join(lines)


def build_existing_freeze_response(
    freeze: SessionFreeze,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Re-render the canonical card for a freeze already in the store.

    Used when a frozen session attempts another tool before resolving
    the freeze. ``project_root`` lets us recover the EXACT original
    scope (and risk) from the stored escalation request; without it
    we fall back to a generic scope.

    LEARNING SURFACE (2026-05-27 step 2): when the freeze is the
    repeated_security_violation lockdown, the deny envelope now
    carries an audit trail of the strikes that led there — every
    tool call against a frozen session teaches the agent which
    verb-and-target combinations crossed the line, not just that
    *something* did.
    """
    # Recover scope / risk from the minted escalation request.
    scope = "the original blocked action"
    risk_class = "destructive_action"
    session_id = str(getattr(freeze, "session_id", "") or "")
    if project_root is not None and freeze.request_id:
        try:
            from .escalation_store import EscalationStore

            req = EscalationStore().get(project_root, freeze.request_id)
            if req is not None:
                if getattr(req, "command_snippet", None):
                    scope = str(req.command_snippet)
                if getattr(req, "gate_permission", None):
                    risk_class = str(req.gate_permission)
        except Exception:
            pass

    if freeze.kind in ("repeated_security_violation", "hostile_operator_prompt"):
        # Admin-clear-only lockdown — the agent's own prompts must NOT
        # clear it. Same canonical card, no self-approval phrase.
        if freeze.kind == "hostile_operator_prompt":
            reason = (
                "a UserPromptSubmit was classified as forbidden; the session was frozen immediately"
            )
            risk_class = "lockdown:hostile_operator_prompt"
        else:
            reason = "the session is frozen after multiple blocked security attempts"
            risk_class = "lockdown:repeated_security_violation"
        card = ApprovalCard(
            request_id=freeze.request_id,
            scope=scope,
            reason=reason,
            consequence=(
                "nothing runs; an operator with rbac.admin_clear_freeze must lift the lock"
            ),
            jurisdiction="in",
            risk_class=risk_class,
            approval_phrase="",
            admin_paths=[
                f"aidocs admin clear-freeze --freeze-id {freeze.request_id} --reason <why>",
            ],
            self_approvable=False,
        )
        # LEARNING SURFACE (2026-05-27 step 2): append the strike trail
        # to the rendered card body. Every tool call against this
        # lockdown shows the agent which attempts got them here.
        rendered = render_approval_card(card)
        trail = _render_strike_trail(
            project_root,
            session_id,
            agent_context_id=str(getattr(freeze, "agent_context_id", "") or ""),
        )
        if trail:
            rendered = rendered + "\n" + trail
        return {
            "permissionDecisionReason": rendered,
            "blocked_by": "session_frozen",
            "freeze_state": _card_state(card, freeze.kind),
        }

    card = ApprovalCard(
        request_id=freeze.request_id,
        scope=scope,
        reason="session frozen pending operator confirmation of the original blocked action",
        consequence="Approval authorizes the NEXT exact matching retry "
        "once — re-issue the same action and it passes a "
        "single time; AIDOCS does not run it for you. Any "
        "other reply leaves the session frozen.",
        jurisdiction="in",
        risk_class=risk_class,
        approval_phrase=freeze.fingerprint_phrase,
        admin_paths=_admin_paths(freeze.request_id),
        self_approvable=True,
    )
    kind = "awaiting_self_approve" if freeze.kind == "self_approve" else "awaiting_admin_escalation"
    return {
        "permissionDecisionReason": render_approval_card(card),
        "blocked_by": "session_frozen",
        "freeze_state": _card_state(card, kind),
    }


# ── Re-render at any surface (host adapter / CLI / TUI / dashboard /
#    audit) WITHOUT re-minting: reconstruct the card from the structured
#    state the envelope already carries, or from a stored escalation row.
def approval_card_from_state(
    freeze_state: dict[str, Any] | None,
) -> ApprovalCard | None:
    """Reconstruct the canonical ApprovalCard from a freeze_state dict
    (the structured `approval_card` mirror). Returns None when absent so
    callers can detect a non-card envelope rather than inventing text.
    """
    if not isinstance(freeze_state, dict):
        return None
    ac = freeze_state.get("approval_card")
    if not isinstance(ac, dict):
        return None
    return ApprovalCard(
        request_id=str(ac.get("request_id") or ""),
        scope=str(ac.get("scope") or ""),
        reason=str(ac.get("reason") or ""),
        consequence=str(ac.get("consequence") or ""),
        jurisdiction=str(ac.get("jurisdiction") or "in"),
        risk_class=str(ac.get("risk_class") or ""),
        approval_phrase=str(ac.get("approval_phrase") or ""),
        cancel_words=tuple(ac.get("cancel_words") or ("cancel", "deny", "no")),
        admin_paths=list(ac.get("admin_paths") or []),
        self_approvable=bool(ac.get("self_approvable", True)),
    )


def render_card_from_state(
    freeze_state: dict[str, Any] | None,
    mode: str = "verbose",
) -> str | None:
    """Render the card directly from a freeze_state dict, or None when the
    state carries no canonical card (caller must NOT fall back to legacy
    free-form text — None means 'not a confirmation envelope').
    """
    card = approval_card_from_state(freeze_state)
    return render_approval_card(card, mode) if card is not None else None


def card_from_escalation(req: Any) -> ApprovalCard:
    """Build the canonical card from a stored EscalationRequest, so the
    dashboard / audit / CLI listing of PENDING escalations renders the
    same card as the live freeze prompt (no parallel formatting).
    """
    request_id = str(getattr(req, "request_id", "") or "")
    return ApprovalCard(
        request_id=request_id,
        scope=str(getattr(req, "command_snippet", None) or "(action)"),
        reason="pending operator approval",
        consequence="Approval authorizes the next exact matching retry "
        "once; AIDOCS does not run the held action for you.",
        jurisdiction="in",
        risk_class=str(getattr(req, "gate_permission", "") or "destructive_action"),
        approval_phrase=str(getattr(req, "gate_phrase", "") or ""),
        admin_paths=_admin_paths(request_id),
        self_approvable=True,
    )
