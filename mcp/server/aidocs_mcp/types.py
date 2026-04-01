from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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


@dataclass(slots=True)
class PlanLane:
    lane_id: str
    phase_id: str
    name: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)


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


@dataclass(slots=True)
class PlanConductorGraph:
    phase_order: list[str] = field(default_factory=list)
    lanes: list[PlanConductorLane] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    file_owners: dict[str, list[str]] = field(default_factory=dict)
    query_first_conflict_analysis: bool = True


@dataclass(slots=True)
class RunnableLaneResult:
    runnable_lane_ids: list[str] = field(default_factory=list)
    blocked_reasons: dict[str, list[str]] = field(default_factory=dict)
    waiting_on: dict[str, list[str]] = field(default_factory=dict)
    lane_agent_limits: dict[str, int] = field(default_factory=dict)
    query_first_conflict_analysis: bool = True


@dataclass(slots=True)
class HandoffData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class MemoryWriteResult:
    target_file: Path
    content: str


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
        }


@dataclass(slots=True)
class SkillOverrideRule:
    provider_match: str
    skill_id: str
    mode: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_match": self.provider_match,
            "skill_id": self.skill_id,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(slots=True)
class SkillOverrideDecision:
    skill_id: str
    provider: str
    provider_match: str
    mode: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "provider": self.provider,
            "provider_match": self.provider_match,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(slots=True)
class SkillTriggerDecision:
    skill_id: str
    provider: str
    runtime_provider: str
    override_mode: str
    why: str
    rank: int

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "provider": self.provider,
            "runtime_provider": self.runtime_provider,
            "override_mode": self.override_mode,
            "why": self.why,
            "rank": self.rank,
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

