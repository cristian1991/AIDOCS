"""Cross-agent coordination over EXISTING session/lane/seat state.

Clause 3 of the Outer Gate App Metadata goal: agents launched from
different chats each bind their own ``session_lane_agents`` row in the
shared execution-index sqlite (the dispatcher writes worker_id, session_id,
lane_id, state and the lane's ``allowed_files`` scope). This module reads
that ONE source of truth to:

  * ``connected_agents`` -- expose a roster of agents across ALL chats with
    their bound session/lane, liveness, and owned files (so an agent can
    detect overlap and coordinate handoff instead of racing); and
  * ``check_cross_agent_conflict`` -- report when a file is already owned by
    a DIFFERENT LIVE agent, so the edit/run gate can refuse the write.

No new presence protocol and no new store: these are pure reads over rows
the conductor dispatcher already writes. A terminal (done/failed/crashed/
canceled) agent frees its scope; the reaper turns stale ``running`` rows
into ``crashed``, so liveness here means "not terminal".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .session_lane_agents_store import SessionLaneAgentsStore, _pid_alive

# A lane agent that reached one of these no longer owns its files -- the scope
# is free. Everything else ('running', 'spawning', ...) is a LIVE candidate,
# subject to the pid/heartbeat liveness check below.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "failed", "crashed", "killed", "canceled", "cancelled", "completed"}
)

# Heartbeat window: a non-terminal row whose updated_at is older than this is
# STALE (graveyard), mirroring SessionLaneAgentsStore.reap_crashed's default.
_DEFAULT_FRESH_SECONDS = 300


def _iso_to_epoch(ts: str | None) -> float:
    """Parse an ISO-8601 (…Z) timestamp to epoch seconds; 0.0 on failure.
    Mirrors SessionLaneAgentsStore._iso_to_epoch so liveness agrees with the
    reaper's staleness cutoff.
    """
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
        return 0.0


def _is_live(
    state: str,
    pid: object = None,
    updated_at: str | None = None,
    *,
    fresh_seconds: int = _DEFAULT_FRESH_SECONDS,
    now_epoch: float | None = None,
) -> bool:
    """True iff a lane-agent row is LIVE, mirroring the reaper's contract.

    Live = (a) non-terminal, (b) NOT a recorded pid that is provably dead, and
    (c) heartbeat-fresh (updated_at within fresh_seconds). pid is the fast
    crash signal once recorded; updated_at is the heartbeat fallback -- the
    dispatcher currently registers workers WITHOUT a pid, so heartbeat
    freshness is what actually carries liveness today. A stale row is
    graveyard: it never owns its files, so it cannot false-block an edit.
    """
    if (state or "").strip().lower() in _TERMINAL_STATES:
        return False
    if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
        return False
    now = now_epoch if now_epoch is not None else datetime.now(UTC).timestamp()
    return (now - _iso_to_epoch(updated_at)) <= fresh_seconds


def file_in_scope(file_path: str, allowed_files: list[str]) -> bool:
    """Exact-or-subdirectory scope match.

    Mirrors ``conductor_verification_service._file_in_scope`` semantics so
    the coordination gate and the lane verifier agree on what "owns this
    file" means: a file is in scope when it equals an allowed entry or sits
    under an allowed directory prefix (case/separator-insensitive).
    """
    normalized = str(file_path).replace("\\", "/").lower()
    for allowed in allowed_files:
        allowed_norm = str(allowed).replace("\\", "/").lower().strip()
        if not allowed_norm:
            continue
        if normalized == allowed_norm or normalized.startswith(allowed_norm.rstrip("/") + "/"):
            return True
    return False


def connected_agents(
    project_root: Path,
    *,
    live_only: bool = True,
    fresh_seconds: int = _DEFAULT_FRESH_SECONDS,
) -> list[dict[str, Any]]:
    """Roster of connected agents across ALL chats/sessions in the project.

    Each entry: ``worker_id``, ``session_id``, ``lane_id``, ``backend``,
    ``state``, ``live`` (bool), ``owned_files`` (the lane's allowed_files),
    ``started_at``, ``updated_at``. Sourced from ``session_lane_agents`` (the
    shared execution-index sqlite) -- an agent launched from a different chat
    appears here because its dispatcher wrote a row. ``live_only`` (default)
    drops terminal agents whose scope is already freed.
    """
    store = SessionLaneAgentsStore()
    out: list[dict[str, Any]] = []
    for r in store.get_all_lane_agents(project_root):
        live = _is_live(
            str(r.get("state", "")),
            r.get("pid"),
            r.get("updated_at"),
            fresh_seconds=fresh_seconds,
        )
        if live_only and not live:
            continue
        out.append(
            {
                "worker_id": r.get("worker_id", ""),
                "host_session_id": r.get("host_session_id", ""),
                "session_id": r.get("session_id", ""),
                "lane_id": r.get("lane_id", ""),
                "backend": r.get("backend", ""),
                "state": r.get("state", ""),
                "live": live,
                "owned_files": list(r.get("allowed_files") or []),
                "started_at": r.get("started_at", ""),
                "updated_at": r.get("updated_at", ""),
            }
        )
    return out


def agent_roster(
    project_root: Path,
    *,
    fresh_seconds: int = _DEFAULT_FRESH_SECONDS,
) -> dict[str, Any]:
    """Live agents and the GRAVEYARD (terminal + stale rows), separated for
    audit.

    The ``live`` list is what coordination acts on; the ``graveyard`` is kept
    visible for forensics (who ran, what they owned, when they died) WITHOUT
    polluting the live view. Both carry the same per-entry shape (incl.
    ``host_session_id`` identity + ``owned_files``).
    """
    everyone = connected_agents(project_root, live_only=False, fresh_seconds=fresh_seconds)
    live = [a for a in everyone if a["live"]]
    graveyard = [a for a in everyone if not a["live"]]
    return {
        "live": live,
        "graveyard": graveyard,
        "live_count": len(live),
        "graveyard_count": len(graveyard),
    }


def roster_view(
    project_root: Path,
    *,
    include_graveyard: bool = False,
    session_id: str = "",
    state: str = "",
    host_session_id: str = "",
    fresh_seconds: int = _DEFAULT_FRESH_SECONDS,
) -> dict[str, Any]:
    """Filtered roster for the ai_agents tool.

    Returns the LIVE agents (and the GRAVEYARD when ``include_graveyard``),
    optionally narrowed by ``session_id`` / ``state`` / ``host_session_id``.
    The roster is already PROJECT-scoped (session_lane_agents lives in the
    bound project's index db, which the gate's project-binding RBAC governs),
    so these filters are the per-session / per-agent lens on top.
    """
    roster = agent_roster(project_root, fresh_seconds=fresh_seconds)

    def _match(a: dict[str, Any]) -> bool:
        if session_id and a.get("session_id") != session_id:
            return False
        if state and a.get("state") != state:
            return False
        if host_session_id and a.get("host_session_id") != host_session_id:
            return False
        return True

    live = [a for a in roster["live"] if _match(a)]
    out: dict[str, Any] = {"live": live, "live_count": len(live)}
    if include_graveyard:
        grave = [a for a in roster["graveyard"] if _match(a)]
        out["graveyard"] = grave
        out["graveyard_count"] = len(grave)
    return out


def check_cross_agent_conflict(
    project_root: Path,
    file_path: str,
    *,
    caller_worker_id: str = "",
    caller_lane_id: str = "",
    caller_session_id: str = "",
    caller_host_session_id: str = "",
) -> dict[str, Any] | None:
    """Return the conflicting LIVE agent if ``file_path`` is owned by a
    DIFFERENT live agent (any chat), else ``None``.

    The caller's own seat never conflicts with itself -- excluded by
    ``caller_host_session_id`` (the per-project agent identity; two agents on
    the SAME session stay distinct by host id), by ``caller_worker_id`` when
    known, or by matching ``caller_lane_id`` + ``caller_session_id``. The
    returned dict carries the owner's identity and a doctrine line the edit/run
    gate can surface as the refusal reason.
    """
    fp = str(file_path).replace("\\", "/").strip()
    if not fp:
        return None
    for agent in connected_agents(project_root, live_only=True):
        if caller_host_session_id and agent["host_session_id"] == caller_host_session_id:
            continue
        if caller_worker_id and agent["worker_id"] == caller_worker_id:
            continue
        if (
            caller_lane_id
            and agent["lane_id"] == caller_lane_id
            and agent["session_id"] == caller_session_id
        ):
            continue
        if file_in_scope(fp, agent["owned_files"]):
            owner = agent["worker_id"] or agent["lane_id"] or "another agent"
            return {
                "conflict": True,
                "owner_worker_id": agent["worker_id"],
                "owner_lane_id": agent["lane_id"],
                "owner_session_id": agent["session_id"],
                "file_path": fp,
                "doctrine": (
                    f"file '{fp}' is owned by live agent {owner} "
                    f"(session {agent['session_id']}, lane {agent['lane_id']}); "
                    f"coordinate or wait -- do not race a concurrent edit."
                ),
            }
    return None


# Surgical single-path file-edit MCP tools whose target should be refused when
# another live agent owns it. Multi-path (ai_batch_edit), shell (ai_run), and
# non-file mutations (memory_capture / edit_rollback / protect_file) are
# intentionally out of scope -- they have no single owned-file target here.
_PATH_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "ai_create_file",
        "ai_edit_lines",
        "ai_insert_lines",
        "ai_str_replace",
        "ai_replace",
        "ai_delete",
        "ai_slop",
    }
)


def edit_conflict_for_tool(
    project_root: Path,
    tool_name: str,
    file_path: str,
    *,
    caller_worker_id: str = "",
    caller_lane_id: str = "",
    caller_session_id: str = "",
    caller_host_session_id: str = "",
) -> dict[str, Any] | None:
    """Cross-agent conflict decision for a single-path file edit, else None.

    Returns the conflict dict (see ``check_cross_agent_conflict``) only when
    ``tool_name`` is a single-path file-edit tool AND another live agent owns
    ``file_path``. Any other tool, or an empty path, returns ``None`` (allow).
    The ``mcp__aidocs__`` host prefix is stripped before the membership test.
    """
    base = tool_name.rsplit("__", 1)[-1]
    if base not in _PATH_EDIT_TOOLS:
        return None
    if not file_path:
        return None
    return check_cross_agent_conflict(
        project_root,
        file_path,
        caller_worker_id=caller_worker_id,
        caller_lane_id=caller_lane_id,
        caller_session_id=caller_session_id,
        caller_host_session_id=caller_host_session_id,
    )
