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
import time
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
    #: WHEN this lock lifts on its own (#740 acceptance 3). Built by
    #: ``freeze_ttl_note`` from the freeze row, never composed by a surface.
    ttl_note: str = ""


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
        # #740: was `approve='<phrase>'`, which reads as "type this to approve".
        # The phrase is an audit token; the deciding path is the admin CLI.
        bits.append(f"phrase='{card.approval_phrase}'")
        bits.append("decide-by=admin-cli")
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
    if card.ttl_note:
        lines += [card.ttl_note, ""]
    if not card.self_approvable:
        lines += [
            "This lock is ADMIN-CLEAR-ONLY — the agent's own prompts cannot lift it.",
        ]
    # THE DECIDING PATH GOES FIRST, and it is the one that was tested (#740).
    #
    # This block used to open with "To APPROVE — Type exactly:" followed by the
    # phrase — an IMPERATIVE, printed above the admin lines, aimed at the
    # operator's chat. Measured live 2026-08-01: the operator typed it, twice,
    # bare and with context, and NO VERDICT EVER REACHED THE GATE. The session
    # unblocked five minutes later on a TTL this card never mentioned, and the
    # runtime announced "NOBODY APPROVED ANYTHING".
    #
    # The phrase is not fictional — `prompt_mutator.resolve_session_freeze`
    # consumes it at UserPromptSubmit — but it can only apply it to a freeze
    # that lookup RETURNS, and every freeze minted here is actor-scoped
    # (`FREEZE_SCOPE_ACTOR`, #588 D1) while the resolver looks up with the host
    # identity from `current_calling_host_session_id()`, which is empty in a
    # fresh hook process. Empty actor key, no row, no verdict. That is NOT a
    # hole to widen: widening it would let one actor's chat clear ANOTHER
    # actor's lock, which #588 D1 deliberately closed. The freeze is right; the
    # INSTRUCTION was wrong, so the instruction is what changed.
    #
    # The phrase is still DISCLOSED, below the working path and labelled for
    # what it is, because it is the real audit token for this request and it
    # does resolve on the surfaces that can name the actor. Law 311bf3e6 bans
    # advertising an unreachable remedy; it does not ban stating a fact.
    if card.admin_paths:
        lines.append("To DECIDE this — the operator shell (real, tested):")
        lines += [f"   {p}" for p in card.admin_paths]
    if card.self_approvable and card.approval_phrase:
        cancel = " / ".join(card.cancel_words)
        lines += [
            "",
            f"Audit phrase for this request: {card.approval_phrase}",
            "   NOT AN INSTRUCTION — do not rely on typing it into chat.",
            (
                "   It is read only at UserPromptSubmit, and only for a freeze "
                "the reading process"
            ),
            (
                "   can attribute to the actor that OWNS it. For an "
                "actor-scoped lock (see Scope)"
            ),
            (
                "   that attribution is unavailable, so a typed reply — or "
                f"{cancel} — reaches"
            ),
            "   nothing. Decide it with the lines above.",
        ]
    lines += [
        "",
        f"Audit: this verdict is recorded under escalation request {card.request_id}.",
    ]
    return "\n".join(lines)


def freeze_ttl_note(freeze: Any) -> str:
    """WHEN this lock lifts on its own — #740 acceptance 3.

    The envelope said "Any other reply leaves the session frozen", which reads
    INDEFINITE. It was FIVE MINUTES (``TTL_SELF_APPROVE_SECONDS``). The operator
    spent that window typing a phrase nothing read; knowing the deadline, waiting
    was a decision available to him and he was never offered it.

    Says NOTHING it cannot read off the row. A freeze with no ``expires_at`` says
    so plainly rather than implying a deadline — an unbounded lock is a real
    shape here (``set_freeze`` mints one for an explicit non-positive ttl), and
    inventing a duration for it would be this item's own defect in reverse.
    """
    expires_at = str(getattr(freeze, "expires_at", "") or "").strip()
    frozen_at = str(getattr(freeze, "frozen_at", "") or "").strip()
    if not expires_at:
        return (
            "Lifts:   NEVER on its own — this lock carries no deadline. It ends "
            "only when an operator clears it."
        )
    span = ""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        seconds = int(
            time.mktime(time.strptime(expires_at, fmt))
            - time.mktime(time.strptime(frozen_at, fmt)),
        )
        if seconds > 0:
            span = (
                f" (TTL {seconds // 60} min)"
                if seconds < 7200
                else f" (TTL {seconds // 3600} h)"
            )
    except (ValueError, TypeError, OverflowError):
        span = ""
    return (
        f"Lifts:   automatically at {expires_at}{span} if nobody decides it. "
        "EXPIRY IS NOT APPROVAL — the escalation stays pending, the refused "
        "action never ran, and retrying it is judged again."
    )


def _card_state(
    card: ApprovalCard,
    kind: str,
    verdict_class: str = "",
) -> dict[str, Any]:
    """Structured mirror of the card for non-text consumers (dashboard /
    agent), alongside the legacy keys callers/tests already read.

    #571: ``verdict_class`` + the derived ``security_class`` boolean ride along
    so every freeze envelope — freshly minted OR re-rendered from an existing
    row — answers "is this security-class?" identically. ``security_class``
    is computed by ``verdict_class.is_security_class``, which fails closed, so
    a legacy row with no class reports True.
    """
    from .verdict_class import is_security_class as _is_security_class

    return {
        "kind": kind,
        "verdict_class": verdict_class,
        "security_class": _is_security_class(verdict_class),
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
            # #740: the deadline travels with the card, so a surface that
            # re-renders from state (dashboard / CLI / TUI) states the same TTL
            # the live envelope did instead of silently dropping it.
            "ttl_note": card.ttl_note,
        },
    }


def _admin_paths(request_id: str) -> list[str]:
    return [
        f"aidocs admin approve-escalation {request_id} --approver-email <email> --reason <why>",
        (f"aidocs admin clear-freeze --freeze-id {request_id} "
        f"--approver-email <email> --reason <why>"),
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


def _resolve_freeze_host_axes(
    host_session_id: str = "",
    host_kind: str = "",
    project_root: Path | None = None,
) -> tuple[str, str]:
    """``(host_session_id, host_kind)`` — BOTH honest empties, no placebo.

    The one place the freeze subsystem asks #587's authority
    (``agent_memory_epoch.resolve_host_identity``: explicit args -> request
    stamp -> process stamp -> the DURABLE record keyed by host_session_id ->
    env sniff). Passing ``project_root`` is what unlocks rung 4.

    #588 D1: the freeze subsystem — the one place where getting the actor
    wrong costs a whole agent tree — used to read the two ContextVar /
    process-global accessors directly, i.e. by a route that stops at the
    request boundary. It goes through the authority now.

    Its two callers below differ ONLY in what they do with an unnameable
    KIND, and that difference is measured, not stylistic. See each.
    """
    from .agent_memory_epoch import resolve_host_identity

    try:
        kind, host = resolve_host_identity(
            host_kind=host_kind,
            host_session_id=host_session_id,
            project_root=project_root,
        )
    except Exception:
        host = str(host_session_id or "").strip()
        kind = str(host_kind or "").strip()
    return host, kind


def _resolve_freeze_actor_identity(
    host_session_id: str = "",
    host_kind: str = "",
    project_root: Path | None = None,
) -> tuple[str, str]:
    """The LEDGER-PARTITION identity. Kind-less hosts keep a partition key.

    Callers: the STRIKE ledger sites in ``security_violation_service``
    (``record_and_escalate``, ``reset_strikes``,
    ``void_self_cancel_after_local_clear``). What they need from this is a
    key that SEPARATES agents so their counts do not pool. It is a counter
    key, not an authority key; nothing is granted or refused on it.

    THE ``LEGACY_UNKNOWN_HOST_KIND`` DEGRADATION IS DELIBERATE AND STAYS —
    and a fallback audit's recommendation to remove it was REFUTED by
    measurement on 2026-08-23. The claim was that the placeholder "collapses
    every kind-less agent into ONE agent_context_id". It does not. Measured
    against the real derivation:

        host_kind="unknown", host_session_id="host-A" -> 9e7daabacfb227d7
        host_kind="unknown", host_session_id="host-B" -> 62ad1b12f51ab5e2
        host_kind="",        host_session_id="host-A" -> ""
        host_kind="",        host_session_id="host-B" -> ""

    The placeholder KEEPS them apart (the session axis still separates them);
    it is the EMPTY that collapses all of them into one bucket, because
    ``derive_agent_context_id`` refuses an empty ``host_kind`` outright. So
    removing it here would pool every kind-less agent's strikes into the
    single unattributed key — N agents with one strike each read as one agent
    with N, which is precisely the 2026-08-21 incident #879 exists to end.
    Three tests measured it live (``test_strikes_partition_by_derived_
    agent_context_id``, ``..._follow_agent_across_sessions_not_other_agents``,
    ``test_on_post_compact_resets_strikes``).

    What the placeholder DOES cost is real but far narrower: two hosts of
    DIFFERENT kinds reporting the SAME session-id string share a partition.
    That is the price, it is named, and it is smaller than pooling.

    THE AUTHORITY PATH DOES NOT USE THIS. ``resolve_freeze_actor`` below
    returns the honest empty, so a FREEZE — which grants and refuses — is
    never keyed on a fabricated axis. Resolution and keying are different
    jobs; this is the line between them.
    """
    from .agent_memory_epoch import LEGACY_UNKNOWN_HOST_KIND

    host, kind = _resolve_freeze_host_axes(
        host_session_id,
        host_kind,
        project_root=project_root,
    )
    return host, kind or LEGACY_UNKNOWN_HOST_KIND


def resolve_freeze_actor(
    host_session_id: str = "",
    host_kind: str = "",
    project_root: Path | None = None,
    agent_id: str | None = None,
) -> tuple[str, str, str]:
    """The AUTHORITY identity: ``(host_session_id, host_kind, agent_id)``,
    every axis an HONEST EMPTY when it cannot be named.

    Callers: everything that MINTS, READS or CLEARS a freeze row —
    ``get_existing_freeze``, ``build_freeze_response``,
    ``security_violation_service._create_freeze``. A freeze grants and
    refuses, so it must never be keyed on an axis nobody could name.

    TWO FIXES LIVE HERE.

    #879 B5 — NO FABRICATED KIND (operator ruling 2026-08-23, IDENTITY HAS
    NO FALLBACK). The freeze mint used to key on
    ``_resolve_freeze_actor_identity``, whose ``LEGACY_UNKNOWN_HOST_KIND`` is
    a NON-EMPTY string and therefore walked straight past
    ``derive_agent_context_id``'s deliberate refusal of an empty ``host_kind``
    (``agent_memory_epoch.py:197-199``). The result was an ACTOR-SCOPED
    authority row keyed on a fabricated axis, and two hosts of DIFFERENT
    kinds reporting the same session-id string derived the SAME actor.
    ``resolve_host_identity`` already strips the placeholder
    (``normalize_host_kind``, ``agent_memory_epoch.py:710-716``); the freeze
    path was putting it back.

    The prose this replaces claimed blanking the kind "would re-derive a
    DIFFERENT agent_context_id for rows already in the store". Measured
    2026-08-23: it does not derive a DIFFERENT id, it derives the EMPTY id —
    which every reader already handles as "no actor" — and ``session_freeze``
    held 0 rows, so no live freeze depended on the fabricated bucket at all.

    So callers now decide what an unnameable actor means for THEM:
      * the confirm/escalation mint asks for ``FREEZE_SCOPE_ACTOR`` by name
        and REFUSES (``UnattributableFreeze`` -> ``FreezeMintError``);
      * the repeated-violation lockdown degrades to a DECLARED session scope
        — the same reach it has always had, recorded as a decision.

    #879 B1 — THE AGENT AXIS. The two host axes name the CONVERSATION; they
    cannot name WHICH AGENT inside it is acting. A subagent's hook payload
    carries its parent's ``session_id`` AND its parent's ``transcript_path``
    — ``agent_id`` is the only field that differs (measured 2026-08-22,
    Claude Code 2.1.239). Without it a subagent crossed the strike threshold
    under its OWN key and latched the resulting freeze under its PARENT's,
    which then matched every sibling and the conductor.

    ``agent_id=None`` (the default) reads the request-scoped axis; pass a
    string to state it explicitly, "" included, to state its ABSENCE.
    """
    host, kind = _resolve_freeze_host_axes(
        host_session_id,
        host_kind,
        project_root=project_root,
    )
    resolved_agent = (
        calling_freeze_agent_id() if agent_id is None else str(agent_id or "").strip()
    )
    return host, kind, resolved_agent


def calling_freeze_agent_id() -> str:
    """The SUBAGENT axis for THIS call, or "" — never a substitute.

    Read here rather than threaded down from the gate for the same reason
    ``security_violation_service._calling_agent_id`` reads it there: nothing
    between a hook entrypoint and the freeze store has an ``agent_id``
    parameter, and the other two identity axes already travel the same
    request-scoped way. Returns "" for every caller outside a hook request —
    the MCP transport cannot carry the axis at all — which is exactly what the
    main thread is, and keeps the derivation byte-identical for it.
    """
    try:
        from .mcp_server_runtime_helpers import current_calling_agent_id

        return current_calling_agent_id()
    except Exception:
        return ""

def get_existing_freeze(
    project_root: Path,
    session_id: str,
    host_session_id: str = "",
    host_kind: str = "",
) -> SessionFreeze | None:
    """Return the active freeze owned by the calling actor, if any."""
    try:
        from .session_freeze_store import SessionFreezeStore

        host, kind, actor_agent_id = resolve_freeze_actor(
            host_session_id,
            host_kind,
            project_root=project_root,
        )
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
            # #879 B1: ask on the SAME axis the row was written on. Without
            # this a subagent asked with its parent's key and was told it was
            # frozen by a lock it never earned (and vice versa).
            agent_id=actor_agent_id,
        )
    except Exception:
        return None


# ── Rung-3 WORKFLOW BLOCK (backlog #571 phase 2) ─────────────────────

#: The denial tier a rung-3 block reports. Distinct from
#: "judge_confirm_required" (freeze) and from a flat "denied" so a host adapter,
#: an audit consumer, and a test can each tell the three apart by string.
BLOCK_BLOCKED_BY = "workflow_block"


def _observed_freeze_for_block(
    project_root: Path | None,
    session_id: str,
    host_session_id: str,
    host_kind: str,
) -> tuple[bool, SessionFreeze | None]:
    """READ BACK what is actually latched on the caller, for the block envelope.

    #588 D5. The block envelope's load-bearing claims ("no strike recorded",
    "session NOT frozen", "no approval phrase to type") were composed from
    what THIS code path intends to write — nothing — while an earlier stage of
    the same tool call could already have minted a freeze. Measured at least
    twice: the envelope said no phrase existed and the very next tool call
    demanded it.

    Returns ``(verified, freeze)``:
      * ``(True, None)``  — the store was consulted and the caller is clear.
      * ``(True, row)``   — the store was consulted and a freeze IS in force.
      * ``(False, None)`` — no project/session context to consult with. The
        renderer then makes NO claim about freeze state, rather than making a
        cheerful one it cannot support.
    """
    if project_root is None or not str(session_id or "").strip():
        return (False, None)
    try:
        return (
            True,
            get_existing_freeze(
                project_root,
                str(session_id).strip(),
                host_session_id,
                host_kind,
            ),
        )
    except Exception:
        # get_existing_freeze already swallows; belt-and-braces so a broken
        # read degrades to "cannot verify", never to a false all-clear.
        return (False, None)


def build_workflow_block_response(
    *,
    tool_name: str,
    tool_input: object,
    reason: str,
    verdict_class: str = "",
    recommendation: str = "",
    project_root: Path | None = None,
    session_id: str = "",
    host_session_id: str = "",
    host_kind: str = "",
) -> dict[str, Any]:
    """The rung-3 BLOCK envelope — law 526fcfdd's "blocks only enforce
    workflow".

    Deliberately writes NOTHING:
      * no freeze row (``SessionFreezeStore.set_freeze`` is never called)
      * no escalation request (``EscalationStore.create_request`` is never
        called) and therefore no approval phrase of its own and no pending row
        for an operator to resolve
      * no strike (this function cannot reach the violation service)
      * nothing latched BY THIS BLOCK — it adds no reason for the agent's next
        tool call to be refused

    Consequences that follow from writing nothing, and are the point:
      * **agent-cancellable for free.** There is no state to clear, so
        "cancelling" costs nothing and requires no operator round-trip. The
        envelope says so explicitly rather than implying it.
      * **identity is OPTIONAL, never required.** The function still returns a
        truthful envelope with no ``project_root``, no ``session_id`` and no
        host identity, so the block path stays immune to the item-5
        ContextVar/process-global nondeterminism that can fail the freeze mint.

    #588 D5 — WHAT THE PROSE IS COMPOSED FROM. It used to assert "No strike was
    recorded. Your session is NOT frozen and nothing is latched … no approval
    phrase to type", which described this function's own intent and NOT the
    row that another stage of the same call may have written. When the caller
    supplies ``project_root`` + ``session_id``, the claims are now rendered
    from a READ-BACK of the freeze store performed after the fact; when it
    does not, the envelope says only what this block can prove about itself
    and explicitly declines to certify the session. A refusal that
    misdescribes its own consequence is worse than a terse one.

    ``block_state`` is deliberately UNCHANGED (same keys, same values): those
    fields describe THIS block's own writes, which are still none.

    Returns an envelope with ``block_state`` and NO ``freeze_state`` key — the
    absence is load-bearing: every downstream latch, hollow-ask check, and
    ``if "freeze_state" in env`` guard keys off that key's presence.
    """
    from .verdict_class import CLASS_WORKFLOW_BLOCK
    from .verdict_class import is_security_class as _is_security_class

    resolved = verdict_class or CLASS_WORKFLOW_BLOCK
    snippet = ""
    if isinstance(tool_input, dict):
        snippet = str(tool_input.get("command") or "").strip()[:200]
    if not snippet:
        snippet = f"{tool_name}(...)"

    body = [
        "⛔ AIDOCS WORKFLOW BLOCK — not a security event.",
        "",
        f"Action:  {snippet}",
        f"Why:     {(reason or 'blocked by workflow policy').strip()}",
    ]
    if recommendation.strip():
        body.append(f"Do this: {recommendation.strip()}")

    # ── The claims, composed AFTER the fact from the store (#588 D5) ──
    verified, active = _observed_freeze_for_block(
        project_root,
        session_id,
        host_session_id,
        host_kind,
    )
    self_report = (
        "This block recorded no strike, minted no escalation request and "
        "latched nothing of its own."
    )
    if verified and active is None:
        consequence = (
            f"{self_report} Checked against the freeze store after the fact: "
            "no freeze is in force on you, so you may drop this action and "
            "continue with other work immediately, at no cost. There is no "
            "approval phrase to type and no operator action pending."
        )
    elif verified and active is not None:
        req = str(getattr(active, "request_id", "") or "").strip()
        kind = str(getattr(active, "kind", "") or "").strip() or "unknown"
        # #740: this branch used to say `Type exactly: "<phrase>"` for a
        # self_approve freeze — the SAME unreachable instruction the freeze
        # envelope itself printed, relocated into the block envelope. It is
        # replaced by the path that was actually tested. Naming the admin
        # command grants nothing: it still goes through ClearFreezeService's
        # capability gate, which refuses a self-clear.
        how = (
            "An operator with rbac.admin_clear_freeze must decide it: "
            + " OR ".join(_admin_paths(req or "<request-id>"))
            + "."
        )
        consequence = (
            f"{self_report} BUT A FREEZE IS ALREADY IN FORCE ON YOU — kind "
            f"{kind}, freeze id {req or 'unknown'} — minted by an earlier "
            f"refusal, not by this block. Your next tool call will be refused "
            f"until it is resolved. {how} {freeze_ttl_note(active)}"
        )
    else:
        consequence = (
            f"{self_report} It could NOT check whether an earlier refusal in "
            "this call left a freeze in force (no project/session context was "
            "supplied), so it makes no claim either way — verify before "
            "assuming your session is clear."
        )
    body += ["", consequence]
    return {
        "permissionDecisionReason": "\n".join(body),
        "blocked_by": BLOCK_BLOCKED_BY,
        "block_state": {
            "kind": BLOCK_BLOCKED_BY,
            "verdict_class": resolved,
            "security_class": _is_security_class(resolved),
            # The three properties law 526fcfdd grants a block, stated as data
            # so a caller branches on values rather than parsing the prose.
            "latched": False,
            "strike_cost": 0,
            "agent_cancellable": True,
        },
    }


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
    blocked_by: str = "",
    matched_rule: str = "",
    verdict_class: str = "",
    user_intent_detected: bool = False,
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
        FREEZE_SCOPE_ACTOR,
        KIND_ADMIN_ESCALATION,
        KIND_SELF_APPROVE,
        SessionFreezeStore,
    )

    host, resolved_host_kind, actor_agent_id = resolve_freeze_actor(
        host_session_id,
        host_kind,
        project_root=project_root,
    )
    if not host:
        raise FreezeMintError(
            "freeze mint refused: canonical calling host_session_id is unavailable",
        )
    fingerprint = build_freeze_fingerprint(tool_name, tool_input)
    kind = KIND_ADMIN_ESCALATION if admin_tier else KIND_SELF_APPROVE

    # ── #571: the VERDICT CLASS, resolved once and persisted ──────────
    # Before this, the only class-shaped value reaching here was the free-form
    # `risk_class`, which existed for exactly one render (into the approval
    # card text) and was then discarded. Nothing downstream could ask "was
    # this a security offence or a workflow block", so:
    #   * a conductor clearing its subagent's freeze had no way to refuse a
    #     security-class one, and
    #   * strike accrual had no way to skip a rung-3 idiocy.
    # The class is now derived from the actual refusal identity (blocked_by /
    # matched_rule / risk_class / detected intent) via verdict_class.classify,
    # normalized, and written onto the freeze ROW.
    from .verdict_class import classify as _classify_verdict
    from .verdict_class import gate_permission_for as _gate_permission_for
    from .verdict_class import normalize as _normalize_verdict

    if str(verdict_class or "").strip():
        resolved_class = _normalize_verdict(verdict_class)
    else:
        resolved_class = _classify_verdict(
            blocked_by=blocked_by,
            matched_rule=matched_rule,
            risk_class=risk_class,
            user_intent_detected=user_intent_detected,
        )
    gate_permission = _gate_permission_for(resolved_class)

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
            # #571: was the literal "run_destructive" for EVERY freeze,
            # regardless of what the action was — which is how a markdown
            # append under scratch/ ended up filed as a destructive run.
            # Now derived from the verdict class. gate_permission_for()
            # deliberately still returns "run_destructive" for the security
            # rungs, because agent_orchestrator's operator-approval lift looks
            # that exact name up; only rung 3 gets its own name, so a workflow
            # block can never consume a destructive-action approval.
            gate_permission=gate_permission,
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

    minted_freeze = None
    try:
        minted_freeze = SessionFreezeStore().set_freeze(
            project_root,
            session_id=session_id,
            request_id=request_id,
            fingerprint_phrase=fingerprint,
            kind=kind,
            host_session_id=host,
            host_kind=resolved_host_kind,
            # #879 B1: the confirm lock belongs to the ONE AGENT whose action
            # was refused, not to its conversation. With no agent_id this is
            # byte-identical to the v1 key, so the conductor is untouched.
            agent_id=actor_agent_id,
            verdict_class=resolved_class,
            # #588 D1: a confirm/escalation lock belongs to the ONE agent
            # whose action was refused. Asking for the actor scope by name
            # also makes the store refuse rather than silently widen if the
            # identity resolved above turns out not to key (the guard above
            # already rejects an empty host; this is the second lock on the
            # same door, at the sink).
            scope=FREEZE_SCOPE_ACTOR,
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
            "retry, is refused. The session stays frozen until an operator "
            "decides it or the deadline below passes."
        ),
        jurisdiction=jurisdiction,
        risk_class=risk_class,
        approval_phrase=fingerprint,
        admin_paths=_admin_paths(request_id),
        self_approvable=not admin_tier,
        # #740: read off the row we JUST wrote, so the stated deadline is the
        # persisted one rather than a duration re-derived (and drifting) here.
        ttl_note=freeze_ttl_note(minted_freeze),
    )
    if scope:
        card.scope = scope
    kind = "awaiting_self_approve" if not admin_tier else "awaiting_admin_escalation"
    return {
        "permissionDecisionReason": render_approval_card(card),
        "blocked_by": "judge_confirm_required",
        # The class travels on the envelope too, so an in-process caller (host
        # adapter, conductor bridge) can branch on it without re-reading the
        # store — and so a test can assert row and envelope agree.
        "freeze_state": _card_state(card, kind, resolved_class),
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


def freeze_reachable_channels_note() -> str:
    """What the FROZEN AGENT — not the operator — may still do (#640).

    Every remedy this card carried was operator-only: an ``aidocs admin
    clear-freeze`` shell line the agent cannot run, and an approval phrase a
    human must type. An agent reading it had nothing it could DO, and the
    measured outcome was four lanes frozen AND MUTE for hours while the
    conductor learned nothing. Law 311bf3e6 — a surfaced remedy must be
    reachable BY THE PARTY IT IS SHOWN TO.

    ONLY channels the freeze gate actually admits are listed, and every call
    spelled here is re-verified against ``freeze_gate_exemption`` itself by
    ``test_ai_msg_block_reporting_640``. Writing this note was nearly the
    defect it cures: the first draft named ``ai_issues(mode='file')``, which
    is task-free but NOT freeze-exempt — it writes a file and runs a git
    commit, so it fails the "performs no work in the project" membership rule
    in ``operation_classes._REMEDY_REACHABILITY_TOOLS``. A card naming it
    would have sent a frozen agent straight into another refusal.

    ORDER IS THE REMEDY (#740 acceptance 4). A frozen agent tries what it reads
    FIRST. This note used to lead with three ai_msg calls and put ai_qa last —
    and #732 measured all three ai_msg channels REFUSING a caller with no bound
    seat or XAACP actor (``role='unmapped'`` / "no canonical XAACP actor/session
    binding"), which is precisely the caller a lane freeze produces. So the
    reader spent its first three attempts on refusals before reaching the one
    that answers. ai_qa is measured working for a seatless caller (2026-08-17,
    #740) and now goes first; the ai_msg lines stay, because they DO work for a
    seated caller and #732 is being repaired, but they carry their measurement
    instead of an implied promise.

    Reporting is not unfreezing: none of these lifts the lock, and none of
    them lets the agent resume work.
    """
    return (
        "\nStill open to YOU (the agent) while frozen — REPORT, do not retry:\n"
        "   ai_qa(mode='ask', question=...)  <- START HERE. Needs no seat and "
        "no actor binding;\n"
        "        measured working 2026-08-17 for a caller holding neither.\n"
        "        Two conditions still refuse it, both MEASURED, neither the "
        "freeze: it is\n"
        "        TASK-gated (no open task -> rule_id=no_active_task) and it is "
        "absent from\n"
        "        RECONNECT_ALLOWED_TOOLS (a reconnect-flagged session refuses "
        "it). If you\n"
        "        hold an open task and are not reconnect-flagged, this is your "
        "channel.\n"
        "   ai_msg(mode='send', to_roles='conductor', body=<what blocked you>)\n"
        "   ai_msg(mode='xaacp_send', session_id=<yours>, "
        "target_actor_id='conductor')\n"
        "   ai_msg(mode='inbox')  <- the reply comes back here\n"
        "        The three ai_msg lines pass the FREEZE gate, but #732 measured "
        "them refusing\n"
        "        a caller with no bound seat / XAACP actor (role='unmapped'). "
        "Try them if you\n"
        "        hold a seat; if you do not, ai_qa is your channel.\n"
        "Reporting does not lift the freeze and does not resume your work.\n"
        "NOTE: ai_issues(mode='file') is NOT available under a freeze — it "
        "commits to the repo. Use the channels above."
    )


def freeze_blast_radius_note(freeze: SessionFreeze) -> str:
    """One line saying WHO this freeze binds (#588 D1).

    The measured complaint was "frozen by an escalation that is not mine",
    and an agent had no way to tell whose lock it had hit. Now that the row
    DECLARES its scope, the deny envelope states it, so a sibling that is
    genuinely bound (a session-wide verdict) and one that is merely reading
    someone else's card can tell the two apart.
    """
    from .session_freeze_store import FREEZE_SCOPE_ACTOR, FREEZE_SCOPE_SESSION

    try:
        scope = str(freeze.freeze_scope or "")
    except AttributeError:
        # Several in-tree callers hand this function a duck-typed stand-in
        # rather than the dataclass. An object that cannot state its scope
        # gets NO scope line — inventing one would be the D5 defect in
        # miniature.
        return ""
    if scope == FREEZE_SCOPE_ACTOR:
        return "Scope:   THIS AGENT only — sibling lanes and the conductor are unaffected."
    if scope == FREEZE_SCOPE_SESSION:
        return "Scope:   the WHOLE work session — every agent in it is bound."
    return "Scope:   legacy row (minted before freeze scoping) — treated as session-wide."


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
            ttl_note=freeze_ttl_note(freeze),
        )
        # LEARNING SURFACE (2026-05-27 step 2): append the strike trail
        # to the rendered card body. Every tool call against this
        # lockdown shows the agent which attempts got them here.
        rendered = render_approval_card(card)
        _radius = freeze_blast_radius_note(freeze)
        if _radius:
            rendered = rendered + "\n" + _radius
        trail = _render_strike_trail(
            project_root,
            session_id,
            agent_context_id=str(getattr(freeze, "agent_context_id", "") or ""),
        )
        if trail:
            rendered = rendered + "\n" + trail
        # #640 — this lockdown is admin-clear-only, which makes it the card
        # MOST likely to leave an agent with nothing to do but go quiet.
        rendered = rendered + "\n" + freeze_reachable_channels_note()
        return {
            "permissionDecisionReason": rendered,
            "blocked_by": "session_frozen",
            # #571: admin-clear-only lockdowns are security-class by
            # construction. Pass the stored class when present; when absent
            # ('' on a legacy row) is_security_class fails closed to True,
            # which is the correct answer for both of these kinds anyway.
            "freeze_state": _card_state(
                card,
                freeze.kind,
                str(getattr(freeze, "verdict_class", "") or ""),
            ),
        }

    card = ApprovalCard(
        request_id=freeze.request_id,
        scope=scope,
        reason="session frozen pending operator confirmation of the original blocked action",
        consequence="Approval authorizes the NEXT exact matching retry "
        "once — re-issue the same action and it passes a "
        "single time; AIDOCS does not run it for you. Until an "
        "operator decides it, the lock stands or expires on the "
        "deadline below.",
        jurisdiction="in",
        risk_class=risk_class,
        approval_phrase=freeze.fingerprint_phrase,
        admin_paths=_admin_paths(freeze.request_id),
        self_approvable=True,
        ttl_note=freeze_ttl_note(freeze),
    )
    kind = "awaiting_self_approve" if freeze.kind == "self_approve" else "awaiting_admin_escalation"
    rendered = render_approval_card(card)
    radius = freeze_blast_radius_note(freeze)
    if radius:
        rendered = rendered + "\n" + radius
    # #640 — the card's other remedies are all operator-only. Name what the
    # AGENT reading this can still do, so a freeze never buys silence.
    rendered = rendered + "\n" + freeze_reachable_channels_note()
    return {
        "permissionDecisionReason": rendered,
        "blocked_by": "session_frozen",
        # #571: carry the ROW's stored class, not a re-derivation. The class was
        # decided once, at mint, from the refusal that actually fired; deriving
        # it again here (from a re-rendered card) would let the two disagree.
        "freeze_state": _card_state(
            card,
            kind,
            str(getattr(freeze, "verdict_class", "") or ""),
        ),
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
        ttl_note=str(ac.get("ttl_note") or ""),
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
