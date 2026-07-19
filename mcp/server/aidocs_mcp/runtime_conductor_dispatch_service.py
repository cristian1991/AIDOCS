from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .types import SubagentTaskPacket


class RuntimeConductorDispatchService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    @property
    def _state(self) -> Any:
        return self.runtime._conductor_state

    def _plan_conductor_lane_is_contract_like(self, lane: object) -> bool:
        tokens = [
            str(getattr(lane, "lane_id", "") or ""),
            str(getattr(lane, "name", "") or ""),
            *[str(item) for item in (getattr(lane, "files", []) or []) if str(item).strip()],
        ]
        return any(
            re.search(r"(?<![a-z0-9])contract(?![a-z0-9])", token, re.IGNORECASE)
            for token in tokens
        )

    def _find_plan_lane(self, project_root: Path, session_id: str, lane_id: str) -> object | None:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        return next(
            (
                lane
                for lane in getattr(plan, "lanes", [])
                if str(getattr(lane, "lane_id", "") or "") == lane_id
            ),
            None,
        )

    def _lane_open_steps(self, lane: object) -> list[str]:
        steps = []
        for step in getattr(lane, "steps", []) or []:
            if str(getattr(step, "status", "") or "") == "completed":
                continue
            text = str(getattr(step, "text", "") or "").strip()
            if text:
                steps.append(text)
        return steps

    def _build_subagent_task_packet(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        lane = self._find_plan_lane(project_root, session_id, lane_id)
        if lane is None:
            raise ValueError(f"Unknown lane id: {lane_id}")
        context = self.hub.sessions.read_context(project_root, session_id)
        context_sections = context.sections if isinstance(context.sections, dict) else {}
        allowed_files = [
            str(item).replace("\\", "/")
            for item in (getattr(lane, "files", []) or [])
            if str(item).strip()
        ]
        # Phoenix 2026-05-09: per-lane tool override threaded through
        # the packet. When the plan's `Allowed tools:` line set
        # `lane.allowed_tools`, that list overrides the config default
        # at runtime gate time (see set_lane_scope override path).
        lane_allowed_tools = [
            str(item).strip()
            for item in (getattr(lane, "allowed_tools", []) or [])
            if str(item).strip()
        ]
        # Verification command: per-lane plan field is preferred;
        # fall back to session context's "Relevant Commands" list.
        lane_verification = str(getattr(lane, "verification", "") or "").strip()
        open_steps = self._lane_open_steps(lane)
        relevant_commands = self.runtime._clean_bullets(
            context_sections.get("Relevant Commands", []),
        )
        if lane_verification:
            # Promote lane-specific verification ahead of session-level
            # context commands so the packet's verification_commands
            # surfaces the lane plan's intent first.
            relevant_commands = [lane_verification, *relevant_commands]
        constraints = self.runtime._clean_bullets(context_sections.get("Constraints", []))
        done_definition = open_steps or [str(getattr(lane, "name", lane_id) or lane_id)]
        packet = SubagentTaskPacket(
            session_id=session_id,
            lane_id=lane_id,
            task_id=f"{session_id}:{lane_id}",
            goal=done_definition[0],
            allowed_files=allowed_files,
            required_reads=list(allowed_files),
            required_symbols=self._resolve_symbols_for_files(project_root, allowed_files),
            constraints=[
                *constraints,
                "Stay within allowed_files unless the conductor explicitly expands scope.",
                "Do NOT use raw bash/shell commands. Use AIDOCS MCP tools instead: ai_run (tests/builds/shell; detached, completion notify is universal — read via ai_run_output), ai_get_lines, ai_replace.",
            ],
            verification_commands=relevant_commands,
            lane_allowed_tools=lane_allowed_tools,
            done_definition=done_definition,
            must_not=[
                "Do not use bash, shell, or terminal commands directly.",
                "Do not change files outside allowed_files.",
                "Do not broaden scope or re-plan the workflow.",
                "Do not claim success without running required verification commands.",
            ],
            output_schema={
                "required": [
                    "files_changed",
                    "commands_run",
                    "command_results",
                    "verification_results",
                    "blockers",
                    "hidden_dependencies",
                    "follow_up",
                    "claimed_done",
                ],
                "notes": "Return structured execution evidence so the conductor can reconcile lane progress deterministically.",
            },
            predecessor_context=self._build_predecessor_context(project_root, session_id, lane),
        )
        # Add self-test requirement when enabled
        try:
            from .config import get_setting

            if get_setting(
                "conductor.require_agent_tests",
                project_root=project_root,
                default=False,
            ):
                packet.constraints.append(
                    "REQUIRED: Write tests for your changes and run them before reporting done. "
                    "Include test evidence in your report: test_evidence = {commands_run: [...], command_results: [...]}.",
                )
                packet.must_not.append(
                    "Do not claim done without writing and running tests for your changes.",
                )
        except Exception:
            pass
        return packet.to_dict()

    def _resolve_symbols_for_files(self, project_root: Path, files: list[str]) -> list[str]:
        """Get key symbol names from the code index for lane context."""
        symbols: list[str] = []
        try:
            for file_path in files[:10]:  # Cap to avoid huge lookups
                results = self.hub.code.search_symbols(
                    project_root,
                    query=file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    limit=5,
                )
                for item in results:
                    if isinstance(item, dict) and item.get("symbol"):
                        symbols.append(
                            f"{item['symbol']} ({item.get('kind', '?')} in {item.get('path', '?')})",
                        )
        except Exception:
            pass
        return symbols[:20]

    def _build_predecessor_context(
        self,
        project_root: Path,
        session_id: str,
        lane: object,
    ) -> list[dict[str, object]]:
        """Build context from completed dependency lanes for cross-agent communication."""
        depends_on = getattr(lane, "depends_on", []) or []
        if not depends_on:
            return []
        # Read stored lane results from conductor state
        state = self._state._read_plan_conductor_state(project_root, session_id)
        lane_results = state.get("lane_results", {})
        context: list[dict[str, object]] = []
        for dep_id in depends_on:
            dep_result = lane_results.get(dep_id)
            if not dep_result:
                continue
            context.append(
                {
                    "from_lane": dep_id,
                    "files_changed": dep_result.get("files_changed", []),
                    "summary": dep_result.get("summary", ""),
                    "verification_passed": dep_result.get("verification_passed", False),
                },
            )
        return context

    def store_lane_result(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        result: dict[str, object],
    ) -> None:
        """Store a completed lane's result for predecessor context injection."""
        state = self._state._read_plan_conductor_state(project_root, session_id)
        lane_results = state.get("lane_results", {})
        lane_results[lane_id] = {
            "files_changed": result.get("files_changed", []),
            "summary": str(result.get("goal", result.get("lane_id", "")))[:200],
            "verification_passed": result.get("success", False),
            "completed_at": __import__("datetime").datetime.now().isoformat(),
        }
        state["lane_results"] = lane_results
        self._state._write_plan_conductor_state(project_root, session_id, state)

    def plan_conductor_status(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        base = self._state.plan_conductor_status(project_root, session_id)
        # Import locally to avoid a circular import with the store's dependencies.
        from .session_lane_agents_store import SessionLaneAgentsStore

        store = SessionLaneAgentsStore()
        store.reap_crashed(project_root, session_id)
        agents = store.get_lane_agents(project_root, session_id)
        now_epoch = datetime.now(UTC).timestamp()
        lane_workers: list[dict[str, object]] = []
        for agent in agents:
            updated_at = agent.get("updated_at")
            stale_for_seconds = 0.0
            if isinstance(updated_at, str) and updated_at:
                ts = updated_at
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    stale_for_seconds = max(0.0, now_epoch - dt.timestamp())
                except ValueError:
                    stale_for_seconds = 0.0
            lane_workers.append(
                {
                    "worker_id": agent.get("worker_id"),
                    "lane_id": agent.get("lane_id"),
                    "state": agent.get("state"),
                    "started_at": agent.get("started_at"),
                    "updated_at": agent.get("updated_at"),
                    "pid": agent.get("pid"),
                    "stale_for_seconds": stale_for_seconds,
                },
            )
        # #107: the conductor's blocked set — lanes the cross-agent gate
        # stamped blocked_on_conflict, with the peer that owns the file.
        blocked_lanes: list[dict[str, object]] = []
        for agent in agents:
            if str(agent.get("state") or "") != "blocked_on_conflict":
                continue
            meta = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
            info = meta.get("blocked_on_conflict") if isinstance(meta, dict) else {}
            info = info if isinstance(info, dict) else {}
            blocked_lanes.append(
                {
                    "lane_id": agent.get("lane_id"),
                    "worker_id": agent.get("worker_id"),
                    "peer_lane_id": info.get("peer_lane_id", ""),
                    "peer_session_id": info.get("peer_session_id", ""),
                    "file_path": info.get("file_path", ""),
                    "reason": "lane_file_conflict",
                },
            )
        return {**base, "lane_workers": lane_workers, "blocked_lanes": blocked_lanes}

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str | None = None,
    ) -> dict[str, object]:
        execution_mode = self._state.execution_mode_select(project_root, session_id)
        mode = str(execution_mode.get("mode") or "inline")
        runnable_lane_ids = [
            str(item)
            for item in (execution_mode.get("runnable_lane_ids") or [])
            if str(item).strip()
        ]
        if mode == "inline" and not runnable_lane_ids:
            return {
                "session_id": session_id,
                "mode": mode,
                "dispatch_state": "inline",
                "reason": str(execution_mode.get("reason") or "inline execution selected"),
                "packet": None,
            }
        if not runnable_lane_ids:
            return {
                "session_id": session_id,
                "mode": mode,
                "dispatch_state": "blocked",
                "reason": str(execution_mode.get("reason") or "no runnable lane available"),
                "packet": None,
                "blocked_reasons": execution_mode.get("blocked_reasons") or {},
            }
        requested = (lane_id or "").strip()
        if requested:
            if requested not in runnable_lane_ids:
                return {
                    "session_id": session_id,
                    "mode": mode,
                    "dispatch_state": "blocked",
                    "reason": f"requested lane {requested!r} is not runnable",
                    "packet": None,
                    "runnable_lane_ids": runnable_lane_ids,
                    "blocked_reasons": execution_mode.get("blocked_reasons") or {},
                }
            selected_lane_id = requested
        else:
            selected_lane_id = runnable_lane_ids[0]
        packet = self._build_subagent_task_packet(project_root, session_id, selected_lane_id)
        _packet_dict = packet if isinstance(packet, dict) else {}
        # WAR D (#452) Task 2: spawn-time cross-lane overlap refusal.
        # A second dispatch whose Files: intersect a RUNNING lane's files
        # is refused with a NAMED conflict (holding lane + overlapping
        # paths). The conductor serializes; no arbitration queue.
        _overlap = self._running_lane_overlap(
            project_root,
            session_id,
            selected_lane_id,
            [str(f) for f in (_packet_dict.get("allowed_files") or [])],
        )
        if _overlap:
            _reason = (
                f"cross-lane file overlap: lane '{selected_lane_id}' declares "
                f"files already held by RUNNING lane "
                f"'{_overlap['holding_lane_id']}': "
                f"{', '.join(_overlap['overlapping_paths'])}. The conductor "
                f"serializes — dispatch after the holding lane completes, or "
                f"re-scope the plan's Files: lists."
            )
            try:
                self.hub.execution.record_event(
                    project_root,
                    event_kind="lane_dispatch_refused",
                    source_kind="mcp",
                    session_id=session_id,
                    capability_name="ai_plan_dispatch",
                    action_kind="dispatch",
                    target_entity=selected_lane_id,
                    status="blocked",
                    payload={**_overlap, "reason": _reason},
                )
            except Exception:
                pass
            return {
                "session_id": session_id,
                "mode": mode,
                "dispatch_state": "blocked",
                "reason": _reason,
                "packet": None,
                "conflict": _overlap,
            }
        # Set lane isolation scope — agent can only access lane's files
        # AND only call tools the lane allows (per packet override or
        # conductor.lane_allowed_tools config default).
        self._state.set_lane_scope(
            project_root,
            session_id,
            selected_lane_id,
            allowed_files=_packet_dict.get("allowed_files", []),
            lane_allowed_tools=_packet_dict.get("lane_allowed_tools", []),
        )
        # Transition lane to RUNNING
        try:
            from .types import LaneState

            self._state.transition_lane(
                project_root,
                session_id,
                selected_lane_id,
                LaneState.RUNNING,
            )
        except (ValueError, Exception):
            pass  # Lane may already be running or state not initialized
        # WAR D (#452) Task 4: the dispatch itself lands in
        # execution_events (record_event stamps user_id/agent_epoch).
        # worker_spawned/worker_exited cover the subprocess; this covers
        # the DISPATCH decision + the stamped scope.
        try:
            self.hub.execution.record_event(
                project_root,
                event_kind="lane_dispatched",
                source_kind="mcp",
                session_id=session_id,
                capability_name="ai_plan_dispatch",
                action_kind="dispatch",
                target_entity=selected_lane_id,
                status="delegated",
                payload={
                    "lane_id": selected_lane_id,
                    "allowed_files": list(_packet_dict.get("allowed_files") or []),
                    "lane_allowed_tools": list(
                        _packet_dict.get("lane_allowed_tools") or [],
                    ),
                    "mode": mode,
                },
            )
        except Exception:
            pass
        return {
            "session_id": session_id,
            "mode": mode,
            "dispatch_state": "delegated",
            "reason": str(execution_mode.get("reason") or "lane ready for delegated execution"),
            "selected_lane_id": selected_lane_id,
            "packet": packet,
        }

    def _running_lane_overlap(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        allowed_files: list[str],
    ) -> dict[str, object] | None:
        """Return {holding_lane_id, overlapping_paths} when `lane_id`'s
        declared files intersect a RUNNING sibling lane's declared files,
        else None. Case-/slash-insensitive (mirrors PlanConductor's
        canonical file identity). WAR D (#452).
        """
        mine = {
            f.replace("\\", "/").lower().strip()
            for f in allowed_files
            if str(f).strip()
        }
        if not mine:
            return None
        try:
            from .types import LaneState

            state = self._state._read_plan_conductor_state(project_root, session_id)
            lane_states = state.get("lane_states", {}) or {}
            running = {
                lid
                for lid, st in lane_states.items()
                if lid != lane_id
                and (
                    st == LaneState.RUNNING
                    or getattr(st, "value", str(st)) == "running"
                )
            }
            if not running:
                return None
            plan = self.hub.sessions.read_plan(project_root, session_id)
            for lane in getattr(plan, "lanes", []) or []:
                lid = str(getattr(lane, "lane_id", "") or "")
                if lid not in running:
                    continue
                theirs = {
                    str(f).replace("\\", "/").lower().strip()
                    for f in (getattr(lane, "files", []) or [])
                    if str(f).strip()
                }
                overlap = sorted(mine & theirs)
                if overlap:
                    return {
                        "holding_lane_id": lid,
                        "overlapping_paths": overlap,
                    }
        except Exception:
            # Fail-open: the static both-blocked overlap check in
            # PlanConductor.runnable_lanes remains the backstop.
            return None
        return None
