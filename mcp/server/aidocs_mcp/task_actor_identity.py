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
    if not actor_id:
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
    """
    try:
        from .agent_memory_epoch import derive_agent_context_id, resolve_host_identity

        kind, sid = resolve_host_identity(project_root=project_root)
        if not kind or not sid:
            return ""
        return derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=sid,
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
