from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import ConfigResolver, render_interaction_text
from .plan_conductor import PlanConductor
from .runtime_bootstrap_service import RuntimeBootstrapService
from .runtime_presentation_service import RuntimePresentationService
from .runtime_project_support_service import RuntimeProjectSupportService
from .runtime_session_state_service import RuntimeSessionStateService
from .runtime_resume_bundle_service import RuntimeResumeBundleService
from .runtime_report_builder_service import RuntimeReportBuilderService
from .runtime_bootstrap_orchestration_service import RuntimeBootstrapOrchestrationService
from .runtime_orchestration_service import RuntimeOrchestrationService
from .runtime_prompt_handling_service import RuntimePromptHandlingService
from .runtime_plan_authoring_service import RuntimePlanAuthoringService
from .runtime_conductor_state_service import RuntimeConductorStateService
from .runtime_conductor_dispatch_service import RuntimeConductorDispatchService
from .conductor_verification_service import ConductorVerificationService
from .agent_worker_service import AgentWorkerService
from .service_hub import AidocsServiceHub
from .skill_resolution import match_selected_skill_id_for_trigger
from .skill_resolution import selected_skill_override_identity
from .skill_resolution import selected_skill_trigger_identity
from .skill_provider import BUNDLED_PROVIDER_ID
from .skill_override_store import SkillOverrideStore
from .types import RuntimeOwnedCapability, SkillTriggerDecision, SkillTriggerState
logger = logging.getLogger("aidocs.runtime")

_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"

_PLAN_CHECKBOX_STATES = {
    "[ ]": "open",
    "[~]": "in_progress",
    "[>]": "awaiting_feedback",
    "[!]": "blocked",
    "[x]": "completed",
    "[X]": "completed",
}

_SKILL_TRIGGER_RULES: dict[str, dict[str, set[str]]] = {
    "brainstorming": {
        "intent": {
            "brainstorming",
            "creative",
            "creative-task",
            "ideation",
            "design",
            "architecture",
            "approach",
            "ambiguous",
        },
        "workflow": {"design", "discovery"},
    },
    "writing-plans": {
        "intent": {"planning", "plan", "roadmap", "spec"},
        "workflow": {"planning", "design", "discovery"},
    },
    "executing-plans": {
        "intent": {"implementation", "implement", "execution", "executing-plans"},
        "workflow": {"implementation", "execution"},
    },
    "subagent-driven-development": {
        "intent": {"parallel", "subagent", "subagents", "independent-tasks"},
        "workflow": {"implementation", "parallel"},
    },
    "systematic-debugging": {
        "intent": {"debugging", "debug", "bug", "bug-work", "bugfix", "fix"},
        "workflow": {"debugging", "bugfix", "incident"},
    },
    "verification-before-completion": {
        "intent": {
            "verification",
            "verify",
            "completion",
            "verification-before-completion",
        },
        "workflow": {"verification", "completion", "release"},
    },
    "deep-retrieval": {
        "intent": {
            "deep-retrieval",
            "retrieval",
            "signature",
            "signatures",
            "constructor",
            "constructors",
            "enum",
            "service-api",
            "api",
            "trace",
        },
        "workflow": {"inspect", "investigate", "trace"},
    },
    "test-driven-validation": {
        "intent": {
            "test-driven-validation",
            "test",
            "tests",
            "testing",
            "validation",
            "regression",
        },
        "workflow": {"verification", "testing"},
    },
}

_HOST_INJECTABLE_SKILL_KINDS = {"helper", "reasoning", "verification", "authoring"}
_RUNTIME_OWNED_OVERRIDE_MODES = {"aidocs_runtime_owned"}

_DEFAULT_PLAN_VALIDATION_VAGUE_PATTERNS = (
    "do the thing",
    "implement stuff",
    "fix it",
    "handle it",
    "work on it",
    "make it better",
)


def _origin_role(name: str, url: str) -> str:
    lower_name = name.lower()
    lower_url = url.lower()
    if lower_name == "public":
        return "public"
    if lower_name == "origin" and ("private" in lower_url or "_private" in lower_url):
        return "private"
    if lower_name == "origin":
        return "primary"
    return "other"


def _resolve_action_tokens_dir() -> Path:
    """Find action_tokens directory: project root first, then legacy MCP location."""
    candidates = [
        Path(__file__).resolve().parents[3] / "action_tokens",  # project root
        Path(__file__).resolve().parent / "action_tokens",  # legacy: inside MCP package
    ]
    env_path = os.environ.get("AIDOCS_PATH")
    if env_path:
        candidates.insert(1, Path(env_path) / "action_tokens")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]  # fallback to project root even if missing


_ACTION_TOKENS_DIR = _resolve_action_tokens_dir()


def _scoped_action_tokens_dir(project_root: Path | None) -> Path:
    if project_root is not None:
        candidate = project_root / "action_tokens"
        if candidate.is_dir():
            return candidate
    return _ACTION_TOKENS_DIR


def _load_action_token_lists(
    directory: Path | None = None,
    enabled_languages: str = "all",
) -> dict[str, list[str]]:
    """Delegate to intent_guard — single source of truth for action token loading."""
    from .intent_guard import _load_action_token_lists as _canonical_load
    return _canonical_load(directory=directory, enabled_languages=enabled_languages)




def _load_action_tokens(
    directory: Path | None = None,
    enabled_languages: str = "all",
) -> list[tuple[str, tuple[str, ...]]]:
    """Load action token mappings from all YAML files in the action_tokens directory.

    Returns an ordered list of (action_kind, tokens) tuples suitable for
    first-match classification.  Files are simple ``key: [- value]`` YAML
    parsed without PyYAML to avoid an extra dependency.
    """
    merged = _load_action_token_lists(
        directory=directory, enabled_languages=enabled_languages
    )

    # Deduplicate tokens per action_kind while preserving order
    result: list[tuple[str, tuple[str, ...]]] = []
    for action_kind, tokens in merged.items():
        if action_kind.startswith("__"):
            continue
        seen: set[str] = set()
        unique: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                unique.append(token)
        result.append((action_kind, tuple(unique)))
    return result


class RuntimeService:
    """High-level runtime orchestration over sessions, memory, and indexes."""

    def __init__(self, hub: AidocsServiceHub) -> None:
        self.hub = hub
        self._action_token_mapping: dict[
            tuple[str | None, str | None], list[tuple[str, tuple[str, ...]]]
        ] = {}
        self._action_token_lists: dict[
            tuple[str | None, str | None], dict[str, list[str]]
        ] = {}
        self._config_resolver = ConfigResolver()
        self._skill_overrides = SkillOverrideStore()
        self._bootstrap = RuntimeBootstrapService(self)
        self._project_support = RuntimeProjectSupportService(hub, logger, _origin_role)
        self._session_state = RuntimeSessionStateService(self, logger)
        self._resume_bundle = RuntimeResumeBundleService(self)
        self._report_builder = RuntimeReportBuilderService(self)
        self._bootstrap_orchestration = RuntimeBootstrapOrchestrationService(self, logger)
        self._orchestration = RuntimeOrchestrationService(self, logger)
        self._prompt_handling = RuntimePromptHandlingService(self)
        self._plan_authoring = RuntimePlanAuthoringService(self)
        self._conductor_state = RuntimeConductorStateService(self)
        self._conductor_dispatch = RuntimeConductorDispatchService(self)
        self._conductor_verification = ConductorVerificationService(self)
        self._agent_worker = AgentWorkerService(self)
        self._presentation = RuntimePresentationService(self)

    def effective_config(
        self, project_root: Path, session_id: str | None = None
    ) -> dict[str, object]:
        return self._config_resolver.effective_config(
            project_root=project_root, session_id=session_id
        )

    def _get_action_tokens(
        self,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> list[tuple[str, tuple[str, ...]]]:
        cache_key = (
            str(project_root.resolve()) if project_root is not None else None,
            session_id.strip()
            if isinstance(session_id, str) and session_id.strip()
            else None,
        )
        mapping = self._action_token_mapping.get(cache_key)
        if mapping is None:
            effective_config = self._config_resolver.effective_config(
                project_root=project_root, session_id=session_id
            )
            languages = (
                effective_config.get("languages")
                if isinstance(effective_config.get("languages"), dict)
                else {}
            )
            enabled_languages = str(languages.get("enabled", "all") or "all")
            mapping = _load_action_tokens(
                directory=_scoped_action_tokens_dir(project_root),
                enabled_languages=enabled_languages,
            )
            self._action_token_mapping[cache_key] = mapping
        return mapping

    def _get_action_token_lists(
        self,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, list[str]]:
        cache_key = (
            str(project_root.resolve()) if project_root is not None else None,
            session_id.strip()
            if isinstance(session_id, str) and session_id.strip()
            else None,
        )
        mapping = self._action_token_lists.get(cache_key)
        if mapping is None:
            effective_config = self._config_resolver.effective_config(
                project_root=project_root, session_id=session_id
            )
            languages = (
                effective_config.get("languages")
                if isinstance(effective_config.get("languages"), dict)
                else {}
            )
            enabled_languages = str(languages.get("enabled", "all") or "all")
            mapping = _load_action_token_lists(
                directory=_scoped_action_tokens_dir(project_root),
                enabled_languages=enabled_languages,
            )
            self._action_token_lists[cache_key] = mapping
        return mapping

    def _legacy_external_skill_state_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "external-skill-state.json"

    def _host_skill_state_path(self, project_root: Path, session_id: str) -> Path:
        return (
            project_root
            / ".MEMORY"
            / ".runtime"
            / "sessions"
            / session_id
            / "host-skill-state.json"
        )

    def _legacy_session_host_skill_state_path(
        self, project_root: Path, session_id: str
    ) -> Path:
        return (
            project_root / ".MEMORY" / "sessions" / session_id / "host-skill-state.json"
        )

    def _delete_legacy_external_skill_state(self, project_root: Path) -> None:
        legacy_path = self._legacy_external_skill_state_path(project_root)
        if legacy_path.is_file():
            try:
                legacy_path.unlink()
            except OSError:
                logger.debug(
                    "Failed to remove legacy external skill state at %s", legacy_path
                )

    def _delete_legacy_session_host_skill_state(
        self, project_root: Path, session_id: str
    ) -> None:
        legacy_path = self._legacy_session_host_skill_state_path(
            project_root, session_id
        )
        if legacy_path.is_file():
            try:
                legacy_path.unlink()
            except OSError:
                logger.debug(
                    "Failed to remove legacy session host skill state at %s",
                    legacy_path,
                )

    def _aggregate_provider_state(self, provider_states: dict[str, str]) -> str | None:
        if not provider_states:
            return None
        ordered = [
            "missing",
            "disabled",
            "detected_incompatible",
            "incompatible_but_user_override",
            "compatible",
        ]
        values = set(provider_states.values())
        for candidate in ordered:
            if candidate in values:
                return candidate
        return next(iter(sorted(values)), None)

    def _skill_guidance_excerpt(
        self,
        content: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
        max_lines: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        resolved_max_lines = int(
            max_lines
            or self._config_resolver.get(
                "presentation.helper_skill_excerpt_lines",
                project_root=project_root,
                session_id=session_id,
            )
            or 12
        )
        resolved_max_chars = int(
            max_chars
            or self._config_resolver.get(
                "presentation.helper_skill_excerpt_chars",
                project_root=project_root,
                session_id=session_id,
            )
            or 1200
        )
        lines = [
            line.rstrip() for line in str(content or "").splitlines() if line.strip()
        ]
        excerpt = "\n".join(lines[:resolved_max_lines]).strip()
        if len(excerpt) > resolved_max_chars:
            excerpt = excerpt[:resolved_max_chars].rstrip()
            if " " in excerpt:
                excerpt = excerpt.rsplit(" ", 1)[0]
            excerpt += "..."
        return excerpt

    def _helper_skill_guidance(
        self,
        active_skill_ids: list[str],
        available_skills: dict[str, dict[str, object]],
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        guidance: list[dict[str, object]] = []
        seen: set[str] = set()
        for skill_id in active_skill_ids:
            normalized_skill_id = str(skill_id or "").strip()
            if not normalized_skill_id or normalized_skill_id in seen:
                continue
            seen.add(normalized_skill_id)
            skill = available_skills.get(normalized_skill_id)
            if not isinstance(skill, dict) and "/" in normalized_skill_id:
                skill = available_skills.get(normalized_skill_id.split("/", 1)[-1])
            if not isinstance(skill, dict):
                continue
            if (
                str(skill.get("skill_kind") or "helper")
                not in _HOST_INJECTABLE_SKILL_KINDS
            ):
                continue
            if str(skill.get("source") or "") not in {"bundled_provider", "project"}:
                continue
            content = self._skill_guidance_excerpt(
                str(skill.get("content") or ""),
                project_root=project_root,
                session_id=session_id,
            )
            if not content:
                continue
            guidance.append(
                {
                    "skill_id": str(skill.get("skill_id") or normalized_skill_id),
                    "name": str(
                        skill.get("name") or normalized_skill_id.split("/")[-1]
                    ),
                    "description": str(skill.get("description") or ""),
                    "skill_kind": str(skill.get("skill_kind") or "helper"),
                    "content": content,
                }
            )
        return guidance

    def _make_runtime_owned_capability(
        self,
        *,
        capability_id: str | None,
        reason: str,
        mode: str,
        selected_skill_id: str | None,
        provider: str | None,
    ) -> dict[str, object] | None:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            return None
        return RuntimeOwnedCapability(
            capability_id=normalized_capability_id,
            source="aidocs_runtime",
            reason=str(reason or "").strip() or "runtime-owned workflow authority",
            mode=str(mode or "").strip() or "aidocs_runtime_owned",
            selected_skill_id=(str(selected_skill_id or "").strip() or None),
            provider=(str(provider or "").strip() or None),
        ).to_dict()

    def _interaction_text(
        self,
        key: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
        **kwargs: object,
    ) -> str:
        return render_interaction_text(
            f"interaction.{key}",
            project_root=project_root,
            session_id=session_id,
            **kwargs,
        )

    def _render_action_directive(
        self,
        action_kind: str | None,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> str:
        normalized = str(action_kind or "").strip()
        if not normalized:
            return ""
        return self._interaction_text(
            f"action_directives.{normalized}",
            project_root=project_root,
            session_id=session_id,
        )

    def _configured_skill_trigger_rule(
        self,
        skill_key: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, set[str]]:
        normalized = self._normalize_skill_trigger_token(skill_key) or ""
        token_lists = self._get_action_token_lists(
            project_root=project_root, session_id=session_id
        )
        token_key = normalized.replace("-", "_")
        intent = {
            token
            for token in (
                self._normalize_skill_trigger_token(str(item))
                for item in token_lists.get(f"__skill_trigger_{token_key}_intent", [])
            )
            if token
        }
        workflow = {
            token
            for token in (
                self._normalize_skill_trigger_token(str(item))
                for item in token_lists.get(f"__skill_trigger_{token_key}_workflow", [])
            )
            if token
        }
        if intent or workflow:
            if normalized:
                intent.add(normalized)
                workflow.add(normalized)
            return {"intent": intent, "workflow": workflow}
        return _SKILL_TRIGGER_RULES.get(
            normalized, {"intent": set(), "workflow": set()}
        )

    def _plan_validation_vague_patterns(
        self,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> tuple[str, ...]:
        token_lists = self._get_action_token_lists(
            project_root=project_root,
            session_id=session_id,
        )
        configured = token_lists.get("__plan_validation_vague_patterns", [])
        if isinstance(configured, list):
            patterns = tuple(
                str(item).strip().casefold() for item in configured if str(item).strip()
            )
            if patterns:
                return patterns
        return _DEFAULT_PLAN_VALIDATION_VAGUE_PATTERNS

    def _summarize_workflow_actions(
        self, project_root: Path, session_id: str | None
    ) -> str:
        workflow = self.hub.workflow.read_compiled(project_root)
        actions = workflow.get("actions") if isinstance(workflow, dict) else []
        if not isinstance(actions, list) or not actions:
            return ""
        limit = int(
            self._config_resolver.get(
                "presentation.workflow_summary_limit",
                project_root=project_root,
                session_id=session_id,
            )
            or 3
        )
        rendered = [
            f"`{str(item.get('trigger') or '?').strip()} -> {str(item.get('kind') or '?').strip()}`"
            for item in actions[:limit]
            if isinstance(item, dict)
        ]
        if len(actions) > len(rendered):
            rendered.append(f"and {len(actions) - len(rendered)} more")
        return ", ".join(rendered)

    def _append_runtime_owned_capability(
        self,
        items: list[dict[str, object]],
        seen: set[tuple[str, str | None]],
        capability: dict[str, object] | None,
    ) -> None:
        if not isinstance(capability, dict):
            return
        capability_id = str(capability.get("capability_id") or "").strip()
        selected_skill_id = (
            str(capability.get("selected_skill_id") or "").strip() or None
        )
        if not capability_id:
            return
        key = (capability_id, selected_skill_id)
        if key in seen:
            return
        seen.add(key)
        items.append(capability)

    def _imported_skill_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        selected_state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected = (
            selected_state
            if isinstance(selected_state, dict)
            else self.hub.skills.get_selected_skills(project_root, session_id)
        )
        selected_skills = [str(item) for item in selected.get("selected_skills", [])]
        invalid_selected_skills = [
            str(item) for item in selected.get("invalid_selected_skills", [])
        ]
        available_skills = {
            str(item.get("skill_id") or ""): item
            for item in self.hub.skills.list_skills(project_root)
        }
        providers = {
            item.provider_id: item
            for item in self.hub.skills.list_external_providers(project_root)
        }

        imported_selected: list[str] = []
        active_skills: list[str] = []
        provider_states: dict[str, str] = {}
        runtime_owned_capabilities: list[dict[str, object]] = []
        seen_runtime_owned_capabilities: set[tuple[str, str | None]] = set()

        for skill_id in [*selected_skills, *invalid_selected_skills]:
            skill = available_skills.get(skill_id)
            if (
                isinstance(skill, dict)
                and str(skill.get("source") or "") == "bundled_provider"
            ):
                provider_id = str(skill.get("provider") or "")
                if not provider_id:
                    continue
                imported_selected.append(skill_id)
                provider_state = str(skill.get("provider_state") or "compatible")
                provider_states[provider_id] = provider_state
                override = self._skill_overrides.resolve(
                    self._override_policy_provider_id(
                        provider=provider_id, source=str(skill.get("source") or "")
                    ),
                    skill_id.split("/")[-1],
                )
                if override.mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                    if provider_state in {
                        "compatible",
                        "incompatible_but_user_override",
                    } and skill.get("selectable", True):
                        self._append_runtime_owned_capability(
                            runtime_owned_capabilities,
                            seen_runtime_owned_capabilities,
                            self._make_runtime_owned_capability(
                                capability_id=override.runtime_capability_id,
                                reason=override.reason,
                                mode=override.mode,
                                selected_skill_id=skill_id,
                                provider=provider_id,
                            ),
                        )
                    continue
                if provider_state in {
                    "compatible",
                    "incompatible_but_user_override",
                } and skill.get("selectable", True):
                    active_skills.append(skill_id)
                continue
            if "/" not in skill_id:
                continue
            provider_id, _skill_name = skill_id.split("/", 1)
            provider = providers.get(provider_id)
            is_external = bool(provider) or (
                isinstance(skill, dict) and self._skill_is_external_provider(skill)
            )
            if not is_external:
                continue

            imported_selected.append(skill_id)
            if provider is None or not provider.root_path.is_dir():
                provider_states[provider_id] = "missing"
                continue
            if not isinstance(skill, dict):
                provider_states[provider_id] = "missing"
                continue

            provider_state = str(
                skill.get("provider_state")
                or provider.compatibility_state
                or "compatible"
            )
            provider_states[provider_id] = provider_state
            override = self._skill_overrides.resolve(
                self._override_policy_provider_id(
                    provider=provider_id, source=str(skill.get("source") or "")
                ),
                skill_id.split("/")[-1],
            )
            if override.mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                if provider_state in {
                    "compatible",
                    "incompatible_but_user_override",
                } and skill.get("selectable", True):
                    self._append_runtime_owned_capability(
                        runtime_owned_capabilities,
                        seen_runtime_owned_capabilities,
                        self._make_runtime_owned_capability(
                            capability_id=override.runtime_capability_id,
                            reason=override.reason,
                            mode=override.mode,
                            selected_skill_id=skill_id,
                            provider=provider_id,
                        ),
                    )
                continue
            if provider_state in {
                "compatible",
                "incompatible_but_user_override",
            } and skill.get("selectable", True):
                active_skills.append(skill_id)

        return {
            "session_id": session_id,
            "selected_skills": imported_selected,
            "active_skills": active_skills,
            "provider_states": provider_states,
            "provider_state": self._aggregate_provider_state(provider_states),
            "runtime_owned_capabilities": runtime_owned_capabilities,
            "helper_skill_guidance": self._helper_skill_guidance(
                active_skills,
                available_skills,
                project_root=project_root,
                session_id=session_id,
            ),
        }

    def _resolve_skill_trigger_state(
        self,
        project_root: Path,
        session_id: str,
        intent: str,
        workflow_state: str | None = None,
    ) -> dict[str, object]:
        selected = self.hub.skills.get_selected_skills(project_root, session_id)
        selected_skills = [str(item) for item in selected.get("selected_skills", [])]
        available_skills = {
            str(item.get("skill_id")): item
            for item in self.hub.skills.list_skills(project_root)
        }
        intent_token = self._normalize_skill_trigger_token(intent)
        workflow_token = self._normalize_skill_trigger_token(workflow_state)
        selected_terminal_skill_ids = {
            self._normalize_skill_trigger_token(str(skill_id).split("/", 1)[-1])
            for skill_id in selected_skills
            if self._normalize_skill_trigger_token(str(skill_id).split("/", 1)[-1])
        }

        triggered: list[SkillTriggerDecision] = []
        seen_skill_ids: set[str] = set()
        for index, skill_id in enumerate(selected_skills):
            skill = available_skills.get(skill_id)
            if not isinstance(skill, dict) or not self._skill_is_runtime_compatible(
                skill
            ):
                continue
            decision = self._build_skill_trigger_decision(
                skill,
                available_skills,
                project_root=project_root,
                session_id=session_id,
                selected_rank=max(0, len(selected_skills) - index) * 100,
                intent_token=intent_token,
                workflow_token=workflow_token,
            )
            if decision is not None:
                triggered.append(decision)
                seen_skill_ids.add(decision.skill_id)

        if not triggered:
            auto_triggered: list[SkillTriggerDecision] = []
            for skill_id, skill in available_skills.items():
                if skill_id in seen_skill_ids or skill_id in selected_skills:
                    continue
                terminal_skill_id = self._normalize_skill_trigger_token(
                    str(skill_id).split("/", 1)[-1]
                )
                if (
                    terminal_skill_id
                    and terminal_skill_id in selected_terminal_skill_ids
                ):
                    continue
                if (
                    not isinstance(skill, dict)
                    or not self._skill_is_external_provider(skill)
                    or not self._skill_is_runtime_compatible(skill)
                ):
                    continue
                decision = self._build_skill_trigger_decision(
                    skill,
                    available_skills,
                    project_root=project_root,
                    session_id=session_id,
                    selected_rank=0,
                    intent_token=intent_token,
                    workflow_token=workflow_token,
                )
                if decision is not None:
                    auto_triggered.append(decision)
            if auto_triggered:
                auto_triggered.sort(key=lambda item: (-item.rank, item.skill_id))
                triggered.append(auto_triggered[0])

        triggered.sort(key=lambda item: (-item.rank, item.skill_id))
        active_skill_ids = [
            item.skill_id for item in triggered if item.runtime_owned_capability is None
        ]
        runtime_owned_capabilities: list[dict[str, object]] = []
        seen_runtime_owned_capabilities: set[tuple[str, str | None]] = set()
        for item in triggered:
            self._append_runtime_owned_capability(
                runtime_owned_capabilities,
                seen_runtime_owned_capabilities,
                item.runtime_owned_capability,
            )
        state = SkillTriggerState(
            session_id=session_id,
            intent=intent,
            workflow_state=workflow_state,
            selected_skills=selected_skills,
            active_skills=active_skill_ids,
            triggered=triggered,
        )
        payload = state.to_dict()
        imported_skill_state = self._imported_skill_state(
            project_root, session_id, selected_state=selected
        )
        if intent == "startup" or workflow_state == "session_start":
            active_imported_skills = self._resolve_startup_host_active_skills(
                [str(item) for item in imported_skill_state.get("active_skills", [])],
                available_skills,
            )
        else:
            active_imported_skills = list(payload["active_skills"])
        effective_runtime_owned_capabilities = (
            prompt_runtime_owned_capabilities
            if (prompt_runtime_owned_capabilities := runtime_owned_capabilities)
            else [
                item
                for item in (
                    imported_skill_state.get("runtime_owned_capabilities") or []
                )
                if isinstance(item, dict)
            ]
        )
        payload["provider_state"] = imported_skill_state.get("provider_state")
        payload["provider_states"] = imported_skill_state.get("provider_states")
        payload["runtime_owned_capabilities"] = effective_runtime_owned_capabilities
        payload["imported_skill_state"] = {
            **imported_skill_state,
            "active_skills": active_imported_skills,
            "source": "skill_trigger_state",
            "intent": intent,
            "workflow_state": workflow_state,
            "triggered": payload["triggered"],
            "runtime_owned_capabilities": effective_runtime_owned_capabilities,
            "helper_skill_guidance": self._helper_skill_guidance(
                active_imported_skills,
                available_skills,
                project_root=project_root,
                session_id=session_id,
            ),
        }
        mode_metadata = self._build_imported_skill_mode_metadata(
            selected_skills=[
                str(item) for item in imported_skill_state.get("selected_skills", [])
            ],
            active_skills=active_imported_skills,
            triggered=[item for item in payload["triggered"] if isinstance(item, dict)],
            provider_states=imported_skill_state.get("provider_states")
            if isinstance(imported_skill_state.get("provider_states"), dict)
            else None,
        )
        if mode_metadata is not None:
            payload["imported_skill_state"]["mode_metadata"] = mode_metadata
        return payload

    def _persist_host_skill_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        intent: str,
        workflow_state: str | None = None,
    ) -> dict[str, object]:
        payload = self._resolve_skill_trigger_state(
            project_root, session_id, intent=intent, workflow_state=workflow_state
        )
        path = self._host_skill_state_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            **payload["imported_skill_state"],
            "path": str(path),
        }
        path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        self._delete_legacy_external_skill_state(project_root)
        self._delete_legacy_session_host_skill_state(project_root, session_id)
        return snapshot

    def _read_host_skill_state(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        path = self._host_skill_state_path(project_root, session_id)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                payload.setdefault("source", "skill_trigger_state")
                payload.setdefault("session_id", session_id)
                payload.setdefault("active_skills", [])
                payload.setdefault("selected_skills", [])
                payload.setdefault("runtime_owned_capabilities", [])
                payload.setdefault("provider_states", {})
                payload.setdefault("provider_state", None)
                payload.setdefault("triggered", [])
                payload.setdefault("path", str(path))
                changed = False
                normalized_selected = self.hub.skills.normalize_selected_skill_ids(
                    [
                        str(item)
                        for item in payload.get("selected_skills", [])
                        if str(item).strip()
                    ]
                )
                if normalized_selected != list(payload.get("selected_skills", [])):
                    payload["selected_skills"] = normalized_selected
                    changed = True
                normalized_active = self.hub.skills.normalize_selected_skill_ids(
                    [
                        str(item)
                        for item in payload.get("active_skills", [])
                        if str(item).strip()
                    ]
                )
                if normalized_active != list(payload.get("active_skills", [])):
                    payload["active_skills"] = normalized_active
                    changed = True
                normalized_triggered: list[dict[str, object]] = []
                for item in payload.get("triggered", []):
                    if not isinstance(item, dict):
                        continue
                    normalized_item = dict(item)
                    normalized_skill_ids = self.hub.skills.normalize_selected_skill_ids(
                        [str(item.get("skill_id") or "")]
                    )
                    normalized_selected_ids = (
                        self.hub.skills.normalize_selected_skill_ids(
                            [str(item.get("selected_skill_id") or "")]
                        )
                    )
                    normalized_skill_id = (
                        normalized_skill_ids[0] if normalized_skill_ids else ""
                    )
                    normalized_selected_skill_id = (
                        normalized_selected_ids[0] if normalized_selected_ids else ""
                    )
                    if normalized_skill_id and normalized_skill_id != str(
                        item.get("skill_id") or ""
                    ):
                        normalized_item["skill_id"] = normalized_skill_id
                        changed = True
                    if (
                        normalized_selected_skill_id
                        and normalized_selected_skill_id
                        != str(item.get("selected_skill_id") or "")
                    ):
                        normalized_item["selected_skill_id"] = (
                            normalized_selected_skill_id
                        )
                        changed = True
                    normalized_triggered.append(normalized_item)
                if normalized_triggered != list(payload.get("triggered", [])):
                    payload["triggered"] = normalized_triggered
                    changed = True
                if changed:
                    path.write_text(
                        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                    )
                return payload
        return {
            "source": "skill_trigger_state",
            "session_id": session_id,
            "intent": None,
            "workflow_state": None,
            "selected_skills": [],
            "active_skills": [],
            "runtime_owned_capabilities": [],
            "provider_states": {},
            "provider_state": None,
            "triggered": [],
            "path": str(path),
        }

    def _refresh_host_skill_state_for_session(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        existing = self._read_host_skill_state(project_root, session_id)
        intent = str(existing.get("intent") or "startup")
        workflow_state = existing.get("workflow_state")
        return self._persist_host_skill_state(
            project_root,
            session_id,
            intent=intent,
            workflow_state=str(workflow_state) if workflow_state else None,
        )

    def _refresh_all_host_skill_states(self, project_root: Path) -> None:
        for session in self.hub.sessions.list_sessions(project_root):
            self._refresh_host_skill_state_for_session(project_root, session.session_id)

    def skill_provider_status(
        self, project_root: Path, provider_id: str
    ) -> dict[str, object]:
        provider = self.hub.skills.get_external_provider(project_root, provider_id)
        return {
            "provider_id": provider.provider_id,
            "provider_state": provider.compatibility_state,
            "aidocs_version": __version__,
            "provider_version": provider.version,
            "compatible_versions": list(provider.compatible_versions),
            "compatible_version_range": provider.compatible_version_range,
            "choices": list(provider.choices),
            "user_choice": provider.user_choice,
        }

    def set_skill_provider_override(
        self, project_root: Path, provider_id: str, choice: str | None
    ) -> dict[str, object]:
        provider = self.hub.skills.set_external_provider_override(
            project_root, provider_id, choice
        )
        self._refresh_all_host_skill_states(project_root)
        return {
            "provider_id": provider.provider_id,
            "provider_state": provider.compatibility_state,
            "override": provider.user_choice,
            "choices": list(provider.choices),
        }

    def set_session_skills(
        self, project_root: Path, session_id: str, selected_skills: list[str]
    ) -> dict[str, object]:
        result = self.hub.skills.try_set_selected_skills(
            project_root, session_id, selected_skills
        )
        if result.get("ok"):
            snapshot = self._refresh_host_skill_state_for_session(
                project_root, session_id
            )
            result["imported_skill_state"] = snapshot
        return result

    def _normalize_skill_trigger_token(self, value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return normalized or None

    def _skill_trigger_text_matches(
        self, value: str | None, expected: set[str]
    ) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        normalized = self._normalize_skill_trigger_token(value)
        if not normalized:
            return False
        pieces = set(normalized.split("-")) | {normalized}
        for item in expected:
            if item == normalized or item in pieces:
                return True
            if item.endswith("ing") and item[:-3] in pieces:
                return True
            if normalized.startswith(item) or item.startswith(normalized):
                return True
        return False

    def _infer_skill_trigger_intent(
        self,
        user_request: str,
        action_kind: str | None = None,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> str:
        request = user_request.strip()
        normalized = self._normalize_skill_trigger_token(request) or "understand"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "brainstorming", project_root=project_root, session_id=session_id
            )["intent"],
        ):
            return "brainstorming"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "systematic-debugging",
                project_root=project_root,
                session_id=session_id,
            )["intent"],
        ):
            return "debugging"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "writing-plans", project_root=project_root, session_id=session_id
            )["intent"],
        ):
            return "planning"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "deep-retrieval", project_root=project_root, session_id=session_id
            )["intent"],
        ):
            return "deep-retrieval"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "test-driven-validation",
                project_root=project_root,
                session_id=session_id,
            )["intent"],
        ):
            return "test-driven-validation"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "verification-before-completion",
                project_root=project_root,
                session_id=session_id,
            )["intent"],
        ):
            return "verification"
        return self._normalize_skill_trigger_token(action_kind) or normalized

    def _skill_trigger_rule(
        self,
        skill: dict[str, object],
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, set[str]]:
        skill_name = self._normalize_skill_trigger_token(str(skill.get("name") or ""))
        terminal_skill_id = self._normalize_skill_trigger_token(
            str(skill.get("skill_id") or "").split("/")[-1]
        )
        rule = self._configured_skill_trigger_rule(
            skill_name or terminal_skill_id or "",
            project_root=project_root,
            session_id=session_id,
        )
        if rule and (rule.get("intent") or rule.get("workflow")):
            return rule

        tags = {
            token
            for token in (
                self._normalize_skill_trigger_token(str(item))
                for item in skill.get("tags", [])
            )
            if token
        }
        names = {token for token in (skill_name, terminal_skill_id) if token}
        return {"intent": tags | names, "workflow": tags | names}

    def _skill_is_runtime_compatible(self, skill: dict[str, object]) -> bool:
        provider_state = str(skill.get("provider_state") or "")
        if provider_state and provider_state not in {
            "compatible",
            "incompatible_but_user_override",
        }:
            return False
        return bool(skill.get("selectable", True))

    def _skill_is_external_provider(self, skill: dict[str, object]) -> bool:
        return str(skill.get("source") or "") in {
            "external_provider",
            "bundled_provider",
        }

    def _override_policy_provider_id(self, *, provider: str, source: str) -> str:
        if source == "bundled_provider" and provider == BUNDLED_PROVIDER_ID:
            return _BUNDLED_OVERRIDE_PROVIDER_ID
        return provider

    def _selected_skill_override_identity(
        self,
        selected_skill_id: str,
        provider_states: dict[str, object] | None = None,
    ) -> tuple[str, str] | None:
        return selected_skill_override_identity(
            selected_skill_id,
            provider_states=provider_states,
        )

    def _selected_skill_trigger_identity(
        self,
        selected_skill_id: str,
        *,
        provider_states: dict[str, object] | None = None,
    ) -> tuple[str, str, str] | None:
        return selected_skill_trigger_identity(
            selected_skill_id,
            provider_states=provider_states,
            override_store=self._skill_overrides,
        )

    def _match_selected_skill_id_for_trigger(
        self,
        *,
        selected_skills: list[str],
        skill_id: str,
        provider: str,
        runtime_provider: str,
        provider_states: dict[str, object] | None = None,
    ) -> str | None:
        return match_selected_skill_id_for_trigger(
            selected_skills=selected_skills,
            skill_id=skill_id,
            provider=provider,
            runtime_provider=runtime_provider,
            provider_states=provider_states,
            override_store=self._skill_overrides,
        )

    def _resolve_trigger_skill(
        self,
        skill: dict[str, object],
        available_skills: dict[str, dict[str, object]],
    ) -> tuple[dict[str, object], str, str, str, str, dict[str, object] | None] | None:
        provider = str(skill.get("provider") or "aidocs")
        source = str(skill.get("source") or "")
        skill_id = str(skill.get("skill_id") or "")
        override_mode = "provider_native"
        runtime_provider = provider
        trigger_skill = skill
        runtime_owned_capability = None

        if self._skill_is_external_provider(skill):
            override = self._skill_overrides.resolve(
                self._override_policy_provider_id(provider=provider, source=source),
                skill_id.split("/")[-1],
            )
            override_mode = override.mode
            if override.mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                skill_id = str(override.skill_id or skill_id.split("/")[-1])
                runtime_provider = "aidocs_runtime"
                runtime_owned_capability = self._make_runtime_owned_capability(
                    capability_id=override.runtime_capability_id,
                    reason=override.reason,
                    mode=override.mode,
                    selected_skill_id=str(skill.get("skill_id") or "").strip() or None,
                    provider=provider,
                )
            elif override.mode == "provider_content_aidocs_runtime":
                runtime_provider = "aidocs"

        return (
            trigger_skill,
            skill_id,
            provider,
            runtime_provider,
            override_mode,
            runtime_owned_capability,
        )

    def _resolve_startup_host_active_skills(
        self,
        active_skill_ids: list[str],
        available_skills: dict[str, dict[str, object]],
    ) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for skill_id in active_skill_ids:
            skill = available_skills.get(skill_id)
            if not isinstance(skill, dict):
                continue
            trigger = self._resolve_trigger_skill(skill, available_skills)
            if trigger is None:
                continue
            resolved_skill_id = trigger[1]
            if resolved_skill_id in seen:
                continue
            seen.add(resolved_skill_id)
            resolved.append(resolved_skill_id)
        return resolved

    def _build_imported_skill_mode_metadata(
        self,
        *,
        selected_skills: list[str],
        active_skills: list[str],
        triggered: list[dict[str, object]],
        provider_states: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        active_skill_modes: dict[str, str] = {}
        selected_skill_modes: dict[str, str] = {}
        decisions: list[dict[str, object]] = []

        for item in triggered:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "").strip()
            override_mode = str(item.get("override_mode") or "").strip()
            runtime_owned_capability = (
                item.get("runtime_owned_capability")
                if isinstance(item.get("runtime_owned_capability"), dict)
                else None
            )
            provider = str(item.get("provider") or "").strip()
            runtime_provider = (
                str(item.get("runtime_provider") or provider).strip() or provider
            )
            if not skill_id or not override_mode:
                continue
            selected_skill_id = self._match_selected_skill_id_for_trigger(
                selected_skills=selected_skills,
                skill_id=skill_id,
                provider=provider,
                runtime_provider=runtime_provider,
                provider_states=provider_states,
            )
            if runtime_owned_capability is None:
                active_skill_modes[skill_id] = override_mode
            if selected_skill_id:
                selected_skill_modes[selected_skill_id] = override_mode
            decisions.append(
                {
                    "skill_id": skill_id,
                    "selected_skill_id": selected_skill_id,
                    "override_mode": override_mode,
                    "provider": item.get("provider"),
                    "runtime_provider": item.get("runtime_provider"),
                    "runtime_owned_capability": runtime_owned_capability,
                }
            )

        for selected_skill_id in selected_skills:
            if selected_skill_id in selected_skill_modes:
                continue
            override_target = self._selected_skill_override_identity(
                selected_skill_id,
                provider_states=provider_states,
            )
            if override_target is None:
                continue
            provider_id, selected_name = override_target
            decision = self._skill_overrides.resolve(provider_id, selected_name)
            override_mode = str(decision.mode or "").strip()
            if not override_mode:
                continue
            resolved_skill_id = selected_skill_id
            provider = provider_id
            runtime_provider = provider_id
            runtime_owned_capability = None
            if override_mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                resolved_skill_id = str(decision.skill_id or selected_name)
                runtime_provider = "aidocs_runtime"
                runtime_owned_capability = self._make_runtime_owned_capability(
                    capability_id=decision.runtime_capability_id,
                    reason=decision.reason,
                    mode=decision.mode,
                    selected_skill_id=selected_skill_id,
                    provider=provider_id,
                )
            elif override_mode == "provider_content_aidocs_runtime":
                runtime_provider = "aidocs"
            if runtime_owned_capability is None and (
                resolved_skill_id not in active_skills
                and selected_skill_id not in active_skills
            ):
                continue
            selected_skill_modes[selected_skill_id] = override_mode
            if runtime_owned_capability is None:
                active_skill_modes[resolved_skill_id] = override_mode
            decisions.append(
                {
                    "skill_id": resolved_skill_id,
                    "selected_skill_id": selected_skill_id,
                    "override_mode": override_mode,
                    "provider": provider,
                    "runtime_provider": runtime_provider,
                    "runtime_owned_capability": runtime_owned_capability,
                }
            )

        if not active_skill_modes and not selected_skill_modes:
            return None
        return {
            "active_skill_modes": active_skill_modes,
            "selected_skill_modes": selected_skill_modes,
            "decisions": decisions,
        }

    def _build_skill_trigger_decision(
        self,
        skill: dict[str, object],
        available_skills: dict[str, dict[str, object]],
        *,
        project_root: Path | None,
        session_id: str | None,
        selected_rank: int,
        intent_token: str | None,
        workflow_token: str | None,
    ) -> SkillTriggerDecision | None:
        resolved = self._resolve_trigger_skill(skill, available_skills)
        if resolved is None:
            return None
        (
            trigger_skill,
            resolved_skill_id,
            provider,
            runtime_provider,
            override_mode,
            runtime_owned_capability,
        ) = resolved
        rule = self._skill_trigger_rule(
            trigger_skill,
            project_root=project_root,
            session_id=session_id,
        )
        reasons: list[str] = []
        rank = selected_rank
        if intent_token and self._skill_trigger_text_matches(
            intent_token, rule.get("intent", set())
        ):
            reasons.append(f"intent:{intent_token}")
            rank += 20
        if workflow_token and self._skill_trigger_text_matches(
            workflow_token, rule.get("workflow", set())
        ):
            reasons.append(f"workflow:{workflow_token}")
            rank += 10
        if (
            selected_rank == 0
            and str(trigger_skill.get("source") or "") == "external_provider"
        ):
            rank += 1
        if not reasons:
            return None
        prefix = "session-selected" if selected_rank > 0 else "auto"
        return SkillTriggerDecision(
            skill_id=resolved_skill_id,
            provider=provider,
            runtime_provider=runtime_provider,
            override_mode=override_mode,
            why=prefix + "+" + "+".join(reasons),
            rank=rank,
            runtime_owned_capability=runtime_owned_capability,
        )

    def skill_trigger_state(
        self,
        project_root: Path,
        session_id: str,
        intent: str,
        workflow_state: str | None = None,
    ) -> dict[str, object]:
        payload = self._resolve_skill_trigger_state(
            project_root, session_id, intent=intent, workflow_state=workflow_state
        )
        if payload.get("triggered"):
            logger.info(
                "Skill trigger state resolved for session %s: %s",
                session_id,
                payload.get("triggered"),
            )
        self._persist_host_skill_state(
            project_root, session_id, intent=intent, workflow_state=workflow_state
        )
        payload["skills_overview"] = self._build_skills_overview(
            session_id=session_id,
            selected_skills={
                "selected_skills": list(payload.get("selected_skills", []))
            },
            active_skills=list(payload.get("active_skills", [])),
            imported_skill_state=payload.get("imported_skill_state")
            if isinstance(payload.get("imported_skill_state"), dict)
            else None,
            skill_trigger_state=payload,
        )
        return payload
    def ensure_claude_mcp_config(self, project_root: Path) -> dict[str, object]:
        return self._project_support.ensure_claude_mcp_config(project_root)

    def project_origins(self, project_root: Path) -> dict[str, object]:
        return self._project_support.project_origins(project_root)

    def _load_project_rules(self, project_root: Path) -> dict[str, str]:
        return self._project_support._load_project_rules(project_root)


    def repo_summary(self, project_root: Path) -> dict[str, object]:
        return self._project_support.repo_summary(project_root)

    def project_structure_gaps(self, project_root: Path) -> list[str]:
        return self._project_support.project_structure_gaps(project_root)

    def _copy_missing_tree(
        self,
        source_root: Path,
        dest_root: Path,
        label_prefix: str,
        created: list[str],
        skipped: list[str],
    ) -> None:
        self._project_support._copy_missing_tree(
            source_root,
            dest_root,
            label_prefix,
            created,
            skipped,
        )

    def _copy_missing_file(
        self,
        source_file: Path,
        dest_file: Path,
        label: str,
        created: list[str],
        skipped: list[str],
    ) -> None:
        self._project_support._copy_missing_file(
            source_file,
            dest_file,
            label,
            created,
            skipped,
        )

    def _latest_mtime_ns(self, paths: list[Path]) -> int | None:
        return self._project_support._latest_mtime_ns(paths)

    def _index_freshness_status(
        self, project_root: Path
    ) -> tuple[str, dict[str, object]]:
        return self._project_support._index_freshness_status(project_root)

    def session_start_state(
        self, project_root: Path, session_id: str | None = None
    ) -> dict[str, object]:
        return self._session_state.session_start_state(project_root, session_id)

    def host_state(
        self,
        project_root: Path,
        session_id: str | None = None,
        prompt_text: str | None = None,
    ) -> dict[str, object]:
        return self._session_state.host_state(project_root, session_id, prompt_text)

    def project_init(
        self, project_root: Path, init_git: bool = True, create_remote: bool = False
    ) -> dict[str, object]:
        return self._bootstrap.project_init(project_root, init_git, create_remote)




    def session_start(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)

        startup_files = [
            "/.MEMORY/.aidocs/index.aidocs",
            "/.MEMORY/.aidocs/global-instructions.aidocs",
            "/.MEMORY/.aidocs/coding-standards.aidocs",
            "/.MEMORY/.aidocs/memory-system.aidocs",
            "/.MEMORY/INDEX.md",
        ]

        sessions = self.hub.sessions.list_sessions(project_root)
        session_summaries = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "owner": item.owner,
                "goal": item.goal,
                "last_updated": item.last_updated,
            }
            for item in sessions
        ]

        if session_id is None:
            active = [item for item in sessions if item.status == "active"]
            if len(active) == 1:
                session_id = active[0].session_id
            else:
                response = {
                    "startup_files": startup_files,
                    "origins": self.project_origins(project_root),
                    "repo_summary": self.repo_summary(project_root),
                    "requires_session_selection": True,
                    "reason": "no_unique_active_session",
                    "sessions": session_summaries,
                }
                response["report"] = self._build_session_start_report(response)
                return response

        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)
        handoff = self.hub.sessions.read_handoff(project_root, session_id)
        handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
        selected_skills = self.hub.skills.get_selected_skills(project_root, session_id)
        imported_skill_state = self._persist_host_skill_state(
            project_root, session_id, intent="startup", workflow_state="session_start"
        )
        skill_trigger_state = self.skill_trigger_state(
            project_root,
            session_id,
            intent="startup",
            workflow_state="session_start",
        )
        compliance = self.session_compliance_summary(project_root, session_id)

        if sync_indexes:
            self.hub.code.sync_session_code(
                project_root, session_id=session_id, include_tests=include_tests
            )

        response: dict[str, object] = {
            "startup_files": startup_files,
            "origins": self.project_origins(project_root),
            "repo_summary": self.repo_summary(project_root),
            "requires_session_selection": False,
            "selected_session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "context": {
                "path": str(context.path),
                "sections": context.sections,
            },
            "handoff": {
                "path": str(handoff.path),
                "sections": handoff.sections,
            },
            "handoff_steps": handoff_steps,
            "selected_skills": selected_skills,
            "imported_skill_state": imported_skill_state,
            "active_imported_skills": list(
                imported_skill_state.get("active_skills", [])
            ),
            "runtime_owned_capabilities": [
                item
                for item in (
                    skill_trigger_state.get("runtime_owned_capabilities") or []
                )
                if isinstance(item, dict)
            ],
            "skill_trigger_state": skill_trigger_state,
            "active_skills": list(skill_trigger_state.get("active_skills", [])),
            "compliance": compliance,
            "sessions": session_summaries,
        }
        response["project_overview"] = self._build_project_overview(
            project_root,
            repo_summary=response.get("repo_summary")
            if isinstance(response.get("repo_summary"), dict)
            else None,
            selected_session_id=session.session_id,
            ready=True,
        )
        response["session_overview"] = self._build_session_overview(
            session_id=session.session_id,
            session_sections=session.sections,
            context_sections=context.sections,
            handoff_steps=handoff_steps,
            compliance=compliance,
        )
        response["skills_overview"] = self._build_skills_overview(
            session_id=session.session_id,
            selected_skills=selected_skills,
            active_skills=list(skill_trigger_state.get("active_skills", [])),
            imported_skill_state=imported_skill_state,
            skill_trigger_state=skill_trigger_state,
        )

        if include_code_bundle:
            response["code_bundle"] = self.hub.code.get_context_bundle(
                project_root, session_id=session_id
            )

        response["report"] = self._build_session_start_report(response)

        return response
    def session_resume_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
    ) -> dict[str, object]:
        return self._resume_bundle.session_resume_bundle(
            project_root,
            session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            journal_last_n=journal_last_n,
        )




    def _dashboard_config_entries(
        self, project_root: Path, session_id: str | None
    ) -> list[dict[str, object]]:
        return self._presentation.dashboard_config_entries(project_root, session_id)

    def _dashboard_token_usage(
        self,
        execution_summary: dict[str, object],
        recent_execution: list[dict[str, object]],
    ) -> dict[str, object]:
        return self._presentation.dashboard_token_usage(
            execution_summary,
            recent_execution,
        )

    def dashboard_snapshot(
        self,
        project_root: Path,
        session_id: str | None = None,
        event_limit: int = 200,
    ) -> dict[str, object]:
        return self._presentation.dashboard_snapshot(
            project_root,
            session_id=session_id,
            event_limit=event_limit,
        )

    def session_compliance_summary(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        return self._presentation.session_compliance_summary(project_root, session_id)

    def _build_project_overview(
        self,
        project_root: Path,
        *,
        repo_summary: dict[str, object] | None,
        selected_session_id: str | None = None,
        stage: str | None = None,
        ready: bool | None = None,
    ) -> dict[str, object]:
        return self._presentation.build_project_overview(
            project_root,
            repo_summary=repo_summary,
            selected_session_id=selected_session_id,
            stage=stage,
            ready=ready,
        )

    def _project_artifact_catalog(
        self, project_root: Path
    ) -> dict[str, dict[str, object]]:
        return self._presentation.project_artifact_catalog(project_root)

    def _result_artifacts_root(
        self, project_root: Path, session_id: str | None = None
    ) -> Path:
        return self._presentation.result_artifacts_root(project_root, session_id)

    def _write_result_artifact(
        self,
        project_root: Path,
        *,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
    ) -> dict[str, object]:
        return self._presentation.write_result_artifact(
            project_root,
            payload=payload,
            artifact_name=artifact_name,
            session_id=session_id,
        )

    def build_artifact_backed_result(
        self,
        project_root: Path,
        *,
        inline_summary: str,
        payload: object,
        artifact_name: str,
        session_id: str | None = None,
        structured_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._presentation.build_artifact_backed_result(
            project_root,
            inline_summary=inline_summary,
            payload=payload,
            artifact_name=artifact_name,
            session_id=session_id,
            structured_summary=structured_summary,
        )

    def _build_session_overview(
        self,
        *,
        session_id: str | None,
        session_sections: dict[str, list[str]] | None,
        context_sections: dict[str, list[str]] | None,
        handoff_steps: list[dict[str, object]] | None,
        compliance: dict[str, object] | None,
    ) -> dict[str, object]:
        return self._presentation.build_session_overview(
            session_id=session_id,
            session_sections=session_sections,
            context_sections=context_sections,
            handoff_steps=handoff_steps,
            compliance=compliance,
        )

    def _build_skills_overview(
        self,
        *,
        session_id: str | None,
        selected_skills: dict[str, object] | None,
        active_skills: list[str] | None,
        imported_skill_state: dict[str, object] | None,
        skill_trigger_state: dict[str, object] | None,
    ) -> dict[str, object]:
        return self._presentation.build_skills_overview(
            session_id=session_id,
            selected_skills=selected_skills,
            active_skills=active_skills,
            imported_skill_state=imported_skill_state,
            skill_trigger_state=skill_trigger_state,
        )

    def _build_default_plan_overview(
        self,
        *,
        session_id: str,
        end_goal: str | None = None,
    ) -> dict[str, object]:
        return self._presentation.build_default_plan_overview(
            session_id=session_id,
            end_goal=end_goal,
        )

    def _build_plan_overview(
        self,
        *,
        session_id: str,
        plan_path: str | None,
        plan_sections: dict[str, list[str]] | None,
        has_lanes: bool,
    ) -> dict[str, object]:
        return self._presentation.build_plan_overview(
            session_id=session_id,
            plan_path=plan_path,
            plan_sections=plan_sections,
            has_lanes=has_lanes,
        )

    def _registered_tools_snapshot(self) -> list[object]:
        from .mcp_server import create_server

        server = create_server()
        components = getattr(
            getattr(server, "_local_provider", None), "_components", {}
        )
        return [
            component
            for key, component in components.items()
            if str(key).startswith("tool:")
        ]

    def _sync_bootstrap_indexes(
        self, project_root: Path, include_tests: bool
    ) -> dict[str, object]:
        workflow = self.hub.workflow.compile_project_rules(project_root)
        capabilities = self.hub.capabilities.sync_capabilities(
            project_root, self._registered_tools_snapshot()
        )
        procedures = self.hub.procedures.sync_procedures(
            project_root, self.hub.workflow.read_compiled(project_root)
        )
        links = self.hub.procedure_links.sync_links(
            project_root,
            self.hub.procedures.find_procedures(project_root, query=None, limit=1000),
            self.hub.capabilities.find_capabilities(
                project_root, query=None, limit=1000
            ),
        )
        return {
            "memory": self.hub.index.sync_all(project_root),
            "code_manifest": {
                "code_files": self.hub.code.sync_code_files(
                    project_root, include_tests=include_tests
                ),
                "modules": self.hub.code.sync_modules(project_root),
            },
            "schema": self.hub.schema.sync_schema(project_root),
            "workflow": workflow,
            "capabilities": {"capability_definitions": capabilities},
            "procedures": {"procedure_definitions": procedures},
            "procedure_capability_links": {"links": links},
            "execution": self.hub.execution.execution_status(project_root),
        }

    def _build_session_start_report(self, response: dict[str, object]) -> dict[str, object]:
        return self._report_builder._build_session_start_report(response)

    def _build_bootstrap_report(self, result: dict[str, object]) -> dict[str, object]:
        return self._report_builder._build_bootstrap_report(result)

    def _build_readiness_summary(
        self,
        *,
        bootstrap: dict[str, object],
        selected_session_id: str | None,
        managed_mode: dict[str, object] | None,
        operator_summary: dict[str, object] | None,
    ) -> dict[str, object]:
        return self._report_builder._build_readiness_summary(
            bootstrap=bootstrap,
            selected_session_id=selected_session_id,
            managed_mode=managed_mode,
            operator_summary=operator_summary,
        )

    def _build_operator_report(
        self,
        *,
        readiness_summary: dict[str, object],
        operator_summary: dict[str, object] | None,
        bootstrap: dict[str, object],
        action_kind: str | None = None,
        project_root: Path | None = None,
    ) -> dict[str, object]:
        return self._report_builder._build_operator_report(
            readiness_summary=readiness_summary,
            operator_summary=operator_summary,
            bootstrap=bootstrap,
            action_kind=action_kind,
            project_root=project_root,
        )

    def _build_handle_prompt_report(
        self,
        *,
        mode: str,
        classification: dict[str, object],
        route: dict[str, object],
        next_step: object = None,
        operator_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._report_builder._build_handle_prompt_report(
            mode=mode,
            classification=classification,
            route=route,
            next_step=next_step,
            operator_report=operator_report,
        )



    def project_bootstrap_or_resume(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        return self._bootstrap_orchestration.project_bootstrap_or_resume(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )



    def aidocs_orchestrate(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        return self._orchestration.aidocs_orchestrate(
            project_root,
            user_request,
            action_kind=action_kind,
            session_id=session_id,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )



    def aidocs_route_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, object]:
        managed = self.hub.managed_mode.get_mode(project_root)
        explicit_targets = [
            item for item in (explicit_targets or []) if str(item).strip()
        ]

        if not managed.get("active"):
            return {
                "managed_mode": False,
                "action_kind": action_kind,
                "allowed_direct_inspection": bool(explicit_targets),
                "requires_session": False,
                "requires_task_lifecycle": False,
                "recommended_mcp_flow": ["/aidocs"],
                "blocked_reason": None,
            }

        session_id = managed.get("session_id")
        skill_trigger_state = None
        if isinstance(session_id, str) and session_id.strip():
            intent = self._infer_skill_trigger_intent(
                user_request,
                action_kind,
                project_root=project_root,
                session_id=str(session_id) if session_id else None,
            )
            skill_trigger_state = self.skill_trigger_state(
                project_root,
                session_id,
                intent=intent,
                workflow_state=action_kind,
            )

        preflight = self.hub.policy.preflight_action(
            project_root,
            action_kind=action_kind,
            session_id=str(session_id) if session_id else None,
            user_explicit_targets=explicit_targets,
        )

        requires_task_lifecycle = action_kind in {
            "edit",
            "write_memory",
        }
        active_managed_session = bool(managed.get("active") and session_id)
        recommended = ["runtime_preflight"]
        if active_managed_session:
            recommended.append("plan_conductor_status")
        elif preflight.get("requires_session"):
            recommended.append("session_start")
        if requires_task_lifecycle:
            recommended.append("task_begin")
        if action_kind in {"understand", "trace", "edit", "code_bundle"}:
            recommended.append("orchestrate")

        blocked_reason = None
        if managed.get("active") and preflight.get("allowed") is False:
            blocked_reason = str(preflight.get("reason"))

        return {
            "managed_mode": True,
            "action_kind": action_kind,
            "session_id": session_id,
            "skill_trigger_state": skill_trigger_state,
            "active_skills": list((skill_trigger_state or {}).get("active_skills", [])),
            "runtime_owned_capabilities": [
                item
                for item in (
                    (skill_trigger_state or {}).get("runtime_owned_capabilities") or []
                )
                if isinstance(item, dict)
            ],
            "imported_skill_state": (skill_trigger_state or {}).get(
                "imported_skill_state"
            ),
            "allowed_direct_inspection": bool(explicit_targets)
            and action_kind in {"inspect", "read_file", "read_error"},
            "requires_session": bool(preflight.get("requires_session")),
            "requires_task_lifecycle": requires_task_lifecycle,
            "recommended_mcp_flow": recommended,
            "blocked_reason": blocked_reason,
            "preflight": preflight,
        }

    def classify_prompt_action(
        self,
        user_request: str,
        explicit_targets: list[str] | None = None,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        text = user_request.strip().lower()
        explicit_targets = [
            item for item in (explicit_targets or []) if str(item).strip()
        ]

        if explicit_targets:
            if any(
                token in text
                for token in ("error", "stack trace", "traceback", "log", "logs", "why")
            ):
                action_kind = "read_error"
            else:
                action_kind = "inspect"
            result: dict[str, object] = {"action_kind": action_kind, "why": ["explicit_targets"]}
            self._enrich_domain_hints(result, text, project_root, session_id)
            self._enrich_tool_discovery(result, text, project_root)
            return result

        mapping = self._get_action_tokens(
            project_root=project_root, session_id=session_id
        )
        for action_kind, tokens in mapping:
            if any(token in text for token in tokens):
                result = {"action_kind": action_kind, "why": [f"matched:{action_kind}"]}
                self._enrich_domain_hints(result, text, project_root, session_id)
                self._enrich_tool_discovery(result, text, project_root)
                return result

        result = {"action_kind": "understand", "why": ["default:understand"]}
        self._enrich_domain_hints(result, text, project_root, session_id)
        self._enrich_tool_discovery(result, text, project_root)
        return result

    def _enrich_domain_hints(
        self,
        result: dict[str, object],
        text: str,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        """Detect domain hints from __domain_hint_* tokens and add to result."""
        hints: list[dict[str, str]] = []
        domain_defs = self._get_domain_hint_defs(project_root)
        for domain, defn in domain_defs.items():
            tokens = defn.get("tokens", [])
            if any(token in text for token in tokens):
                matched = [t for t in tokens if t in text]
                hint: dict[str, str] = {"domain": domain, "matched": matched}
                tools = defn.get("tools")
                if tools:
                    hint["recommended_tools"] = str(tools)
                hints.append(hint)
        if hints:
            result["domain_hints"] = hints

    def _enrich_tool_discovery(
        self,
        result: dict[str, object],
        text: str,
        project_root: Path | None = None,
    ) -> None:
        """Match __tool_discovery_* keywords and add recommended_tools to result."""
        token_lists = self._get_action_token_lists(project_root=project_root)
        discovered: list[str] = []
        matched_categories: list[str] = []
        for key, values in token_lists.items():
            if not key.startswith("__tool_discovery_"):
                continue
            category = key.removeprefix("__tool_discovery_")
            # values is a dict with "tokens" and "tools" for TOML tables
            # but _get_action_token_lists flattens to lists — check the raw TOML
            pass
        # Read from raw TOML instead since action_token_lists flattens tables
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return
        tokens_dir = _scoped_action_tokens_dir(project_root)
        if not tokens_dir.is_dir():
            return
        for toml_file in sorted(tokens_dir.glob("*.toml")):
            try:
                data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key, section in data.items():
                if not key.startswith("__tool_discovery_") or not isinstance(section, dict):
                    continue
                category = key.removeprefix("__tool_discovery_")
                tokens = section.get("tokens", [])
                tools = section.get("tools", [])
                if not isinstance(tokens, list) or not isinstance(tools, list):
                    continue
                if any(str(t).lower() in text for t in tokens):
                    matched_categories.append(category)
                    for tool in tools:
                        tool_str = str(tool).strip()
                        if tool_str and tool_str not in discovered:
                            discovered.append(tool_str)
        if discovered:
            result["recommended_tools"] = discovered[:15]  # cap at 15
            result["tool_discovery_categories"] = matched_categories

    def _get_domain_hint_defs(self, project_root: Path | None = None) -> dict[str, dict[str, object]]:
        """Load __domain_hint_* definitions from TOML action token files."""
        cache_key = str(project_root.resolve()) if project_root else "_global"
        cached = getattr(self, "_domain_hint_cache", {})
        if cache_key in cached:
            return cached[cache_key]
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return {}
        root = _scoped_action_tokens_dir(project_root)
        if not root.is_dir():
            return {}
        defs: dict[str, dict[str, object]] = {}
        for toml_file in sorted(root.glob("*.toml")):
            try:
                data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
                for key, value in data.items():
                    if key.startswith("__domain_hint_") and isinstance(value, dict):
                        domain = key[len("__domain_hint_"):]
                        defs[domain] = value
            except Exception:
                continue
        if not hasattr(self, "_domain_hint_cache"):
            self._domain_hint_cache: dict[str, dict[str, dict[str, object]]] = {}
        self._domain_hint_cache[cache_key] = defs
        return defs

    def aidocs_handle_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        return self._prompt_handling.aidocs_handle_prompt(
            project_root,
            user_request,
            action_kind,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )




    def _parse_spec_task_line(self, text: str) -> dict[str, object] | None:
        return self._plan_authoring._parse_spec_task_line(text)

    def _spec_to_plan_sections(
        self,
        project_root: Path,
        session_id: str,
        spec_text: str,
        scope: str | None,
        constraints: list[str] | None,
    ) -> dict[str, list[str]]:
        return self._plan_authoring._spec_to_plan_sections(
            project_root,
            session_id,
            spec_text,
            scope,
            constraints,
        )

    def plan_create_from_spec(
        self,
        project_root: Path,
        session_id: str,
        spec_text: str,
        scope: str | None = None,
        constraints: list[str] | None = None,
    ) -> dict[str, object]:
        return self._plan_authoring.plan_create_from_spec(
            project_root,
            session_id,
            spec_text,
            scope=scope,
            constraints=constraints,
        )

    def plan_validate(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._plan_authoring.plan_validate(project_root, session_id)

    def plan_preflight(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._plan_authoring.plan_preflight(project_root, session_id)

    def _plan_conductor_state_path(self, project_root: Path, session_id: str) -> Path:
        return self._conductor_state._plan_conductor_state_path(project_root, session_id)

    def _plan_conductor_lane_ids(self, project_root: Path, session_id: str) -> set[str]:
        return self._conductor_state._plan_conductor_lane_ids(project_root, session_id)

    def _require_plan_conductor_lane_id(
        self, project_root: Path, session_id: str, lane_id: str
    ) -> None:
        self._conductor_state._require_plan_conductor_lane_id(project_root, session_id, lane_id)

    def _read_plan_conductor_state(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        return self._conductor_state._read_plan_conductor_state(project_root, session_id)

    def _write_plan_conductor_state(
        self, project_root: Path, session_id: str, state: dict[str, object]
    ) -> None:
        self._conductor_state._write_plan_conductor_state(project_root, session_id, state)

    def _plan_conductor_snapshot(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, dict[str, object]]:
        return self._conductor_state._plan_conductor_snapshot(project_root, session_id)

    def plan_conductor_graph(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._conductor_state.plan_conductor_graph(project_root, session_id)

    def plan_conductor_status(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._conductor_state.plan_conductor_status(project_root, session_id)

    def _execution_overlap_risk(self, status: dict[str, object]) -> str:
        return self._conductor_state._execution_overlap_risk(status)

    def _execution_dependency_pressure(self, status: dict[str, object]) -> str:
        return self._conductor_state._execution_dependency_pressure(status)

    def execution_mode_select(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._conductor_state.execution_mode_select(project_root, session_id)

    def _plan_conductor_lane_is_contract_like(self, lane: object) -> bool:
        return self._conductor_dispatch._plan_conductor_lane_is_contract_like(lane)

    def _find_plan_lane(
        self, project_root: Path, session_id: str, lane_id: str
    ) -> object | None:
        return self._conductor_dispatch._find_plan_lane(project_root, session_id, lane_id)

    def _lane_open_steps(self, lane: object) -> list[str]:
        return self._conductor_dispatch._lane_open_steps(lane)

    def _build_subagent_task_packet(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        return self._conductor_dispatch._build_subagent_task_packet(project_root, session_id, lane_id)

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._conductor_dispatch.plan_dispatch_next(project_root, session_id)

    def _build_subagent_task_packet(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        return self._conductor_dispatch._build_subagent_task_packet(project_root, session_id, lane_id)

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        return self._conductor_dispatch.plan_dispatch_next(project_root, session_id)



    def _plan_lanes_complete(self, project_root: Path, session_id: str) -> bool:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        reopened_lane_ids = set(
            self._read_plan_conductor_state(project_root, session_id).get(
                "reopened_lane_ids", []
            )
        )
        lanes = list(getattr(plan, "lanes", []) or [])
        if not lanes:
            return False
        for lane in lanes:
            lane_id = str(getattr(lane, "lane_id", "") or "")
            if lane_id in reopened_lane_ids:
                return False
            if self._lane_open_steps(lane):
                return False
        return True

    def _build_host_interaction_text(
        self,
        *,
        project_root: Path,
        session_id: str | None,
        startup_state: str | None,
        managed: bool,
        prompt_action_kind: str | None,
        active_skills: list[str],
        override_modes: dict[str, str],
        runtime_owned_capabilities: list[dict[str, object]],
        helper_skill_guidance: list[dict[str, object]],
        workflow_summary: str,
        lifecycle_state: dict[str, object] | None,
    ) -> dict[str, object]:
        startup_key = {
            "not_initialized": "startup.not_initialized",
            "not_bootstrapped": "startup.not_bootstrapped",
            "no_session": "startup.no_session",
            "multiple_sessions": "startup.multiple_sessions",
            "stale_indexes": "startup.stale_indexes",
        }.get(str(startup_state or ""))
        startup_message = (
            self._interaction_text(
                startup_key,
                project_root=project_root,
                session_id=session_id,
            )
            if startup_key
            else ""
        )
        unmanaged_message = ""
        if not managed and str(startup_state or "") == "ready":
            unmanaged_message = self._interaction_text(
                "startup.unmanaged",
                project_root=project_root,
                session_id=session_id,
            )

        prompt_lines = []
        if managed:
            prompt_lines.append(
                self._interaction_text(
                    "managed.active",
                    project_root=project_root,
                    session_id=session_id,
                )
            )
            if session_id:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.bound_session",
                        project_root=project_root,
                        session_id=session_id,
                        bound_session_id=session_id,
                    )
                )
                prompt_lines.append(
                    self._interaction_text(
                        "managed.stay_in_session",
                        project_root=project_root,
                        session_id=session_id,
                    )
                )
            if prompt_action_kind:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.active_short",
                        project_root=project_root,
                        session_id=session_id,
                        action_kind=prompt_action_kind,
                    )
                )
            prompt_lines.append(
                self._interaction_text(
                    "managed.use_mcp_first",
                    project_root=project_root,
                    session_id=session_id,
                )
            )
            if active_skills:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.imported_skills",
                        project_root=project_root,
                        session_id=session_id,
                        skills=", ".join(
                            f"`{item}`" for item in active_skills if str(item).strip()
                        ),
                    )
                )
            if override_modes:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.imported_skill_modes",
                        project_root=project_root,
                        session_id=session_id,
                        modes=", ".join(
                            f"`{skill_id}={mode}`"
                            for skill_id, mode in override_modes.items()
                            if str(skill_id).strip() and str(mode).strip()
                        ),
                    )
                )
            if runtime_owned_capabilities:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.runtime_owned_capabilities",
                        project_root=project_root,
                        session_id=session_id,
                        capabilities=", ".join(
                            f"`{str(item.get('capability_id') or '').strip()}`"
                            for item in runtime_owned_capabilities
                            if isinstance(item, dict)
                            and str(item.get("capability_id") or "").strip()
                        ),
                    )
                )
            if helper_skill_guidance:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.helper_guidance_header",
                        project_root=project_root,
                        session_id=session_id,
                    )
                )
                for item in helper_skill_guidance[:2]:
                    content = str(item.get("content") or "").strip()
                    name = str(
                        item.get("name") or item.get("skill_id") or "skill"
                    ).strip()
                    if content:
                        prompt_lines.append(
                            f'<aidocs-skill name="{name}">{content}</aidocs-skill>'
                        )
            if workflow_summary:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.workflow_actions",
                        project_root=project_root,
                        session_id=session_id,
                        workflow_summary=workflow_summary,
                    )
                )
            if lifecycle_state and lifecycle_state.get("needs_task_complete"):
                prompt_lines.append(
                    self._interaction_text(
                        "lifecycle.task_complete",
                        project_root=project_root,
                        session_id=session_id,
                    )
                )
            elif lifecycle_state and lifecycle_state.get("needs_task_update"):
                prompt_lines.append(
                    self._interaction_text(
                        "lifecycle.task_update",
                        project_root=project_root,
                        session_id=session_id,
                    )
                )
        action_directive = self._render_action_directive(
            prompt_action_kind,
            project_root=project_root,
            session_id=session_id,
        )
        execution_prompt = self._interaction_text(
            "entry.execution_prompt",
            project_root=project_root,
            session_id=session_id,
        )
        return {
            "startup_message": startup_message,
            "unmanaged_message": unmanaged_message,
            "prompt_context": " ".join(
                line for line in prompt_lines if isinstance(line, str) and line.strip()
            ),
            "prompt_context_lines": [
                line for line in prompt_lines if isinstance(line, str) and line.strip()
            ],
            "action_directive": action_directive,
            "execution_prompt": execution_prompt,
        }

    def verification_gate(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str | None = None,
        verification_evidence: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if lane_id:
            self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        evidence = (
            verification_evidence if isinstance(verification_evidence, dict) else {}
        )
        context = self.hub.sessions.read_context(project_root, session_id)
        context_sections = (
            context.sections if isinstance(context.sections, dict) else {}
        )
        required_commands = self._clean_bullets(
            context_sections.get("Relevant Commands", [])
        )
        commands_run = [
            str(item).strip()
            for item in (evidence.get("commands_run") or [])
            if str(item).strip()
        ]
        command_results = [
            str(item).strip()
            for item in (evidence.get("command_results") or [])
            if str(item).strip()
        ]
        full_suite_failed = bool(evidence.get("full_suite_failed"))
        failure_evidence = (
            evidence.get("failure_evidence")
            if isinstance(evidence.get("failure_evidence"), dict)
            else {}
        )

        if full_suite_failed:
            attributed_lanes = self._attribute_failure_to_lane(
                project_root, session_id, failure_evidence
            )
            reopened_status = None
            for attributed_lane_id in attributed_lanes:
                reopened_status = self.plan_conductor_reopen_lane_on_fullsuite_failure(
                    project_root,
                    session_id,
                    lane_id=attributed_lane_id,
                    failure_evidence=failure_evidence,
                )
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": False,
                "status": "reopened_full_suite_failure",
                "reason": "full suite verification failed",
                "required_commands": required_commands,
                "commands_run": commands_run,
                "command_results": command_results,
                "attributed_lanes": attributed_lanes,
                "conductor_status": reopened_status,
            }

        if not commands_run:
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": False,
                "status": "blocked_missing_evidence",
                "reason": "no fresh verification commands were provided",
                "required_commands": required_commands,
                "commands_run": commands_run,
                "command_results": command_results,
            }

        missing_required = [cmd for cmd in required_commands if cmd not in commands_run]
        if missing_required:
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": False,
                "status": "blocked_missing_required_commands",
                "reason": "required verification commands were not all executed",
                "required_commands": required_commands,
                "commands_run": commands_run,
                "command_results": command_results,
                "missing_required_commands": missing_required,
            }

        return {
            "session_id": session_id,
            "lane_id": lane_id,
            "verified": True,
            "status": "verified",
            "reason": "fresh verification evidence is present",
            "required_commands": required_commands,
            "commands_run": commands_run,
            "command_results": command_results,
        }

    def plan_dispatch_report(
        self,
        project_root: Path,
        session_id: str,
        packet_result: dict[str, object],
    ) -> dict[str, object]:
        lane_id = str(packet_result.get("lane_id") or "").strip()
        if not lane_id:
            raise ValueError("packet_result.lane_id is required")
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)

        overlap = packet_result.get("overlap_found")
        if isinstance(overlap, dict):
            conflicting_lane_id = str(overlap.get("conflicting_lane_id") or "").strip()
            file_path = str(overlap.get("file_path") or "").strip()
            if conflicting_lane_id and file_path:
                status = self.plan_conductor_report_inflight_overlap(
                    project_root,
                    session_id,
                    paused_lane_id=lane_id,
                    conflicting_lane_id=conflicting_lane_id,
                    file_path=file_path,
                )
                return {
                    "session_id": session_id,
                    "lane_id": lane_id,
                    "result": "paused_overlap",
                    "status": status,
                }

        for item in packet_result.get("hidden_dependencies", []) or []:
            if not isinstance(item, dict):
                continue
            target_lane_id = str(item.get("target_lane_id") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if not target_lane_id:
                continue
            status = self.plan_conductor_record_lane_signal(
                project_root,
                session_id,
                lane_id=lane_id,
                signal_kind="hidden_dependency_found",
                target_lane_id=target_lane_id,
                detail=detail,
            )
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "result": "signaled_hidden_dependency",
                "status": status,
            }

        undeclared_files = [
            item
            for item in (packet_result.get("undeclared_files") or [])
            if isinstance(item, dict)
        ]
        for item in undeclared_files:
            target_lane_id = (
                str(item.get("target_lane_id") or lane_id).strip() or lane_id
            )
            file_path = str(item.get("file_path") or "").strip()
            detail = str(item.get("detail") or file_path).strip()
            status = self.plan_conductor_record_lane_signal(
                project_root,
                session_id,
                lane_id=lane_id,
                signal_kind="undeclared_file_needed",
                target_lane_id=target_lane_id,
                detail=detail,
            )
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "result": "signaled_undeclared_file",
                "status": status,
            }

        # Post-edit validation — check agent's work before accepting
        claimed_done = bool(packet_result.get("claimed_done"))
        if claimed_done:
            validation = self._conductor_verification.validate_agent_output(
                project_root, session_id, lane_id, packet_result,
            )
            if not validation.get("valid"):
                issues = validation.get("issues", [])
                has_scope_violation = any(i.get("category") == "scope_violation" for i in issues)
                has_test_failure = any(i.get("category") in ("test_failure", "missing_test_evidence", "no_tests_run") for i in issues)
                has_syntax_error = any(i.get("category") == "syntax_error" for i in issues)

                # Build actionable instructions for the agent
                instructions: list[str] = []
                if has_scope_violation:
                    instructions.append(
                        "STOP: You modified files outside your lane scope. "
                        "Revert those changes immediately. Only modify files listed in your lane's allowed_files. "
                        "If you need a file from another lane, use plan_conductor_record_lane_signal with signal_kind='undeclared_file_needed'."
                    )
                if has_syntax_error:
                    instructions.append(
                        "FIX: Your edits introduced syntax errors. Fix the broken files before reporting done again."
                    )
                if has_test_failure:
                    instructions.append(
                        "FIX: Your tests are failing. Debug and fix the failures, then re-run tests and report done with passing test evidence."
                    )
                if any(i.get("category") == "missing_test_evidence" for i in issues):
                    instructions.append(
                        "REQUIRED: conductor.require_agent_tests is enabled. Write tests for your changes, run them, "
                        "and include the results in your dispatch report under 'test_evidence': {'commands_run': [...], 'command_results': [...]}."
                    )

                return {
                    "session_id": session_id,
                    "lane_id": lane_id,
                    "result": "validation_failed",
                    "action_required": "stop" if has_scope_violation else "fix_and_retry",
                    "instructions": instructions,
                    "validation": validation,
                    "status": self.plan_conductor_status(project_root, session_id),
                }

        verification_results = (
            packet_result.get("verification_results")
            if isinstance(packet_result.get("verification_results"), dict)
            else {}
        )
        if claimed_done or verification_results:
            gate = self.verification_gate(
                project_root,
                session_id,
                lane_id=lane_id,
                verification_evidence={
                    **verification_results,
                    "commands_run": packet_result.get("commands_run") or [],
                    "command_results": packet_result.get("command_results") or [],
                },
            )
            if not gate.get("verified"):
                return {
                    "session_id": session_id,
                    "lane_id": lane_id,
                    "result": str(gate.get("status") or "verification_blocked"),
                    "verification": gate,
                    "status": gate.get("conductor_status")
                    or self.plan_conductor_status(project_root, session_id),
                    "attributed_lanes": gate.get("attributed_lanes") or [],
                }

        status = self.plan_conductor_status(project_root, session_id)
        return {
            "session_id": session_id,
            "lane_id": lane_id,
            "result": "accepted",
            "claimed_done": claimed_done,
            "verification": gate if claimed_done or verification_results else None,
            "status": status,
        }

    def execution_loop_next(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        # Check for lanes awaiting verification
        state = self._conductor_state._read_plan_conductor_state(project_root, session_id)
        lane_states = state.get("lane_states", {})
        for lid, ls in lane_states.items():
            if isinstance(ls, LaneState):
                ls_val = ls.value
            else:
                ls_val = str(ls)
            if ls_val == "implementation_done":
                return {
                    "session_id": session_id,
                    "action": "verify_lane",
                    "lane_id": lid,
                }

        dispatch = self.plan_dispatch_next(project_root, session_id)
        if dispatch.get("dispatch_state") == "delegated":
            return {
                "session_id": session_id,
                "action": "dispatch_lane",
                "lane_id": dispatch.get("selected_lane_id"),
                "dispatch": dispatch,
            }
        if dispatch.get("dispatch_state") == "inline":
            return {
                "session_id": session_id,
                "action": "inline",
                "dispatch": dispatch,
            }
        if self._plan_lanes_complete(project_root, session_id):
            # All lanes complete — run full suite verification
            return {
                "session_id": session_id,
                "action": "verify_full_suite",
            }
        return {
            "session_id": session_id,
            "action": "all_blocked",
            "dispatch": dispatch,
        }
    def plan_conductor_report_inflight_overlap(
        self,
        project_root: Path,
        session_id: str,
        paused_lane_id: str,
        conflicting_lane_id: str,
        file_path: str,
    ) -> dict[str, object]:
        """Pause a lane when another in-flight lane reports an emergent file overlap."""
        self._require_plan_conductor_lane_id(project_root, session_id, paused_lane_id)
        self._require_plan_conductor_lane_id(
            project_root, session_id, conflicting_lane_id
        )
        state = self._read_plan_conductor_state(project_root, session_id)
        display = file_path.replace("\\", "/").lower()
        paused_lanes = dict(state["paused_lanes"])
        paused_lanes[paused_lane_id] = (
            f"inflight-file-overlap:{display}:{conflicting_lane_id}"
        )
        paused_lanes[conflicting_lane_id] = (
            f"inflight-file-overlap:{display}:{paused_lane_id}"
        )
        self._write_plan_conductor_state(
            project_root,
            session_id,
            {
                "paused_lanes": paused_lanes,
                "contract_ready_lane_ids": list(state["contract_ready_lane_ids"]),
                "reopened_lane_ids": list(state.get("reopened_lane_ids", [])),
                "lane_ownership_history": dict(state.get("lane_ownership_history", {})),
            },
        )
        return self.plan_conductor_status(project_root, session_id)

    def plan_conductor_resume_lane(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        """Clear a pause/hold so the conductor can reevaluate a lane."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        paused_lanes = dict(state["paused_lanes"])
        paused_lanes.pop(lane_id, None)
        self._write_plan_conductor_state(
            project_root,
            session_id,
            {
                "paused_lanes": paused_lanes,
                "contract_ready_lane_ids": list(state["contract_ready_lane_ids"]),
                "reopened_lane_ids": list(state.get("reopened_lane_ids", [])),
                "lane_ownership_history": dict(state.get("lane_ownership_history", {})),
            },
        )
        return self.plan_conductor_status(project_root, session_id)

    def plan_conductor_mark_contract_ready(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        ready: bool = True,
    ) -> dict[str, object]:
        """Mark a contract lane ready so compatible dependent lanes can proceed."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        contract_ready_lane_ids = set(state["contract_ready_lane_ids"])
        conductor = PlanConductor(self.hub, project_root, session_id)
        lane = next(
            (
                plan_lane
                for plan_lane in conductor.plan.lanes
                if plan_lane.lane_id == lane_id
            ),
            None,
        )
        if ready and lane is not None and conductor._lane_is_contract_like(lane):
            contract_ready_lane_ids.add(lane_id)
        elif not ready:
            contract_ready_lane_ids.discard(lane_id)
        self._write_plan_conductor_state(
            project_root,
            session_id,
            {
                "paused_lanes": dict(state["paused_lanes"]),
                "contract_ready_lane_ids": sorted(contract_ready_lane_ids),
                "reopened_lane_ids": list(state.get("reopened_lane_ids", [])),
                "lane_ownership_history": dict(state.get("lane_ownership_history", {})),
            },
        )
        return self.plan_conductor_status(project_root, session_id)

    def plan_conductor_record_lane_signal(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        signal_kind: str,
        target_lane_id: str,
        detail: str = "",
    ) -> dict[str, object]:
        """Record a structured signal from one lane about another lane."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)
        self._require_plan_conductor_lane_id(project_root, session_id, target_lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        lane_signals = dict(state.get("lane_signals", {}))
        signal_entry = {
            "kind": signal_kind,
            "target_lane_id": target_lane_id,
            "detail": detail,
        }
        lane_signals.setdefault(lane_id, []).append(signal_entry)
        self._write_plan_conductor_state(
            project_root,
            session_id,
            {
                "paused_lanes": dict(state["paused_lanes"]),
                "contract_ready_lane_ids": list(state["contract_ready_lane_ids"]),
                "reopened_lane_ids": list(state.get("reopened_lane_ids", [])),
                "lane_ownership_history": dict(state.get("lane_ownership_history", {})),
                "lane_signals": lane_signals,
            },
        )
        return self.plan_conductor_status(project_root, session_id)

    def _attribute_failure_to_lane(
        self,
        project_root: Path,
        session_id: str,
        failure_evidence: dict[str, object],
    ) -> list[str]:
        """Deterministically attribute a full-suite failure to specific lanes.

        Uses failed_files and failed_tests to find which lane owns the failing code.
        Returns a sorted list of attributed lane IDs.
        """
        plan = self.hub.sessions.read_plan(project_root, session_id)
        attributed: set[str] = set()

        failed_files = [
            str(f).replace("\\", "/").lower()
            for f in failure_evidence.get("failed_files", [])
        ]
        for lane in plan.lanes:
            lane_files = [fp.replace("\\", "/").lower() for fp in lane.files]
            for ff in failed_files:
                if any(ff.endswith(lf) or lf.endswith(ff) for lf in lane_files):
                    attributed.add(lane.lane_id)
                    break

        if not attributed:
            failed_tests = failure_evidence.get("failed_tests", [])
            for lane in plan.lanes:
                lane_files = [fp.replace("\\", "/").lower() for fp in lane.files]
                for ft in failed_tests:
                    test_path = str(ft).replace("\\", "/").lower()
                    if any(
                        lf.split("/")[-1].replace(".py", "") in test_path
                        for lf in lane_files
                    ):
                        attributed.add(lane.lane_id)
                        break

        return sorted(attributed)

    def plan_conductor_verify_full_suite(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        test_output: str = "",
    ) -> dict[str, object]:
        """Verify a lane's work against the full test suite and attribute failures.

        Returns deterministic attribution results based on test output analysis.
        """
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)

        failed_files: list[str] = []
        if test_output:
            for line in test_output.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[0].strip().startswith("src/"):
                    failed_files.append(parts[0].strip())

        failure_evidence = {
            "failed_files": failed_files,
            "failed_tests": [],
            "error": test_output[:200] if test_output else "",
        }

        attributed_lanes = self._attribute_failure_to_lane(
            project_root, session_id, failure_evidence
        )

        return {
            "lane_id": lane_id,
            "attributed_lanes": attributed_lanes,
            "failed_files": failed_files,
            "verified": len(attributed_lanes) == 0,
        }

    def plan_conductor_reopen_lane_on_fullsuite_failure(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        failure_evidence: dict[str, object],
    ) -> dict[str, object]:
        """Reopen a completed lane when full-suite verification attributes failure to it."""
        self._require_plan_conductor_lane_id(project_root, session_id, lane_id)

        state = self._read_plan_conductor_state(project_root, session_id)
        reopened_lane_ids = list(state.get("reopened_lane_ids", []))
        lane_ownership_history = dict(state.get("lane_ownership_history", {}))

        if lane_id not in reopened_lane_ids:
            reopened_lane_ids.append(lane_id)

        reopen_count = len(lane_ownership_history.get(lane_id, [])) + 1
        event = {
            "event": "reopened",
            "reopen_count": reopen_count,
            "reason": str(failure_evidence.get("error", "unknown"))[:200],
            "failed_files": [str(f) for f in failure_evidence.get("failed_files", [])],
            "failed_tests": [str(t) for t in failure_evidence.get("failed_tests", [])],
        }
        lane_ownership_history.setdefault(lane_id, []).append(event)

        self._write_plan_conductor_state(
            project_root,
            session_id,
            {
                "paused_lanes": dict(state["paused_lanes"]),
                "contract_ready_lane_ids": list(state["contract_ready_lane_ids"]),
                "reopened_lane_ids": reopened_lane_ids,
                "lane_ownership_history": lane_ownership_history,
            },
        )
        return self.plan_conductor_status(project_root, session_id)

    def plan_conductor_lane_ownership_history(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, list[dict[str, object]]]:
        """Return the persistent ownership history for all lanes across reopen cycles."""
        state = self._read_plan_conductor_state(project_root, session_id)
        return dict(state.get("lane_ownership_history", {}))

    def plan_connect(
        self,
        project_root: Path,
        session_id: str,
        run_preflight: bool = True,
    ) -> dict[str, object]:
        """Connect to an existing session plan — read its state, find where it left off,
        and optionally run preflight analysis on remaining steps.

        Returns the plan content, completion progress, incomplete steps, and (if run_preflight=True)
        a decision map for the remaining work. The agent can then start executing from the first
        incomplete step without needing user guidance.
        """
        plan = self.hub.sessions.read_plan_optional(project_root, session_id)
        if plan is not None:
            plan_feedback = self.hub.sessions.preview_plan_feedback_sections(
                project_root, session_id
            )
            result = self._connect_existing_plan(
                project_root, session_id, plan, run_preflight=run_preflight
            )
            if plan_feedback.get("status") == "awaiting_feedback":
                result["plan_feedback"] = plan_feedback
                result["instruction"] = (
                    "Plan includes prose-only additions. Review the proposed structured steps awaiting feedback, "
                    "then confirm or revise them before continuing implementation."
                )
            return result

        roadmap_steps = self.hub.sessions.read_roadmap_steps(project_root)
        open_work = self._collect_session_open_work(project_root, session_id)
        if roadmap_steps or open_work:
            session = self.hub.sessions.read_session(project_root, session_id)
            goal_values = self._clean_bullets(session.sections.get("Goal", []))
            return {
                "session_id": session_id,
                "connected": True,
                "plan_source": "roadmap_summary"
                if roadmap_steps
                else "session_open_work",
                "roadmap_steps": roadmap_steps,
                "open_work": open_work,
                "plan_overview": self._build_default_plan_overview(
                    session_id=session_id,
                    end_goal=goal_values[0] if goal_values else None,
                ),
                "next_action": "ask_user_what_to_work_on",
                "instruction": self._interaction_text(
                    "runtime.no_session_plan",
                    project_root=project_root,
                    session_id=session_id,
                ),
            }
        session = self.hub.sessions.read_session(project_root, session_id)
        goal_values = self._clean_bullets(session.sections.get("Goal", []))
        return self._build_no_plan_no_roadmap_result(
            session_id, end_goal=goal_values[0] if goal_values else None
        )

    def _connect_existing_plan(
        self,
        project_root: Path,
        session_id: str,
        plan,
        run_preflight: bool = True,
    ) -> dict[str, object]:
        if not plan.sections:
            return self._build_no_plan_no_roadmap_result(session_id)

        # Parse steps from all sections, tracking completion
        completed: list[str] = []
        incomplete: list[str] = []
        for section_name, lines in plan.sections.items():
            for line in lines:
                parsed = self._parse_plan_checkbox_line(line)
                if not parsed:
                    continue
                text = str(parsed["text"])
                if parsed["status"] == "completed":
                    completed.append(text)
                else:
                    incomplete.append(text)

        total = len(completed) + len(incomplete)
        progress = f"{len(completed)}/{total}" if total > 0 else "0/0"

        result: dict[str, object] = {
            "session_id": session_id,
            "connected": True,
            "plan_source": "session_plan",
            "plan_path": str(plan.path),
            "progress": progress,
            "completed_count": len(completed),
            "incomplete_count": len(incomplete),
            "completed_steps": completed,
            "next_steps": incomplete[:5],
        }
        result["plan_overview"] = self._build_plan_overview(
            session_id=session_id,
            plan_path=str(plan.path),
            plan_sections=plan.sections,
            has_lanes=bool(plan.lanes),
        )
        if plan.lanes:
            lane_summary = self._plan_conductor_snapshot(project_root, session_id)
            result["lane_summary"] = lane_summary
            result["conductor"] = lane_summary

        # Include plan goal/purpose if available
        purpose = plan.sections.get("Purpose", [])
        if purpose:
            result["purpose"] = purpose[0].lstrip("- ").strip()

        end_goal = plan.sections.get("End Goal", [])
        if end_goal:
            result["end_goal"] = end_goal[0].lstrip("- ").strip()

        # Run preflight on remaining steps to surface decisions upfront
        if run_preflight and incomplete:
            preflight = self.plan_preflight(project_root, session_id)
            if preflight.get("decisions"):
                result["decisions"] = preflight["decisions"]
            result["step_analysis"] = preflight.get("steps", [])
            result["recommended_order"] = preflight.get("recommended_order", "")

        if incomplete:
            result["instruction"] = self._interaction_text(
                "runtime.plan_progress",
                project_root=project_root,
                session_id=session_id,
                progress=progress,
                next_step=incomplete[0],
                suffix=(
                    f"Resolve {len(result.get('decisions', []))} decision(s) first, then implement."
                    if result.get("decisions")
                    else "Begin implementation."
                ),
            )
        else:
            result["instruction"] = self._interaction_text(
                "runtime.plan_complete",
                project_root=project_root,
                session_id=session_id,
            )

        return result

    def _build_no_plan_no_roadmap_result(
        self, session_id: str, end_goal: str | None = None
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "connected": True,
            "plan_source": "none",
            "roadmap_steps": [],
            "open_work": [],
            "plan_overview": self._build_default_plan_overview(
                session_id=session_id, end_goal=end_goal
            ),
            "next_action": "create_plan_or_roadmap",
            "instruction": self._interaction_text(
                "runtime.no_plan_or_roadmap",
                session_id=session_id,
            ),
        }

    def _collect_session_open_work(
        self, project_root: Path, session_id: str
    ) -> list[dict[str, str]]:
        open_work: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        def add_item(source: str, status: str, text: str) -> None:
            cleaned = text.strip()
            if not cleaned:
                return
            key = (source, status, cleaned)
            if key in seen:
                return
            seen.add(key)
            open_work.append({"source": source, "status": status, "text": cleaned})

        for step in self.hub.sessions.read_handoff_steps_optional(
            project_root, session_id
        ):
            status = str(step.get("status") or "open")
            if status in {"completed", "done"}:
                continue
            add_item("handoff_step", status, str(step.get("text") or ""))

        session = self.hub.sessions.read_session(project_root, session_id)
        for blocker in self._clean_bullets(session.sections.get("Blockers", [])):
            if blocker.lower() != "none":
                add_item("session_blocker", "blocked", blocker)
                if self._looks_like_pending_user_input(blocker):
                    add_item("pending_feedback", "pending_user_feedback", blocker)

        handoff = self.hub.sessions.read_handoff_optional(project_root, session_id)
        feedback_sections = [
            session.sections.get("State", []),
            session.sections.get("Upcoming", []),
        ]
        if handoff is not None:
            for blocker in self._clean_bullets(
                handoff.sections.get("Risks and Blockers", [])
            ):
                if blocker.lower() != "none":
                    add_item("handoff_blocker", "blocked", blocker)
                    if self._looks_like_pending_user_input(blocker):
                        add_item("pending_feedback", "pending_user_feedback", blocker)
            feedback_sections.extend(
                [
                    handoff.sections.get("What Matters Now", []),
                    handoff.sections.get("Open Questions", []),
                    handoff.sections.get("Suggested Next Steps", []),
                ]
            )

        for lines in feedback_sections:
            for item in self._clean_bullets(lines):
                if self._looks_like_pending_user_input(item):
                    add_item("pending_feedback", "pending_user_feedback", item)

        return open_work

    def _parse_plan_checkbox_line(self, line: str) -> dict[str, str] | None:
        stripped = line.strip()
        match = re.match(r"^-\s+(\[[ xX~>!]\])\s+(.+)$", stripped)
        if not match:
            return None
        marker, text = match.groups()
        status = _PLAN_CHECKBOX_STATES.get(marker)
        if status is None:
            return None
        return {"status": status, "text": text.strip()}

    def _looks_like_pending_user_input(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            token in lowered
            for token in (
                "feedback",
                "confirm",
                "confirmation",
                "approve",
                "approval",
                "sign off",
                "sign-off",
                "user input",
                "user decision",
            )
        )

    def _feedback_confirms_completion(self, feedback: str) -> bool:
        lowered = feedback.strip().casefold()
        if not lowered:
            return False
        return any(
            token in lowered
            for token in (
                "confirm",
                "confirmed",
                "complete",
                "completed",
                "done",
                "approved",
                "looks good",
                "lgtm",
            )
        )

    def _normalize_plan_prose_text(self, text: str) -> str:
        normalized = text.strip().rstrip(".")
        normalized = re.sub(
            r"^(the\s+agent\s+should|agent\s+should|should)\s+",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return text.strip()
        return normalized[0].upper() + normalized[1:]

    def _mark_matching_roadmap_step_pending_feedback(
        self,
        project_root: Path,
        session_id: str,
        plan,
    ) -> dict[str, object] | None:
        if plan is None:
            return None

        completed: list[str] = []
        incomplete_found = False
        for lines in plan.sections.values():
            for line in lines:
                parsed = self._parse_plan_checkbox_line(line)
                if parsed is None:
                    continue
                if parsed["status"] == "completed":
                    completed.append(parsed["text"])
                else:
                    incomplete_found = True
        if not completed or incomplete_found:
            return None

        session = self.hub.sessions.read_session(project_root, session_id)
        candidate_texts = completed + [
            self._clean_bullet_value(plan.sections.get("Purpose", [])),
            self._clean_bullet_value(plan.sections.get("End Goal", [])),
            self._clean_bullet_value(session.sections.get("Goal", [])),
        ]
        normalized_candidates = {
            self._normalize_state_text(text)
            for text in candidate_texts
            if text and self._normalize_state_text(text)
        }
        if not normalized_candidates:
            return None

        roadmap_steps = self.hub.sessions.read_roadmap_steps(project_root)
        matches = [
            step
            for step in roadmap_steps
            if step.get("status") in {"open", "in_progress"}
            and self._normalize_state_text(str(step.get("text") or ""))
            in normalized_candidates
        ]
        if len(matches) != 1:
            return None
        return self.mark_roadmap_step_pending_feedback(
            project_root, str(matches[0]["text"])
        )

    def _clean_bullet_value(self, lines: list[str]) -> str:
        cleaned = self._clean_bullets(lines)
        return cleaned[0] if cleaned else ""

    def _normalize_state_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()

    def _execution_state(
        self, goal: str | None, state: list[str] | None
    ) -> list[str] | None:
        items = list(state or [])
        if goal:
            items.insert(0, f"Current task: {goal}")
        return items or None

    def _clean_file_bullets(self, values: list[str] | None) -> list[str]:
        cleaned: list[str] = []
        for item in values or []:
            text = str(item or "").strip()
            if text.startswith("- "):
                text = text[2:].strip()
            if text.startswith("`") and text.endswith("`") and len(text) >= 2:
                text = text[1:-1].strip()
            if text and text != "-":
                cleaned.append(text)
        return cleaned

    def _resolve_task_lane_scope(
        self,
        project_root: Path,
        session_id: str,
        relevant_files: list[str] | None,
    ) -> tuple[str | None, list[str]]:
        normalized_files = list(
            dict.fromkeys(
                path.replace("\\", "/").strip()
                for path in (relevant_files or [])
                if path and path.strip()
            )
        )
        if not normalized_files:
            return None, []
        try:
            conductor = PlanConductor(self.hub, project_root, session_id)
        except Exception:
            return None, []
        matches: list[tuple[str, list[str]]] = []
        for lane in conductor.plan.lanes:
            lane_files = [
                file_path.replace("\\", "/").strip()
                for file_path in lane.files
                if file_path and file_path.strip()
            ]
            if normalized_files and all(
                path in lane_files for path in normalized_files
            ):
                matches.append((lane.lane_id, lane_files))
        if len(matches) != 1:
            return None, []
        return matches[0]

    def task_begin(
        self,
        project_root: Path,
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        current_lane_id, lane_exact_paths = self._resolve_task_lane_scope(
            project_root, session_id, relevant_files
        )
        self.hub.query_gate.set(
            project_root,
            session_id,
            last_tool="task_begin",
            known_exact_paths=[],
            current_lane_id=current_lane_id,
            lane_exact_paths=lane_exact_paths,
        )
        execution_state = self._execution_state(goal, state)
        session_patch: dict[str, list[str]] = {"Status": ["- active"]}
        if execution_state is not None:
            session_patch["State"] = self._as_bullets(execution_state)
        if upcoming is not None:
            session_patch["Upcoming"] = self._as_bullets(upcoming)
        if blockers is not None:
            session_patch["Blockers"] = self._as_bullets(blockers)
        session = self.hub.sessions.update_session(
            project_root, session_id, session_patch
        )

        plan_patch: dict[str, list[str]] = {}
        session_scope = self.hub.sessions.read_session(
            project_root, session_id
        ).sections.get("Scope", ["-"])
        if session_scope:
            plan_patch.setdefault("Scope", session_scope)
        if execution_state is not None:
            plan_patch["Current State"] = self._as_bullets(execution_state)
        if partial_goals is not None:
            plan_patch["Partial Goals"] = self._as_bullets(partial_goals)
        elif upcoming is not None:
            plan_patch["Partial Goals"] = self._as_bullets(upcoming)
        if end_goal is not None:
            plan_patch["End Goal"] = [f"- {end_goal}"]

        if constraints is not None:
            plan_patch["Constraints"] = self._as_bullets(constraints)
        if blockers is not None:
            existing_constraints = []
            try:
                existing_plan = self.hub.sessions.read_plan(project_root, session_id)
                existing_constraints = self._clean_bullets(
                    existing_plan.sections.get("Constraints", [])
                )
            except Exception:
                existing_constraints = []
            merged_constraints = [
                item
                for item in existing_constraints
                if item and not item.startswith("Blockers: ")
            ]
            merged_constraints.extend(f"Blockers: {item}" for item in blockers)
            plan_patch["Constraints"] = self._as_bullets(merged_constraints)
        if upcoming is not None:
            plan_patch["Next Steps"] = self._as_bullets(upcoming)
        if not plan_patch:
            plan = self.hub.sessions.read_plan(project_root, session_id)
        else:
            plan = self.hub.sessions.update_plan(project_root, session_id, plan_patch)

        context_patch: dict[str, list[str]] = {}
        if relevant_files is not None:
            context_patch["Relevant Files"] = self._as_file_bullets(relevant_files)
        if relevant_commands is not None:
            context_patch["Relevant Commands"] = self._as_bullets(relevant_commands)
        if relevant_snippets is not None:
            context_patch["Relevant Snippets"] = self._as_code_block(relevant_snippets)
        if session_facts is not None:
            context_patch["Session Facts"] = self._as_bullets(session_facts)
        if constraints is not None:
            context_patch["Constraints"] = self._as_bullets(constraints)
        context = (
            self.hub.sessions.update_context(project_root, session_id, context_patch)
            if context_patch
            else self.hub.sessions.read_context(project_root, session_id)
        )

        result: dict[str, object] = {
            "session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "plan": {"path": str(plan.path), "sections": plan.sections},
            "context": {"path": str(context.path), "sections": context.sections},
        }
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )

        # Auto-journal the task start
        try:
            self.hub.sessions.write_journal_entry(
                project_root, session_id,
                action_kind="task_lifecycle",
                intent=goal or "Task started",
                outcome="Task work begun" + (f" — goal: {goal}" if goal else ""),
            )
        except Exception:
            pass

        return result

    def task_update(
        self,
        project_root: Path,
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        effective_relevant_files = relevant_files
        if effective_relevant_files is None:
            try:
                existing_context = self.hub.sessions.read_context(
                    project_root, session_id
                )
                effective_relevant_files = self._clean_file_bullets(
                    existing_context.sections.get("Relevant Files", [])
                )
            except Exception:
                effective_relevant_files = None
        result = self.task_begin(
            project_root=project_root,
            session_id=session_id,
            goal=None,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=effective_relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )
        try:
            journal_parts: list[str] = []
            if state:
                journal_parts.append(f"state={'; '.join(state[:2])}")
            if partial_goals:
                journal_parts.append(f"partial_goals={'; '.join(partial_goals[:2])}")
            if blockers:
                journal_parts.append(f"blockers={'; '.join(blockers[:2])}")
            summary = (
                "; ".join(part for part in journal_parts if part).strip()
                or "updated task progress"
            )
            self.hub.sessions.write_journal_entry(
                project_root,
                session_id,
                action_kind="task_progress",
                intent=summary[:120],
                outcome="progress updated",
            )
        except Exception:
            pass
        return result

    def task_complete(
        self,
        project_root: Path,
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        verification_evidence: dict[str, object] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        gate = None
        if str(next_status).strip().lower() == "done":
            gate = self.verification_gate(
                project_root,
                session_id,
                lane_id=None,
                verification_evidence=verification_evidence,
            )
            if not gate.get("verified"):
                return {
                    "session_id": session_id,
                    "blocked": True,
                    "status": "verification_blocked",
                    "verification": gate,
                }
        self.hub.query_gate.set(
            project_root,
            session_id,
            last_tool="task_complete",
            known_exact_paths=[],
            current_lane_id=None,
            lane_exact_paths=[],
        )
        session = self.hub.sessions.read_session(project_root, session_id)
        existing_state = self._clean_bullets(session.sections.get("State", []))
        existing_state.append(result_summary)
        session_patch = {
            "Status": [f"- {next_status}"],
            "State": self._as_bullets(existing_state),
        }
        updated = self.hub.sessions.update_session(
            project_root, session_id, session_patch
        )
        try:
            existing_plan = self.hub.sessions.read_plan(project_root, session_id)
            existing_validation = self._clean_bullets(
                existing_plan.sections.get("Validation", [])
            )
            existing_validation.append(f"Completion result: {result_summary}")
            plan = self.hub.sessions.update_plan(
                project_root,
                session_id,
                {
                    "Current State": self._as_bullets(existing_state),
                    "Validation": self._as_bullets(existing_validation),
                    "Next Steps": [
                        self._interaction_text(
                            "runtime.task_complete_next_steps",
                            project_root=project_root,
                            session_id=session_id,
                        )
                    ],
                },
            )
        except Exception:
            plan = None
        try:
            handoff = self.hub.sessions.update_handoff(
                project_root,
                session_id,
                {
                    "Current State": self._as_bullets(existing_state),
                    "What Was Done": self._as_bullets([result_summary]),
                    "What Matters Now": [
                        "- This session has completed its current work; review whether follow-up should stay here or move to a successor session."
                    ],
                    "Suggested Next Steps": [
                        "- Review remaining roadmap or plan work and decide whether to pause, close, or hand off this session."
                    ],
                    "Freshness": [
                        f"- Updated {self._timestamp()} after task completion."
                    ],
                },
            )
        except Exception:
            handoff = None

        # Auto-journal the task completion
        try:
            self.hub.sessions.write_journal_entry(
                project_root,
                session_id,
                action_kind="task_complete",
                intent=result_summary[:120],
                outcome=f"completed → {next_status}",
            )
        except Exception:
            pass  # journal is best-effort, never block task_complete

        result: dict[str, object] = {
            "session": {
                "session_id": updated.session_id,
                "path": str(updated.path),
                "sections": updated.sections,
            }
        }
        if gate is not None:
            result["verification"] = gate
        if plan is not None:
            result["plan"] = {"path": str(plan.path), "sections": plan.sections}
        if handoff is not None:
            result["handoff"] = {
                "path": str(handoff.path),
                "sections": handoff.sections,
            }
        try:
            roadmap_feedback = self._mark_matching_roadmap_step_pending_feedback(
                project_root, session_id, plan
            )
        except Exception:
            roadmap_feedback = None
        if roadmap_feedback is not None:
            result["roadmap_feedback"] = roadmap_feedback
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def _refresh_session_code_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_tests: bool = False,
        sync_indexes: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)
        self.hub.code.sync_session_code(
            project_root, session_id=session_id, include_tests=include_tests
        )
        return self.hub.code.get_context_bundle(project_root, session_id=session_id)

    def _as_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- {item}" for item in cleaned] or ["-"]

    def mark_roadmap_step_pending_feedback(
        self, project_root: Path, step_text: str
    ) -> dict[str, object]:
        return self.hub.sessions.update_roadmap_step_state(
            project_root, step_text, "pending_user_feedback"
        )

    def update_roadmap_feedback_state(
        self, project_root: Path, step_text: str, feedback: str
    ) -> dict[str, object]:
        matches = self.hub.sessions.find_roadmap_step_matches(project_root, step_text)
        if len(matches) > 1:
            return {
                "ok": False,
                "error_code": "roadmap_step_ambiguous",
                "message": "Multiple roadmap items match that step text. Use a more specific target.",
                "step_text": step_text,
                "match_count": len(matches),
                "matches": matches,
            }
        if not matches:
            return {
                "ok": False,
                "error_code": "roadmap_step_not_found",
                "message": "No actionable roadmap step matched that text in ROADMAP_2_0_0.md.",
                "step_text": step_text,
            }
        current = matches[0]
        current_status = str(current.get("status") or "")
        if current_status != "pending_user_feedback":
            return {
                "ok": False,
                "error_code": "roadmap_feedback_state_required",
                "message": "Roadmap feedback updates only apply to steps currently pending user feedback.",
                "step_text": step_text,
                "current_status": current_status,
                "expected_status": "pending_user_feedback",
                "matches": matches,
            }
        status = (
            "completed"
            if self._feedback_confirms_completion(feedback)
            else "in_progress"
        )
        result = self.hub.sessions.update_roadmap_step_state(
            project_root, step_text, status
        )
        result["ok"] = True
        result["feedback"] = feedback
        return result

    def normalize_plan_prose(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        existing_lines = list(plan.sections.get("Steps", []))
        original_prose: list[str] = []
        normalized_lines: list[str] = []
        seen_existing = {line.strip() for line in existing_lines}

        for line in existing_lines:
            parsed = self._parse_plan_checkbox_line(line)
            if parsed is not None:
                continue
            stripped = line.strip()
            if self.hub.sessions._is_lane_metadata_line(stripped):
                continue
            if not stripped or stripped == "-" or not stripped.startswith("-"):
                continue
            prose = stripped[1:].strip()
            if not prose:
                continue
            original_prose.append(prose)
            normalized = f"- [>] {self._normalize_plan_prose_text(prose)}"
            if normalized.strip() not in seen_existing:
                normalized_lines.append(normalized)
                seen_existing.add(normalized.strip())

        if normalized_lines:
            plan = self.hub.sessions.update_plan(
                project_root,
                session_id,
                {"Steps": existing_lines + normalized_lines},
            )

        return {
            "status": "awaiting_feedback" if normalized_lines else "unchanged",
            "original_prose": original_prose,
            "normalized_lines": normalized_lines,
            "plan_path": str(plan.path),
        }

    def _as_file_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- `{item}`" for item in cleaned]

    def _as_code_block(self, values: list[str]) -> list[str]:
        cleaned = [item.rstrip() for item in values if item and item.strip()]
        if not cleaned:
            return []
        return ["```text", *cleaned, "```"]

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _handoff_freshness(
        self,
        sections: dict[str, list[str]],
        stale_after_hours: int | None = None,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        resolved_stale_after_hours = int(
            stale_after_hours
            or self._config_resolver.get(
                "presentation.handoff_stale_after_hours",
                project_root=project_root,
                session_id=session_id,
            )
            or 24
        )
        freshness_lines = (
            sections.get("Freshness", []) if isinstance(sections, dict) else []
        )
        for line in freshness_lines:
            match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", line)
            if not match:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
            if not match:
                continue
            raw = match.group(1)
            try:
                if len(raw) == 10:
                    dt = datetime.strptime(raw, "%Y-%m-%d")
                else:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
                age_hours = max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
                return {
                    "status": "stale"
                    if age_hours > resolved_stale_after_hours
                    else "fresh",
                    "timestamp": raw,
                    "age_hours": round(age_hours, 2),
                    "stale_after_hours": resolved_stale_after_hours,
                }
            except ValueError:
                continue
        return {
            "status": "unknown",
            "timestamp": None,
            "age_hours": None,
            "stale_after_hours": resolved_stale_after_hours,
        }

    def _step_changed_recently(
        self,
        step: dict[str, object],
        recent_hours: int | None = None,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> bool:
        resolved_recent_hours = int(
            recent_hours
            or self._config_resolver.get(
                "presentation.handoff_recent_hours",
                project_root=project_root,
                session_id=session_id,
            )
            or 24
        )
        raw = str(step.get("updated_at") or "").strip()
        if not raw:
            return False
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        age_hours = (datetime.now() - dt).total_seconds() / 3600.0
        return age_hours <= resolved_recent_hours

    def _clean_bullets(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped or stripped == "-":
                continue
            if stripped.startswith("-"):
                stripped = stripped[1:].strip()
            if stripped:
                result.append(stripped)
        return result

    def _collect_pending_workflow(
        self, action_kind: str | None, project_root: Path | None
    ) -> str:
        """Collect pending workflow actions for a given action_kind and format as a summary string."""
        if not action_kind or not project_root:
            return ""
        try:
            triggers = self.hub.workflow.triggers_for_action_kind(action_kind)
            if not triggers:
                return ""
            pending: list[dict[str, object]] = []
            flows: list[dict[str, object]] = []
            compiled = self.hub.workflow.read_compiled(project_root) or {}
            rule_defs = (
                compiled.get("rules", [])
                if isinstance(compiled.get("rules"), list)
                else []
            )
            for trigger in triggers:
                pending.extend(
                    self.hub.workflow.pending_actions_for_trigger(project_root, trigger)
                )
                flows.extend(
                    item
                    for item in rule_defs
                    if isinstance(item, dict) and item.get("trigger") == trigger
                )
            if not pending:
                return ""
            # Record trigger evaluation event
            try:
                managed = self.hub.managed_mode.get_mode(project_root)
                session_id = str(managed.get("session_id") or "").strip() or None
                self.hub.execution.record_event(
                    project_root,
                    event_kind="workflow_trigger_evaluated",
                    source_kind="operator_report",
                    session_id=session_id,
                    action_kind=action_kind,
                    status="pending",
                    payload={
                        "triggers": triggers,
                        "pending_count": len(pending),
                        "pending_actions": [
                            {"trigger": a.get("trigger"), "kind": a.get("kind")}
                            for a in pending[:5]
                        ],
                        "pending_flows": [
                            {
                                "trigger": item.get("trigger"),
                                "rule": item.get("source_rule"),
                                "steps": [
                                    step.get("action_ref") or step.get("kind")
                                    for step in (item.get("steps") or [])[:5]
                                    if isinstance(step, dict)
                                ],
                            }
                            for item in flows[:3]
                        ],
                    },
                )
            except Exception as exc:
                logger.debug(
                    "Failed to record workflow trigger evaluation event: %s", exc
                )
            parts = []
            for item in flows[:3]:
                trigger = item.get("trigger", "?")
                steps = [
                    str(step.get("action_ref") or step.get("kind") or "?")
                    for step in (item.get("steps") or [])
                    if isinstance(step, dict)
                ]
                if steps:
                    parts.append(f"`{trigger} → {' then '.join(steps)}`")
            if not parts:
                for action in pending[:3]:
                    trigger = action.get("trigger", "?")
                    kind = action.get("action_ref") or action.get("kind", "?")
                    parts.append(f"`{trigger} → {kind}`")
            if len(pending) > 3:
                parts.append(f"and {len(pending) - 3} more")
            return ", ".join(parts)
        except Exception as exc:
            logger.warning(
                "Failed to collect pending workflow for action_kind=%s: %s",
                action_kind,
                exc,
            )
            return ""

    def _memory_structure_summary(self, project_root: Path) -> dict[str, object]:
        root = project_root / ".MEMORY"
        sections: list[dict[str, object]] = []

        def add_file_section(
            name: str, relative_dir: str, legacy: bool = False
        ) -> None:
            directory = root / relative_dir
            if not directory.exists():
                return
            files = sorted(
                path.name for path in directory.glob("*.md") if path.is_file()
            )
            if not files and relative_dir != "config":
                return
            sections.append(
                {
                    "name": name,
                    "file_count": len(files),
                    "samples": files[:3],
                    "legacy": legacy,
                }
            )

        sessions = self.hub.sessions.list_sessions(project_root)
        archived_sessions_root = root / "archive" / "sessions"
        archived_sessions = 0
        if archived_sessions_root.exists():
            archived_sessions = sum(
                1 for path in archived_sessions_root.iterdir() if path.is_dir()
            )
        sections.append(
            {
                "name": "sessions",
                "active_count": len(sessions),
                "archived_count": archived_sessions,
                "legacy": False,
            }
        )

        add_file_section("rules", "rules")
        add_file_section("domains", "domains")
        add_file_section("system", "system")
        add_file_section("config", "config")
        add_file_section("daily", "daily")
        add_file_section("archive", "archive")
        add_file_section("policy", "policy", legacy=True)
        add_file_section("architecture", "architecture", legacy=True)
        add_file_section("operations", "operations", legacy=True)
        add_file_section("decisions", "decisions", legacy=True)

        return {
            "router_files": ["/.MEMORY/.aidocs/index.aidocs", "/.MEMORY/INDEX.md"],
            "sections": sections,
        }
