"""DRT-13 -- the Stop -> wait_next wiring.

THE RULING, AND WHAT THIS BOX CAN ACTUALLY ENFORCE
--------------------------------------------------
The ruling: "everything where hooks can fire a 'Stop' hook must end with
a wait_next that can transmit an OPERATOR's intent/prompt via xaacp."
The park happens AT STOP, when the actor is idle -- not mid-turn -- so
the earlier 300s-vs-30s objection does not apply to the park's PLACE in
the turn. It still applies to the park's PLACE IN THE PROCESS, and that
distinction is the whole design here:

    The Stop HOOK must not park. The Stop PATH must end in a park.

A hook is a short-lived subprocess under a host-enforced budget (~30s on
this host) and this box's broker is already timing out around 10s. A
300s ``xaacp_wait_next`` inside ``_handle_stop`` would blow the budget
and, worse, starve every other Stop function -- stewardship, the update
gate, the deploy nudge, the backlog banner -- because the host kills the
hook wholesale. So the park belongs IN-MODEL, one tool call after Stop,
where it costs no hook budget at all and can block the full lease.

What the hook does instead is the part only the hook can do: it ARMS the
lease, recording that this actor now owes a park. That write is the fact
a dashboard needs and the fact an alarm is computed from. Whether the
actor then actually parks is observable -- that is precisely the
``rearm_overdue`` state -- so coverage is MEASURED rather than assumed.

REGIME-A ENFORCEMENT FOR SEATED CONSULS -- ORDERED 2026-09-14 (#1061)
-------------------------------------------------------------------
The runtime-enforceable half of regime A is a Stop ``decision: block``
whose reason orders the agent to call ``ai_msg(mode='wait_next')``. It was
withheld while it was an unapproved change to Stop's exit semantics. The
operator ORDERED it on 2026-09-14: "stop hook should send the session's r0b
the r0a stop hook for verification and interpretation of alignment and force
r0a to wait_next ... the wait_next channel is the GLOBAL messaging/waiting
layer". ``consul_stop_duty`` below is that enforcement, scoped to a SEATED
Consul (conductor / co_conductor seat resolved from durable occupancy). The
reason is the wording preserved while it was withheld, reconstructed
verbatim from ``.MEMORY/sessions/ubermega/plans/drt-closure.md`` ("WITHHELD:
DRT-11 regime-A Stop enforcement")::

    STOP REQUIRES A PARK: this turn may not seal until you call
    ai_msg(mode='wait_next', timeout_seconds=300). XAACP is the universal
    receive rail; an unparked actor cannot be reached by the operator.

The hook still never PARKS (see below); it refuses the SEAL, and the park
happens in-model one tool call later, where it costs no hook budget.

ARMS LAST, FAILS QUIET, FAILS AFTER
-----------------------------------
``arm_on_stop`` is called at the very end of ``_handle_stop``, after
every pre-existing function has run to completion and after all four of
that handler's early-return gates. A blocked turn is not an ending turn,
so a blocked Stop correctly arms nothing. Every failure mode -- an
unresolvable route, a broken ledger, a locked database -- returns or
raises into the caller's ``except Exception: pass`` and leaves Stop's
behaviour bit-for-bit as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .receive_lease_store import ReceiveLeaseStore

# The lease a Stop-time arm takes out. Deliberately the wait_next ceiling
# (xaacp_wait_next clamps timeout to 300s), because the lease must not
# expire while a legitimate park is still running -- that would report a
# healthy actor as overdue, which is crying wolf. A little slack is added
# so clock skew between the hook process and the parking process cannot
# manufacture a false alarm.
STOP_LEASE_SECONDS = 300.0 + 30.0

# Regime B: doctrine REQUIRES the park and the runtime REQUESTS it. This
# is the request. It is informational text for an operator surface; it
# carries no enforcement and triggers no kill, eviction or transfer.
WAIT_NEXT_REQUEST = (
    "RECEIVE OBLIGATION: end this turn with "
    "ai_msg(mode='wait_next', session_id=..., timeout_seconds=300) so an "
    "OPERATOR prompt or an agent message can reach you over XAACP while "
    "you are idle. A turn that ends without a park leaves you addressable "
    "but not receptive."
)

# Regime A, SHIPPED for seated Consuls (#1061, operator ruling 2026-09-14).
# Consumed by `consul_stop_duty`; the wording is the verbatim reconstruction
# of the text preserved while the enforcement was withheld.
STOP_PARK_REASON = (
    "STOP REQUIRES A PARK: this turn may not seal until you call "
    "ai_msg(mode='wait_next', timeout_seconds=300). XAACP is the universal "
    "receive rail; an unparked actor cannot be reached by the operator."
)


def _resolve_route(project_root: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the canonical actor route for the stopping actor.

    Returns ``None`` rather than guessing. An unresolvable actor must
    stay UNKNOWN: arming a route we invented would launder an unknown
    into a definite answer, and a wrong route is worse than no row
    because it reports coverage that does not exist.

    ``actor_kind`` is recorded for the alarm label and the dashboard
    only. How the actor was spawned never enters identity or a gate.
    """
    try:
        from .agent_memory_epoch import derive_agent_context_id, resolve_host_identity
        from .managed_mode_service import ManagedModeService, resolve_managed_session
    except Exception:
        return None

    host_session_id = str(payload.get("session_id") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    if not host_session_id:
        return None
    try:
        host_kind, _sid = resolve_host_identity(project_root=project_root)
    except Exception:
        host_kind = ""
    host_kind = str(host_kind or "claude_code")
    try:
        actor_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            agent_id=agent_id or None,
        ).strip()
    except Exception:
        return None
    if not actor_id:
        return None
    try:
        session_id = str(
            resolve_managed_session(
                ManagedModeService(), project_root, host_session_id=host_session_id
            )
            or ""
        ).strip()
    except Exception:
        session_id = ""
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "actor_id": actor_id,
        "lane_id": "",
        "actor_kind": "subagent" if agent_id else "agent",
        "host": host_kind,
        "role": "",
    }


def _is_finished_subagent(event_name: str, payload: dict[str, Any]) -> bool:
    """True only on the canonical signal: SubagentStop WITH a payload agent_id.

    Operator ruling 2026-09-13 (#489 retention). Never inferred from a name or
    a route's recorded kind. A SubagentStop without an agent_id is an
    UNRESOLVABLE kind, and unknown is not finished -- it keeps the arm.
    """
    return str(event_name or "") == "SubagentStop" and bool(
        str((payload or {}).get("agent_id") or "").strip()
    )


def arm_on_stop(
    project_root: Path,
    *,
    event_name: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Discharge the Stop-time receive duty for the actor whose turn just ended.

    A SEAT (Stop, or any stop whose kind cannot be resolved) ARMS the lease:
    it is idle and now owes a park. Returns the armed row plus the
    ``request`` text -- unchanged.

    A FINISHED SUBAGENT (SubagentStop carrying an agent_id) RELEASES its lease
    instead (operator ruling 2026-09-13). Arming there left every finished
    subagent ``waiting`` forever -- ~501 overdue leases in one session.
    Release is the explicit settle the receive law requires for a finished
    actor; a resumed subagent re-arms at its existing arm point (the
    ``xaacp_wait_next`` park). Returns ``{"event", "lease", "action":
    "released"}`` so the Stop duty ledger can record the release.

    Returns ``None`` when the route could not be resolved. NEVER blocks,
    NEVER parks, NEVER returns a hook decision.
    """
    route = _resolve_route(project_root, payload)
    if route is None:
        return None
    if _is_finished_subagent(event_name, payload):
        row = ReceiveLeaseStore().release(
            project_root,
            session_id=route["session_id"],
            actor_id=route["actor_id"],
            lane_id=route["lane_id"],
            reason="subagent_stop",
        )
        return {"event": str(event_name or ""), "lease": row, "action": "released"}
    row = ReceiveLeaseStore().arm(
        project_root,
        lease_seconds=STOP_LEASE_SECONDS,
        **route,
    )
    return {
        "event": str(event_name or ""),
        "lease": row,
        "request": WAIT_NEXT_REQUEST,
    }


# ── THE PARK REQUEST'S READER HALF ───────────────────────────────────
#
# `arm_on_stop` COMPUTES the request and its caller DISCARDS the value --
# measured in claude_hook._handle_stop, and said out loud in arm_on_stop's
# own docstring ("the caller ignores the value entirely"). A lease was armed
# and NOTHING EVER TOLD THE AGENT TO PARK. The loop was four inches short.
#
# The missing inch is not a second write at Stop. Stop is a STDERR-ONLY
# event for this host (claude_hook_shim's #589 posture table): it carries no
# additionalContext shape, so text cannot leave the hook without changing
# what Stop RETURNS -- an exit-semantics change that is reported, never
# shipped. The request therefore rides the surface the operator-controlled
# host already reads on every turn: the notification rail.
#
# WHO READS WHAT. Every actor reads the same obligation: the one
# `actor_receive_lease` row. SUPERSEDED CLAIM (operator ruling 2026-09-14,
# #1061): this comment used to say a Claude Code seat is exempt from parking
# because it has hooks. The operator ruled the opposite -- wait_next is the
# GLOBAL messaging/waiting layer -- so a SEATED Consul on Claude Code IS made
# to park: `consul_stop_duty` refuses its seal until it has. Hooks and
# notices are additional surfaces, never a substitute for the park. Pinned by
# tests/host/test_consul_stop_park_1061.py.


def park_request_block(
    project_root: Path,
    *,
    host_session_id: str,
    agent_id: str = "",
) -> str:
    """The notification-rail block for an actor that owes a park, or "".

    ONE LINE OF TEXT, ONCE PER ARM. `claim_park_request` is a conditional
    SQL latch cleared by the next `arm`, so this announces once per arm
    EVENT while the OBLIGATION outlives the announcement -- it stays in the
    lease row, queryable through `ReceiveLeaseStore.get` / `list_actors`,
    which is what "announce once, stay queryable" means.

    KIND IS `must_act`; THIS BLOCK DOES NOT ITSELF ENFORCE. These are two
    different questions and an earlier draft of this docstring conflated them,
    claiming "informational traffic only". KIND answers "is an action owed" --
    and an owed park owes one, which is `must_act` by the taxonomy's own
    definition. ENFORCEMENT answers "does this block", and THIS text does not:
    the Stop enforcement for seated Consuls lives in `consul_stop_duty`
    (#1061), and every other actor's park remains requested, not enforced.
    Classifying this `info` to express "does not block" would be
    the announcement/obligation conflation the notification law forbids: an
    actor would read a real duty as a weather report. (r0a ruling 2026-09-10,
    resolving the conflict the notification lane flagged in-code.)

    So: NO ACK is required of the reader, and nothing here blocks -- but it is
    enforced-workflow traffic and must travel in the `must_act` channel,
    never concatenated into informational prose.

    Returns "" -- never a placeholder line -- when the route is
    unresolvable, when no lease is armed, or when this arm was already
    announced. No-padding: nothing to say means no block.

    /!\\ UNWIRED. THIS FUNCTION HAS NO PRODUCTION CALLER YET, and by this
    repo's law that means it is NOT DONE. It must be wired into
    ``notification_injector._collect_notification_blocks`` alongside the
    other pull-model builders -- ``backlog_surfacer``'s block is the exact
    precedent: same shape, same fail-quiet contract, same
    dedupe-claimed-at-the-reader placement. That file belongs to another
    lane, so the wiring is NAMED here rather than taken.
    """
    route = _resolve_route(project_root, {"session_id": host_session_id, "agent_id": agent_id})
    if route is None:
        return ""
    store = ReceiveLeaseStore()
    try:
        row = store.get(
            project_root,
            session_id=route["session_id"],
            actor_id=route["actor_id"],
            lane_id=route["lane_id"],
        )
    except Exception:
        return ""
    # Only a route that is ACTUALLY armed-and-owed gets a request. A
    # `working`, `released` or `unknown` route is not owed a park right now,
    # and nagging one would be the false-alarm generator this store exists
    # to avoid.
    if str(row.get("state") or "") != "waiting":
        return ""
    if not store.claim_park_request(
        project_root,
        session_id=route["session_id"],
        actor_id=route["actor_id"],
        lane_id=route["lane_id"],
    ):
        return ""
    return WAIT_NEXT_REQUEST


# ══ #1061 SEATED CONSUL STOP: MIRROR TO THE PEER, REFUSE THE SEAL UNTIL PARKED ══
#
# PINNED STATE MACHINE (r0b, binding; operator ruling 2026-09-14):
#   1. existing Stop blockers run first (the caller returns before reaching
#      this duty when any of them blocks -- a blocked turn is not a turn end);
#   2. only a SEATED Consul is affected: the seat is resolved from durable
#      occupancy (`xaacp_seat_incumbent`, matched on this host session), never
#      from a name; a subagent payload (agent_id) is never the seat;
#   3. pair only: ONE turn_end mirror per substantive turn to the unique peer
#      seat through the one actor stream, sanitized user-visible text only,
#      capped with hash + protected pointer; no peer -> consul_peer_absent;
#   4. PAIR ONLY: refuse the seal until the actor has parked in wait_next since
#      its last seal. A lone seat (no peer holder) is audited
#      lone_seat_not_enforced and seals (operator ruling 2026-09-15, superseding
#      "lone seat and pair alike");
#   5. stop_hook_active (already blocked once this stop) never blocks again:
#      missed_park is audited and the turn seals;
#   6. frozen actor: wait_next is freeze-reachable as receive-only
#      (operation_classes._RECEIVE_ONLY_TOOLS), so the order to park stays
#      performable. If that reachability is ever withdrawn, Stop YIELDS
#      (frozen_yield) rather than ordering an unperformable park;
#   7. every transition is a retained DECISION audit event carrying ids and
#      hashes, never the mirrored text.

CONSUL_SEAT_ROLES = ("conductor", "co_conductor")
MIRROR_BODY_CAP = 4000
#: The message_kind stream-core's turn-mirror door stores (local filter only).
MIRROR_MESSAGE_KIND = "consul_turn_mirror"
OUTCOME_BLOCKED_FOR_PARK = "blocked_for_park"
OUTCOME_SEALED_AFTER_PARK = "sealed_after_park"
OUTCOME_MISSED_PARK = "missed_park"
OUTCOME_FROZEN_YIELD = "frozen_yield"
OUTCOME_LONE_SEAT_NOT_ENFORCED = "lone_seat_not_enforced"


def _hidden_block_re():
    import re

    return re.compile(
        r"<(system-reminder|system|developer|thinking|antml:[a-z_]+)\b[^>]*>.*?</\1\s*>",
        re.S | re.I,
    )


def _sanitize_visible(text: str) -> str:
    """User-visible final output only: hidden blocks removed, then the
    project's credential guard (`output_guard.scrub_persisted_text`). A guard
    failure degrades to its mask, never to unscanned text."""
    import re

    raw = str(text or "")
    raw = _hidden_block_re().sub("", raw)
    # An UNCLOSED hidden block hides everything after its open tag.
    raw = re.sub(r"<(system-reminder|system|developer|thinking)\b[^>]*>.*\Z", "", raw, flags=re.S | re.I)
    from .output_guard import scrub_persisted_text

    return scrub_persisted_text(raw).strip()


def _consul_state_init(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS consul_stop_state ("
        "session_id TEXT NOT NULL, actor_id TEXT NOT NULL, "
        "last_mirror_turn TEXT NOT NULL DEFAULT '', "
        "last_mirror_marker TEXT NOT NULL DEFAULT '', "
        "last_mirror_at REAL, last_seal_at REAL, updated_at REAL NOT NULL, "
        "PRIMARY KEY (session_id, actor_id))"
    )


def _consul_state(project_root: Path, session_id: str, actor_id: str) -> dict[str, Any]:
    from .receive_lease_store import _connect as _lease_connect

    with _lease_connect(project_root) as conn:
        _consul_state_init(conn)
        row = conn.execute(
            "SELECT * FROM consul_stop_state WHERE session_id=? AND actor_id=?",
            (session_id, actor_id),
        ).fetchone()
        conn.commit()
    return dict(row) if row is not None else {}


def _consul_state_put(project_root: Path, session_id: str, actor_id: str, **fields: Any) -> None:
    import time

    from .receive_lease_store import _connect as _lease_connect

    allowed = {"last_mirror_turn", "last_mirror_marker", "last_mirror_at", "last_seal_at"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    now = time.time()
    with _lease_connect(project_root) as conn:
        _consul_state_init(conn)
        conn.execute(
            "INSERT OR IGNORE INTO consul_stop_state (session_id, actor_id, updated_at) "
            "VALUES (?, ?, ?)",
            (session_id, actor_id, now),
        )
        for key, value in cols.items():
            conn.execute(
                f"UPDATE consul_stop_state SET {key}=?, updated_at=? "  # noqa: S608 - allowlisted
                "WHERE session_id=? AND actor_id=?",
                (value, now, session_id, actor_id),
            )
        conn.commit()


#: STREAM-CORE's consul-stop doors (conductor_comms, #1061). Each routes to the
#: local store on an unbound project and to the XAACP AUTHORITY on a bound one,
#: so a cloud-bound Consul is enforced through the same calls.
STREAM_CORE_API: dict[str, str] = {
    "seat_of_host": "xaacp_seat_of_host",
    "last_park": "xaacp_last_park",
    "turn_mirror": "xaacp_turn_mirror_append",
}
OUTCOME_AUTHORITY_UNAVAILABLE = "authority_unavailable"


class ConsulAuthorityUnavailable(Exception):
    """A stream-core door could not answer (bound authority down/refusing)."""


class _ConsulPort:
    def __init__(self, project_root: Path) -> None:
        from . import conductor_comms as cc

        self.root = project_root
        self.cc = cc
        self.remote = cc.xaacp_authority_for(project_root) is not None

    def call(self, capability: str, **kwargs: Any) -> dict[str, Any]:
        name = STREAM_CORE_API[capability]
        # DIRECT imports at the use site (read at call time, so a patched
        # conductor_comms attribute is honoured). No string dispatch.
        if capability == "seat_of_host":
            from .conductor_comms import xaacp_seat_of_host as fn
        elif capability == "last_park":
            from .conductor_comms import xaacp_last_park as fn
        elif capability == "turn_mirror":
            from .conductor_comms import xaacp_turn_mirror_append as fn
        else:  # pragma: no cover - STREAM_CORE_API lookup already raised
            raise ConsulAuthorityUnavailable(f"unknown stream-core capability {capability!r}")
        try:
            out = fn(self.root, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported as an outcome, never silent
            raise ConsulAuthorityUnavailable(f"{name}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(out, dict):
            raise ConsulAuthorityUnavailable(f"{name} returned {type(out).__name__}")
        if out.get("ok") is False and (self.remote or capability != "turn_mirror"):
            raise ConsulAuthorityUnavailable(
                f"{name} refused: {out.get('status')}: {out.get('error') or ''}"
            )
        return out


def resolve_consul_seat(
    project_root: Path,
    payload: dict[str, Any],
    *,
    port: _ConsulPort | None = None,
) -> dict[str, Any] | None:
    """The seat THIS host session holds (``xaacp_seat_of_host``) -- or None.

    None for a subagent payload (agent_id present), an unresolvable managed
    route, and a host session holding no (or an ambiguous) Consul seat. A door
    that cannot answer RAISES ``ConsulAuthorityUnavailable``: a bound project
    never silently skips the duty.
    """
    if str((payload or {}).get("agent_id") or "").strip():
        return None
    host = str((payload or {}).get("session_id") or "").strip()
    if not host:
        return None
    route = _resolve_route(project_root, payload)
    if route is None or route.get("actor_kind") == "subagent":
        return None
    port = port or _ConsulPort(project_root)
    sid = str(route["session_id"])
    out = port.call("seat_of_host", session_id=sid, host_session_id=host)
    role = str(out.get("seat_role") or "").strip()
    actor = str(out.get("actor_id") or "").strip()
    if role not in CONSUL_SEAT_ROLES or not actor or out.get("ambiguous"):
        return None
    peer_actor = str(out.get("peer_actor_id") or "").strip()
    if peer_actor == actor:
        peer_actor = ""
    return {
        "session_id": sid,
        "actor_id": actor,
        "route_actor_id": str(route.get("actor_id") or ""),
        "role": role,
        "host_session_id": host,
        "peer_role": str(out.get("peer_role") or next(r for r in CONSUL_SEAT_ROLES if r != role)),
        "peer_actor_id": peer_actor,
        "peer_status": "held" if peer_actor else "vacant",
        "peer_liveness": "live" if out.get("peer_live") else ("unproven" if peer_actor else ""),
        "authority": "remote" if port.remote else "local",
    }


def _park_state(port: _ConsulPort, seat: dict[str, Any]) -> dict[str, Any]:
    """``xaacp_last_park``: the authority's own park log (enter/return, written
    by xaacp_wait_next) + the activity marker. A Stop-time lease arm cannot
    forge it."""
    return port.call(
        "last_park",
        session_id=seat["session_id"],
        actor_id=seat["actor_id"],
        # the gate derives the actor from this host identity (cloud-bound Stop)
        host_session_id=seat["host_session_id"],
    )


def _iso_to_epoch(value: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


#: ai_msg modes that are RECEIVING, not work: the park itself and its reads.
_RECEIVE_MODES = frozenset({"wait_next", "xaacp_inbox", "inbox", "xaacp_ack"})

#: Bytes read from the END of the host transcript for the activity anchor.
_TRANSCRIPT_TAIL_BYTES = 1_048_576


def transcript_last_work_at(payload: dict[str, Any]) -> float | None:
    """When this actor last did WORK: the timestamp of the most recent tool_use
    in the host transcript that is not a wait_next park. Structure only --
    tool NAMES and the ai_msg mode; no tool output, no text, is ever read into
    anything. None when unknowable (no transcript, unreadable, no tool use)."""
    import json

    path = str((payload or {}).get("transcript_path") or "").strip()
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in reversed(content):
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = str(item.get("name") or "").lower()
            args = item.get("input") if isinstance(item.get("input"), dict) else {}
            if name.endswith("ai_msg") and str(args.get("mode") or "").lower() in _RECEIVE_MODES:
                continue
            return _iso_to_epoch(entry.get("timestamp"))
    return None


def _mirror_turn(
    port: _ConsulPort,
    seat: dict[str, Any],
    payload: dict[str, Any],
    *,
    causal_turn_id: str,
    state: dict[str, Any],
    park: dict[str, Any],
    now: float,
) -> None:
    import hashlib
    from datetime import UTC, datetime

    from .execution_index_store import ExecutionIndexStore

    project_root = port.root
    sid, actor = seat["session_id"], seat["actor_id"]
    marker = str(park.get("activity_marker") or "")
    # SUBSTANTIVE TURNS ONLY (r0a, cloud ping-pong fix): mirror iff the host
    # transcript shows non-receive WORK after this actor's last seal AND after
    # its last mirror. The stream activity marker is NOT a trigger: an inbound
    # peer mirror moves it, and mirroring on that made two Consuls wake each
    # other forever. Unknown work (no/unreadable transcript) -> no mirror,
    # audited; the park is still required by the caller.
    last_work_at = transcript_last_work_at(payload)
    if last_work_at is None:
        if str(state.get("last_mirror_turn") or "") != f"nowork:{causal_turn_id}":
            ExecutionIndexStore().record_event(
                project_root,
                event_kind="consul_turn_mirror",
                source_kind="hook",
                session_id=sid,
                action_kind="stop_mirror",
                target_entity=seat["peer_actor_id"],
                status="skipped",
                payload={"session_id": sid, "actor_id": actor, "causal_turn_id": causal_turn_id,
                         "reason": "work_unknown", "at": now},
            )
            _consul_state_put(project_root, sid, actor, last_mirror_turn=f"nowork:{causal_turn_id}")
        return
    floor = max(float(state.get("last_seal_at") or 0.0), float(state.get("last_mirror_at") or 0.0))
    if last_work_at <= floor:
        return
    visible = _sanitize_visible(
        str(payload.get("last_assistant_message") or payload.get("message") or "")
    )
    if not visible:
        return
    digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
    truncated = len(visible) > MIRROR_BODY_CAP
    pointer = f"aidocs-turn://{sid}/{actor}/{causal_turn_id}"
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    header = (
        f"[turn_end] {seat['role']} {actor} -> {seat['peer_role']} (informational; "
        "review for alignment)\n"
        f"causal_turn_id: {causal_turn_id}\n"
        f"operator_prompt_ref: {str(payload.get('prompt_id') or '')}\n"
        f"sent_at: {stamp}\n"
        f"content_sha256: {digest}\n"
        f"pointer: {pointer}\n"
        f"truncated: {'true' if truncated else 'false'}\n---\n"
    )
    body = header + (visible[:MIRROR_BODY_CAP] if truncated else visible)
    error = ""
    try:
        # stream-core's door: origin 'hook', the authority re-verifies the seat
        # from ITS derived host identity, idempotent on causal_turn_id.
        sent = port.call(
            "turn_mirror",
            session_id=sid,
            host_session_id=seat["host_session_id"],
            body=body,
            sha256=digest,
            pointer=pointer,
            causal_turn_id=causal_turn_id,
        )
    except ConsulAuthorityUnavailable as exc:
        sent, error = {"ok": False, "status": "authority_unavailable"}, str(exc)[:300]
    ok = bool(sent.get("ok"))
    ExecutionIndexStore().record_event(
        project_root,
        event_kind="consul_turn_mirror",
        source_kind="hook",
        session_id=sid,
        action_kind="stop_mirror",
        target_entity=seat["peer_actor_id"],
        status="ok" if ok else "failed",
        payload={
            "session_id": sid,
            "actor_id": actor,
            "sender_seat": seat["role"],
            "peer_actor_id": seat["peer_actor_id"],
            "peer_seat": seat["peer_role"],
            "causal_turn_id": causal_turn_id,
            "message_id": str(sent.get("message_id") or ""),
            "duplicate": bool(sent.get("duplicate")),
            "content_sha256": digest,
            "content_len": len(visible),
            "truncated": truncated,
            "pointer": pointer,
            "origin": "hook",
            "authority": seat.get("authority"),
            "send_status": str(sent.get("status") or ("ok" if ok else "")),
            "error": error,
            "at": now,
        },
    )
    if ok:
        try:
            after = str(_park_state(port, seat).get("activity_marker") or marker)
        except ConsulAuthorityUnavailable:
            after = marker
        _consul_state_put(
            project_root, sid, actor,
            last_mirror_turn=causal_turn_id, last_mirror_marker=after, last_mirror_at=now,
        )


def _stop_outcome(project_root: Path, seat: dict[str, Any], outcome: str, now: float, **extra: Any) -> None:
    from .execution_index_store import ExecutionIndexStore

    ExecutionIndexStore().record_event(
        project_root,
        event_kind="consul_stop_outcome",
        source_kind="hook",
        session_id=seat.get("session_id"),
        action_kind="stop",
        target_entity=seat.get("actor_id") or seat.get("host_session_id"),
        status=outcome,
        payload={
            "session_id": seat.get("session_id"),
            "actor_id": seat.get("actor_id"),
            "seat": seat.get("role"),
            "host_session_id": seat.get("host_session_id"),
            "peer_actor_id": seat.get("peer_actor_id"),
            "peer_present": bool(seat.get("peer_actor_id")),
            "authority": seat.get("authority"),
            "outcome": outcome,
            "at": now,
            **extra,
        },
    )


def _authority_unavailable(
    project_root: Path, payload: dict[str, Any], seat: dict[str, Any] | None, error: str, now: float
) -> None:
    """A BOUND project whose authority cannot answer: never trap the turn,
    never skip silently -- the outcome is a retained DECISION row."""
    base = dict(seat or {})
    if not base:
        route = _resolve_route(project_root, payload) or {}
        base = {
            "session_id": route.get("session_id"),
            "host_session_id": str(payload.get("session_id") or ""),
            "authority": "remote",
        }
    _stop_outcome(project_root, base, OUTCOME_AUTHORITY_UNAVAILABLE, now, error=error[:300])


def consul_stop_duty(
    project_root: Path,
    *,
    event_name: str,
    payload: dict[str, Any],
    frozen: Any = False,
    now: float | None = None,
) -> dict[str, Any] | None:
    """#1061: the seated-Consul Stop duty. Returns a Stop block decision or None.

    None (Stop unchanged) for anything that is not a genuine Stop of a seated
    Consul, and for a bound project whose authority cannot answer (audited
    ``authority_unavailable``). See the state machine above.
    """
    import hashlib
    import time

    if str(event_name or "") != "Stop":
        return None
    if str((payload or {}).get("agent_id") or "").strip():
        return None
    ts = float(now if now is not None else time.time())
    port = _ConsulPort(project_root)
    try:
        seat = resolve_consul_seat(project_root, payload, port=port)
    except ConsulAuthorityUnavailable as exc:
        _authority_unavailable(project_root, payload, None, str(exc), ts)
        return None
    if seat is None:
        return None
    sid, actor = seat["session_id"], seat["actor_id"]
    state = _consul_state(project_root, sid, actor)
    anchor = float(state.get("last_seal_at") or 0.0)
    causal_turn_id = str(payload.get("turn_id") or "").strip() or hashlib.sha256(
        f"{sid}:{actor}:{seat['host_session_id']}:{anchor!r}".encode()
    ).hexdigest()[:24]
    # 2b. PAIR ONLY (operator ruling 2026-09-15). The forced park exists so the
    # Consul PAIR can reach each other and the operator between turns. A lone
    # seat -- typically an agent that once ran ai_seat(enter) in some project
    # and kept working -- has no peer to be reached by, and blocking every one
    # of its Stops made ordinary project work unusable. It keeps the soft
    # receive request (arm_on_stop) and is audited once per turn; never blocked.
    if not seat["peer_actor_id"]:
        if str(state.get("last_mirror_turn") or "") != f"absent:{causal_turn_id}":
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="consul_peer_absent",
                source_kind="hook",
                session_id=sid,
                action_kind="stop_mirror",
                target_entity=seat["peer_role"],
                status="peer_absent",
                payload={
                    "session_id": sid,
                    "actor_id": actor,
                    "seat": seat["role"],
                    "peer_seat": seat["peer_role"],
                    "peer_status": seat["peer_status"],
                    "peer_liveness": seat["peer_liveness"],
                    "causal_turn_id": causal_turn_id,
                    "at": ts,
                },
            )
            _stop_outcome(project_root, seat, OUTCOME_LONE_SEAT_NOT_ENFORCED, ts,
                          causal_turn_id=causal_turn_id)
            _consul_state_put(project_root, sid, actor, last_mirror_turn=f"absent:{causal_turn_id}")
        # No last_seal_at: nothing was discharged, and the anchor keeps the
        # causal turn stable so a re-stop is not re-audited.
        return None
    try:
        park = _park_state(port, seat)
    except ConsulAuthorityUnavailable as exc:
        _authority_unavailable(project_root, payload, seat, str(exc), ts)
        return None

    # 3. mirror -- only a pair reaches here (a lone seat returned at 2b).
    # A failed mirror must not cancel the park order: contained, audited.
    try:
        _mirror_turn(
            port, seat, payload,
            causal_turn_id=causal_turn_id, state=state, park=park, now=ts,
        )
    except Exception as exc:  # noqa: BLE001
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="consul_turn_mirror",
            source_kind="hook",
            session_id=sid,
            action_kind="stop_mirror",
            target_entity=seat["peer_actor_id"],
            status="failed",
            payload={
                "session_id": sid,
                "actor_id": actor,
                "peer_actor_id": seat["peer_actor_id"],
                "causal_turn_id": causal_turn_id,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "at": ts,
            },
        )

    # 6. freeze: order a park only while the park is performable.
    if callable(frozen):
        frozen = bool(frozen())
    if frozen:
        from .operation_classes import freeze_remedy_reachable

        if not freeze_remedy_reachable("ai_msg", {"mode": "wait_next"}):
            _stop_outcome(project_root, seat, OUTCOME_FROZEN_YIELD, ts, causal_turn_id=causal_turn_id)
            _consul_state_put(project_root, sid, actor, last_seal_at=ts)
            return None

    # 4. park proof: a wait_next RETURN that is later than BOTH this actor's
    # last seal AND its last WORK (last non-park tool_use in the host
    # transcript). A park taken mid-turn and followed by more work does not
    # discharge the turn end (r0a send-back).
    # stream-core: activity_marker moves only on stream events, never on tool
    # calls, so the WORK anchor is this hook's own turn ledger (the host
    # transcript) compared to when the park was ENTERED.
    park_at = float(park.get("last_park_return_at") or 0.0)
    entered_at = float(park.get("last_park_enter_at") or park_at or 0.0)
    last_work_at = transcript_last_work_at(payload)
    parked_after_work = last_work_at is None or entered_at > last_work_at
    if park_at > anchor and parked_after_work:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="consul_park_return",
            source_kind="hook",
            session_id=sid,
            action_kind="park_observed_at_stop",
            target_entity=actor,
            status=str(park.get("last_return_kind") or ""),
            payload={
                "session_id": sid,
                "actor_id": actor,
                "seat": seat["role"],
                "return_kind": str(park.get("last_return_kind") or ""),
                "entered_at": park.get("last_park_enter_at"),
                "returned_at": park_at,
                "last_work_at": last_work_at,
                "cursor": park.get("cursor"),
                "observed_via": "conductor_comms.xaacp_last_park",
                "causal_turn_id": causal_turn_id,
                "at": ts,
            },
        )
        _stop_outcome(
            project_root, seat, OUTCOME_SEALED_AFTER_PARK, ts,
            causal_turn_id=causal_turn_id, park_returned_at=park_at, last_work_at=last_work_at,
        )
        _consul_state_put(project_root, sid, actor, last_seal_at=ts)
        return None

    # 5. loop safety.
    if bool(payload.get("stop_hook_active")):
        _stop_outcome(
            project_root, seat, OUTCOME_MISSED_PARK, ts,
            causal_turn_id=causal_turn_id, park_returned_at=park_at or None, last_work_at=last_work_at,
        )
        _consul_state_put(project_root, sid, actor, last_seal_at=ts)
        return None

    _stop_outcome(
        project_root, seat, OUTCOME_BLOCKED_FOR_PARK, ts,
        causal_turn_id=causal_turn_id, park_returned_at=park_at or None, last_work_at=last_work_at,
    )
    return {"decision": "block", "reason": STOP_PARK_REASON}
