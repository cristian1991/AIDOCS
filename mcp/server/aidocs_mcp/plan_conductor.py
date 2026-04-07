from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import re

from .service_hub import AidocsServiceHub
from .types import LaneState, PlanConductorGraph, PlanConductorLane, RunnableLaneResult


_CONTRACT_TOKEN_RE = re.compile(r"(?<![a-z0-9])contract(?![a-z0-9])", re.IGNORECASE)


class PlanConductor:
    """Compute lane graph and safe runnable lanes from a parsed plan."""

    def __init__(
        self,
        hub: AidocsServiceHub,
        project_root: Path,
        session_id: str,
        paused_lanes: dict[str, str] | None = None,
        contract_ready_lane_ids: set[str] | None = None,
        reopened_lane_ids: set[str] | None = None,
        lane_signals: dict[str, list[dict[str, str]]] | None = None,
        lane_states: dict[str, LaneState] | None = None,
    ) -> None:
        self.hub = hub
        self.project_root = project_root
        self.session_id = session_id
        self.plan = hub.sessions.read_plan(project_root, session_id)
        self.paused_lanes = dict(paused_lanes or {})
        self.contract_ready_lane_ids = set(contract_ready_lane_ids or set())
        self.reopened_lane_ids = set(reopened_lane_ids or set())
        self.lane_signals: dict[str, list[dict[str, str]]] = dict(lane_signals or {})
        self.lane_states: dict[str, LaneState] = dict(lane_states or {})

    def graph(self) -> dict[str, object]:
        lanes = [
            PlanConductorLane(
                lane_id=lane.lane_id,
                phase_id=lane.phase_id,
                name=lane.name,
                files=list(lane.files),
                depends_on=list(lane.depends_on),
            )
            for lane in self.plan.lanes
        ]
        graph = PlanConductorGraph(
            phase_order=[phase.phase_id for phase in self.plan.phases],
            lanes=lanes,
            dependencies={
                lane.lane_id: list(lane.depends_on)
                for lane in self.plan.lanes
                if lane.depends_on
            },
            file_owners=self._file_owners(),
        )
        return asdict(graph)

    def runnable_lanes(self) -> dict[str, object]:
        result = RunnableLaneResult(
            lane_agent_limits={lane.lane_id: 1 for lane in self.plan.lanes},
        )
        completed_lanes = {
            lane.lane_id for lane in self.plan.lanes if self._lane_is_completed(lane)
        }
        active_phase = self._first_unresolved_phase_id()

        if active_phase is None:
            return self._with_interaction_state(asdict(result))

        for phase in self.plan.phases:
            if phase.phase_id == active_phase:
                continue
            for lane in phase.lanes:
                if lane.lane_id in completed_lanes:
                    continue
                result.blocked_reasons[lane.lane_id] = [
                    f"waiting-on-phase:{active_phase}"
                ]

        candidate_lanes = [
            lane
            for lane in self.plan.lanes
            if lane.phase_id == active_phase and lane.lane_id not in completed_lanes
        ]

        # First, identify lanes blocked by dependencies
        dependency_blocked_ids: set[str] = set()
        for lane in candidate_lanes:
            unresolved_dependencies = [
                dependency
                for dependency in lane.depends_on
                if dependency not in completed_lanes
                and not self._dependency_is_satisfied_by_contract_ready(
                    dependency, lane
                )
            ]
            if unresolved_dependencies:
                dependency_blocked_ids.add(lane.lane_id)

        # Overlap only matters among lanes that could run simultaneously
        # (i.e., lanes not already blocked by dependencies)
        overlap_eligible = [
            lane
            for lane in candidate_lanes
            if lane.lane_id not in dependency_blocked_ids
        ]
        overlap_reasons = self._candidate_overlap_reasons(overlap_eligible)

        # Track deterministic overlap groups for interaction/debugging state.
        # Shared-file overlap is still a hard block today; tests expect all
        # overlapping lanes to remain blocked until the conflict is resolved.
        overlap_groups: dict[str, list[str]] = {}
        for lane_id, reasons in overlap_reasons.items():
            for reason in reasons:
                # Extract the other lane from the reason string
                parts = reason.split(":")
                if len(parts) >= 3:
                    other_lane = parts[-1]
                    group_key = ":".join(parts[:-1])  # file path
                    overlap_groups.setdefault(group_key, []).append(
                        (lane_id, other_lane)
                    )

        # Keep a deterministic representative per overlap group for future
        # policy/reporting uses without changing the current hard-block model.
        allowed_from_overlap: set[str] = set()
        for group_key, pairs in overlap_groups.items():
            all_lanes_in_group = sorted(
                set(lane for lane, _ in pairs) | set(other for _, other in pairs)
            )
            if all_lanes_in_group:
                allowed_from_overlap.add(all_lanes_in_group[0])

        for lane in candidate_lanes:
            reasons: list[str] = list(overlap_reasons.get(lane.lane_id, []))
            reasons.extend(self._lane_signal_reasons(lane.lane_id))
            unresolved_dependencies = [
                dependency
                for dependency in lane.depends_on
                if dependency not in completed_lanes
                and not self._dependency_is_satisfied_by_contract_ready(
                    dependency, lane
                )
            ]
            if unresolved_dependencies:
                result.waiting_on[lane.lane_id] = unresolved_dependencies
                reasons.extend(
                    f"waiting-on:{dependency}" for dependency in unresolved_dependencies
                )

            # Shared-file overlap remains blocking for all affected lanes.
            # The allowed_from_overlap bookkeeping above is retained only as
            # deterministic group metadata, not as a runnable-lane override.
            if (
                lane.lane_id in overlap_reasons
                and lane.lane_id not in allowed_from_overlap
            ):
                pass

            paused_reason = self.paused_lanes.get(lane.lane_id)
            if paused_reason:
                reasons.append(f"paused:{paused_reason}")

            # Lane state machine check — only READY lanes are runnable
            lane_state = self.lane_states.get(lane.lane_id)
            if lane_state is not None and lane_state != LaneState.READY:
                reasons.append(f"state:{lane_state.value}")

            if reasons:
                result.blocked_reasons[lane.lane_id] = reasons
                continue

            result.runnable_lane_ids.append(lane.lane_id)

        return self._with_interaction_state(asdict(result))

    def summary(self) -> dict[str, object]:
        return {
            "graph": self.graph(),
            "runnable": self.runnable_lanes(),
        }

    def _with_interaction_state(self, payload: dict[str, object]) -> dict[str, object]:
        payload["paused_lane_ids"] = sorted(self.paused_lanes)
        payload["contract_ready_lane_ids"] = sorted(self.contract_ready_lane_ids)
        payload["lane_signals"] = {
            lane_id: signals
            for lane_id, signals in self.lane_signals.items()
            if signals
        }
        return payload

    def _lane_signal_reasons(self, lane_id: str) -> list[str]:
        signals = self.lane_signals.get(lane_id, [])
        return [f"signaled:{s['kind']}:{s['target_lane_id']}" for s in signals]

    def _file_owners(self) -> dict[str, list[str]]:
        owners: dict[str, list[str]] = {}
        for lane in self.plan.lanes:
            for file_path in lane.files:
                display = self._display_file_identity(file_path)
                owners.setdefault(display, []).append(lane.lane_id)
        return owners

    def owns_file(self, file_path: str | None, lane_id: str | None = None) -> bool:
        if not file_path:
            return False
        owners = self._file_owners().get(self._display_file_identity(file_path), [])
        if lane_id is None:
            return bool(owners)
        return lane_id in owners

    def _first_unresolved_phase_id(self) -> str | None:
        for phase in self.plan.phases:
            if any(not self._lane_is_completed(lane) for lane in phase.lanes):
                return phase.phase_id
        return None

    def _lane_is_completed(self, lane: PlanConductorLane) -> bool:
        # State machine takes precedence when lane has an explicit state
        state = self.lane_states.get(lane.lane_id)
        if state is not None:
            return state == LaneState.COMPLETED
        # Fallback: check step markers (backwards compat for plans without lane states)
        if lane.lane_id in self.reopened_lane_ids:
            return False
        matching_lane = next(
            (
                plan_lane
                for plan_lane in self.plan.lanes
                if plan_lane.lane_id == lane.lane_id
            ),
            None,
        )
        if matching_lane is None or not matching_lane.steps:
            return False
        return all(step.status == "completed" for step in matching_lane.steps)

    def _dependency_is_satisfied_by_contract_ready(
        self, dependency: str, lane: PlanConductorLane
    ) -> bool:
        if (
            dependency not in self.contract_ready_lane_ids
            or not self._lane_is_contract_like(lane)
        ):
            return False
        dependency_lane = next(
            (
                plan_lane
                for plan_lane in self.plan.lanes
                if plan_lane.lane_id == dependency
            ),
            None,
        )
        if dependency_lane is None:
            return False
        return self._lane_is_contract_like(dependency_lane)

    def _lane_is_contract_like(self, lane: PlanConductorLane) -> bool:
        tokens = [lane.lane_id, lane.name, *lane.files]
        return any(_CONTRACT_TOKEN_RE.search(token) for token in tokens)

    def _candidate_overlap_reasons(
        self, lanes: list[PlanConductorLane]
    ) -> dict[str, list[str]]:
        owners: dict[str, list[tuple[str, str]]] = {}
        for lane in lanes:
            for file_path in lane.files:
                canonical = self._canonical_file_identity(file_path)
                display = self._display_file_identity(file_path)
                owners.setdefault(canonical, []).append((lane.lane_id, display))

        reasons: dict[str, list[str]] = {}
        for collisions in owners.values():
            if len(collisions) < 2:
                continue
            for lane_id, display in collisions:
                lane_reasons = reasons.setdefault(lane_id, [])
                for other_lane_id, _ in collisions:
                    if other_lane_id == lane_id:
                        continue
                    lane_reasons.append(
                        f"shared-file-overlap:{display}:{other_lane_id}"
                    )
        return reasons

    def _canonical_file_identity(self, file_path: str) -> str:
        return os.path.normcase(file_path.replace("\\", "/"))

    def _display_file_identity(self, file_path: str) -> str:
        return file_path.replace("\\", "/").lower()
