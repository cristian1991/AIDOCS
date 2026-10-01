"""OPERATOR PROMPT BRIDGE (#1061 lane-operator-bridge).

The MINIMUM bridge the seated-Consul forced park needs: an operator prompt that
has ALREADY passed the governed UPS safety head (login -> secret block ->
preflight judge -> origin gate) becomes ONE canonical XAACP stream entry,
server-stamped ``origin='operator'`` with a ``causal_ref``, addressed to the
seat actor THIS host session holds. A parked ``wait_next`` of that actor then
returns it.

What this module is NOT:
  * not an authority surface. It never mints a grant, a seal, a soul token or
    a sticky grant; those stay in the UPS privilege stages that mint them
    today. The stream entry is a RECEIVE event, and reading it confers nothing.
  * not a bypass. It is only called downstream of the safety head's verdict. A
    refused prompt produces a named refusal audit and NO content entry.
  * not a name resolver. The target is the seat whose incumbent's
    ``host_session_id`` equals the prompting host session, resolved through
    ``conductor_comms.xaacp_seat_occupancy`` at ENQUEUE time and stored on the
    entry; a later succession never retargets an already-created entry.

IDEMPOTENCY keys on the immutable causal prompt identity (``causal_ref``), never
on a body hash: two identical texts sent as two prompts are two entries; one
retried hook for the same prompt is one entry.

AUDIT: one retained event per bridged prompt (``operator_prompt_bridged``) and
one per refusal (``operator_prompt_bridge_refused``); ids and hashes only, never
raw prompt text (stream stores content, audit references id/hash).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Retained DECISION audit kinds (execution_event_retention registers them).
AUDIT_OPERATOR_PROMPT_BRIDGED = "operator_prompt_bridged"
AUDIT_OPERATOR_PROMPT_BRIDGE_REFUSED = "operator_prompt_bridge_refused"

#: The seats a Consul holds. Resolution is by HOST SESSION, never by name.
SEAT_ROLES = ("conductor", "co_conductor")

TRANSPORT_UPS = "ups"
TRANSPORT_DASHBOARD = "dashboard"

#: Serializes check-then-append inside one process (a retried hook in the same
#: broker). Cross-process retries are serialized by the UPS prompt-submit lock.
_BRIDGE_LOCK = threading.Lock()


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def causal_ref_for(*, transport: str, host_session_id: str, prompt_event_id: str) -> str:
    """The immutable causal identity of ONE operator prompt/event.

    Built only from server-held identity (transport + host session + the
    prompt's event id). Never from the prompt body.
    """
    event = str(prompt_event_id or "").strip()
    if not event:
        return ""
    return f"{transport}:{str(host_session_id or '').strip()}:{event}"


def resolve_host_seat_actor(project_root: Path, *, session_id: str, host_session_id: str) -> dict:
    """The seat actor held by THIS host session in THIS managed session.

    Returns ``{"status": "held", "actor_id", "role"}`` or
    ``{"status": "no_seat"}``. Never infers from a name or role label.
    """
    from . import conductor_comms as cc

    sid = str(session_id or "").strip()
    host = str(host_session_id or "").strip()
    if not sid or not host:
        return {"status": "no_seat"}
    for role in SEAT_ROLES:
        occ = cc.xaacp_seat_occupancy(project_root, session_id=sid, role=role)
        if not occ.get("ok") or occ.get("status") != "held":
            continue
        if str(occ.get("host_session_id") or "").strip() == host and occ.get("actor_id"):
            return {"status": "held", "actor_id": str(occ["actor_id"]), "role": role}
    return {"status": "no_seat"}


def _existing_entry(project_root: Path, *, session_id: str, causal_ref: str) -> dict | None:
    from . import conductor_comms as cc
    from . import xaacp_stream

    with cc._connect(project_root) as conn:
        row = conn.execute(
            "SELECT seq, message_id, reader_actor_id FROM xaacp_stream "
            "WHERE session_id=? AND origin=? AND causal_ref=? AND event_kind=? "
            "ORDER BY seq LIMIT 1",
            (session_id, xaacp_stream.ORIGIN_OPERATOR, causal_ref, xaacp_stream.EVENT_INCOMING),
        ).fetchone()
    if row is None:
        return None
    return {"seq": int(row[0]), "message_id": str(row[1]), "target_actor_id": str(row[2])}


def _default_record_event() -> Callable[..., Any]:
    from .execution_index_store import ExecutionIndexStore

    return ExecutionIndexStore().record_event


def append_operator_entry(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    body: str,
    causal_ref: str,
    sender_actor_id: str,
    transport: str,
    operator_principal: dict | None = None,
    record_event: Callable[..., Any] | None = None,
    equivalent_refs: tuple[str, ...] = (),
) -> dict:
    """Append ONE operator-origin stream entry. Server-internal only.

    Callers MUST have established operator provenance and a passed safety
    verdict before calling; nothing here trusts a caller-supplied origin.

    AUTHORITY SELECTION: a local/unbound project appends to its own store; a
    cloud-bound project forwards to the remote authority's server-internal
    ``operator_append`` (idempotent on session+origin+causal_ref AT THE
    AUTHORITY). An unavailable authority, or one without that door, refuses by
    name -- never a local write, never a silent drop.
    """
    from . import conductor_comms as cc

    sid = str(session_id or "").strip()
    target = str(target_actor_id or "").strip()
    ref = str(causal_ref or "").strip()
    if not sid or not target or not ref or not str(body or "").strip():
        return {"ok": False, "status": "invalid", "error": "session, target, causal_ref and body required"}
    authority = cc.xaacp_authority_for(project_root)
    if authority is not None:
        # CLOUD-BOUND: the remote authority's server-internal door. It is
        # idempotent on (session, origin='operator', causal_ref) AT THE
        # AUTHORITY, so the cross-process guarantee lives there.
        reason = str(getattr(authority, "reason", "") or "").strip()
        door = getattr(authority, "operator_append", None)
        if reason or not callable(door):
            return record_refusal(
                project_root, session_id=sid, transport=transport, causal_ref=ref,
                reason_code="append_failed:unavailable" if reason else "append_failed:not_authority",
                body=body, target_actor_id=target, record_event=record_event,
            )
        call: Callable[..., Any] = door
    else:
        # LOCAL/UNBOUND: the same stream-core door, in process. Equivalent refs
        # (a fallback identity's previous receive window) dedupe here first.
        for candidate in [r for r in equivalent_refs if r and r != ref]:
            prior = _existing_entry(project_root, session_id=sid, causal_ref=candidate)
            if prior is not None:
                return {"ok": True, "status": "duplicate", "causal_ref": candidate, **prior}

        def call(**kw: Any) -> dict:
            return cc.xaacp_operator_append(project_root, **kw)

    try:
        with _BRIDGE_LOCK:
            sent = call(
                session_id=sid,
                target_actor_id=target,
                body=str(body),
                causal_ref=ref,
                operator_principal=dict(operator_principal or {}),
                transport=transport,
            )
    except Exception as exc:  # noqa: BLE001 -- a transport failure is a named refusal
        sent = {"ok": False, "status": f"transport_error:{type(exc).__name__}"}
    if not isinstance(sent, dict):
        sent = {"ok": False, "status": "malformed_authority_reply"}
    if sent.get("ok") and sent.get("idempotent_replay"):
        return {
            "ok": True, "status": "duplicate", "causal_ref": ref,
            "message_id": sent.get("message_id"),
            "target_actor_id": str(sent.get("target_actor_id") or target),
        }
    return _finish_append(
        project_root, sent, session_id=sid,
        target_actor_id=str(sent.get("target_actor_id") or target), body=body, causal_ref=ref,
        sender_actor_id=str(sent.get("sender_actor_id") or sender_actor_id), transport=transport,
        record_event=record_event,
    )


def _finish_append(
    project_root: Path,
    sent: dict,
    *,
    session_id: str,
    target_actor_id: str,
    body: str,
    causal_ref: str,
    sender_actor_id: str,
    transport: str,
    record_event: Callable[..., Any] | None,
) -> dict:
    sid, target, ref = session_id, target_actor_id, causal_ref
    if not sent.get("ok"):
        return record_refusal(
            project_root,
            session_id=sid,
            transport=transport,
            causal_ref=ref,
            reason_code=f"append_failed:{sent.get('status') or 'send_failed'}",
            body=body,
            target_actor_id=target,
            record_event=record_event,
        )
    try:
        (record_event or _default_record_event())(
            project_root,
            event_kind=AUDIT_OPERATOR_PROMPT_BRIDGED,
            source_kind="operator_prompt_bridge",
            session_id=sid,
            capability_name="operator_prompt_bridge",
            action_kind="bridge",
            target_entity=target,
            status="appended",
            payload={
                "transport": transport,
                "causal_ref": ref,
                "message_id": sent.get("message_id"),
                "stream_cursor": sent.get("stream_cursor"),
                "target_actor_id": target,
                "sender_actor_id": str(sender_actor_id or "operator"),
                "content_sha256": _sha(body),
                "content_len": len(str(body)),
            },
        )
    except Exception:  # noqa: BLE001 -- the entry is durable; audit failure is logged by the store
        pass
    return {
        "ok": True,
        "status": "appended",
        "causal_ref": ref,
        "message_id": sent.get("message_id"),
        "target_actor_id": target,
    }


def record_refusal(
    project_root: Path,
    *,
    session_id: str,
    transport: str,
    causal_ref: str,
    reason_code: str,
    body: str = "",
    target_actor_id: str = "",
    record_event: Callable[..., Any] | None = None,
) -> dict:
    """Named refusal: audited, and NO content entry reaches any actor."""
    payload = {
        "transport": transport,
        "causal_ref": str(causal_ref or ""),
        "reason_code": str(reason_code or "refused"),
        "target_actor_id": str(target_actor_id or ""),
        "content_sha256": _sha(body) if body else "",
        "content_len": len(str(body or "")),
    }
    try:
        (record_event or _default_record_event())(
            project_root,
            event_kind=AUDIT_OPERATOR_PROMPT_BRIDGE_REFUSED,
            source_kind="operator_prompt_bridge",
            session_id=str(session_id or "") or None,
            capability_name="operator_prompt_bridge",
            action_kind="bridge",
            target_entity=str(target_actor_id or "") or None,
            status="refused",
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "status": "refused", **payload}


def bridge_ups_prompt(
    project_root: Path,
    *,
    session_id: str,
    host_session_id: str,
    prompt: str,
    prompt_event_id: str,
    record_event: Callable[..., Any] | None = None,
    now: float | None = None,
) -> dict:
    """UPS leg: called ONLY after the safety head allowed the prompt."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": True, "status": "no_session"}
    seat = resolve_host_seat_actor(project_root, session_id=sid, host_session_id=host_session_id)
    if seat.get("status") != "held":
        return {"ok": True, "status": "no_seat"}
    event_id, equivalents = ups_prompt_identity(prompt, prompt_event_id, now=now)
    ref = causal_ref_for(transport=TRANSPORT_UPS, host_session_id=host_session_id, prompt_event_id=event_id)
    if not ref:
        return {"ok": True, "status": "no_causal_identity"}
    return append_operator_entry(
        project_root,
        session_id=sid,
        target_actor_id=seat["actor_id"],
        body=prompt,
        causal_ref=ref,
        sender_actor_id=f"operator:{TRANSPORT_UPS}:{str(host_session_id or '').strip()}",
        transport=TRANSPORT_UPS,
        operator_principal=_ups_principal(project_root, host_session_id),
        record_event=record_event,
        equivalent_refs=tuple(
            causal_ref_for(transport=TRANSPORT_UPS, host_session_id=host_session_id, prompt_event_id=e)
            for e in equivalents
        ),
    )


#: Receive window for a host that sends NO per-prompt id (Claude Code's UPS
#: payload carries session_id / transcript_path / cwd / prompt, no prompt id).
UPS_FALLBACK_WINDOW_SECONDS = 120


def ups_prompt_identity(prompt: str, host_prompt_id: str, *, now: float | None = None) -> tuple[str, tuple[str, ...]]:
    """(event_id, equivalent_event_ids) for one UPS prompt. Never rotates per retry.

    * host sends a prompt id -> that id, exactly (two prompts = two ids).
    * host sends none -> ``w<bucket>:<sha256(normalized prompt)[:24]>`` over a
      fixed receive window; the PREVIOUS bucket's id is an equivalent, so a
      retry that straddles a window boundary still dedupes (locally). Documented
      trade: identical text re-sent inside the window by a host with no prompt
      id collapses to one entry.
    """
    import time as _time

    pid = str(host_prompt_id or "").strip()
    if pid:
        return pid, ()
    normalized = " ".join(str(prompt or "").split())
    if not normalized:
        return "", ()
    digest = _sha(normalized)[:24]
    bucket = int((_time.time() if now is None else float(now)) // UPS_FALLBACK_WINDOW_SECONDS)
    return f"w{bucket}:{digest}", (f"w{bucket - 1}:{digest}",)


def _ups_principal(project_root: Path, host_session_id: str) -> dict:
    """What the edge knows about the prompting principal. The AUTHORITY derives
    operator status itself; this is attribution input, never authority."""
    principal = {"transport": TRANSPORT_UPS, "host_session_id": str(host_session_id or "")}
    try:
        from .identity_resolver import current_principal_type, current_user_id

        principal["user_id"] = str(current_user_id(project_root) or "")
        principal["principal_type"] = str(current_principal_type(project_root) or "")
    except Exception:  # noqa: BLE001
        pass
    return principal


def resolve_dashboard_target(project_root: Path, *, session_id: str, target_role: str) -> dict:
    """The seat actor CURRENTLY holding ``target_role`` in this session, resolved
    at ENQUEUE time (the stored entry keeps it; succession never retargets)."""
    from . import conductor_comms as cc

    role = str(target_role or "").strip().lower().replace("-", "_")
    if role not in SEAT_ROLES:
        return {"status": "invalid_role"}
    occ = cc.xaacp_seat_occupancy(project_root, session_id=session_id, role=role)
    if occ.get("ok") and occ.get("status") == "held" and occ.get("actor_id"):
        return {"status": "held", "actor_id": str(occ["actor_id"]), "role": role}
    return {"status": "vacant", "role": role}


def bridge_dashboard_operator_message(
    project_root: Path,
    *,
    runtime: Any,
    principal: dict | None,
    session_id: str,
    body: str,
    client_message_id: str,
    target_role: str = "",
    target_actor_id: str = "",
    record_event: Callable[..., Any] | None = None,
) -> dict:
    """Dashboard/XAACP operator transport. Same gates as UPS, no bypass.

    ``principal`` and ``session_id`` are GATE-derived (the authenticated request
    and its bound selection) -- never payload, never a role label. The principal
    must (1) be authenticated and an admin of the bound org
    (``outer_gate_project_acl.is_org_admin``) AND (2) BE this session's bound
    operator (``xaacp_session_operator``, fail-closed).
    The body passes the SAME UPS safety head (secret block + preflight judge)
    before any entry exists. The target is the CURRENT seat holder, resolved now.
    Idempotency keys on the dashboard message id (``client_message_id``).
    """
    from . import conductor_comms as cc
    from . import hook_pipeline
    from . import outer_gate_project_acl as _acl

    sid = str(session_id or "").strip()
    target = str(target_actor_id or "").strip()
    user = str((principal or {}).get("user_id") or "").strip() if isinstance(principal, dict) else ""
    ref = causal_ref_for(
        transport=TRANSPORT_DASHBOARD, host_session_id=user, prompt_event_id=client_message_id
    )

    def refuse(code: str) -> dict:
        return record_refusal(
            project_root, session_id=sid, transport=TRANSPORT_DASHBOARD, causal_ref=ref,
            reason_code=code, body=body, target_actor_id=target, record_event=record_event,
        )

    # Consul ruling (r0a + r0b): the floor is an AUTHENTICATED gate principal who
    # is an admin of the bound org (the ONE verdict, org_admin_verdict — its
    # selection is intra-org, so the bound project is in that org) AND is this
    # session's bound operator (checked below, fail-closed). A platform
    # super-admin role is NOT required: it would refuse a legitimately bound
    # org-admin operator.
    if (
        not isinstance(principal, dict)
        or not user
        or not principal.get("authenticated")
        or not _acl.is_org_admin(principal)
    ):
        return refuse("operator_required")
    if not ref:
        return {"ok": False, "status": "invalid", "error": "client_message_id required"}
    if not sid:
        return refuse("no_session")
    try:
        bound = cc.xaacp_session_operator(project_root, session_id=sid)
    except Exception:  # noqa: BLE001 -- unreadable binding is a refusal
        bound = {"ok": False}
    if not bound.get("ok"):
        return refuse(f"operator_unresolved:{bound.get('status') or 'error'}")
    if str(bound.get("operator_user_id") or "") != user:
        return refuse("not_session_operator")
    if target_role:
        seat = resolve_dashboard_target(project_root, session_id=sid, target_role=target_role)
        if seat.get("status") != "held":
            return refuse(f"target_{seat.get('status')}")
        target = seat["actor_id"]
    else:
        held = False
        for role in SEAT_ROLES:
            occ = cc.xaacp_seat_occupancy(project_root, session_id=sid, role=role)
            if occ.get("ok") and occ.get("status") == "held" and str(occ.get("actor_id") or "") == target:
                held = True
                break
        if not held:
            return refuse("target_not_seat_holder")
    try:
        envelope, _advisory = hook_pipeline._ups_safety_screen(
            runtime, str(body or ""), {"session_id": "", "source_surface": TRANSPORT_DASHBOARD}, project_root
        )
    except Exception:  # noqa: BLE001 -- an unavailable safety head never allows
        envelope = {"blocked_by": "preflight_unavailable"}
    if envelope is not None:
        return refuse(str(envelope.get("blocked_by") or "preflight_blocked"))
    return append_operator_entry(
        project_root,
        session_id=sid,
        target_actor_id=target,
        body=body,
        causal_ref=ref,
        sender_actor_id=f"operator:{TRANSPORT_DASHBOARD}:{user}",
        transport=TRANSPORT_DASHBOARD,
        operator_principal={"user_id": user, "transport": TRANSPORT_DASHBOARD},
        record_event=record_event,
    )


def refuse_ups_prompt(
    project_root: Path,
    *,
    session_id: str,
    host_session_id: str,
    prompt: str,
    prompt_event_id: str,
    reason_code: str,
    record_event: Callable[..., Any] | None = None,
) -> dict:
    """UPS leg for a safety-head BLOCK: refusal audit only, and only when a seat
    would otherwise have been addressed (nothing to refuse otherwise)."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": True, "status": "no_session"}
    seat = resolve_host_seat_actor(project_root, session_id=sid, host_session_id=host_session_id)
    if seat.get("status") != "held":
        return {"ok": True, "status": "no_seat"}
    return record_refusal(
        project_root,
        session_id=sid,
        transport=TRANSPORT_UPS,
        causal_ref=causal_ref_for(
            transport=TRANSPORT_UPS, host_session_id=host_session_id,
            prompt_event_id=ups_prompt_identity(prompt, prompt_event_id)[0],
        ),
        reason_code=reason_code,
        body=prompt,
        target_actor_id=seat["actor_id"],
        record_event=record_event,
    )
