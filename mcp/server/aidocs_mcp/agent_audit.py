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
  * ``live``            -- the agent's MCP-server pid (parsed from
                          ``bound_by_boot_token`` 'mcp-<pid>-...') is alive;
  * ``session_id``      -- the work session the agent is bound to;
  * ``lane_workers``    -- the lane subagents this agent spawned, nested.

Reads through the store APIs / server-internal sqlite (the identity dbs are
gate-protected against raw agent reads), so this is server-internal only.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

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


def _roles_by_host(project_root: Path) -> dict[str, str]:
    """host_session_id -> messagerie role (conductor/co_conductor/king)."""
    try:
        from .conductor_comms import _connect

        with _connect(project_root) as conn:
            rows = conn.execute("SELECT host_session_id, role FROM msg_role_map").fetchall()
        return {r["host_session_id"]: r["role"] for r in rows}
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
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT host_kind, host_session_id FROM agent_memory_compaction_state",
            ).fetchall()
        return {r["host_session_id"]: r["host_kind"] for r in rows}
    except Exception:
        return {}


def connected_agents_audit(
    project_root: Path,
    *,
    include_dead: bool = False,
    role: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Role-based audit of CONNECTED agents (conductors), keyed by
    host_session_id.

    Filters: ``role`` / ``session_id``. ``include_dead`` returns agents whose
    MCP-server pid is gone (separately, for audit). Each agent nests its
    spawned ``lane_workers``.
    """
    store = AidocsManagedStore()
    conductors = store.list_conductors(project_root)
    roles = _roles_by_host(project_root)
    host_kinds = _host_kinds(project_root)

    # Lane subagents grouped by the agent (host_session_id) that spawned them.
    from .cross_agent_coordination import connected_agents as _lane_agents

    workers_by_host: dict[str, list] = {}
    for w in _lane_agents(project_root, live_only=False):
        workers_by_host.setdefault(w.get("host_session_id", ""), []).append(w)

    live: list[dict[str, Any]] = []
    dead: list[dict[str, Any]] = []
    for c in conductors:
        hsid = c.get("cli_session_id", "")
        if session_id and c.get("session_id") != session_id:
            continue
        agent_role = roles.get(hsid, "conductor")
        if role and agent_role != role:
            continue
        pid = _boot_token_pid(c.get("bound_by_boot_token", ""))
        is_live = bool(pid) and _pid_alive(pid)
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
        entry = {
            "host_session_id": hsid,
            "agent_context_id": ctx,
            "agent_memory_epoch": epoch,
            "role": agent_role,
            "session_id": c.get("session_id", ""),
            "host_kind": kind,
            "live": is_live,
            "pid": pid,
            "activated_at": c.get("activated_at", ""),
            "last_updated": c.get("last_updated", ""),
            "source": c.get("source", ""),
            "lane_workers": workers_by_host.get(hsid, []),
        }
        (live if is_live else dead).append(entry)

    result: dict[str, Any] = {"agents": live, "live_count": len(live)}
    if include_dead:
        result["dead"] = dead
        result["dead_count"] = len(dead)
    return result
