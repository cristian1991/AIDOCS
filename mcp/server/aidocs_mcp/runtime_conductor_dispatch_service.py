from __future__ import annotations

import re
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
            *[
                str(item)
                for item in (getattr(lane, "files", []) or [])
                if str(item).strip()
            ],
        ]
        return any(
            re.search(r"(?<![a-z0-9])contract(?![a-z0-9])", token, re.IGNORECASE)
            for token in tokens
        )

    def _find_plan_lane(
        self, project_root: Path, session_id: str, lane_id: str
    ) -> object | None:
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
        context_sections = (
            context.sections if isinstance(context.sections, dict) else {}
        )
        allowed_files = [
            str(item).replace("\\", "/")
            for item in (getattr(lane, "files", []) or [])
            if str(item).strip()
        ]
        open_steps = self._lane_open_steps(lane)
        relevant_commands = self.runtime._clean_bullets(
            context_sections.get("Relevant Commands", [])
        )
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
                "Do NOT use raw bash/shell commands. Use AIDOCS MCP tools instead: code_build_project, code_test_project, code_run_command, code_get_lines, code_edit_lines.",
            ],
            verification_commands=relevant_commands,
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
                "notes": "Return structured execution evidence so the conductor can reconcile lane progress deterministically.",
            },
            predecessor_context=self._build_predecessor_context(project_root, session_id, lane),
        )
        # Add self-test requirement when enabled
        try:
            from .config import get_setting
            if get_setting("conductor.require_agent_tests", project_root=project_root, default=False):
                packet.constraints.append(
                    "REQUIRED: Write tests for your changes and run them before reporting done. "
                    "Include test evidence in your report: test_evidence = {commands_run: [...], command_results: [...]}."
                )
                packet.must_not.append(
                    "Do not claim done without writing and running tests for your changes."
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
                    project_root, query=file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    limit=5,
                )
                for item in results:
                    if isinstance(item, dict) and item.get("symbol"):
                        symbols.append(f"{item['symbol']} ({item.get('kind', '?')} in {item.get('path', '?')})")
        except Exception:
            pass
        return symbols[:20]

    def _build_predecessor_context(
        self, project_root: Path, session_id: str, lane: object,
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
            context.append({
                "from_lane": dep_id,
                "files_changed": dep_result.get("files_changed", []),
                "summary": dep_result.get("summary", ""),
                "verification_passed": dep_result.get("verification_passed", False),
            })
        return context

    def store_lane_result(
        self, project_root: Path, session_id: str, lane_id: str, result: dict[str, object],
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

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
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
                "reason": str(
                    execution_mode.get("reason") or "inline execution selected"
                ),
                "packet": None,
            }
        if not runnable_lane_ids:
            return {
                "session_id": session_id,
                "mode": mode,
                "dispatch_state": "blocked",
                "reason": str(
                    execution_mode.get("reason") or "no runnable lane available"
                ),
                "packet": None,
                "blocked_reasons": execution_mode.get("blocked_reasons") or {},
            }
        selected_lane_id = runnable_lane_ids[0]
        packet = self._build_subagent_task_packet(
            project_root, session_id, selected_lane_id
        )
        # Set lane isolation scope — agent can only access lane's files
        self._state.set_lane_scope(
            project_root, session_id, selected_lane_id,
            allowed_files=packet.get("allowed_files", []) if isinstance(packet, dict) else [],
        )
        # Transition lane to RUNNING
        try:
            from .types import LaneState
            self._state.transition_lane(project_root, session_id, selected_lane_id, LaneState.RUNNING)
        except (ValueError, Exception):
            pass  # Lane may already be running or state not initialized
        return {
            "session_id": session_id,
            "mode": mode,
            "dispatch_state": "delegated",
            "reason": str(
                execution_mode.get("reason") or "lane ready for delegated execution"
            ),
            "selected_lane_id": selected_lane_id,
            "packet": packet,
        }
