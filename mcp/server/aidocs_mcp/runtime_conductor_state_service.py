from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .plan_conductor import PlanConductor
from .types import ExecutionModeSelection, LaneState


class RuntimeConductorStateService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def _plan_conductor_state_path(self, project_root: Path, session_id: str) -> Path:
        return (
            self.hub.sessions.session_path(project_root, session_id)
            / "artifacts"
            / "plan_conductor_state.json"
        )

    def _plan_conductor_lane_ids(self, project_root: Path, session_id: str) -> set[str]:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        return {lane.lane_id for lane in plan.lanes}

    def _require_plan_conductor_lane_id(
        self, project_root: Path, session_id: str, lane_id: str
    ) -> None:
        if lane_id not in self._plan_conductor_lane_ids(project_root, session_id):
            raise ValueError(f"Unknown lane id: {lane_id}")

    def _read_plan_conductor_state(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        path = self._plan_conductor_state_path(project_root, session_id)
        lane_ids = self._plan_conductor_lane_ids(project_root, session_id)
        empty_state = {
            "paused_lanes": {},
            "contract_ready_lane_ids": [],
            "reopened_lane_ids": [],
            "lane_ownership_history": {},
            "lane_signals": {},
            "lane_states": {},
        }
        if not path.exists():
            return empty_state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return empty_state
        if not isinstance(payload, dict):
            return empty_state

        raw_paused_lanes = payload.get("paused_lanes") or {}
        if not isinstance(raw_paused_lanes, dict):
            raw_paused_lanes = {}
        paused_lanes = {
            str(lane_id): str(reason)
            for lane_id, reason in raw_paused_lanes.items()
            if str(lane_id).strip() in lane_ids and str(reason).strip()
        }

        raw_contract_ready = payload.get("contract_ready_lane_ids") or []
        if not isinstance(raw_contract_ready, list):
            raw_contract_ready = []
        contract_ready_lane_ids = sorted(
            {
                str(lane_id)
                for lane_id in raw_contract_ready
                if str(lane_id).strip() in lane_ids
            }
        )

        raw_reopened = payload.get("reopened_lane_ids") or []
        if not isinstance(raw_reopened, list):
            raw_reopened = []
        reopened_lane_ids = sorted(
            {
                str(lane_id)
                for lane_id in raw_reopened
                if str(lane_id).strip() in lane_ids
            }
        )

        raw_history = payload.get("lane_ownership_history") or {}
        if not isinstance(raw_history, dict):
            raw_history = {}
        lane_ownership_history = {
            str(lane_id): events
            for lane_id, events in raw_history.items()
            if str(lane_id).strip() in lane_ids and isinstance(events, list)
        }

        raw_lane_signals = payload.get("lane_signals") or {}
        if not isinstance(raw_lane_signals, dict):
            raw_lane_signals = {}
        lane_signals = {
            str(lane_id): [
                dict(entry)
                for entry in entries
                if isinstance(entries, list) and isinstance(entry, dict)
            ]
            for lane_id, entries in raw_lane_signals.items()
            if str(lane_id).strip() in lane_ids
        }

        raw_lane_states = payload.get("lane_states") or {}
        if not isinstance(raw_lane_states, dict):
            raw_lane_states = {}
        lane_states = {}
        for lid, state_val in raw_lane_states.items():
            lid_str = str(lid).strip()
            if lid_str in lane_ids:
                try:
                    lane_states[lid_str] = LaneState(str(state_val))
                except ValueError:
                    pass

        return {
            "paused_lanes": paused_lanes,
            "contract_ready_lane_ids": contract_ready_lane_ids,
            "reopened_lane_ids": reopened_lane_ids,
            "lane_ownership_history": lane_ownership_history,
            "lane_signals": lane_signals,
            "lane_states": lane_states,
        }

    def _write_plan_conductor_state(
        self, project_root: Path, session_id: str, state: dict[str, object]
    ) -> None:
        path = self._plan_conductor_state_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        serializable = dict(state)
        if "lane_states" in serializable:
            serializable["lane_states"] = {
                k: v.value if isinstance(v, LaneState) else str(v)
                for k, v in serializable["lane_states"].items()
            }
        path.write_text(
            json.dumps(serializable, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )

    def transition_lane(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        to_state: LaneState,
    ) -> LaneState:
        """Transition a lane to a new state. Returns the new state. Raises on invalid transition."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        lane_states: dict[str, LaneState] = state.get("lane_states", {})
        current = lane_states.get(lane_id, LaneState.BLOCKED)
        if not LaneState.can_transition(current, to_state):
            raise ValueError(
                f"Invalid lane transition: {lane_id} cannot go from {current.value} to {to_state.value}"
            )
        lane_states[lane_id] = to_state
        state["lane_states"] = lane_states
        self._write_plan_conductor_state(project_root, session_id, state)
        return to_state

    def set_lane_scope(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        allowed_files: list[str],
    ) -> None:
        """Set allowed files and tool permissions for a lane in the query gate."""
        # Load tool allowlist from config
        from .config import get_setting
        lane_allowed_tools = get_setting(
            "conductor.lane_allowed_tools", project_root=project_root, default=[]
        ) or []
        lane_extra_tools = get_setting(
            "conductor.lane_extra_tools", project_root=project_root, default=[]
        ) or []

        self.hub.query_gate.set(
            project_root,
            session_id,
            current_lane_id=lane_id,
            lane_exact_paths=[f.replace("\\", "/") for f in allowed_files],
            lane_allowed_tools=list(lane_allowed_tools),
            lane_extra_tools=list(lane_extra_tools),
        )

        # Register lane scope in conductor comms for cross-lane conflict detection
        try:
            from .conductor_comms import register_lane_scope, set_lane_state
            register_lane_scope(project_root, lane_id, allowed_files, session_id=session_id)
            set_lane_state(project_root, lane_id, "active", session_id=session_id)
        except Exception:
            pass

    def expand_lane_scope(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        file_path: str,
        reason: str = "",
    ) -> list[str]:
        """Add a file to the current lane's allowed files. Returns updated list."""
        gate = self.hub.query_gate.get(project_root, session_id)
        current_lane = gate.get("current_lane_id")
        if current_lane != lane_id:
            raise ValueError(f"Lane {lane_id} is not the current lane (current: {current_lane})")
        lane_paths = list(gate.get("lane_exact_paths") or [])
        normalized = file_path.replace("\\", "/").strip()
        if normalized not in lane_paths:
            lane_paths.append(normalized)
        self.hub.query_gate.set(
            project_root,
            session_id,
            lane_exact_paths=lane_paths,
        )
        # Record signal for audit trail
        state = self._read_plan_conductor_state(project_root, session_id)
        signals = state.get("lane_signals", {})
        lane_signals = signals.get(lane_id, [])
        lane_signals.append({
            "kind": "undeclared_file_needed",
            "target_lane_id": lane_id,
            "detail": f"Expanded scope: {normalized}" + (f" ({reason})" if reason else ""),
        })
        signals[lane_id] = lane_signals
        state["lane_signals"] = signals
        self._write_plan_conductor_state(project_root, session_id, state)
        return lane_paths

    def clear_lane_scope(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        """Clear lane isolation — no lane active."""
        self.hub.query_gate.set(
            project_root,
            session_id,
            current_lane_id=None,
            lane_exact_paths=[],
        )

    def pause_lane(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        reason: str = "",
    ) -> dict[str, str]:
        """Pause a running lane."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        paused = state.get("paused_lanes", {})
        paused[lane_id] = reason or "paused by user"
        state["paused_lanes"] = paused
        self._write_plan_conductor_state(project_root, session_id, state)
        return {"lane_id": lane_id, "reason": paused[lane_id]}

    def _plan_conductor_snapshot(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, dict[str, object]]:
        state = self._read_plan_conductor_state(project_root, session_id)
        conductor = PlanConductor(
            self.hub,
            project_root,
            session_id,
            paused_lanes=dict(state["paused_lanes"]),
            contract_ready_lane_ids=set(state["contract_ready_lane_ids"]),
            reopened_lane_ids=set(state.get("reopened_lane_ids", [])),
            lane_signals=dict(state.get("lane_signals", {})),
            lane_states=dict(state.get("lane_states", {})),
        )
        return {
            "graph": conductor.graph(),
            "runnable": conductor.runnable_lanes(),
        }

    def plan_conductor_graph(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Return the parsed conductor lane graph for a lane-aware session plan."""
        return self._plan_conductor_snapshot(project_root, session_id)["graph"]

    def plan_conductor_status(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Return conductor graph plus current runnable lane state for a session plan."""
        snapshot = self._plan_conductor_snapshot(project_root, session_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        return {
            **snapshot["graph"],
            **snapshot["runnable"],
            "reopened_lane_ids": list(state.get("reopened_lane_ids", [])),
            "lane_ownership_history": dict(state.get("lane_ownership_history", {})),
            "lane_signals": dict(state.get("lane_signals", {})),
        }

    def _execution_overlap_risk(self, status: dict[str, object]) -> str:
        blocked_reasons = (
            status.get("blocked_reasons")
            if isinstance(status.get("blocked_reasons"), dict)
            else {}
        )
        for reasons in blocked_reasons.values():
            if any(
                isinstance(reason, str) and reason.startswith("shared-file-overlap:")
                for reason in (reasons if isinstance(reasons, list) else [])
            ):
                return "high"
        return "none"

    def _execution_dependency_pressure(self, status: dict[str, object]) -> str:
        waiting_on = (
            status.get("waiting_on")
            if isinstance(status.get("waiting_on"), dict)
            else {}
        )
        runnable_lane_ids = [
            str(item)
            for item in (status.get("runnable_lane_ids") or [])
            if str(item).strip()
        ]
        if not waiting_on:
            return "none"
        if not runnable_lane_ids:
            return "high"
        if len(waiting_on) > len(runnable_lane_ids):
            return "medium"
        return "low"

    def execution_mode_select(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        lanes = list(getattr(plan, "lanes", []) or [])
        if not lanes:
            return ExecutionModeSelection(
                session_id=session_id,
                mode="inline",
                reason="plan has no conductor lanes",
                lane_count=0,
                runnable_lane_count=0,
                blocked_lane_count=0,
                has_lanes=False,
                has_contract_lanes=False,
                overlap_risk="none",
                dependency_pressure="none",
            ).to_dict()

        status = self.plan_conductor_status(project_root, session_id)
        runnable_lane_ids = [
            str(item)
            for item in (status.get("runnable_lane_ids") or [])
            if str(item).strip()
        ]
        blocked_reasons = (
            status.get("blocked_reasons")
            if isinstance(status.get("blocked_reasons"), dict)
            else {}
        )
        overlap_risk = self._execution_overlap_risk(status)
        dependency_pressure = self._execution_dependency_pressure(status)
        has_contract_lanes = any(
            self.runtime._plan_conductor_lane_is_contract_like(lane) for lane in lanes
        )

        if overlap_risk == "high":
            mode = "delegated_serial"
            reason = "shared-file overlap blocks safe parallel execution"
        elif len(runnable_lane_ids) > 1:
            mode = "delegated_parallel"
            reason = "multiple independent runnable lanes are available"
        elif len(lanes) == 1:
            mode = "delegated_serial"
            reason = "single lane plan is structured for delegated execution"
        elif len(runnable_lane_ids) == 1:
            mode = "delegated_serial"
            reason = "only one lane is safely runnable at this time"
        else:
            mode = "delegated_serial"
            reason = "lane-structured plan exists but work is dependency-blocked"

        return ExecutionModeSelection(
            session_id=session_id,
            mode=mode,
            reason=reason,
            lane_count=len(lanes),
            runnable_lane_count=len(runnable_lane_ids),
            blocked_lane_count=len(blocked_reasons),
            has_lanes=True,
            has_contract_lanes=has_contract_lanes,
            overlap_risk=overlap_risk,
            dependency_pressure=dependency_pressure,
            runnable_lane_ids=runnable_lane_ids,
            blocked_reasons={
                str(key): [str(item) for item in value]
                for key, value in blocked_reasons.items()
                if isinstance(value, list)
            },
            query_first_conflict_analysis=bool(
                status.get("query_first_conflict_analysis", True)
            ),
        ).to_dict()

