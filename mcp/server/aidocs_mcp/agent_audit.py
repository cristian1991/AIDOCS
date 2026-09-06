"""Conductor-level agent audit — the role-based roster of CONNECTED agents.

The lane-worker roster (``ai_lane(action='agents')`` over session_lane_agents)
lists spawned SUBAGENTS. THIS audit surfaces the actual connected interactive
agents (conductors): one row per ``aidocs_managed_per_conductor`` binding,
keyed by ``cli_session_id`` (= host_session_id = the agent identity), enriched
with:

  * ``role``            -- from the messagerie's ``msg_role_map`` (the
                          cross-agent communication identity);
  * ``agent_context_id`` / ``agent_memory_epoch`` -- the durable identity
                          stack (project + host_kind + host_session_id);
  * ``live``            -- positive evidence only: current caller, shared XAACP
                          presence in this MCP generation, or a live binder pid;
  * ``session_id``      -- the work session the agent is bound to;
  * ``lane_workers``    -- the lane subagents this agent spawned, nested.

Reads through the store APIs / server-internal sqlite (the identity dbs are
gate-protected against raw agent reads), so this is server-internal only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ._sqlite_connect import connect as _canonical_connect
from .aidocs_managed_store import AidocsManagedStore
from .session_lane_agents_store import _pid_alive

_BOOT_PID_RE = re.compile(r"^mcp-(\d+)-")


def _boot_token_pid(token: str) -> int | None:
    """Parse the pid from a ``mcp-<pid>-<unix>-<hash>`` boot token, else None."""
    m = _BOOT_PID_RE.match(str(token or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


#: What an agent that has taken NO seat reports as its role.
#
# Deliberately NOT a member of `conductor_comms.MSG_ROLES` -- it is the ABSENCE
# of a seat, not a seat, and msg_send must keep rejecting it as a target. The
# same discipline `conductor_comms` already applies to its own non-MSG_ROLES
# marker (#215).
UNSEATED = "unseated"


def _roles_by_host(project_root: Path) -> dict[str, tuple[str, str]]:
    """host_session_id -> (messagerie role, THE SESSION IT IS SCOPED TO).

    THE SCOPE COMES BACK. This selected ``host_session_id, role`` and dropped
    ``session_id`` on the floor, so a seat taken on ONE session answered for
    whichever session the agent happened to be bound to -- and a row with a
    BLANK scope answered for every session there is.

    Measured on the operator's box 2026-08-24: nine rows, all
    ``role='conductor'``, four of them with a blank ``session_id`` (the legacy
    `cerberus_role_map` migration copies ``(host_session_id, role)`` and
    nothing else). Under the old select, all four were conductors of anything.

    Same defect class as the ``roles.get(hsid, "conductor")`` default removed
    from this file the day before: a confident answer from a source that never
    knew. There the missing fact was the seat; here it is the seat's SCOPE.
    """
    try:
        from .conductor_comms import _connect

        with _connect(project_root) as conn:
            rows = conn.execute(
                "SELECT host_session_id, role, session_id FROM msg_role_map",
            ).fetchall()
        return {
            r["host_session_id"]: (
                str(r["role"] or ""),
                str(r["session_id"] or ""),
            )
            for r in rows
        }
    except Exception:
        return {}


def _host_kinds(project_root: Path) -> dict[str, str]:
    """host_session_id -> host_kind, from agent_memory_compaction_state
    (best-effort; absent until the agent's first compaction)."""
    try:
        from .agent_memory_epoch import _db_path

        db = _db_path(project_root)
        if not db.exists():
            return {}
        # Canonical connect (#755). read_only=True is both the pragma fix and
        # an honest declaration: an AUDIT READER has no business being able to
        # write the store it is auditing. The `with` also CLOSES the handle --
        # sqlite3's own context manager commits the transaction and leaks the
        # connection, and this one is created right here, so it is ours.
        with _canonical_connect(db, read_only=True) as conn:
            rows = conn.execute(
                "SELECT host_kind, host_session_id FROM agent_memory_compaction_state",
            ).fetchall()
        return {r["host_session_id"]: r["host_kind"] for r in rows}
    except Exception:
        return {}




def _xaacp_current_generation_hosts(project_root: Path) -> set[str]:
    """Host sessions positively observed through this MCP generation's XAACP path.

    This is shared evidence, unlike the caller rung.  A row only enters this set
    when that exact actor called XAACP through the current process boot token.
    A mismatch/absent stamp is merely no evidence here — never a death verdict.
    Any read/schema failure therefore returns the empty set (no extra grants).
    """
    try:
        from .conductor_comms import _connect
        from .managed_mode_service import current_boot_token

        boot = str(current_boot_token() or "").strip()
        if not boot:
            return set()
        with _connect(project_root) as conn:
            rows = conn.execute(
                "SELECT host_session_id FROM xaacp_actors "
                "WHERE last_seen_boot_token=? AND host_session_id != ''",
                (boot,),
            ).fetchall()
        return {str(r["host_session_id"] or "").strip() for r in rows if r["host_session_id"]}
    except Exception:
        return set()
def liveness_by_host_session(
    project_root: Path,
    *,
    session_id: str = "",
    caller_host_session_id: str = "",
) -> dict[str, Any]:
    """THE liveness projection, for consumers that route or roster by actor.

    Local backlog 987. XAACP had its own answer to "who is here": every
    historical `xaacp_actors` row, unconditionally `addressable: True`. Measured
    on the gate — `xaacp_directory` offered 7 addressable actors while
    `ai_agents` independently reported 2 live + 5 unverifiable from the SAME
    store. Two tools, one question, two answers, and only one of them honest.

    So this is a PROJECTION of `connected_agents_audit`, not a second oracle.
    Nothing here probes anything: it reshapes that audit's verdicts into a
    host_session_id lookup, which is the key `xaacp_actors` already carries.

    WHAT IT INHERITS, and why that is the point:
      * the CALLER rung — an agent issuing this very call is provably live
        whatever any stamp says;
      * the SHARED XAACP-current-generation rung — if another actor has itself
        spoken through this MCP generation, every caller sees that same positive
        evidence instead of each directory declaring only itself live;
      * pid used ONLY POSITIVELY. A failed probe is UNVERIFIABLE, never dead
        (#603), because the only pid available is the SERVER's, not the actor's.
        This is what keeps 987 from reintroducing the actor-death predicate
        `dd19b8b40` removed — there is no rung here that concludes "dead".
      * no TTL. An arbitrary clock would be pid-death with extra steps.

    Returns ``{"by_host": {hsid: {live, live_source, reason}}, "roster_status",
    "roster_status_reason"}``. A host_session_id ABSENT from `by_host` is
    unknown, and unknown is not live — callers must treat a miss as
    unverifiable rather than as permission.
    """
    audit = connected_agents_audit(
        project_root,
        include_dead=True,
        session_id=session_id,
        caller_host_session_id=caller_host_session_id,
    )
    by_host: dict[str, dict[str, Any]] = {}
    for entry in audit.get("agents", []) or []:
        hsid = str(entry.get("host_session_id") or "").strip()
        if hsid:
            by_host[hsid] = {
                "live": True,
                "live_source": str(entry.get("live_source") or ""),
                "reason": "",
            }
    for entry in audit.get("unverifiable", []) or []:
        hsid = str(entry.get("host_session_id") or "").strip()
        if hsid and hsid not in by_host:
            by_host[hsid] = {
                "live": False,
                "live_source": "",
                "reason": str(entry.get("liveness_reason") or "liveness could not be verified"),
            }
    return {
        "by_host": by_host,
        "roster_status": str(audit.get("roster_status") or "ok"),
        "roster_status_reason": str(audit.get("roster_status_reason") or ""),
    }


def connected_agents_audit(
    project_root: Path,
    *,
    include_dead: bool = False,
    role: str = "",
    session_id: str = "",
    caller_host_session_id: str = "",
) -> dict[str, Any]:
    """Role-based audit of CONNECTED agents (conductors), keyed by
    host_session_id.

    Filters: ``role`` / ``session_id``. ``include_dead`` returns agents whose
    liveness could not be confirmed (separately, for audit). Each agent nests
    its spawned ``lane_workers``.

    LIVENESS HONESTY (#603). The only pid this audit can probe is the one
    baked into ``bound_by_boot_token`` -- and that is the pid of the MCP
    SERVER PROCESS THAT BOUND the agent, NOT the agent's own process. When
    that server generation exits (a deploy hot-swap, a restart, a crash) the
    probe goes dead for EVERY binding it stamped, while those agents are
    still connected and still calling. Reporting them as dead emptied the
    roster, and an empty roster is indistinguishable from an authoritative
    "nobody is connected".

    So a failed pid probe now means UNVERIFIABLE, not dead, and the roster
    carries its own trustworthiness:

      * ``roster_status`` -- ``ok`` (every binding decided; an empty
        ``agents`` really does mean nobody), ``degraded`` (some agents
        verified, but the list is known to be incomplete), or
        ``unavailable`` (nothing could be verified while at least one
        binding exists -- an empty ``agents`` here must NOT be read as idle).
      * ``roster_status_reason`` -- prose for a non-``ok`` status.
      * ``unverifiable`` / ``unverifiable_count`` -- the bindings whose
        liveness is unknown. They are listed so they are not lost, never
        counted as live, and never presented as confirmed.

    Positive liveness has three rungs, in order: the current caller; an actor
    that has itself spoken through XAACP in this MCP generation (a durable,
    shared fact all callers can see); then a still-live binding-server PID.
    Entries carry ``live_source`` (``caller``, ``xaacp_current_generation`` or
    ``boot_token_pid``) so a reader can tell exactly what proved liveness.
    A caller missing from the registry proves the roster is incomplete.

    Status is computed over the UNFILTERED bindings, so a narrowing ``role``
    or ``session_id`` filter can never manufacture a scary status.
    """
    store = AidocsManagedStore()
    conductors = store.list_conductors(project_root)
    roles = _roles_by_host(project_root)
    host_kinds = _host_kinds(project_root)
    xaacp_current_hosts = _xaacp_current_generation_hosts(project_root)
    caller = (caller_host_session_id or "").strip()

    # Lane subagents grouped by the agent (host_session_id) that spawned them.
    from .cross_agent_coordination import connected_agents as _lane_agents

    workers_by_host: dict[str, list] = {}
    for w in _lane_agents(project_root, live_only=False):
        workers_by_host.setdefault(w.get("host_session_id", ""), []).append(w)

    live: list[dict[str, Any]] = []
    unverifiable: list[dict[str, Any]] = []
    # Unfiltered tallies -- the status must describe the WHOLE roster.
    total_live = 0
    total_unverifiable = 0
    caller_registered = False
    for c in conductors:
        hsid = c.get("cli_session_id", "")
        pid = _boot_token_pid(c.get("bound_by_boot_token", ""))
        if caller and hsid == caller:
            caller_registered = True
            # Strongest proof: this exact agent is issuing the current call.
            is_live, live_source = True, "caller"
        elif hsid in xaacp_current_hosts:
            # Shared positive proof: this actor itself has spoken through the
            # current MCP generation's XAACP path. Unlike the caller rung, every
            # other conversation can observe the same durable fact.
            is_live, live_source = True, "xaacp_current_generation"
        elif bool(pid) and _pid_alive(pid):
            is_live, live_source = True, "boot_token_pid"
        else:
            is_live, live_source = False, ""
        if is_live:
            total_live += 1
        else:
            total_unverifiable += 1

        if session_id and c.get("session_id") != session_id:
            continue
        # UNSEATED, NOT CONDUCTOR. This defaulted to the most privileged role
        # in the system, so every agent that had merely CONNECTED was reported
        # as holding the conductor seat. Measured 2026-08-23: `ai_agents` showed
        # bc8bd9e3 as role=conductor/session=ubermega while that id had no row
        # in msg_role_map at all -- the seat belonged to a different id. The
        # tool an operator would use to ask "who holds the seat" answered with a
        # fabrication, and the role FILTER below returned unseated agents to a
        # caller asking for conductors.
        #
        # A seat is TAKEN (`ai_seat enter` / `co-enter`), never inherited by
        # connecting. Connection and seat are different facts; only the first
        # was ever being measured here.
        #
        # AND THE SEAT'S SCOPE MUST MATCH THE BINDING'S SESSION. A seat is a
        # fact about a (session, role) pair -- operator law is "only 1
        # conductor and co-conductor can be active on an AIDOCS SESSION". A
        # row scoped to another session, or to NO session, is not this agent's
        # seat; `seat_scope_matches` refuses both, and blank never matches
        # blank.
        from .conductor_comms import seat_scope_matches

        seat_role, seat_scope = roles.get(hsid, ("", ""))
        agent_role = (
            seat_role
            if seat_role and seat_scope_matches(seat_scope, c.get("session_id"))
            else UNSEATED
        )
        if role and agent_role != role:
            continue
        kind = host_kinds.get(hsid, "")
        ctx = ""
        epoch = ""
        try:
            from .agent_memory_epoch import current_epoch, derive_agent_context_id

            ctx = derive_agent_context_id(
                host_kind=kind, project_root=project_root, host_session_id=hsid
            )
            epoch = current_epoch(project_root, host_kind=kind, host_session_id=hsid)
        except Exception:
            pass
        entry: dict[str, Any] = {
            "host_session_id": hsid,
            "agent_context_id": ctx,
            "agent_memory_epoch": epoch,
            "role": agent_role,
            "session_id": c.get("session_id", ""),
            "host_kind": kind,
            "live": is_live,
            "live_source": live_source,
            "pid": pid,
            "activated_at": c.get("activated_at", ""),
            "last_updated": c.get("last_updated", ""),
            "source": c.get("source", ""),
            "lane_workers": workers_by_host.get(hsid, []),
        }
        if is_live:
            live.append(entry)
        else:
            entry["liveness"] = "unverifiable"
            entry["liveness_reason"] = (
                "the MCP server generation that stamped this binding has "
                "exited, so this agent's liveness cannot be probed -- it may "
                "still be connected"
            )
            unverifiable.append(entry)

    if caller and not caller_registered:
        status = "degraded"
        reason = (
            f"the calling agent '{caller}' is provably connected but has no "
            f"binding in the registry, so this roster is incomplete"
        )
    elif total_unverifiable and total_live == 0:
        status = "unavailable"
        reason = (
            f"{total_unverifiable} binding(s) exist but NONE could be verified "
            f"live (their MCP server generation has exited) -- this empty "
            f"roster does NOT mean nobody is connected"
        )
    elif total_unverifiable:
        status = "degraded"
        reason = (
            f"{total_unverifiable} binding(s) could not be verified live "
            f"(their MCP server generation has exited) -- this roster is "
            f"incomplete, not authoritative"
        )
    else:
        status = "ok"
        reason = ""

    result: dict[str, Any] = {
        "agents": live,
        "live_count": len(live),
        "roster_status": status,
        "roster_status_reason": reason,
        "unverifiable": unverifiable,
        "unverifiable_count": len(unverifiable),
    }
    if include_dead:
        # Back-compat alias: the historical bucket name for the same rows.
        result["dead"] = unverifiable
        result["dead_count"] = len(unverifiable)
    return result
