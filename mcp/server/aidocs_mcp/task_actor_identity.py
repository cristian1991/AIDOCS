"""Per-actor task-slot identity (#463) + subagent lane auto-bind (#457).

One seam answers "who owns the task slot for THIS call?" for every
consumer (RuntimeService.task_*, the universal task gate, execution-event
task attribution, todo-state ownership). Before this module each consumer
re-derived worker-ness from the env/principal pair independently, so a
caller could be a worker for one store and the conductor for another —
the #463 stomp (two concurrent fables sharing the session's single
active-task slot) grew out of exactly that divergence.

Identity doctrine (#457, Emperor ruling 2026-07-18):

- The LANE ID is the durable respawn identity of a spawned agent. It is
  derived from the AUTHENTICATED chain only — spawn-path env stamps
  (written by the conductor's dispatcher into the subprocess env),
  the #217 ``session_lane_agents`` registry (rows written by the spawn
  path), or the identity resolver — never from agent-supplied tool
  arguments.
- Derivation key = authenticated user + SPAWNER's canonical identity +
  lane slot. The spawned agent's own (rotating) host identity is
  attribution on events, never the binding key: the same conductor
  respawning the same slot for the same user yields the SAME lane id.
- A caller with no worker evidence at all (the operator, the conductor
  seat) resolves as non-worker and behaves exactly as before.

Resolution order for "is this caller a worker?" (first hit wins):

1. ``AIDOCS_EXPERT_LANE_ID`` env — spawn-path stamp on lane subprocesses.
2. ``identity_resolver.current_principal_type == 'subagent'``.
3. ``protected_file_runtime.is_sub_agent_call()`` — the one-way per-process
   latch set by the spawn/bind middleware.
4. A *running* ``session_lane_agents`` row whose stamped
   ``agent_context_id`` equals the caller's canonical id (#217 chain).

Lane resolution for a worker (first non-empty wins): env stamp →
registry row → deterministic derivation (user + spawner + slot).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any

_LANE_VERSION_TAG = "subagent-lane:v1:"

# Registry-lookup micro-cache: record_event is a hot path and must not
# open the execution-index sqlite on every event for every caller. Keyed
# by (project_root, agent_context_id); entries expire after TTL seconds.
_REGISTRY_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_REGISTRY_CACHE_TTL_SECONDS = 15.0
_REGISTRY_CACHE_LOCK = threading.Lock()


def reset_registry_cache_for_tests() -> None:
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.clear()


def derive_subagent_lane_id(
    *,
    user_id: str,
    spawner_agent_context_id: str,
    lane_slot: str = "0",
) -> str:
    """Deterministic lane id from the authenticated spawning lineage.

    Same (user, spawner, slot) → same lane id, forever — the respawn
    identity that lets a re-dispatched agent re-attach to its mailbox,
    scope stamps and context (#457, substrate for #157 park/respawn).
    Returns "" when either identity half is missing: no derivation
    without an authenticated chain.
    """
    uid = str(user_id or "").strip()
    spawner = str(spawner_agent_context_id or "").strip()
    if not uid or not spawner:
        return ""
    slot = str(lane_slot or "").strip() or "0"
    payload = f"{_LANE_VERSION_TAG}{uid}:{spawner}:{slot}"
    return "lane_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _spawner_agent_context_id(project_root: Path | str) -> str:
    """The spawner's canonical identity, from spawn-path env stamps only.

    Preference order:
    1. ``AIDOCS_SPAWNER_AGENT_CONTEXT_ID`` — the conductor's own
       agent_context_id, stamped by the dispatcher at spawn.
    2. ``AIDOCS_EXPERT_SESSION_ID`` — the conductor's work-session label
       (also a spawn-path stamp), reduced to its deterministic
       session_uuid so the derivation input is a canonical id, not a
       free-form label.
    Both are process-env values written by the SPAWNING side before
    exec — the subagent cannot mint them through tool arguments.
    """
    stamped = os.environ.get("AIDOCS_SPAWNER_AGENT_CONTEXT_ID", "").strip()
    if stamped:
        return stamped
    conductor_session = os.environ.get("AIDOCS_EXPERT_SESSION_ID", "").strip()
    if conductor_session:
        try:
            from .agent_memory_epoch import derive_session_uuid

            return derive_session_uuid(project_root, conductor_session)
        except Exception:
            return ""
    return ""


def _lane_slot() -> str:
    """The lane ordinal / plan slot for derivation. Spawn-path env stamp
    only; defaults to "0" (a spawner that names no slot gets one stable
    lane per (user, spawner) pair). Deliberately NOT AIDOCS_EXPERT_ID —
    that value rotates per spawn and would break respawn determinism.
    """
    return os.environ.get("AIDOCS_EXPERT_LANE_SLOT", "").strip() or "0"


def _registry_lane_for_actor(project_root: Path | str, actor_id: str) -> str:
    """Lane id from the #217 registry for this actor's RUNNING row, or ""."""
    # #1043b: `not actor_id` does not exclude the refusal, which is a non-empty
    # string. Left in, it becomes a registry LOOKUP KEY and — worse — a
    # _REGISTRY_CACHE key shared by every unstamped subagent, so one would be
    # handed another's cached lane.
    if not actor_id or identity_is_unproven(actor_id):
        return ""
    key = (str(project_root), actor_id)
    now = time.monotonic()
    with _REGISTRY_CACHE_LOCK:
        hit = _REGISTRY_CACHE.get(key)
        if hit is not None and (now - hit[0]) < _REGISTRY_CACHE_TTL_SECONDS:
            return hit[1]
    lane = ""
    try:
        from .session_lane_agents_store import SessionLaneAgentsStore

        row = SessionLaneAgentsStore().find_latest_by_agent_context_id(
            Path(str(project_root)),
            actor_id,
            state_filter="running",
        )
        if row is not None:
            lane = str(row.get("lane_id") or "")
    except Exception:
        lane = ""
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE[key] = (now, lane)
    return lane


#: The THIRD identity state (#1043): worker/subagent evidence is PROVEN and the
#: exact actor axis is ABSENT.
#:
#: It is a distinguished VALUE, not "", and that is the whole point. "" already
#: means "legacy actorless caller — keep the pre-#483 SESSION-LEVEL slot", which
#: is the slot the conductor uses. Returning "" to refuse a substitution
#: therefore handed the caller the conductor's task by a second route:
#: `resolve_caller_task_id` reads `if not actor_id: return session_task`.
#:
#: As a non-empty id it also fails closed in every consumer that was never
#: taught about this state: keyed by actor, an unproven subagent lands in its
#: OWN slot rather than the shared one, so it cannot claim, file against, or
#: complete the conductor's task even where no explicit check exists. The
#: explicit refusals below are the second layer, not the only one.
#:
#: Deliberately not hash-shaped: a derived actor id is hex, so this can never
#: collide with one, and it reads as itself in a log or a DB row.
UNPROVEN_SUBAGENT_ACTOR = "__aidocs_unproven_subagent__"


def identity_is_unproven(actor_id: str | None) -> bool:
    """True for the refused state — worker proven, actor axis missing."""
    return str(actor_id or "") == UNPROVEN_SUBAGENT_ACTOR


class UnprovenActorIdentity(RuntimeError):
    """Raised where a refusal must not be REPRESENTABLE as an actor value.

    A REFUSAL MUST NOT SHARE A TYPE WITH A GRANT, which is the lesson four
    iterations of this bug have taught in turn:

        #599     keyed per actor, derived the actor WITHOUT agent_id
        #1039 S1 added agent_id, read "unstamped" as "main thread"
        #1042b   refused with ""      -> read downstream as LEGACY CONSENT
        #1043    refused with a value -> read downstream as AN ACTOR (truthy)

    Every `str` is either falsy or truthy, and this codebase reads falsy as
    "legacy caller, use the shared session slot" and truthy as "a real actor".
    There is therefore NO safe string, and the distinguished value can only be a
    DETECTOR at boundaries that check it explicitly — never a value handed to a
    consumer that expects a canonical id.

    So at the seams where the triple is CONSUMED to create actor-owned state —
    task/todo rows, lane bindings, event attribution — the refusal is raised
    instead of returned. A caller that forgets to check gets an exception, not
    a silently-attributed write.
    """

    @staticmethod
    def is_refused(actor_id: str | None) -> bool:
        return identity_is_unproven(actor_id)


def subagent_evidence(project_root: Path | str) -> str:
    """Non-empty when something says THIS CALLER IS A SUBAGENT.

    "Subagent" here means ONE SPECIFIC HAZARD, not the job title: a caller that
    would derive THE SAME ACTOR ID AS ITS CONDUCTOR. That is what makes the
    missing agent axis dangerous — the id is not merely unknown, it is somebody
    else's. A Claude Code subagent has exactly that shape: it shares its
    parent's ``host_session_id`` (measured 2026-08-22, CC 2.1.239) and differs
    only in ``agent_id``, so without the stamp it IS the conductor.

    A LANE WORKER IS NOT THAT SHAPE, and treating it as one was a real defect
    in the first cut of this function (#1042b, corrected in #1043b). A lane
    worker is a separate PROCESS with its OWN host identity, so it derives its
    own distinct actor id with no agent_id at all — nothing to collapse onto and
    nothing to steal. Refusing it cost a genuine, documented production flow:
    the first governed ``task_begin`` in a lane auto-binds the lane in the #217
    registry, and that begin happens BEFORE any registry row exists to prove
    workerhood by. Six pre-existing tests failed on exactly that path, which is
    how the over-reach was caught.

    So the lane env stamp is not evidence here, and it also SUPPRESSES the other
    two: ``identity_resolver`` derives ``principal_type == "subagent"`` FROM
    ``AIDOCS_EXPERT_LANE_ID`` (see its step 1), so inside a lane worker those
    signals are the same fact wearing a second name, not corroboration.

    Reads only signals that do NOT depend on an actor id, so it can be consulted
    from inside `stable_actor_id` without recursion:
      * ``identity_resolver.current_principal_type() == "subagent"``;
      * ``protected_file_runtime.is_sub_agent_call()``.
    (The #217 registry-lane rung is deliberately excluded: it resolves BY actor
    id, which is the value under construction here.)

    Returns the winning signal's name for diagnostics, "" when none fires.
    """
    try:
        # A lane worker owns its host identity — see the docstring. Not
        # evidence, and it disqualifies the two signals derived from it.
        if os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
            return ""
    except Exception:
        pass
    try:
        from .identity_resolver import current_principal_type

        if current_principal_type(Path(str(project_root))) == "subagent":
            return "principal_type"
    except Exception:
        pass
    try:
        from .protected_file_runtime import is_sub_agent_call

        if is_sub_agent_call():
            return "sub_agent_call"
    except Exception:
        pass
    return ""


def stable_actor_id(project_root: Path | str) -> str:
    """The caller's canonical actor id, resolved through the #587 authority.

    MEASURED ON THE LIVE SESSION while fixing #599: an agent's own
    task_complete could not find the slot its own task_begin had written
    seconds earlier. The reason was not the slot — it was the KEY. This
    seam used to derive the id from ``current_calling_agent_context_id``,
    which reads the RAW request-scoped accessors; when nothing was
    stamped those substitute the ``"unknown"`` placeholder, so the same
    agent hashed to one id on the request that stamped a real kind and a
    different id on the request that did not. A slot keyed by a value
    that changes between two calls is not a slot.

    ``resolve_host_identity`` is the one authority (#587-A): explicit →
    request stamp → process stamp → THE DURABLE RECORD (which is what
    makes the answer survive a request boundary) → env sniff, every rung
    normalised, and it strips the ``"unknown"`` bucket rather than
    hashing it. The same repair commit ``63a3432aa`` made for the freeze
    store's resolver.

    Returns "" when the host genuinely cannot be identified — an honest
    empty, not an invented default. Such a caller is actor-less and
    keeps the pre-#483 session-slot contract.

    #1039 S1 — THE SUBAGENT AXIS. A Claude Code subagent shares its
    parent's host_session_id (measured 2026-08-22, CC 2.1.239); the only
    field that differs is ``agent_id``, stamped on the request by the
    hook / #1007 call-claim path and read through
    ``current_calling_agent_id`` (no process-global rung — an unstamped
    call is honestly agent-less, i.e. the main thread). Derived WITHOUT
    it, the conductor and every subagent of that conversation hashed to
    ONE actor id, so a subagent's task_complete found "its own" active
    row — the conductor's — and closed it; the conductor's next
    ai_replace batch was refused no_active_task (operator report
    2026-09-07, twice). The strike scope (security_violation_service)
    and the freeze scope (clear_freeze_service) were already re-keyed
    onto the agent_id-carrying id; the task slot now follows the same
    precedent. Byte-identical for agent-less callers, so no live slot
    row rotates.
    """
    try:
        from .agent_memory_epoch import derive_agent_context_id, resolve_host_identity

        kind, sid = resolve_host_identity(project_root=project_root)
        if not kind or not sid:
            return ""
        try:
            from .mcp_server_runtime_helpers import current_calling_agent_id

            agent_id = current_calling_agent_id()
        except Exception:
            agent_id = ""
        # A SUBAGENT WITHOUT ITS AXIS IS UNIDENTIFIED, NOT ITS PARENT (#1042).
        #
        # The S1 fix assumed an unstamped call is "honestly agent-less, i.e. the
        # main thread". That is true for the conductor and FALSE for a subagent
        # whose stamp is missing — and there, derived without agent_id, this
        # returns the CONDUCTOR'S id: the exact collapse S1 exists to remove,
        # surviving in the one case nobody could see. It would hand a subagent
        # its parent's task slot and, through `tool_latency`, its parent's
        # parked results.
        #
        # Worker evidence is available WITHOUT an actor id (the spawn env stamp,
        # the resolved principal type, the sub-agent-call flag), so the two
        # facts can disagree — and when they do, the honest answer is "" rather
        # than a borrowed identity. Refusing costs an actor-less caller the
        # session-level slot it had before #483; substituting costs the
        # conductor its task.
        if not agent_id and subagent_evidence(project_root):
            return UNPROVEN_SUBAGENT_ACTOR
        return derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=sid,
            agent_id=agent_id or None,
        ).strip()
    except Exception:
        return ""


def _derive_host_actor_id(project_root: Path | str) -> str:
    """The host-derived id WITHOUT the unproven-subagent refusal.

    Only for the one caller that has already established a distinguishing axis
    of its own — `resolve_task_actor`, once a LANE is resolved. Everything else
    must go through `stable_actor_id`, which refuses.
    """
    try:
        from .agent_memory_epoch import derive_agent_context_id, resolve_host_identity

        kind, sid = resolve_host_identity(project_root=project_root)
        if not kind or not sid:
            return ""
        try:
            from .mcp_server_runtime_helpers import current_calling_agent_id

            agent_id = current_calling_agent_id()
        except Exception:
            agent_id = ""
        return derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=sid,
            agent_id=agent_id or None,
        ).strip()
    except Exception:
        return ""


def resolve_task_actor(project_root: Path | str) -> tuple[str, str, bool]:
    """Resolve (actor_id, lane_id, is_worker) for the current caller.

    The single authority every task-slot consumer shares. Non-workers
    (operator / conductor seat) return ("", "", False) — their slot
    stays the session-level one, exactly as before #463.
    """
    lane_id = os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
    is_worker = bool(lane_id)
    if not is_worker:
        try:
            from .identity_resolver import current_principal_type

            is_worker = current_principal_type(Path(str(project_root))) == "subagent"
        except Exception:
            is_worker = False
    if not is_worker:
        try:
            from .protected_file_runtime import is_sub_agent_call

            is_worker = bool(is_sub_agent_call())
        except Exception:
            is_worker = False
    actor_id = stable_actor_id(project_root)
    registry_lane = ""
    if not is_worker:
        # #217 chain: a caller whose canonical id is registered as a
        # RUNNING lane agent is a worker even without env/principal
        # markers (e.g. a respawned host process that lost its env).
        registry_lane = _registry_lane_for_actor(project_root, actor_id)
        if registry_lane:
            is_worker = True
    if not is_worker:
        return "", "", False
    if not lane_id:
        lane_id = registry_lane or _registry_lane_for_actor(project_root, actor_id)
    if not lane_id:
        # #457 auto-derivation at the first governed call.
        try:
            from .identity_resolver import current_user_id

            lane_id = derive_subagent_lane_id(
                user_id=current_user_id(Path(str(project_root))),
                spawner_agent_context_id=_spawner_agent_context_id(project_root),
                lane_slot=_lane_slot(),
            )
        except Exception:
            lane_id = ""
    # A LANE IS AN AXIS (#1043b). The refusal means "we know this is a subagent
    # and have NOTHING that tells it apart from its conductor". A worker that
    # resolved a lane has something: its slot is keyed (actor, lane), and the
    # conductor's is keyed (actor, ""), so even a shared derived id cannot reach
    # the conductor's row. Refusing here would instead cost lane workers their
    # task slot entirely — measured, as six pre-existing lane tests.
    #
    # The refusal stands where it is real: `stable_actor_id` still returns it,
    # and `resolve_slot_actor`'s NON-worker branch still hands it on, because a
    # non-worker has no lane and therefore nothing to be told apart by.
    if lane_id and identity_is_unproven(actor_id):
        actor_id = _derive_host_actor_id(project_root)
    return actor_id, lane_id, True


def resolve_slot_actor(project_root: Path | str) -> tuple[str, str, bool]:
    """Resolve (actor_id, lane_id, is_worker) for the task-SLOT owner (#483).

    Extends :func:`resolve_task_actor` beyond lane workers: EVERY caller
    with a host-derived canonical identity owns its own task slot
    (lane_id="" for non-workers), so one actor's task_complete can never
    clobber another actor's active task on the same session.

    - Worker: identical to resolve_task_actor (lane slots, #463).
    - Non-worker with a host-derived identity: (actor_id, "", False).
    - No host identity at all (legacy caller, e.g. process without a
      request-scoped host binding): ("", "", False) — the session-level
      slot remains its contract, exactly as before #483.
    """
    actor_id, lane_id, is_worker = resolve_task_actor(project_root)
    if is_worker:
        return actor_id, lane_id, True
    return stable_actor_id(project_root), "", False


def resolve_caller_task_id(
    project_root: Path | str,
    session_id: str,
    session_slot_task_id: str,
) -> str:
    """The task id THIS caller may claim as its active one, or "" (#599).

    One read order for every consumer that asks "does the caller have a
    task, and which?" — the universal gate and the filing surfaces
    (``ai_backlog`` / ``ai_task`` todo adds). They derived it separately
    before, and a divergence between them is how an agent ends up passing
    the gate while its filing is attributed to another actor's task.

    Order:

    1. The caller's OWN actor slot when it is active — the answer nothing
       another actor does can change.
    2. "" when the caller's own slot exists and is CLOSED. An actor that
       completed its task has no task; riding the shared session slot
       (which may now hold a DIFFERENT actor's task) is the read-side
       twin of the completion theft.
    3. Otherwise the shared session slot: actor-less legacy callers, and
       identified callers with no slot row at all (a task opened before
       per-actor slots existed). Deliberately left permissive — the hot
       gate path must not start refusing hosts whose identity cannot be
       derived, and nothing is ever CLOSED on the strength of this
       answer (task_complete does its own ownership check).
    """
    session_task = str(session_slot_task_id or "").strip()
    try:
        actor_id, lane_id, _is_worker = resolve_slot_actor(project_root)
    except Exception:
        return session_task
    if identity_is_unproven(actor_id):
        # THE REFUSED STATE NEVER RIDES THE SHARED SLOT (#1043).
        #
        # This must come BEFORE the `not actor_id` fallback below, and the
        # ordering is the entire fix: the refused state used to be spelled ""
        # and fell straight into "return session_task" — handing an unproven
        # subagent the CONDUCTOR'S task, which is the theft the whole #599 /
        # #1039-S1 / #1042b line exists to stop, arriving by a second route.
        #
        # "" here means the caller has NO task. It cannot claim one it did not
        # open, and step 3's permissiveness — correct for a legacy host whose
        # identity merely cannot be derived — must not extend to a caller we
        # have POSITIVE evidence is a subagent.
        return ""
    if not actor_id:
        return session_task
    try:
        from .todo_state_store import ActorTaskStateStore

        store = ActorTaskStateStore()
        root = Path(str(project_root))
        # Any lane: ownership is per ACTOR, and the lane a caller presents
        # can differ between two of its own requests (#599).
        row = store.active_row_for_actor(root, session_id, actor_id)
        if row is None:
            row = store.get(root, session_id, actor_id, lane_id)
    except Exception:
        return session_task
    if row is None:
        return session_task
    if str(row.get("status") or "") == "active":
        return str(row.get("task_id") or "")
    return ""


def ensure_worker_lane_binding(
    project_root: Path | str,
    session_id: str,
    actor_id: str,
    lane_id: str,
    *,
    source: str = "task_begin",
) -> dict[str, Any]:
    """Idempotent lane auto-bind (#457): guarantee a registry row binds
    this actor to its lane, and audit the FIRST bind.

    Repeat calls for an already-bound (session, lane, actor) triple are
    no-ops — one audited bind per binding, not per task. Best-effort by
    contract: a registry hiccup must never fail task_begin.
    """
    root = Path(str(project_root))
    sid = str(session_id or "").strip()
    actor = str(actor_id or "").strip()
    lane = str(lane_id or "").strip()
    if not (sid and actor and lane):
        return {"bound": False, "reason": "missing_identity"}
    # #1043b: the emptiness test above passes for the refusal, which is a
    # non-empty string — so a lane registry ROW would be written keyed on it,
    # binding a lane to a caller nobody can name and letting the next unstamped
    # subagent find a binding it never created. Today the only production
    # caller resolves through `_task_actor_identity`, which raises first; that
    # is caller ORDERING, not a property of this function, and this function is
    # the one that writes.
    if identity_is_unproven(actor):
        return {"bound": False, "reason": "unproven_subagent_identity"}
    try:
        from .session_lane_agents_store import SessionLaneAgentsStore

        store = SessionLaneAgentsStore()
        existing = store.find_latest_by_agent_context_id(
            root,
            actor,
            session_id=sid,
            lane_id=lane,
        )
        if existing is not None:
            return {
                "bound": True,
                "created": False,
                "worker_id": str(existing.get("worker_id") or ""),
                "lane_id": lane,
            }
        worker_id = store.register_worker(
            root,
            sid,
            lane,
            backend="auto_bind",
            metadata={
                "auto_bound": True,
                "bind_source": source,
                "agent_context_id": actor,
            },
        )
        store.stamp_agent_context_id(root, worker_id, actor)
        _audit_auto_bind(root, sid, lane, actor, worker_id)
        # The new row must be visible to the next resolve immediately.
        with _REGISTRY_CACHE_LOCK:
            _REGISTRY_CACHE.pop((str(project_root), actor), None)
            _REGISTRY_CACHE.pop((str(root), actor), None)
        return {"bound": True, "created": True, "worker_id": worker_id, "lane_id": lane}
    except Exception:
        return {"bound": False, "reason": "registry_unavailable"}


def _audit_auto_bind(
    project_root: Path,
    session_id: str,
    lane_id: str,
    actor_id: str,
    worker_id: str,
) -> None:
    """Every auto-bind audited (#457 design floor). record_event stamps
    the full attribution set (user_id, principal_type, effective_role,
    agent_epoch, scope, immutable event_id) and folds it into the v3
    row hash (#440) — nothing bespoke here, by design.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="lane_auto_bound",
            source_kind="task_actor_identity",
            session_id=session_id,
            capability_name="ai_task",
            action_kind="bind",
            target_entity=lane_id,
            status="ok",
            payload={
                "lane_id": lane_id,
                "agent_context_id": actor_id,
                "worker_id": worker_id,
                "auto_bound": True,
            },
        )
    except Exception:
        pass
