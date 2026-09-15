"""Universal tool-call notification injector — emperor 2026-05-07.

The Emperor's directive: notifications must surface on EVERY tool call,
not just `@renders_as`-wrapped ones. Tools returning plain dicts
(session_connect, conductor_*, lane_*, etc.) silently bypassed the
existing tool_display.py drain because it only fires inside
`@renders_as` and `text_result`.

Single chokepoint: monkey-patch `server.tool` AFTER FastMCP init so
every subsequent `@server.tool(...)` registration is wrapped. The
wrapper:
  1. Calls the original tool function.
  2. Peeks pending run_notifications + lane_completion_reviews for
     this conductor's session.
  3. Augments the return value WITHOUT changing its shape semantics:
       - ToolResult: append a TextContent block.
       - dict: add a `_notifications` key with the formatted block.
       - str: prepend the formatted block.
       - list/scalar/None: wrap with a notifications field.

Notifications PERSIST until satisfied (run_notifications.dismiss_run on
output read; lane_completion_reviews status flips on conductor verdict).
That's the 'until satisfied' lifecycle.

Workers fenced via the identity seam (task_actor_identity.
resolve_task_actor: env stamp / principal / #217 registry) — they
don't surface their parent conductor's notifications.

Best-effort: any failure in injection passes the original return
through unchanged. Tool semantics never break for a notification glitch.
"""

from __future__ import annotations

import functools
import inspect
import os
from pathlib import Path
from typing import Any


#: What each CALLER has already been told: {caller_key: reason_last_warned}.
#:
#: PER CALLER, NOT PER PROCESS. The daemon is shared — one process serves every
#: window, session and lane — so a process-wide set meant the FIRST caller to
#: hit an outage consumed the only warning and every other caller was told
#: nothing. That is the #1033 false green again, one layer up: silence that
#: looks like health because somebody else already got the message.
#:
#: CLEARED BY A SUCCESSFUL READ, not by a timer. The state tracked is "this
#: caller has an unacknowledged outage", and the only honest evidence it has
#: ended is an inbox that actually answered. Recovery is therefore silent
#: (nothing to say) but it RE-ARMS the warning, so an outage / recovery /
#: outage cycle speaks twice instead of the second one being swallowed as a
#: duplicate of the first.
_XAACP_UNAVAILABLE_SEEN: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════════
# THE ONE CANONICAL NOTIFICATION COMPOSITION AUTHORITY
# (empire law `promoted-9f9756ad9462`, doctrine empire/the-notification-rail.md)
# ═══════════════════════════════════════════════════════════════════════
#
# THERE IS EXACTLY ONE COMPOSER. `compose_notices` below is it. Every other
# surface that wants to put a notice in front of an agent is a CALLER of this
# module, never a second collector:
#
#   tool_display._append_pending_notifications  RETIRED 2026-07-12 — already a
#       no-op (`return`, tool_display.py:2033) and pinned as one by
#       tests/runtime/test_run_notifications_drain_layer_isolation.py. It is NOT
#       a live appender; its docstring describes the drain it USED to do.
#   prompt_mutator.notifications_drain          now DELEGATES here (it used to
#       peek run_notifications itself — a genuine second emitter on the hook
#       path; see the note on that function).
#   outer_gate_transport._with_webmcp_update_notifications
#       PROVABLY DISJOINT: its only producer is `_webmcp_update_intent_blocks`
#       -> update_intent_hook.process_user_prompt, gated on an explicit
#       `params.prompt`. It touches none of the event classes collected here.
#       It now renders THROUGH `render_notice_payload` so the kind taxonomy and
#       the must_act/informational separation hold on the WebMCP surface too.
#
# WHY THIS ORDER MATTERS: a rendering fix applied before the emitters were
# unified would have made duplicate DELIVERY worse, not better (r0b's ruling).

#: The four kinds. The kind is carried IN THE PAYLOAD, structurally separated —
#: not merely labelled in prose inside one undifferentiated blob. An agent that
#: cannot tell a veto from a weather report eventually treats both as weather.
KIND_MUST_ACT = "must_act"
KIND_STATE = "state"
KIND_INFRA = "infra"
KIND_INFO = "info"

#: Every kind, for validation and for the registry-derived coverage test.
NOTICE_KINDS = (KIND_MUST_ACT, KIND_STATE, KIND_INFRA, KIND_INFO)

#: Kinds that are ADVISORY — they may share one rendered block.
INFORMATIONAL_KINDS = (KIND_STATE, KIND_INFRA, KIND_INFO)

#: The payload key carrying enforced-workflow traffic. SEPARATE FROM
#: `_notifications` ON PURPOSE: mixing a must_act into the informational blob is
#: the exact defect this law was written to kill.
MUST_ACT_KEY = "_must_act"
NOTIFICATIONS_KEY = "_notifications"


class Notice:
    """One notice, with its KIND and its DURABLE dedup identity.

    `dedup_key` IS NOT THE RENDERED TEXT. That distinction is the whole fix for
    the measured gate-health repeat: `dedupe_state_notice` hashed the rendered
    banner, and the banner embeds a rolling 60-minute decline COUNT
    (gate_health.py:478-483, `f"{recent} hook decline/failure breadcrumb(s)"`).
    Every time that count moved, the hash moved, the ledger called it a NEW
    state, and the "unchanged" banner re-fired. A dedup key must be the SEMANTIC
    state — here, the probe status tuple — never the prose that describes it.
    """

    __slots__ = ("kind", "dedup_key", "text", "once", "claims")

    def __init__(
        self,
        kind: str,
        dedup_key: str,
        text: str,
        *,
        once: bool = True,
    ) -> None:
        if kind not in NOTICE_KINDS:
            msg = f"unknown notice kind {kind!r}; expected one of {NOTICE_KINDS}"
            raise ValueError(msg)
        self.kind = kind
        self.dedup_key = dedup_key
        self.text = text
        #: False ONLY for a surface that owns its own surface-count lifecycle
        #: (run_notifications' max_displays, deploy notices' per-epoch cap).
        #: Those already anchor "once" durably in their own tables; wrapping a
        #: second ledger around them would fight their cap, not reinforce it.
        self.once = once
        #: #1061 two-phase delivery: ((session_id, actor_id, claim_ids), ...)
        #: confirmed by `confirm_surfaced_claims` only once rendered.
        self.claims: tuple = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Notice(kind={self.kind!r}, dedup_key={self.dedup_key!r})"


def announce_once(
    project_root: Path,
    scope: str,
    notices: list[Notice],
) -> list[Notice]:
    """Drop every notice whose dedup_key this scope has ALREADY been told.

    ANCHORED DURABLY, in sqlite, via the estate's existing censused
    notify-on-change ledger (`session_response_ledger.should_emit`). In-process
    dedup is not an option and never was: this box flips runtime generations and
    respawns brokers constantly, and an in-memory set re-announces the entire
    backlog as brand new on every flip.

    WHY NOT A NEW TABLE: the former `conductor_comms.xaacp_notice_once` claimed
    atomically by INSERT-OR-IGNORE rowcount (retired in #1061 -- XAACP delivery is
    the claim/surface rail), but it was bound to `msg_reads` and message ids. A
    dedicated table for this rail would have to be census-classified in
    `canonical_taxonomy`, which is not a file
    this lane may write — so the censused ledger is reused. The residual gap is
    NAMED, not laundered: `should_emit` uses ON CONFLICT UPDATE rather than an
    INSERT claim, so two genuinely concurrent tool calls in the same scope can
    both win the race and announce the same notice twice.

    FAIL-OPEN is inherited deliberately (session_response_ledger.py:197-199): a
    ledger failure EMITS. A real veto must never be lost to bookkeeping. The
    cost is that sqlite lock contention degrades dedup to no dedup, which is a
    second named cause of the measured repeats.
    """
    if not notices:
        return []
    scope = str(scope or "").strip()
    if not scope:
        # No identity to dedup against. Fail open (emit) rather than silently
        # swallowing traffic for an unidentified caller.
        return list(notices)
    try:
        from .session_response_ledger import dedupe_state_notice
    except Exception:
        return list(notices)
    fresh: list[Notice] = []
    for notice in notices:
        if not notice.once:
            fresh.append(notice)
            continue
        try:
            emitted = dedupe_state_notice(
                project_root,
                scope,
                f"rail:{notice.kind}:{notice.dedup_key}",
                # The ledger hashes this value as "the state". Feed it the
                # SEMANTIC key, never the rendered prose.
                notice.dedup_key or notice.kind,
            )
        except Exception:
            emitted = notice.text
        if emitted:
            fresh.append(notice)
    return fresh


def render_notice_payload(notices: list[Notice]) -> dict[str, Any]:
    """Render typed notices into a STRUCTURALLY SEPARATED payload.

    Returns at most two keys:
      `_notifications`  one joined block of state/infra/info prose.
      `_must_act`       a LIST of {kind, key, text} — enforced-workflow traffic,
                        never concatenated into the informational block.

    An empty dict when there is nothing to say (no-padding law).
    """
    payload: dict[str, Any] = {}
    must_act = [n for n in notices if n.kind == KIND_MUST_ACT]
    informational = [n for n in notices if n.kind in INFORMATIONAL_KINDS]
    info_text = "\n\n".join(n.text for n in informational if n.text)
    if info_text:
        payload[NOTIFICATIONS_KEY] = info_text
    if must_act:
        rendered = [
            {"kind": n.kind, "key": n.dedup_key, "text": n.text}
            for n in must_act
            if n.text
        ]
        if rendered:
            payload[MUST_ACT_KEY] = rendered
    return payload


def _xaacp_caller_key(agent_context_id: str, session_id: str, lane_id: str) -> str:
    """The axis an outage notice is deduped on: WHO is being told.

    THE FIRST CUT USED `session_id|lane_id` AND THAT IS NOT A CALLER. An AIDOCS
    session is a work LABEL — two Claude windows, two dashboard tabs, a host and
    a subagent can all resolve the SAME session_id while being different agents
    with different screens. Keying on it means the first window to see an outage
    silently suppresses the warning for every other one, and the agent that
    never saw it cannot know its cross-surface mail is dark. Dedupe was supposed
    to stop repetition to ONE reader, not stop the second reader being told.

    `agent_context_id` is the derived per-agent axis
    (host_kind + project_root + host_session_id, deliberately excluding the work
    session) and it is already threaded into this function for exactly this
    class of decision — `freeze_strike_notice_store.surface_pending` takes it a
    few blocks below. THE CODEBASE HAD ALREADY MADE THIS CORRECTION TWICE: the
    CHANGELOG records "re-key strike scope onto derived agent_context_id" and
    "re-key session freeze onto derived agent_context_id". Both are security
    surfaces that must not leak across agents. So is this one.

    Session and lane remain as ADDITIONAL discrimination, not as the identity:
    one agent working two lanes really does have two different XAACP inboxes,
    and an outage on one is not news about the other.

    NO IDENTITY MEANS NO DEDUPE AT ALL — returns "" (#1039).

    The first fallback here substituted `anon:<session_id>`, reasoning that it
    "over-warns rather than under-warns". IT DOES NOT. Every caller that cannot
    resolve an agent_context_id in the same session collapses onto that ONE key,
    so the first anonymous agent to see an outage silences every other anonymous
    agent — the exact defect this function was written to fix, reintroduced in
    its own fallback branch, one paragraph below the explanation of why it is
    wrong.

    A synthesized key is a GUESS AT IDENTITY, and dedupe is only ever safe when
    the identity is real. Absent it, the honest answer is not a worse key but no
    key: an empty string disables suppression, so every unidentified caller is
    told every time. Repetition is a cost paid in one line per call; silence
    costs the message, and the reader cannot tell it happened.
    """
    axis = (agent_context_id or "").strip()
    if not axis:
        return ""  # unidentified caller -> never suppress
    return f"{axis}|{(session_id or '').strip()}|{(lane_id or '').strip()}"


def _resolve_agent_context_id(project_root: Path, agent_context_id: str) -> str:
    """Last-resort DERIVATION before conceding that identity is unavailable.

    #1039 made "no exact identity" mean "never suppress", which is right — but
    it also made GIVING UP EARLY expensive, because every unidentified caller
    is warned on every call. The complaint #1033 makes about that noise is
    fair: "a rail nobody reads is the false green in a louder costume".

    Both are satisfied by conceding less often. The rail is HANDED an
    agent_context_id by its caller; when that arrives empty the identity is
    usually still RESOLVABLE from the same request-scoped helpers the XAACP
    dispatch itself uses. So ask the canonical deriver before giving up.

    `derive_agent_context_id` refuses to fabricate: an empty host_kind or
    host_session_id yields "", never an "unknown" bucket that would collide
    every kind-less host into one identity. That refusal is exactly what makes
    it safe to use here — a derived id is a real id or it is nothing, and
    nothing still means never suppress.

    It also includes `agent_id`, the subagent link, so N concurrent subagents
    do not collapse into their shared parent's identity — the same collapse
    that once scored three lane agents' strikes against one conductor.
    """
    axis = (agent_context_id or "").strip()
    if axis:
        return axis
    try:
        from .agent_memory_epoch import derive_agent_context_id
        from .mcp_server_runtime_helpers import (
            current_calling_agent_id,
            current_calling_host_kind,
            current_calling_host_session_id,
        )

        return (
            derive_agent_context_id(
                host_kind=str(current_calling_host_kind() or ""),
                project_root=project_root,
                host_session_id=str(current_calling_host_session_id() or ""),
                agent_id=str(current_calling_agent_id() or "") or None,
            )
            or ""
        )
    except Exception:  # noqa: BLE001 — an unresolvable identity is not an error
        return ""


def _xaacp_unavailable_text(reason: str, *, deduped: bool = True) -> str:
    """The notice itself, shared by the deduped and never-deduped paths.

    THE CLOSING SENTENCE IS PER-CASE BECAUSE IT IS A PROMISE ABOUT REPETITION.
    "Shown once per reason" is true for a caller we can identify and FALSE for
    one we cannot — #1039 deliberately repeats to unidentified callers, since
    suppression without a real identity silences readers it was never told
    about. Printing the same closing line to both would make the notice lie to
    the reader who is about to see it again on the next call.
    """
    tail = (
        "Shown once per reason."
        if deduped
        else (
            "Repeated on every call: this surface could not resolve WHICH agent "
            "is asking, and suppressing a warning for a caller we cannot name "
            "risks silencing a different one."
        )
    )
    return (
        "⚠️ XAACP DELIVERY UNAVAILABLE — cross-surface messages are NOT being "
        f"checked on this surface: {reason}. An inbox that cannot be read is "
        "not an empty inbox; queued messages stay queued on the gate (nothing "
        "is marked read), so nothing is lost — but nothing will be delivered "
        "here until this is resolved. If it names an expired or rejected "
        f"credential, sign in again from the Dashboard. {tail}"
    )


def _xaacp_delivery_ok(caller_key: str) -> None:
    """A successful inbox read clears this caller's outage state.

    Silent by design: recovery is not news. What it buys is that the NEXT
    outage is news again.
    """
    _XAACP_UNAVAILABLE_SEEN.pop(caller_key or "", None)


def _xaacp_unavailable_notice(reason: str, caller_key: str | None = None) -> str:
    """The warning that replaces a silently-empty inbox, or "" if already said.

    Deliberately NOT an error: delivery being unavailable does not make the
    caller's tool call wrong, and failing it would punish the operator for an
    outage they did not cause. The tool succeeds; the rail tells the truth.
    """
    key = (reason or "").strip().lower()[:200]
    if not key:
        return ""
    # THREE STATES, NOT TWO — and the difference is who is making a claim.
    #
    #   caller_key=None   NOBODY CLAIMED ANYTHING. The caller did not pass a
    #                     key at all, so this is a direct/legacy invocation
    #                     rather than the rail. Dedupe by REASON, which is
    #                     #1033's contract: "a persistent outage must not stamp
    #                     every result — a rail nobody reads is the false green
    #                     in a louder costume."
    #   caller_key=""     THE RAIL LOOKED AND FOUND NOTHING. It was handed an
    #                     agent_context_id, tried `_resolve_agent_context_id`,
    #                     and the canonical deriver REFUSED to fabricate one.
    #                     Suppressing here would silence a reader we cannot
    #                     name — #1039. Warn every time, store nothing.
    #   caller_key=<id>   A real caller. Dedupe per caller, per reason.
    #
    # THE TWO RULINGS ONLY COLLIDED WHILE THOSE MIDDLE TWO WERE ONE VALUE.
    # Distinguishing them is not a loophole: an absent argument and a resolved
    # absence are genuinely different facts, and the earlier code lost that
    # distinction by defaulting to "".
    if caller_key is None:
        who = f"__unclaimed__:{key}"
    elif not caller_key.strip():
        return _xaacp_unavailable_text(reason, deduped=False)
    else:
        who = caller_key
    # A CHANGED REASON IS NEWS even for a caller already warned: expired ->
    # latched -> unreachable are different facts with different remedies.
    if _XAACP_UNAVAILABLE_SEEN.get(who) == key:
        return ""
    _XAACP_UNAVAILABLE_SEEN[who] = key
    return _xaacp_unavailable_text(reason)


def _is_worker_caller() -> bool:
    """Lane workers must NOT see conductor notifications.
    Their lane plan / handoff path delivers run results separately.

    #360/#356: worker-ness comes from the ONE identity seam
    (``task_actor_identity.resolve_task_actor`` — spawn-path env stamp,
    resolved principal type, per-process latch, or the #217 registry
    chain), not the env stamp alone. A subagent whose process lost the
    env stamp (e.g. a host-spawned subagent sharing the conductor's MCP
    process) previously drained the CONDUCTOR's session rail — the #356
    leak. Fail-open to the env check so a seam hiccup never changes the
    conductor path.
    """
    if os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
        return True
    try:
        from .mcp_server_runtime_helpers import resolve_project_root
        from .task_actor_identity import resolve_task_actor

        return bool(resolve_task_actor(resolve_project_root())[2])
    except Exception:
        return False


def _resolve_drain_identity() -> tuple[Path | None, str, str, str]:
    """Return project, work session, actor context, and lane for this call."""
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_agent_context_id,
            current_calling_host_session_id,
            resolve_project_root,
        )

        project_root = resolve_project_root()
        actor_id = current_calling_agent_context_id(project_root)
    except Exception:
        return None, "", "", ""
    lane_id = os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    try:
        from . import managed_mode_service as _mm

        _sid = _mm.resolve_managed_session(
            _mm.ManagedModeService(),
            project_root,
            host_session_id=current_calling_host_session_id(),
        )
        if _sid:
            return (project_root, _sid, actor_id, lane_id)
    except Exception:
        pass
    return project_root, "", actor_id, lane_id


def _worker_mailbox_id() -> str:
    """The lane worker's OWN registry id, stamped into the subprocess env
    by agent_expert_service (worker_env["AIDOCS_EXPERT_ID"]). Empty for a
    conductor — the conductor has no mailbox to drain here."""
    return os.environ.get("AIDOCS_EXPERT_ID", "").strip()


def _collect_worker_mailbox_blocks(project_root: Path) -> list[str]:
    """#1005: drain THIS worker's lane mailbox on every tool call.

    A `claude -p` worker runs one long turn, so the UserPromptSubmit
    intercept (prompt_mutator.worker_lane_intercept) fires once and never
    again; a conductor ai_lane(send) issued mid-turn was never seen. This
    rail rides every tool call, so the worker's own rows are delivered
    here. take() stamps consumed_at exactly once per row (Merkle-audited
    lane_mailbox_consume), so a row is never re-delivered. Keyed on the
    worker's own env id only — never another worker's rows, never a
    conductor. Independent of any managed session_id.
    """
    worker_id = _worker_mailbox_id()
    if not worker_id:
        return []
    try:
        from .lane_mailbox_store import LaneMailboxStore

        store = LaneMailboxStore()
        try:
            store.expire_stale(project_root)
        except Exception:
            pass
        lines: list[str] = []
        for _ in range(20):  # bounded drain; FIFO per worker
            msg = store.take(project_root, worker_id=worker_id)
            if msg is None:
                break
            lines.append(
                f"[AIDOCS mailbox] conductor sent task "
                f"(mailbox_id={msg['mailbox_id']}): {msg['prompt']}"
            )
        if not lines:
            return []
        lines.append(
            "Act on the conductor's instruction above within this turn; "
            "report via `mcp__aidocs__ai_task(mode='update', ...)` when done."
        )
        return ["\n".join(lines)]
    except Exception:
        return []


#: #1061: the surface name recorded on a delivery claimed by this rail.
STREAM_NOTICE_SURFACE = "tool_result_notice"
#: Bounded per tool result; the rest stay undelivered for the next call.
STREAM_NOTICE_BATCH = 20


def _collect_xaacp_stream(
    project_root: Path,
    *,
    expected_session_id: str | None,
) -> dict[str, Any]:
    """#1061: claim this caller's undelivered stream events for a tool result.

    ONE RECEIVE SOURCE. Every entry is claimed through
    `conductor_comms.xaacp_stream_claim_next(surface='tool_result_notice')` --
    the SAME server-held compare-and-set delivery claim `wait_next` takes, on
    the local store or forwarded to the cloud authority (stream-core owns the
    forwarding) -- so an event surfaced here is never handed to a parked
    wait_next and vice versa. The claim is durable, so a restart or generation
    flip cannot re-surface a confirmed event (no in-memory once-set). A claim
    is DELIVERY only: it never marks read and never decides.

    TWO-PHASE: each entry carries a `claim_id`; the notice records them and
    `confirm_surfaced_claims` confirms them only after the text is placed in
    the returned tool result. An unconfirmed claim is lease-bounded and
    becomes claimable again (stream-core's lease expiry).

    IDENTITY IS SERVER-DERIVED: the reader is `xaacp_resolve_caller_route`
    (host session + host agent id + lane registry), never a caller-supplied
    session or actor. A Claude Code subagent resolves to its own `subagent`
    actor (the xaacp_directory row), a lane worker to its lane route.
    `expected_session_id` (conductor path) must match the route's session, or
    nothing is claimed; `None` (worker path) trusts only the derived route.

    Returns {"ok": True, "notices": [...]} or a named non-ok status
    ({"ok": False, "status": "unavailable"|"refused"|..., "error": ...}).
    """
    from . import conductor_comms as _cc

    route = _cc.xaacp_resolve_caller_route(project_root) or {}
    actor = str(route.get("actor_id") or "").strip()
    sid = str(route.get("session_id") or "").strip()
    if not actor or not sid:
        return {"ok": False, "status": "forbidden", "error": "no canonical XAACP route"}
    if expected_session_id is not None and sid != str(expected_session_id).strip():
        return {"ok": False, "status": "forbidden", "error": "route session mismatch"}
    kind = str(route.get("actor_kind") or "").strip()
    lane = str(route.get("lane_id") or "").strip()
    if kind == "worker" and not lane:
        return {"ok": False, "status": "forbidden", "error": "worker route without lane"}
    claimed = _cc.xaacp_stream_claim_next(
        project_root,
        session_id=sid,
        actor_id=actor,
        lane_id=lane,
        reader_actor_kind=kind,
        surface=STREAM_NOTICE_SURFACE,
        limit=STREAM_NOTICE_BATCH,
    )
    if not isinstance(claimed, dict) or claimed.get("ok") is not True:
        # A named status from the authority (unavailable / refused /
        # forbidden) -- never an empty success (#1033).
        claimed = claimed if isinstance(claimed, dict) else {}
        return {
            "ok": False,
            "status": str(claimed.get("status") or "unavailable"),
            "error": str(claimed.get("error") or ""),
        }
    entries = [e for e in (claimed.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return {"ok": True, "notices": []}
    text = _cc.xaacp_format_block(entries)
    if not text:
        return {"ok": True, "notices": []}
    key = ",".join(str(e.get("notice_id") or e.get("id") or "") for e in entries)
    claim_ids = tuple(str(e.get("claim_id") or "") for e in entries if e.get("claim_id"))
    # once=False: the durable CAS claim above IS this surface's once-per-event.
    notice = Notice(KIND_MUST_ACT, f"xaacp:{key}", text, once=False)
    notice.claims = ((sid, actor, claim_ids),) if claim_ids else ()
    return {"ok": True, "notices": [notice]}


def confirm_surfaced_claims(project_root: Path, notices: list[Notice]) -> None:
    """Phase two: the notice text IS in the returned tool result -- confirm it.

    Called ONLY after `augment_return_typed` has produced the result that is
    returned. A rendering/injection exception skips this, so the claim stays
    unconfirmed and stream-core's lease expiry makes it claimable once more.
    """
    from . import conductor_comms as _cc

    for notice in notices:
        for sid, actor, claim_ids in getattr(notice, "claims", ()) or ():
            if claim_ids:
                _cc.xaacp_stream_confirm_surfaced(
                    project_root,
                    session_id=sid,
                    actor_id=actor,
                    claim_ids=list(claim_ids),
                )


def _worker_stream_notices(project_root: Path) -> list[Notice]:
    """#1061 worker branch: ONLY the stream collector, for the worker's own route."""
    try:
        routed = _collect_xaacp_stream(project_root, expected_session_id=None)
    except Exception:  # noqa: BLE001 -- delivery must not break the tool
        return []
    return list(routed.get("notices") or []) if routed.get("ok") else []

def compose_notices(
    project_root: Path,
    session_id: str,
    agent_context_id: str,
    lane_id: str,
    *,
    host_session_id: str | None = None,
) -> list[Notice]:
    """THE canonical composer. Every notice the estate shows an agent on a tool
    result is built here, and each one DECLARES ITS KIND at its source.

    The kind is never sniffed out of rendered prose afterwards — that is the
    "labelled in a blob" anti-pattern the law forbids. Each collector below
    states what its traffic IS:

      must_act  addressed messages, lane verdicts, strikes, the backlog
                INSTRUCTION — traffic that demands a specific action.
      state     a fact that changed (gate health, hook wiring, deploy window).
      infra     a background event completed (background runs).
      info      advisory (durable-capture hints).

    `once=False` marks the surfaces that ALREADY own a durable surface-count
    lifecycle of their own (run_notifications.max_displays, the deploy per-epoch
    cap, the strike 3-surface cap, the durable-hint 2-surface cap). Wrapping the
    announce-once ledger around those would fight their cap instead of
    reinforcing it; they are durable already, just on their own terms.
    """
    notices: list[Notice] = []
    # #1005: the worker's own mailbox rides BEFORE the session gate — a
    # lane worker resolves no managed session_id, and its mailbox must
    # not depend on one. Addressed traffic is must_act.
    for _wm in _collect_worker_mailbox_blocks(project_root):
        notices.append(Notice(KIND_MUST_ACT, f"worker_mailbox:{_hash_key(_wm)}", _wm))
    if not session_id:
        return notices
    # Run-done notifications. Phoenix 2026-05-10: surface_for_session
    # bumps each record's surfaced_count and auto-dismisses it when
    # the count hits `notifications.max_displays`. Default 3 — three
    # surfaces, then the record drops itself from the queue even if
    # the agent never read the output. max_displays=0 → classic
    # 'until satisfied' behavior (notification persists forever
    # until ai_run_output dismisses it).
    #
    # OPERATOR-MEASURED DEFECT (`r_34c5200a55cc3517` announced on THREE separate
    # tool results): that is max_displays=3 working exactly as written. "Three
    # surfaces" was the OLD contract; the law now says ONCE PER EVENT. The cap
    # is no longer the dedup — `announce_once` is, keyed on the run id, so a
    # completed run announces once and the max_displays cap only bounds what
    # happens if the ledger ever fails open.
    try:
        from . import run_notifications as _rn

        try:
            from .config import get_setting

            _max_displays = int(
                get_setting(
                    "notifications.max_displays",
                    project_root=project_root,
                    default=3,
                )
                or 0,
            )
        except Exception:
            _max_displays = 3
        pending_runs = _rn.surface_for_session(
            project_root,
            session_id=session_id,
            agent_context_id=agent_context_id,
            lane_id=lane_id,
            max_displays=_max_displays,
        )
        if pending_runs:
            # ONE NOTICE PER RUN, keyed on the run id — not one lumped block.
            # The operator measured "two run notices concatenated and then
            # re-carried on the following call"; a lumped block cannot be
            # deduped per event, only per accidental concatenation order.
            for _run in pending_runs:
                _rid = str(
                    (_run or {}).get("run_id")
                    or (_run or {}).get("id")
                    or "",
                ).strip()
                _text = _rn.format_block([_run])
                if not _text:
                    continue
                notices.append(
                    Notice(
                        KIND_INFRA,
                        f"run_done:{_rid or _hash_key(_text)}",
                        _text,
                    ),
                )
    except Exception:
        pass
    # Lane completion reviews. OR-match on session_id + host_session_id
    # so a conductor that swapped sessions mid-run still sees their
    # pending reviews. Orphan rows missing one field but matching the
    # other still reach their owner. A pending VERDICT is must_act.
    try:
        from . import lane_completion_review_store as _lcr

        # WHOEVER KNOWS THE HOST SESSION ID MUST BE ABLE TO SAY SO. The MCP rail
        # has no argument for it and reads the calling thread; the HOOK path
        # receives it in its payload, and on that path the thread-local is a
        # DIFFERENT value (often empty). Delegating the hook path here without
        # this override silently changed the OR-match input and broke
        # test_prompt_mutator_parity::test_lane_completion_reviews_or_matched —
        # which is the contract that lets a conductor who swapped sessions
        # mid-run still see its pending reviews.
        if host_session_id is not None:
            host_sid = str(host_session_id or "")
        else:
            try:
                from .mcp_server_runtime_helpers import (
                    current_calling_host_session_id,
                )

                host_sid = current_calling_host_session_id()
            except Exception:
                host_sid = ""
        pending_reviews = _lcr.pending_for_session(
            project_root,
            session_id=session_id,
            host_session_id=host_sid,
        )
        if pending_reviews:
            _text = _lcr.format_pending_block(pending_reviews)
            if _text:
                notices.append(
                    Notice(
                        KIND_MUST_ACT,
                        "lane_review:"
                        + ",".join(
                            sorted(
                                str((r or {}).get("id") or "")
                                for r in pending_reviews
                            ),
                        ),
                        _text,
                    ),
                )
    except Exception:
        pass
    # Messages — role-addressed comms available to all agents. must_act: a
    # message addressed to this seat is awaiting a reply, and a Consul's veto
    # arrives on this path.
    try:
        from . import conductor_comms as _cc

        role = _cc.msg_resolve_caller_role(project_root)
        # #215: only a bound seat has an inbox. An unmapped caller resolves to a
        # non-seat role — skip the read (msg_inbox rejects it anyway) so a lane
        # worker never drains a seat inbox and we avoid a per-turn exception.
        if role in _cc.MSG_ROLES:
            pending_chat = _cc.msg_inbox(
                project_root,
                role=role,
                unread_only=True,
                mark_read=True,
            )
            if pending_chat:
                _text = _cc.msg_format_block(pending_chat)
                if _text:
                    # mark_read=True above already consumes these, so this
                    # surface anchors its own "once" in msg_reads. once=False
                    # avoids a second, redundant ledger write per call.
                    notices.append(
                        Notice(
                            KIND_MUST_ACT,
                            f"msg:{role}:{_hash_key(_text)}",
                            _text,
                            once=False,
                        ),
                    )
    except Exception:
        pass
    # XAACP — actor-routed cross-surface messages. This is the portable delivery
    # floor: a message sent while the target is busy/offline appears on its NEXT
    # governed tool result. The call goes through xaacp_dispatch, so a cloud-bound
    # local/remote agent reads the gate authority while the gate itself reads its
    # canonical copy. Delivery and wake remain separate truths.
    try:
        # #1061 ONE RECEIVE SOURCE, LOCAL AND CLOUD-BOUND ALIKE: the rail CLAIMS
        # deliveries through `xaacp_stream_claim_next` (surface
        # 'tool_result_notice') -- the same server-held compare-and-set wait_next
        # uses, on whichever store is the authority -- so one event can never
        # surface both here and on a parked wait_next. There is NO inbox +
        # notice_once fallback any more. A claim is delivery only (never read,
        # never decision) and is two-phase: confirmed only once the text is in
        # the returned tool result (`confirm_surfaced_claims`).
        routed = _collect_xaacp_stream(project_root, expected_session_id=session_id)
        # "COULD NOT ASK" IS NOT "NOTHING TO SAY" (#1033): an unavailable or
        # refused claim is a named status that warns; it never reads as empty.
        # WHO is being told — see _xaacp_caller_key (derived agent_context_id,
        # never session|lane).
        _axis = _resolve_agent_context_id(project_root, agent_context_id)
        _caller_key = _xaacp_caller_key(_axis, session_id, lane_id)
        if isinstance(routed, dict) and routed.get("ok"):
            # THE CLAIM ANSWERED — clear this caller's outage state so the NEXT
            # failure is news again, whether or not anything was delivered.
            _xaacp_delivery_ok(_caller_key)
            notices.extend(routed.get("notices") or [])
        else:
            # WHICH FAILURES ARE AN OUTAGE, AND WHICH ARE JUST "NOT YOU".
            #
            # The first cut warned on every non-ok status and was too broad: a
            # caller with no canonical host identity gets `forbidden`, which is
            # not a delivery failure at all — nothing was ever going to be
            # delivered to a non-participant, and telling every such caller
            # about a channel they do not use is the noise that trains a reader
            # to skip this rail. Measured: it fired on an ordinary local tool
            # call and broke test_notification_no_dupe.
            #
            #   unavailable  the credential or the transport failed. We WOULD
            #                have delivered and could not. This is the #1033
            #                false green — warn.
            #   refused      the gate actively refused (scope/permission). Mail
            #                is being blocked, not absent — warn.
            #   forbidden    this caller is not an XAACP route. Silent.
            #   invalid      the injector built a bad request; that is our bug,
            #                and not something the operator can act on. Silent.
            status = str(routed.get("status") or "").strip() if isinstance(routed, dict) else ""
            if status in ("unavailable", "refused"):
                reason = str(routed.get("error") or status).strip()
                notice = _xaacp_unavailable_notice(reason or status, _caller_key)
                if notice:
                    # A DELIVERY OUTAGE IS A STATE, NOT AN OBLIGATION. Keyed on
                    # the reason so a changed failure mode is news again.
                    # once=False — `_xaacp_unavailable_notice` ALREADY owns this
                    # surface's dedup, and its contract is deliberately NOT
                    # "announce once": it is per-CALLER, cleared by a successful
                    # read (so outage/recovery/outage speaks twice), and it
                    # warns an UNIDENTIFIED caller EVERY SINGLE TIME on purpose —
                    # an unidentified caller cannot be safely deduped, because
                    # one such caller would otherwise silence every other.
                    # Pinned by test_xaacp_rail_composition_1038::
                    # TestUnidentifiedCallersAreNeverDeduped. Layering the rail
                    # ledger on top suppressed exactly those warnings.
                    notices.append(
                        Notice(
                            KIND_STATE,
                            f"xaacp_outage:{_caller_key}:{reason or status}",
                            notice,
                            once=False,
                        ),
                    )
    except Exception as exc:  # noqa: BLE001 -- delivery must not break the tool
        # Even the EXCEPTION path speaks now. It previously vanished into
        # `pass`, which is how a broken dispatch became indistinguishable from
        # a quiet one.
        _ck = _xaacp_caller_key(
            _resolve_agent_context_id(project_root, agent_context_id),
            session_id,
            lane_id,
        )
        notice = _xaacp_unavailable_notice(f"{type(exc).__name__}", _ck)
        if notice:
            # once=False for the same reason as the status branch above:
            # `_xaacp_unavailable_notice` owns this surface's dedup contract.
            notices.append(
                Notice(
                    KIND_STATE,
                    f"xaacp_outage:{_ck}:{type(exc).__name__}",
                    notice,
                    once=False,
                ),
            )
    # Deploy phase-2 SAFE-TO-EDIT (#326): while a crown deploy is PAST its local
    # gates, surface "live tree free to edit; do NOT move HEAD" on every tool
    # call until the deploy clears its marker OR the per-epoch surface cap
    # (3, operator 2026-07-16) is reached — told, then it stops nagging. The
    # gate still owns the marker's lifetime; the cap only bounds repetition.
    try:
        from .deploy_edit_window import safe_to_edit_notice

        _ste = safe_to_edit_notice(project_root, session_id=session_id)
        if _ste:
            # once=False: deploy_edit_window owns a durable per-epoch cap.
            notices.append(
                Notice(KIND_STATE, "deploy_safe_to_edit", _ste, once=False),
            )
    except Exception:
        pass
    # Deploy FAIL SUMMARY (2026-07-13): #271 made the exit codes valuable (class +
    # step + reason + artifact link) but printed them ONLY to the deploy's stdout —
    # the harness surfaces just "failed with exit code 11", so the operator had to
    # look up a number to learn what broke. Surface the HUMAN-READABLE summary on
    # this same rail. Project-scoped; cleared when the next deploy starts; fail-quiet.
    try:
        from .deploy_edit_window import deploy_failure_notice

        _fail = deploy_failure_notice(project_root, session_id=session_id)
        if _fail:
            notices.append(
                Notice(KIND_STATE, "deploy_failure", _fail, once=False),
            )
    except Exception:
        pass
    # HOOK BOOTSTRAP (#364): the hook's own self-repair rides the hooks it
    # repairs — remove the `hooks` key from ~/.claude/settings.json and no
    # hook ever fires again, so self-repair is unreachable and the host-level
    # gate stays OFF forever. THIS rail is the one surface that survives hook
    # death (the agent still calls MCP tools), so every governed call for a
    # commissioned project verifies the host hook wiring and HEALS it via the
    # canonical installer (`aidocs setup` code path, Article VI) — or, when
    # healing is impossible, refuses LOUDLY with the exact repair command.
    # Healthy path is a memoized single os.stat. Fail-quiet on crash — the
    # gate_health hook-silence probe below remains the independent backstop.
    try:
        from .hook_bootstrap import ensure_host_hooks

        _hb = ensure_host_hooks(project_root)
        if _hb:
            # #474 notify-on-change. THE KEY INCLUDES THE BANNER TEXT HERE, and
            # that is NOT the gate-health defect repeated — the two surfaces
            # genuinely differ, and conflating them breaks one or the other:
            #
            #   gate_health  its banner interpolates a ROLLING 60-MINUTE DECLINE
            #                COUNT (gate_health.py:478-483). The text changes
            #                while the state does not, so a text key re-fires
            #                forever. It must key on the probe status tuple.
            #   hook_bootstrap  its notice has NO volatile component; the text IS
            #                the description of the wiring state, and "degraded
            #                FURTHER" shows up only as changed text under an
            #                unchanged `status`. Keying on `status` alone would
            #                SWALLOW a real worsening —
            #                tests/test_session_response_ledger.py::
            #                test_injector_reemits_on_state_change pins exactly
            #                that, and it caught this over-generalization.
            #
            # The rule is therefore "key on the SEMANTIC state", not "never key
            # on text": for this surface the text carries state that nothing
            # else exposes.
            _hb_state = str(_hb.get("state") or _hb.get("status") or "degraded")
            _hb_notice = str(_hb["notice"])
            notices.append(
                Notice(
                    KIND_STATE,
                    f"hook_bootstrap:{_hb_state}:{_hash_key(_hb_notice)}",
                    _hb_notice,
                ),
            )
        else:
            # Healthy — record the transition so a later re-degradation with
            # the IDENTICAL state still re-emits (heal → re-break).
            from .session_response_ledger import mark_state

            mark_state(project_root, session_id, "rail:state:hook_bootstrap", "healthy")
    except Exception:
        pass
    # GATE LIVENESS: the security gate can die SILENTLY (hook declines itself
    # on package drift; hooks stop firing; the NLP security surface returns
    # None forever) and today both the operator and the agent would see a
    # perfectly normal, green, UNGOVERNED session. Two jobs here, on the same
    # proven rail deploy_failure_notice rides (Article VI — one pattern, not
    # a second one):
    #   1. record_mcp_activity() — every governed tool call feeds the "active
    #      traffic" clock, so the hook-silence probe can distinguish "active
    #      session, no hooks arriving" (the ALARM) from "idle" (normal, and it
    #      must never cry wolf).
    #   2. gate_health_notice() — a DEGRADED or UNKNOWN gate announces itself
    #      on the agent's NEXT tool call, so no agent can work ungoverned in
    #      ignorance. An OK gate is SILENT here (green is displayed on the
    #      dashboard, not shouted on the rail). UNKNOWN is a WARNING, never a
    #      pass — a probe that could not check must never read as green.
    # Fail-quiet like every other layer: a broken health signal degrades to no
    # block, never to a fabricated all-clear.
    #
    # THE MEASURED REPEAT, AND ITS CAUSE. The operator measured a
    # `GATE HEALTH: DEGRADED` block re-firing "unchanged on result after
    # result". The dedup was never missing — it was keyed on the RENDERED
    # BANNER, and the banner interpolates a rolling 60-minute breadcrumb COUNT
    # (gate_health.py:478-483). Every new decline moved the count, moved the
    # sha256, and the ledger correctly concluded "new state". The banner looked
    # unchanged to a reader because the count sits in a buried detail line.
    # FIX: the key is the probe STATUS TUPLE — the semantic state — so the
    # banner announces on the TRANSITION and a mere count drift is silent.
    try:
        from . import gate_health as _gh

        _gh.record_mcp_activity()
        _ghn = _gh.gate_health_notice(project_root)
        if _ghn:
            notices.append(
                Notice(
                    KIND_STATE,
                    f"gate_health:{_gate_health_state_key(project_root)}",
                    _ghn,
                ),
            )
        else:
            from .session_response_ledger import mark_state

            mark_state(project_root, session_id, "rail:state:gate_health", "healthy")
    except Exception:
        pass
    # Durable-content capture hint (#9): when conversation/prompt content was
    # classified as DECLARATIVE durable knowledge (rule/decision/preference/
    # invariant), surface a terse 💾 "record as durable?" hint. Once per
    # content (hash ledger) and max 2 surfaces, then auto-drop — mirrors
    # run_notifications max_displays semantics. Fail-quiet like every layer.
    try:
        from .aidocs_nlp.durable_hint_store import (
            format_hint_block,
            surface_pending,
        )

        _hints = surface_pending(project_root, session_id=session_id)
        if _hints:
            _text = format_hint_block(_hints)
            if _text:
                # ADVISORY, not an obligation — and once=False because
                # durable_hint_store owns its own 2-surface durable cap.
                notices.append(
                    Notice(KIND_INFO, f"durable_hint:{_hash_key(_text)}", _text, once=False),
                )
    except Exception:
        pass
    # Freeze-strike notices (operator directive 2026-07-15): a security strike
    # — including a self-cancel strike minted when the AGENT clears its own
    # freeze — surfaces here on the rail, 3 times then auto-drops. Replaces the
    # old per-prompt UPS strike-note (hook_pipeline): told, then it stops
    # nagging. Fail-quiet like every layer.
    try:
        from . import freeze_strike_notice_store as _fsn

        _strikes = _fsn.surface_pending(
            project_root, session_id=session_id, agent_context_id=agent_context_id
        )
        if _strikes:
            _text = _fsn.format_strike_block(_strikes)
            if _text:
                # A STRIKE IS AN OBLIGATION (acknowledge / report). once=False:
                # the store owns a durable 3-surface cap.
                notices.append(
                    Notice(
                        KIND_MUST_ACT,
                        "freeze_strike:"
                        + ",".join(
                            sorted(str((s or {}).get("id") or "") for s in _strikes),
                        ),
                        _text,
                        once=False,
                    ),
                )
    except Exception:
        pass
    # Open-backlog surfacing (#419 War DD): compact counts + top items with
    # the tell-the-user instruction, epoch-deduped via the session ledger.
    # Shares the 'backlog_surface' key with the UPS additionalContext path,
    # so whichever surface fires first wins and the other stays quiet —
    # this rail is the fallback for hookless contexts. Fail-quiet: empty
    # backlog or dead store → nothing (no-padding law). Workers are already
    # fenced upstream in _inject_into_return AND inside the surfacer.
    #
    # THE CANONICAL ANTI-PATTERN, AND WHY THIS IS NOW must_act. The operator
    # measured this banner re-firing *including* its
    # `INSTRUCTION: tell the user about these backlogged task(s) now` line —
    # must_act CONTENT riding a state NOTICE, so each repeat demanded an action
    # that had already been performed. The banner IS an obligation; it is
    # classified as one rather than dissected, so it can never again share a
    # block with informational traffic, and `announce_once` keyed on the
    # backlog's own identity stops the phantom duties.
    try:
        from .agent_memory_epoch import resolve_host_identity
        from .backlog_surfacer import context_backlog_block

        # #587-B: give this writer the identity its sibling already has. The two
        # writers of the SAME `backlog_surface` ledger row are this rail and
        # `prompt_context_service.py:211`; that one passes host_kind +
        # host_session_id, this one passed NEITHER, so the surfacer fell back to
        # a different resolution and the two disagreed about the same row. Both
        # now resolve through the ONE authority with the same inputs.
        _kind, _hsid = resolve_host_identity(project_root=project_root)
        _bl = context_backlog_block(
            project_root,
            session_id,
            host_kind=_kind,
            host_session_id=_hsid,
        )
        if _bl:
            # once=False — AND THAT IS THE STRONGER CHOICE HERE, not a waiver.
            # `context_backlog_block` already anchors its own "once" durably in
            # the session ledger, keyed on the backlog EPOCH (a sha256 over the
            # id-tree plus the sqlite compaction count). That key is strictly
            # better than anything this rail could compute:
            #   * a content hash cannot tell "same roster, new epoch" from a
            #     repeat, and
            #   * COMPACTION MUST RE-ANNOUNCE. When the agent's context is
            #     compacted, what it was told is ERASED; re-emitting is
            #     RE-DELIVERY, not duplication. That is the #475 re-injection
            #     contract, pinned by
            #     test_notification_rail_epoch_dedupe::test_compaction_reemits,
            #     and a content-hash key silently broke it.
            # Layering the rail ledger on top would fight the epoch key and
            # suppress a legitimate re-delivery. What this rail adds is the
            # KIND — see above for why the roster is must_act.
            notices.append(
                Notice(KIND_MUST_ACT, "backlog_roster", _bl, once=False),
            )
    except Exception:
        pass
    # PARK REQUEST (#589 receive-lease): an actor whose lease is ARMED AND
    # WAITING owes a park. Stop cannot carry this — Stop is stderr-only per the
    # shim's #589 posture table, and emitting result text there would change
    # Stop's exit semantics, which is a security-relevant change deliberately
    # not being made. The rail is therefore the correct surface, and this is a
    # pull-model builder exactly like `backlog_surfacer` above.
    #
    # ONCE PER ARM, NOT ONCE EVER — and `once=False` IS WHAT GUARANTEES THAT.
    # `park_request_block` already anchors its own "once" durably:
    # `claim_park_request` is a conditional SQL latch that the next `arm`
    # CLEARS, so each new arm is a new event and a re-read within one arm
    # returns "". Critically, the block's text is the CONSTANT
    # `WAIT_NEXT_REQUEST`, identical on every arm — so routing it through this
    # rail's content-keyed ledger would hash every arm to the SAME key and
    # silently degrade once-per-ARM into once-EVER. That is the sibling lane's
    # mutant M6, and `once=False` is the reason it cannot land here.
    #
    # KIND: must_act. The actor OWES a park; the notice demands a specific
    # action (call wait_next), and the obligation outlives the announcement in
    # the lease row, queryable via ReceiveLeaseStore.get / list_actors — which
    # is precisely "announce once, stay queryable".
    #
    # CONTRADICTION REPORTED, NOT RESOLVED SILENTLY: the function's own
    # docstring (receive_lease_stop.py:200-203) says "Informational traffic
    # only: no ack, no enforcement, never mixed into an enforced-workflow
    # rail." That conflicts with the coordinator's ruling that this is
    # must_act. Classified must_act here because an owed park demands an
    # action, which is the law's own definition of the kind; the docstring's
    # claim is accurate only in the narrower sense that nothing BLOCKS on it.
    # Flagged for the operator.
    #
    # Fail-quiet: a lease-ledger problem must never cost the actor its other
    # notifications, so this sits in its own try/except at the end.
    try:
        from .receive_lease_stop import park_request_block

        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            _park_hsid = current_calling_host_session_id()
        except Exception:
            _park_hsid = ""
        _park = park_request_block(
            project_root,
            host_session_id=_park_hsid,
            agent_id=agent_context_id or "",
        )
        if _park:
            notices.append(
                Notice(KIND_MUST_ACT, "park_request", _park, once=False),
            )
    except Exception:
        pass
    # RECEIVE-LEASE ALARM (#589 DRT-11, constraint 6): a route that went
    # `rearm_overdue` owes exactly ONE notification, and until now nothing in
    # production ever claimed it. `ReceiveLeaseStore.overdue` /
    # `claim_alarm` were written, tested, and left with no consumer — which
    # means the release would have SHIPPED THE LEDGER AND NOT THE ALARM. A
    # dashboard nobody built is not observability, so the alarm rides the one
    # rail that reaches every registered tool result instead.
    #
    # The false-alarm precondition is already closed: `settle` is wired at
    # conductor_comms.py:5265-5294, so a correctly-parking actor no longer ages
    # into `rearm_overdue`. That was the stated blocker for consuming these two
    # methods (conductor_comms.py:5265-5273), and it is met.
    #
    # KIND: state, NOT must_act. This says "WE STOPPED BEING ABLE TO TELL
    # whether this route is covered" — it names no action the reader can take,
    # and `receive_lease_store.claim_alarm`'s own contract forbids mixing it
    # into an enforced-ack rail.
    #
    # once=False, for the same reason as the park: `claim_alarm` is a
    # conditional SQL latch that the next `arm` CLEARS, so it is already
    # once-per-TRANSITION. Routing it through this rail's content-keyed ledger
    # would hash every transition of the same route to the same key and
    # silently degrade once-per-transition into once-ever.
    #
    # Fail-quiet in its own try/except: a lease-ledger fault must never cost
    # the actor its other notifications.
    try:
        if session_id:
            from .receive_lease_store import ReceiveLeaseStore as _RLS

            _store = _RLS()
            # ONE PASS (#489): one store call, one connection, and no write
            # transaction at all when every overdue latch is already set.
            for _alarm in _RLS().claim_overdue_alarms(project_root, session_id=session_id):
                notices.append(
                    Notice(
                        KIND_STATE,
                        "receive_lease_overdue",
                        (
                            "⚠️ RECEIVE COVERAGE UNKNOWN for "
                            f"{_alarm.get('label') or _alarm.get('actor_id')}: "
                            f"{_alarm.get('why') or 'rearm_overdue'}. This route "
                            "has not re-armed within its lease, so whether the "
                            "operator can reach that actor is no longer "
                            "observable. Announced once per transition; the "
                            "state stays queryable via ai_msg lease surfaces."
                        ),
                        once=False,
                    ),
                )
    except Exception:
        pass
    # BACKLOG SYNC ALARM (#1063 ruling 5). 214 writes sat unsent for hours
    # while every ai_backlog receipt read as success and the credential
    # refusal lived only in a meta row. Fires with writes pending AND (no
    # sitter, a credential refusal, or a stale last success). KIND must_act:
    # a credential refusal owes a sign-in. once=True on a key that names the
    # causes + reason, so it announces once per TRANSITION, not per call.
    try:
        from .backlog_outbox_service import sync_alarm as _sync_alarm

        _alarm = _sync_alarm(project_root)
        if _alarm:
            notices.append(Notice(KIND_MUST_ACT, _alarm[0], _alarm[1]))
    except Exception:
        pass
    return notices


def _hash_key(text: str) -> str:
    """Stable short digest, for a notice whose source exposes no stable id.

    A CONTENT hash is the weakest acceptable dedup key and it is used ONLY
    where the producing store hands back no identifier. It is explicitly NOT
    used for gate health (see `_gate_health_state_key`) — that was the measured
    defect: hashing volatile prose turns every cosmetic drift into a new event.
    """
    import hashlib

    return hashlib.sha256(
        str(text or "").encode("utf-8", errors="replace"),
    ).hexdigest()[:16]


def _gate_health_state_key(project_root: Path) -> str:
    """The SEMANTIC gate-health state: the per-probe status tuple.

    Deliberately excludes every `reason` string. `_decline_probe` builds its
    reason from a rolling 60-minute breadcrumb COUNT (gate_health.py:475-489),
    so a reason-inclusive key changes whenever a decline lands or ages out —
    which is exactly how the "unchanged" DEGRADED banner re-fired on result
    after result. Status transitions are events; count drift is not.

    Returns "unknown" when health cannot be computed — never "ok". A key that
    could not be derived must not collapse onto the healthy key, because that
    would suppress a real alarm.
    """
    try:
        from . import gate_health as _gh

        health = _gh.compute_gate_health(project_root)
        probes = health.get("probes") or {}
        return ";".join(
            f"{name}={str((probes.get(name) or {}).get('status') or 'unknown')}"
            for name in sorted(probes)
        ) or str(health.get("status") or "unknown")
    except Exception:
        return "unknown"


# `_collect_notification_blocks` and `_augment_return` USED TO LIVE HERE.
# Both were removed 2026-09-12. Neither had a production caller: every call
# site was a test, which made the TEST the consumer of record -- a second,
# rival definition of "used", and the exact "coded but unwired" shape the
# structural gate exists to catch. Tests are provers, not production
# consumers, and an allowlist is not a freezer for superseded code.
#
# NO ASSERTION WAS DELETED. Both were thin adapters, so the assertions MOVED
# to the live path: `tests/notification_rail_support.py` now holds the two
# adapters, each a direct call into the canonical authority below. The rail
# production actually runs is
# `install_universal_notification_injection` -> `_inject_into_return` ->
# `compose_notices` -> `announce_once` -> `render_notice_payload` ->
# `augment_return_typed`, and `augment_return_typed` is where the kind
# taxonomy lives. `_augment_return` additionally carried a DEFECT the typed
# version fixed: it `return raw`-ed when `_notifications` was already present,
# silently discarding the blocks it had just been handed.


def augment_return_typed(raw: Any, payload: dict[str, Any]) -> Any:
    """Attach a KIND-SEPARATED notification payload to `raw`.

    `payload` comes from `render_notice_payload`, so enforced-workflow traffic
    arrives under `_must_act` and advisory traffic under `_notifications`. They
    are NEVER concatenated: an agent must be able to tell a veto from a weather
    report by LOOKING AT THE SHAPE, not by reading the prose.

    Shape contracts are preserved exactly as `_augment_return` preserved them —
    including the 2026-07-13 rule that a ToolResult's `structured_content` and
    `meta` survive untouched. See the REPORT for the measured consequence of
    that rule for dict-returning tools.
    """
    if not payload:
        return raw
    info_text = str(payload.get(NOTIFICATIONS_KEY) or "")
    must_act = payload.get(MUST_ACT_KEY) or []

    # ToolResult: one TextContent per CHANNEL, so the separation is visible in
    # the rendered result and not just in the data model.
    try:
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        if isinstance(raw, ToolResult):
            try:
                extra = []
                if info_text:
                    extra.append(TextContent(type="text", text=info_text))
                if must_act:
                    extra.append(
                        TextContent(
                            type="text",
                            text=_render_must_act_text(must_act),
                        ),
                    )
                if not extra:
                    return raw
                # PRESERVE structured_content + meta (2026-07-13). Rebuilding
                # with content only silently DESTROYED the structured payload
                # every time a notification fired — dual-audience tools
                # (task_begin/complete, edit tools) contractually return
                # {ok: ...} in structured_content, and any client reading it
                # got None instead. On the VPS proof run the gate-health
                # DEGRADED rail block fired mid-suite and two query-gate
                # tests crashed with KeyError: 'ok' — that KeyError was this
                # contract break announcing itself, not a test bug.
                return ToolResult(
                    content=list(raw.content) + extra,
                    structured_content=raw.structured_content,
                    meta=raw.meta,
                )
            except Exception:
                return raw
    except Exception:
        pass

    # dict: separate keys, never a merged blob.
    if isinstance(raw, dict):
        out = dict(raw)
        # A TOOL THAT ALREADY SPOKE KEEPS ITS WORDS, BUT THE RAIL IS NOT LOST.
        # `_augment_return` used to `return raw` outright when `_notifications`
        # was already present, which SILENTLY DISCARDED every block this
        # composer had just built (measured: SHAPE 3 of the probe in the
        # REPORT). The pre-existing text is preserved and the rail's own text is
        # appended after it.
        if info_text:
            existing = str(out.get(NOTIFICATIONS_KEY) or "").strip()
            out[NOTIFICATIONS_KEY] = (
                existing + "\n\n" + info_text if existing else info_text
            )
        if must_act:
            prior = out.get(MUST_ACT_KEY)
            out[MUST_ACT_KEY] = (
                list(prior) + list(must_act)
                if isinstance(prior, list)
                else list(must_act)
            )
        return out

    # str: prepend, must_act first (it is the part that demands action).
    if isinstance(raw, str):
        head = "\n\n".join(
            part
            for part in (
                _render_must_act_text(must_act) if must_act else "",
                info_text,
            )
            if part
        )
        return f"{head}\n\n{raw}" if head else raw

    # list: wrap with notification metadata, keys kept separate.
    if isinstance(raw, list):
        return {"items": raw, **payload}

    # None / scalars: wrap.
    if raw is None:
        return dict(payload)
    return {"value": raw, **payload}


def _render_must_act_text(must_act: list[dict[str, Any]]) -> str:
    """Render enforced-workflow traffic as its OWN block, clearly marked.

    The header exists so the kind is unmistakable even on a surface that can
    only carry text. It is a RENDERING of the kind, never the carrier of it —
    the kind itself travels structurally, in `_must_act`.
    """
    lines = [
        (
            "⛔ ACTION REQUIRED — the following each demand a specific action "
            "from you. Announced ONCE; the obligation persists and stays "
            "queryable."
        ),
    ]
    lines.extend(str(item.get("text") or "") for item in must_act)
    return "\n\n".join(part for part in lines if part)


def _inject_into_return(raw: Any) -> Any:
    """Best-effort identity-scoped notification injection.

    THE TWO REAL EXCLUSIONS from the otherwise-universal rail, named rather
    than left to be discovered:
      1. `project_root is None` — nothing is resolvable, so nothing is read.
      2. a non-worker caller with an EMPTY `session_id` — the session-scoped
         collectors have no session to read.
    Neither is a tool-class exemption: `install_universal_notification_injection`
    wraps EVERY `@server.tool` registration, edit tools included.
    """
    try:
        project_root, session_id, actor_id, lane_id = _resolve_drain_identity()
        if project_root is None:
            return raw
        if _is_worker_caller():
            # #1005: a lane worker drains ONLY its own mailbox here — never
            # the conductor's session rail (#356 leak). session_id is passed
            # EMPTY on purpose so the session-scoped collectors stay off.
            notices = compose_notices(project_root, "", actor_id, lane_id)
            # #1061: every actor class receives on its next governed tool
            # result -- subagents and lane workers included. ONLY the stream
            # collector runs, for the worker's OWN server-derived route; the
            # run/deploy/strike/backlog/session collectors stay off (#356).
            notices.extend(_worker_stream_notices(project_root))
            scope = actor_id or lane_id
        elif not session_id:
            return raw
        else:
            notices = compose_notices(project_root, session_id, actor_id, lane_id)
            scope = actor_id or session_id
        if not notices:
            return raw
        # ONCE PER EVENT, ANCHORED DURABLY. This is the single place the
        # announce-once ledger is applied, so no collector can forget it and no
        # second appender can bypass it.
        notices = announce_once(project_root, scope, notices)
        if not notices:
            return raw
        result = augment_return_typed(raw, render_notice_payload(notices))
        # #1061 PHASE TWO: confirm stream claims ONLY when the notices were
        # actually placed in the result being returned. `augment_return_typed`
        # hands back `raw` itself when it could not attach them; an exception
        # anywhere above skips this too -- the claim stays unconfirmed and its
        # lease expiry makes it claimable once more.
        if result is not raw:
            try:
                confirm_surfaced_claims(project_root, notices)
            except Exception:  # noqa: BLE001 -- unconfirmed = re-claimable
                pass
        return result
    except Exception:
        return raw


def install_universal_notification_injection(server: Any) -> None:
    """Monkey-patch `server.tool` so EVERY subsequent `@server.tool(...)`
    registration wraps the registered function with the notification
    injector. Idempotent — guarded by an attribute marker.

    Must be called BEFORE any @server.tool() decoration. mcp_server.py
    invokes this immediately after `server = FastMCP(...)` construction.
    """
    if getattr(server, "_aidocs_universal_drain_installed", False):
        return

    original_tool = server.tool

    def patched_tool(*args, **kwargs):
        inner_decorator = original_tool(*args, **kwargs)

        def wrap_with_drain(fn):
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapped(*a, **kw):
                    raw = await fn(*a, **kw)
                    return _inject_into_return(raw)

                return inner_decorator(async_wrapped)

            @functools.wraps(fn)
            def sync_wrapped(*a, **kw):
                raw = fn(*a, **kw)
                return _inject_into_return(raw)

            return inner_decorator(sync_wrapped)

        return wrap_with_drain

    try:
        server.tool = patched_tool  # type: ignore[assignment]
        server._aidocs_universal_drain_installed = True
    except Exception:
        # If FastMCP refuses re-binding `tool`, fall back to the
        # existing per-tool drain in tool_display.py — universal
        # coverage is best-effort.
        pass
