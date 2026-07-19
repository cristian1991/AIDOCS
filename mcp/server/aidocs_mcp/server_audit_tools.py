"""Audit MCP tools — Layer 2 audit hardening (B+C foundation).

Exposes the execution-event audit chain + query-by-task surface so
dashboard / compliance callers can:

- verify a session's event chain is untampered (verify_audit_chain)
- list every event stamped with a given task_id (audit_events_for_task)

Both are read-only. Mutating/enforcement hooks live elsewhere —
this module is the audit surface, not the gate.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def register_audit_tools(*, server: Any, hub: Any, runtime: Any) -> None:

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Verify Audit Chain",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("status", title="audit chain")
    def verify_audit_chain(session_id: str) -> Any:
        """Walk a session's execution_events Merkle chain and report
        whether it's intact.

        Each event's prev_hash = sha256(previous row's content). Any
        retroactive edit or delete breaks the chain — this tool
        returns verified=False and identifies the first broken link
        with expected vs stored prev_hash.

        Use from dashboards / compliance reports / forensic triage.
        Not hot-path; O(n) in session event count.
        """
        from .execution_index_store import ExecutionIndexStore

        root = resolve_project_root()
        store = ExecutionIndexStore()
        return store.verify_audit_chain(root, session_id)

    # @server.tool removed (120% clause B): folded into ai_lane(action='events').
    # register_impl target (end of this function) so the consolidator reaches it.
    def ai_events(
        worker_id: str = "",
        lane_id: str = "",
        session_id: str = "",
        limit: int = 100,
        tail: bool = False,
    ) -> Any:
        """Return a lane worker's tool-call timeline.

        Subagent events don't carry worker_id in payload (workers
        don't know their own registry id at tool-call time), so
        resolving by worker_id requires a JOIN against
        session_lane_agents to translate worker_id → (session_id,
        lane_id, started_at, completed_at) and then filter events
        by principal_type='subagent' + session + time window.

        Filter shapes:
          - worker_id="w-..." → resolves identity via registry.
          - lane_id + session_id → all subagent events for that
            lane (any worker, any run).

        ``tail`` (Phoenix 2026-05-09): when True, returns the LAST
        ``limit`` events instead of the first. Output is still
        chronological (oldest→newest within the returned slice) —
        only the slicing differs. Use for live-monitoring an
        in-flight worker; default False preserves the from-start
        scan for replaying a run.

        Returns ordered (observed_at, capability_name, action_kind,
        event_kind, status) — enough to spot refused calls (audit
        row without a matching tool_call_started/completed pair).
        """
        from .execution_index_store import ExecutionIndexStore

        root = resolve_project_root()
        store = ExecutionIndexStore()
        store.init_db(root)
        capped = max(1, min(int(limit or 100), 500))
        wid = (worker_id or "").strip()
        lid = (lane_id or "").strip()
        sid = (session_id or "").strip()
        if not wid and not (lid and sid):
            return {
                "error": (
                    "Pass worker_id OR (lane_id + session_id). "
                    "Get worker_id from agent_spawn_worker_async / "
                    "agent_worker_jobs."
                ),
                "events": [],
            }
        resolved_worker: dict[str, Any] | None = None
        started_at: str | None = None
        completed_at: str | None = None
        with sqlite3.connect(str(store.db_path(root))) as conn:
            conn.row_factory = sqlite3.Row
            if wid:
                # Two worker_id shapes:
                # (a) "w-xxxxxxxxxxxx" — in-memory job handle from
                #     agent_spawn_worker_async. Resolve via runtime.
                # (b) 32-char hex — registry id in session_lane_agents.
                reg = conn.execute(
                    "SELECT worker_id, session_id, lane_id, "
                    "started_at, completed_at, state, backend "
                    "FROM session_lane_agents WHERE worker_id = ?",
                    (wid,),
                ).fetchone()
                if reg is None and wid.startswith("w-"):
                    # "w-xxx" is the in-memory job handle. Find the
                    # matching session_lane_agents row via (session_id,
                    # lane_id) from the job, picking the most recent
                    # row (latest started_at) for this lane. That's the
                    # worker the conductor just spawned and is asking
                    # about. Works across MCP restarts too — the hex
                    # id persists in sqlite even when the w-xxx handle
                    # is gone from the in-memory jobs table.
                    try:
                        job_status = runtime._agent_expert.get_worker_status(
                            wid,
                            verbose=False,
                        )
                    except Exception:
                        job_status = {}
                    job_lane = str(job_status.get("lane_id") or "").strip()
                    if job_lane:
                        lid = job_lane
                        if not sid:
                            try:
                                m = hub.managed_mode.get_mode(root)
                                if m.get("active"):
                                    sid = str(m.get("session_id") or "").strip()
                            except Exception:
                                pass
                        # Join to session_lane_agents for started_at.
                        if sid:
                            latest = conn.execute(
                                "SELECT worker_id, started_at, completed_at, "
                                "backend, state FROM session_lane_agents "
                                "WHERE session_id = ? AND lane_id = ? "
                                "ORDER BY started_at DESC LIMIT 1",
                                (sid, lid),
                            ).fetchone()
                            if latest is not None:
                                started_at = (
                                    str(
                                        latest["started_at"] or "",
                                    )
                                    or None
                                )
                                completed_at = (
                                    str(
                                        latest["completed_at"] or "",
                                    )
                                    or None
                                )
                        resolved_worker = {
                            "worker_id": wid,
                            "backend": str(job_status.get("backend") or ""),
                            "state": str(job_status.get("state") or ""),
                        }
                    elif not (lid and sid):
                        return {
                            "worker_id": wid,
                            "lane_id": None,
                            "session_id": None,
                            "count": 0,
                            "events": [],
                            "error": (
                                f"worker_id {wid!r} not in registry and "
                                f"not found in live job table. Pass "
                                f"lane_id + session_id instead."
                            ),
                        }
                elif reg is not None:
                    sid = str(reg["session_id"])
                    lid = str(reg["lane_id"])
                    started_at = str(reg["started_at"] or "") or None
                    completed_at = str(reg["completed_at"] or "") or None
                    resolved_worker = {
                        "worker_id": reg["worker_id"],
                        "backend": reg["backend"],
                        "state": reg["state"],
                    }
                else:
                    return {
                        "worker_id": wid,
                        "lane_id": None,
                        "session_id": None,
                        "count": 0,
                        "events": [],
                        "error": (f"worker_id {wid!r} not found in session_lane_agents registry."),
                    }
            params: list[Any] = [sid]
            time_clause = ""
            if started_at:
                time_clause += " AND observed_at >= ?"
                params.append(started_at)
            if completed_at:
                time_clause += " AND observed_at <= datetime(?, '+60 seconds')"
                params.append(completed_at)
            params.append(capped)
            # Phoenix 2026-05-09: tail flag flips ORDER to DESC so
            # the LATEST `capped` rows are returned. Python-side
            # reverse keeps the output chronological (oldest→newest
            # within the returned slice) so callers don't have to
            # mentally re-sort.
            order_clause = "DESC" if tail else "ASC"
            rows = conn.execute(
                f"SELECT observed_at, capability_name, action_kind, "
                f"event_kind, status, principal_type, target_entity "
                f"FROM execution_events "
                f"WHERE session_id = ? "
                f"  AND principal_type = 'subagent' "
                f"  {time_clause} "
                f"ORDER BY observed_at {order_clause}, event_id {order_clause} LIMIT ?",
                params,
            ).fetchall()
            if tail:
                rows = list(reversed(rows))
        return {
            "worker_id": wid or None,
            "lane_id": lid or None,
            "session_id": sid or None,
            "started_at": started_at,
            "completed_at": completed_at,
            "resolved_worker": resolved_worker,
            "count": len(rows),
            "events": [
                {
                    "observed_at": r["observed_at"],
                    "capability_name": r["capability_name"],
                    "action_kind": r["action_kind"],
                    "event_kind": r["event_kind"],
                    "status": r["status"],
                    "principal_type": r["principal_type"],
                    "target_entity": r["target_entity"],
                }
                for r in rows
            ],
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Audit Events for Task",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("list", title="audit events")
    def audit_events_for_task(
        task_id: str,
        limit: int = 200,
    ) -> Any:
        """Return every execution_event stamped with this task_id.

        task_id is minted by task_begin (SHA-based, returned in the
        task_begin result). Every subsequent mutating tool call
        carries it in the event log so "what did this task actually
        do?" becomes one query.
        """
        from .execution_index_store import ExecutionIndexStore

        root = resolve_project_root()
        store = ExecutionIndexStore()
        store.init_db(root)
        capped = max(1, min(int(limit or 200), 1000))
        with sqlite3.connect(str(store.db_path(root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT event_id, session_id, event_kind, source_kind, "
                "action_kind, target_entity, status, observed_at, "
                "chain_seq, payload_json "
                "FROM execution_events "
                "WHERE task_id = ? "
                "ORDER BY chain_seq ASC LIMIT ?",
                (task_id, capped),
            ).fetchall()
        return {
            "task_id": task_id,
            "count": len(rows),
            "events": [
                {
                    "event_id": r["event_id"],
                    "session_id": r["session_id"],
                    "event_kind": r["event_kind"],
                    "source_kind": r["source_kind"],
                    "action_kind": r["action_kind"],
                    "target_entity": r["target_entity"],
                    "status": r["status"],
                    "observed_at": r["observed_at"],
                    "chain_seq": r["chain_seq"],
                    "payload_json": r["payload_json"],
                }
                for r in rows
            ],
        }

    # ai_events is folded into ai_lane(action='events') (120% clause B); keep it
    # callable by the consolidator via direct-dispatch (no standalone alias).
    from . import tool_interface as _ti_c20

    _ti_c20.register_impl("ai_events", ai_events)
