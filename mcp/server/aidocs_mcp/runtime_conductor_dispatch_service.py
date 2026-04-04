from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .types import SubagentTaskPacket


class RuntimeConductorDispatchService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def _plan_conductor_state_path(self, project_root: Path, session_id: str) -> Path:
        return self.runtime._conductor_state._plan_conductor_state_path(project_root, session_id)

    def _plan_conductor_lane_ids(self, project_root: Path, session_id: str) -> set[str]:
        return self.runtime._conductor_state._plan_conductor_lane_ids(project_root, session_id)

    def _require_plan_conductor_lane_id(
        self, project_root: Path, session_id: str, lane_id: str
    ) -> None:
        self.runtime._conductor_state._require_plan_conductor_lane_id(project_root, session_id, lane_id)

    def _read_plan_conductor_state(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        return self.runtime._conductor_state._read_plan_conductor_state(project_root, session_id)

    def _write_plan_conductor_state(
        self, project_root: Path, session_id: str, state: dict[str, object]
    ) -> None:
        self.runtime._conductor_state._write_plan_conductor_state(project_root, session_id, state)

    def _plan_conductor_snapshot(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, dict[str, object]]:
        return self.runtime._conductor_state._plan_conductor_snapshot(project_root, session_id)

    def plan_conductor_graph(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self.runtime._conductor_state.plan_conductor_graph(project_root, session_id)

    def plan_conductor_status(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self.runtime._conductor_state.plan_conductor_status(project_root, session_id)

    def _execution_overlap_risk(self, status: dict[str, object]) -> str:
        return self.runtime._conductor_state._execution_overlap_risk(status)

    def _execution_dependency_pressure(self, status: dict[str, object]) -> str:
        return self.runtime._conductor_state._execution_dependency_pressure(status)

    def execution_mode_select(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self.runtime._conductor_state.execution_mode_select(project_root, session_id)




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
            required_symbols=[],
            constraints=[
                *constraints,
                "Stay within allowed_files unless the conductor explicitly expands scope.",
            ],
            verification_commands=relevant_commands,
            done_definition=done_definition,
            must_not=[
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
        )
        return packet.to_dict()

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        execution_mode = self.runtime.execution_mode_select(project_root, session_id)
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
        return {
            "session_id": session_id,
            "mode": mode,
            "dispatch_state": "delegated",
            "reason": str(
                execution_mode.get("reason") or "lane ready for delegated execution"
            ),
            "selected_lane_id": selected_lane_id,
            "packet": self._build_subagent_task_packet(
                project_root, session_id, selected_lane_id
            ),
        }
