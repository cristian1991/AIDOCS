from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class LaneState(Enum):
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    IMPLEMENTATION_DONE = "implementation_done"
    REOPENED = "reopened"
    AWAITING_USER_FEEDBACK = "awaiting_user_feedback"
    COMPLETED = "completed"

    _VALID_TRANSITIONS: dict[LaneState, set[LaneState]] = {}  # type: ignore[misc]

    @classmethod
    def can_transition(cls, from_state: LaneState, to_state: LaneState) -> bool:
        return to_state in cls._transitions().get(from_state, set())

    @classmethod
    def _transitions(cls) -> dict[LaneState, set[LaneState]]:
        return {
            cls.BLOCKED: {cls.READY},
            cls.READY: {cls.RUNNING},
            cls.RUNNING: {cls.AWAITING_REVIEW, cls.IMPLEMENTATION_DONE, cls.AWAITING_USER_FEEDBACK},
            cls.AWAITING_REVIEW: {cls.IMPLEMENTATION_DONE, cls.RUNNING},
            cls.IMPLEMENTATION_DONE: {cls.COMPLETED, cls.REOPENED},
            cls.COMPLETED: {cls.REOPENED},
            cls.REOPENED: {cls.RUNNING},
            cls.AWAITING_USER_FEEDBACK: {cls.RUNNING},
        }


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    path: Path
    title: str | None
    status: str | None
    owner: str | None
    goal: str | None
    last_updated: str | None


@dataclass(slots=True)
class SessionData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class ContextData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class PlanStep:
    status: str
    text: str


# LaneQuality removed 2026-05-12 (#157). Workers ARE experts and
# experts ARE workers — the tier split was vestigial doctrine that
# never enforced behavior at the spawn layer (agent_expert_service
# never branched on it; the field was captured by the plan parser,
# carried through PlanLane/PlanConductorLane, and ignored). One
# agent tier, one spawn path, no plan-side `Quality:` declaration.


@dataclass(slots=True)
class PlanLane:
    lane_id: str
    phase_id: str
    name: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    # Phoenix 2026-05-09: per-lane tool override. When non-empty,
    # overrides config `conductor.lane_allowed_tools` default for
    # this lane's runtime gate. Plan parser fills from `- Allowed
    # tools: ...` line. Empty = use config default.
    allowed_tools: list[str] = field(default_factory=list)
    # Verification command for this lane (single string). Plan
    # parser fills from `- Verification: ...` line. Captured for
    # dispatch packet's verification_commands.
    verification: str = ""


@dataclass(slots=True)
class PlanPhase:
    phase_id: str
    name: str
    lanes: list[PlanLane] = field(default_factory=list)


@dataclass(slots=True)
class PlanData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]
    phases: list[PlanPhase] = field(default_factory=list)
    lanes: list[PlanLane] = field(default_factory=list)


@dataclass(slots=True)
class PlanConductorLane:
    lane_id: str
    phase_id: str
    name: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    agent_count: int = 1
    # #161 (Phoenix 2026-05-10): surface plan-parsed allowed_tools +
    # verification on conductor_status so operators can confirm "did
    # my plan parse?" without diving into the dispatch packet.
    # Empty list / empty string when the plan didn't specify a per-
    # lane override; the default cascade (worker default tools, no
    # per-lane verification) still applies at dispatch time.
    allowed_tools: list[str] = field(default_factory=list)
    verification: str = ""


@dataclass(slots=True)
class PlanConductorGraph:
    phase_order: list[str] = field(default_factory=list)
    lanes: list[PlanConductorLane] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    file_owners: dict[str, list[str]] = field(default_factory=dict)
    query_first_conflict_analysis: bool = True


@dataclass(slots=True)
class LaneSignal:
    kind: str
    target_lane_id: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target_lane_id": self.target_lane_id,
            "detail": self.detail,
        }


@dataclass(slots=True)
class RunnableLaneResult:
    runnable_lane_ids: list[str] = field(default_factory=list)
    blocked_reasons: dict[str, list[str]] = field(default_factory=dict)
    waiting_on: dict[str, list[str]] = field(default_factory=dict)
    lane_agent_limits: dict[str, int] = field(default_factory=dict)
    lane_signals: dict[str, list[LaneSignal]] = field(default_factory=dict)
    query_first_conflict_analysis: bool = True


@dataclass(slots=True)
class ExecutionModeSelection:
    session_id: str
    mode: str
    reason: str
    lane_count: int
    runnable_lane_count: int
    blocked_lane_count: int
    has_lanes: bool
    has_contract_lanes: bool
    overlap_risk: str
    dependency_pressure: str
    runnable_lane_ids: list[str] = field(default_factory=list)
    blocked_reasons: dict[str, list[str]] = field(default_factory=dict)
    query_first_conflict_analysis: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "reason": self.reason,
            "lane_count": self.lane_count,
            "runnable_lane_count": self.runnable_lane_count,
            "blocked_lane_count": self.blocked_lane_count,
            "has_lanes": self.has_lanes,
            "has_contract_lanes": self.has_contract_lanes,
            "overlap_risk": self.overlap_risk,
            "dependency_pressure": self.dependency_pressure,
            "runnable_lane_ids": list(self.runnable_lane_ids),
            "blocked_reasons": {
                str(key): list(value) for key, value in self.blocked_reasons.items()
            },
            "query_first_conflict_analysis": self.query_first_conflict_analysis,
        }


@dataclass(slots=True)
class SubagentTaskPacket:
    session_id: str
    lane_id: str
    task_id: str
    goal: str
    allowed_files: list[str] = field(default_factory=list)
    required_reads: list[str] = field(default_factory=list)
    required_symbols: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    done_definition: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    output_schema: dict[str, object] = field(default_factory=dict)
    predecessor_context: list[dict[str, object]] = field(default_factory=list)
    # Phoenix 2026-05-09: per-lane tool override propagated from the
    # plan's `Allowed tools:` line through the dispatch packet to
    # both the worker's CLI --allowedTools flag (already used by
    # agent_expert_service._build_cli_allowed_tools) and the
    # AIDOCS-side runtime gate (set_lane_scope override path).
    # Empty list = no override; gate uses config default.
    lane_allowed_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "lane_id": self.lane_id,
            "task_id": self.task_id,
            "goal": self.goal,
            "allowed_files": list(self.allowed_files),
            "required_reads": list(self.required_reads),
            "required_symbols": list(self.required_symbols),
            "constraints": list(self.constraints),
            "verification_commands": list(self.verification_commands),
            "done_definition": list(self.done_definition),
            "must_not": list(self.must_not),
            "output_schema": dict(self.output_schema),
            "predecessor_context": list(self.predecessor_context),
            "lane_allowed_tools": list(self.lane_allowed_tools),
        }


@dataclass(slots=True)
class HandoffData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class MemoryWriteResult:
    target_file: Path
    content: str
    # Phase-8 hardening (2026-05-19): record which surfaces accepted the
    # write so callers can detect degraded states. sqlite is canonical;
    # when ``sqlite_ok`` is False, no canonical row exists and the
    # markdown export was suppressed (fail-closed). When ``sqlite_ok``
    # is True but ``markdown_ok`` is False, the canonical row landed
    # but the markdown export failed (e.g. disk full) — readers will
    # still see canonical content via memory_index but disk consumers
    # see stale or missing bytes.
    sqlite_ok: bool = True
    markdown_ok: bool = True
    # Class name of the exception that caused markdown export failure,
    # or None when markdown_ok is True. Audit payload carries this so
    # operators can see "PermissionError on rules/x.md" without re-
    # running the failing call.
    markdown_error: str | None = None
    # sha256 of the canonical content actually written to sqlite (when
    # sqlite_ok). Used by export-lag audit rows so a delayed exporter
    # can verify the disk matches AFTER fixing whatever caused the lag.
    sqlite_checksum: str | None = None


@dataclass(slots=True)
class ExternalSkillProvider:
    provider_id: str
    root_path: Path
    version: str | None
    compatibility_state: str
    compatible_versions: list[str] = field(default_factory=list)
    compatible_version_range: str | None = None
    choices: list[str] = field(default_factory=list)
    user_choice: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "root_path": str(self.root_path),
            "version": self.version,
            "compatibility_state": self.compatibility_state,
            "compatible_versions": list(self.compatible_versions),
            "compatible_version_range": self.compatible_version_range,
            "choices": list(self.choices),
            "user_choice": self.user_choice,
        }


@dataclass(slots=True)
class SkillRecord:
    provider: str
    skill_id: str
    name: str
    description: str
    path: str
    origin: str
    source: str
    tags: list[str] = field(default_factory=list)
    content: str = ""
    skill_kind: str = "helper"

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "origin": self.origin,
            "source": self.source,
            "tags": list(self.tags),
            "content": self.content,
            "skill_kind": self.skill_kind,
        }


@dataclass(slots=True)
class SkillOverrideRule:
    provider_match: str
    skill_id: str
    mode: str
    reason: str
    runtime_capability_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_match": self.provider_match,
            "skill_id": self.skill_id,
            "mode": self.mode,
            "reason": self.reason,
            "runtime_capability_id": self.runtime_capability_id,
        }


@dataclass(slots=True)
class SkillOverrideDecision:
    skill_id: str
    provider: str
    provider_match: str
    mode: str
    reason: str
    runtime_capability_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "provider": self.provider,
            "provider_match": self.provider_match,
            "mode": self.mode,
            "reason": self.reason,
            "runtime_capability_id": self.runtime_capability_id,
        }


@dataclass(slots=True)
class RuntimeOwnedCapability:
    capability_id: str
    source: str
    reason: str
    mode: str
    selected_skill_id: str | None = None
    provider: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "source": self.source,
            "reason": self.reason,
            "mode": self.mode,
            "selected_skill_id": self.selected_skill_id,
            "provider": self.provider,
        }


@dataclass(slots=True)
class SkillTriggerDecision:
    skill_id: str
    provider: str
    runtime_provider: str
    override_mode: str
    why: str
    rank: int
    runtime_owned_capability: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "provider": self.provider,
            "runtime_provider": self.runtime_provider,
            "override_mode": self.override_mode,
            "why": self.why,
            "rank": self.rank,
            "runtime_owned_capability": self.runtime_owned_capability,
        }


@dataclass(slots=True)
class SkillTriggerState:
    session_id: str
    intent: str
    workflow_state: str | None = None
    selected_skills: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    triggered: list[SkillTriggerDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "intent": self.intent,
            "workflow_state": self.workflow_state,
            "selected_skills": list(self.selected_skills),
            "active_skills": list(self.active_skills),
            "triggered": [item.to_dict() for item in self.triggered],
        }


def lines_to_text(lines: Iterable[str]) -> str:
    text = "\n".join(lines).rstrip()
    return text + "\n"
