from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import LaneState

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .agent_expert_service import AgentExpertService
from .conductor_verification_service import ConductorVerificationService
from .config import ConfigResolver, render_interaction_text
from .managed_mode_service import (  # THE authority door (#1027)
    explain_managed_session,
    resolve_managed_session,
)
from .plan_conductor import PlanConductor
from .runtime_bootstrap_orchestration_service import (
    RuntimeBootstrapOrchestrationService,
)
from .runtime_bootstrap_service import RuntimeBootstrapService
from .runtime_conductor_dispatch_service import RuntimeConductorDispatchService
from .runtime_conductor_state_service import RuntimeConductorStateService
from .runtime_orchestration_service import RuntimeOrchestrationService
from .runtime_plan_authoring_service import RuntimePlanAuthoringService
from .runtime_presentation_service import RuntimePresentationService
from .runtime_project_support_service import RuntimeProjectSupportService
from .runtime_prompt_handling_service import RuntimePromptHandlingService
from .runtime_report_builder_service import RuntimeReportBuilderService
from .runtime_resume_bundle_service import RuntimeResumeBundleService
from .runtime_session_state_service import RuntimeSessionStateService
from .service_hub import AidocsServiceHub
from .skill_override_store import SkillOverrideStore
from .skill_provider import BUNDLED_PROVIDER_ID
from .skill_resolution import (
    match_selected_skill_id_for_trigger,
    selected_skill_override_identity,
)
from .types import RuntimeOwnedCapability, SkillTriggerDecision, SkillTriggerState

logger = logging.getLogger("aidocs.runtime")

_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_EMPTY_STARTUP_CONTEXT: dict[str, object] = {"path": None, "sections": {}}
_EMPTY_STARTUP_HANDOFF: dict[str, object] = {"path": None, "sections": {}}
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
    # #130 (Empire directive 2026-05-02): the castle doctrine is ONE bundled
    # NLP-activated skill. The lawbook lives in empire-doctrine (the
    # doctrine-skill consolidation chose empire/emperor as the canonical
    # names); "castle doctrine" / "kingdom law" phrasings activate it.
    "empire-doctrine": {
        "intent": {
            "castle",
            "castle-doctrine",
            "kingdom",
            "kingdom-law",
            "doctrine",
            "empire-doctrine",
        },
        "workflow": {"doctrine", "governance"},
    },
}

# "doctrine" added 2026-07-04 (#130): the castle-doctrine lawbook must be
# injectable — carrying the law to the agent IS its purpose.
_HOST_INJECTABLE_SKILL_KINDS = {"helper", "reasoning", "verification", "authoring", "doctrine"}
_RUNTIME_OWNED_OVERRIDE_MODES = {"aidocs_runtime_owned"}

_DEFAULT_PLAN_VALIDATION_VAGUE_PATTERNS = (
    "do the thing",
    "implement stuff",
    "fix it",
    "handle it",
    "work on it",
    "make it better",
)


def _short_lane_id(full: object, *, prefix_len: int = 24, hash_len: int = 6) -> str | None:
    """Compact form of a content-addressed lane id for UI display.

    Content-addressed lane IDs look like
    ``mmsql-1-sqlite-first-per-plans-managed-m-f7f555e2a3``. Auditable but
    ugly in dashboards. Returns ``mmsql-1-sqlite-first…f7f555e``: keeps
    the human-readable head + a short hash slice so the operator can still
    cross-reference but the row fits a sidebar.
    """
    if full is None:
        return None
    s = str(full).strip()
    if not s:
        return None
    if len(s) <= prefix_len + 1 + hash_len:
        return s
    # The hash suffix is the last 10 chars after the slugified head;
    # take its first hash_len chars so dedup stays meaningful.
    parts = s.rsplit("-", 1)
    is_hash_suffix = (
        len(parts) == 2
        and len(parts[1]) >= hash_len
        and all(c in "0123456789abcdef" for c in parts[1].lower())
    )
    if is_hash_suffix:
        head = parts[0][:prefix_len].rstrip("-")
        return f"{head}\u2026{parts[1][:hash_len]}"
    head = s[:prefix_len].rstrip("-")
    return f"{head}\u2026"


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


def _resolve_intent_tokens_dir() -> Path:
    """Deprecated shim — sqlite is the source. Returns the package-bundled
    seed dir post-Phase 5b. Per Empire-directive 2026-05-13: intent vocabulary
    lives in the empire intent-tokens store. This shim survives only for
    backwards-compatible imports.
    """
    return Path(__file__).resolve().parent / "seed" / "intent_tokens"


_INTENT_TOKENS_DIR = _resolve_intent_tokens_dir()


def _scoped_intent_tokens_dir(project_root: Path | None) -> Path:
    """Deprecated shim — sqlite is the source. Returns the legacy dir
    only so the few remaining file-path-shaped callers keep working
    until they're migrated. Per-project TOML overrides are no longer
    consulted; project-scoped tokens move to the kingdom DB in a
    future phase.
    """
    return _INTENT_TOKENS_DIR


def _load_intent_token_lists(
    directory: Path | None = None,
    enabled_languages: str = "all",
) -> dict[str, list[str]]:
    """Delegate to intent_guard — single source of truth for action token loading."""
    from .intent_guard import _load_intent_token_lists as _canonical_load

    return _canonical_load(directory=directory, enabled_languages=enabled_languages)


def _load_intent_tokens(
    directory: Path | None = None,
    enabled_languages: str = "all",
) -> list[tuple[str, tuple[str, ...]]]:
    """Load action token mappings from all YAML files in the intent_tokens directory.

    Returns an ordered list of (action_kind, tokens) tuples suitable for
    first-match classification.  Files are simple ``key: [- value]`` YAML
    parsed without PyYAML to avoid an extra dependency.
    """
    merged = _load_intent_token_lists(directory=directory, enabled_languages=enabled_languages)

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
            tuple[str | None, str | None],
            list[tuple[str, tuple[str, ...]]],
        ] = {}
        self._action_token_lists: dict[tuple[str | None, str | None], dict[str, list[str]]] = {}
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
        self._agent_expert = AgentExpertService(self)
        self._presentation = RuntimePresentationService(self)
        # REQUEST-SCOPED skill listing cache (#489). Safe by CONSTRUCTION, not
        # by invalidation: ClaudeHookHandler builds a fresh RuntimeService per
        # hook event (claude_hook.py:88) and the broker builds a fresh handler
        # per call, so this dict cannot outlive one event and can never serve a
        # stale skill set to a later prompt. Same shape as the request-scoped
        # empire-DB read cache handle() already opens for the same reason.
        #
        # WHY: profiled warm UserPromptSubmit called list_skills 15x and
        # _register_skill_payload 525x for FOURTEEN distinct skills;
        # _resolve_skill_trigger_state alone ran 5x at ~345ms each (1.724s).
        self._request_skill_list: dict[str, list] = {}

    def _request_scoped_skill_list(self, project_root: Path) -> list:
        """``hub.skills.list_skills`` memoized for THIS hook event only.

        Keyed on the resolved project root so a cross-project call still gets
        its own listing. Returns the cached list object itself: callers on this
        path read it (build dicts, filter) and must not mutate it — the same
        contract they already had with the freshly returned list.
        """
        key = str(project_root)
        cached = self._request_skill_list.get(key)
        if cached is None:
            cached = list(self.hub.skills.list_skills(project_root))
            self._request_skill_list[key] = cached
        return cached

    def effective_config(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> dict[str, object]:
        return self._config_resolver.effective_config(
            project_root=project_root,
            session_id=session_id,
        )

    def _get_intent_tokens(
        self,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> list[tuple[str, tuple[str, ...]]]:
        cache_key = (
            str(project_root.resolve()) if project_root is not None else None,
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
        )
        mapping = self._action_token_mapping.get(cache_key)
        if mapping is None:
            effective_config = self._config_resolver.effective_config(
                project_root=project_root,
                session_id=session_id,
            )
            languages = (
                effective_config.get("languages")
                if isinstance(effective_config.get("languages"), dict)
                else {}
            )
            enabled_languages = str(languages.get("enabled", "all") or "all")
            mapping = _load_intent_tokens(
                directory=_scoped_intent_tokens_dir(project_root),
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
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
        )
        mapping = self._action_token_lists.get(cache_key)
        if mapping is None:
            effective_config = self._config_resolver.effective_config(
                project_root=project_root,
                session_id=session_id,
            )
            languages = (
                effective_config.get("languages")
                if isinstance(effective_config.get("languages"), dict)
                else {}
            )
            enabled_languages = str(languages.get("enabled", "all") or "all")
            mapping = _load_intent_token_lists(
                directory=_scoped_intent_tokens_dir(project_root),
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

    def _legacy_session_host_skill_state_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "host-skill-state.json"

    def _delete_legacy_external_skill_state(self, project_root: Path) -> None:
        legacy_path = self._legacy_external_skill_state_path(project_root)
        if legacy_path.is_file():
            try:
                legacy_path.unlink()
            except OSError:
                logger.debug("Failed to remove legacy external skill state at %s", legacy_path)

    def _delete_legacy_session_host_skill_state(self, project_root: Path, session_id: str) -> None:
        legacy_path = self._legacy_session_host_skill_state_path(project_root, session_id)
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
        def _resolve(override: int | None, key: str, default: int) -> int:
            # 0 = unlimited (no truncation). Honor an explicit override
            # (incl. 0); otherwise read config without the `or default`
            # footgun that turned a configured 0 back into the default.
            if override is not None:
                try:
                    return int(override)
                except (TypeError, ValueError):
                    return default
            v = self._config_resolver.get(
                key,
                project_root=project_root,
                session_id=session_id,
            )
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        resolved_max_lines = _resolve(
            max_lines,
            "presentation.helper_skill_excerpt_lines",
            12,
        )
        resolved_max_chars = _resolve(
            max_chars,
            "presentation.helper_skill_excerpt_chars",
            1200,
        )
        lines = [line.rstrip() for line in str(content or "").splitlines() if line.strip()]
        kept = lines if resolved_max_lines <= 0 else lines[:resolved_max_lines]
        excerpt = "\n".join(kept).strip()
        if resolved_max_chars > 0 and len(excerpt) > resolved_max_chars:
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
            if str(skill.get("skill_kind") or "helper") not in _HOST_INJECTABLE_SKILL_KINDS:
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
                    "name": str(skill.get("name") or normalized_skill_id.split("/")[-1]),
                    "description": str(skill.get("description") or ""),
                    "skill_kind": str(skill.get("skill_kind") or "helper"),
                    "content": content,
                },
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
        token_lists = self._get_action_token_lists(project_root=project_root, session_id=session_id)
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
        return _SKILL_TRIGGER_RULES.get(normalized, {"intent": set(), "workflow": set()})

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

    def _summarize_workflow_actions(self, project_root: Path, session_id: str | None) -> str:
        workflow = self.hub.workflow.read_compiled(project_root)
        actions = workflow.get("actions") if isinstance(workflow, dict) else []
        if not isinstance(actions, list) or not actions:
            return ""
        _wsl = self._config_resolver.get(
            "presentation.workflow_summary_limit",
            project_root=project_root,
            session_id=session_id,
        )
        try:
            limit = int(_wsl) if _wsl is not None else 3
        except (TypeError, ValueError):
            limit = 3
        # 0 = unlimited (no cap on rendered actions).
        shown = actions if limit <= 0 else actions[:limit]
        rendered = [
            f"`{str(item.get('trigger') or '?').strip()} -> {str(item.get('kind') or '?').strip()}`"
            for item in shown
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
        selected_skill_id = str(capability.get("selected_skill_id") or "").strip() or None
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
            for item in self._request_scoped_skill_list(project_root)
        }
        providers = {
            item.provider_id: item for item in self.hub.skills.list_external_providers(project_root)
        }

        imported_selected: list[str] = []
        active_skills: list[str] = []
        provider_states: dict[str, str] = {}
        runtime_owned_capabilities: list[dict[str, object]] = []
        seen_runtime_owned_capabilities: set[tuple[str, str | None]] = set()

        for skill_id in [*selected_skills, *invalid_selected_skills]:
            skill = available_skills.get(skill_id)
            if isinstance(skill, dict) and str(skill.get("source") or "") == "bundled_provider":
                provider_id = str(skill.get("provider") or "")
                if not provider_id:
                    continue
                imported_selected.append(skill_id)
                provider_state = str(skill.get("provider_state") or "compatible")
                provider_states[provider_id] = provider_state
                override = self._skill_overrides.resolve(
                    self._override_policy_provider_id(
                        provider=provider_id,
                        source=str(skill.get("source") or ""),
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
                skill.get("provider_state") or provider.compatibility_state or "compatible",
            )
            provider_states[provider_id] = provider_state
            override = self._skill_overrides.resolve(
                self._override_policy_provider_id(
                    provider=provider_id,
                    source=str(skill.get("source") or ""),
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
            for item in self._request_scoped_skill_list(project_root)
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
            if not isinstance(skill, dict) or not self._skill_is_runtime_compatible(skill):
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
                    str(skill_id).split("/", 1)[-1],
                )
                if terminal_skill_id and terminal_skill_id in selected_terminal_skill_ids:
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
            project_root,
            session_id,
            selected_state=selected,
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
                for item in (imported_skill_state.get("runtime_owned_capabilities") or [])
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
            selected_skills=[str(item) for item in imported_skill_state.get("selected_skills", [])],
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
        # Post-Beat-3 storage lives in the session_host_skill_state
        # sqlite table. Callers still receive a dict with a `path`
        # field so diagnostics UIs that show "where this cache lives"
        # keep displaying the legacy path; the file is absent post
        # Beat 3 because the store's init deletes it.
        from .session_host_skill_state_store import SessionHostSkillStateStore

        payload = self._resolve_skill_trigger_state(
            project_root,
            session_id,
            intent=intent,
            workflow_state=workflow_state,
        )
        path = self._host_skill_state_path(project_root, session_id)
        snapshot = dict(payload["imported_skill_state"])
        store = SessionHostSkillStateStore()
        store.init_db(project_root)
        store.set(project_root, session_id, snapshot)
        self._delete_legacy_external_skill_state(project_root)
        self._delete_legacy_session_host_skill_state(project_root, session_id)
        snapshot["path"] = str(path)
        return snapshot

    def _read_host_skill_state(self, project_root: Path, session_id: str) -> dict[str, object]:
        # Post-Beat-3 storage lives in session_host_skill_state. The
        # store's init sweeps both legacy JSON locations so every call
        # after migration reads from sqlite. The normalization /
        # self-healing logic below stays because the stored payload
        # may still contain stale skill IDs from before this beat
        # ran — rewriting via store.set() keeps the cache coherent.
        from .session_host_skill_state_store import SessionHostSkillStateStore

        path = self._host_skill_state_path(project_root, session_id)
        store = SessionHostSkillStateStore()
        store.init_db(project_root)
        payload = store.get(project_root, session_id)
        if payload is not None and isinstance(payload, dict):
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
                [str(item) for item in payload.get("selected_skills", []) if str(item).strip()],
            )
            if normalized_selected != list(payload.get("selected_skills", [])):
                payload["selected_skills"] = normalized_selected
                changed = True
            normalized_active = self.hub.skills.normalize_selected_skill_ids(
                [str(item) for item in payload.get("active_skills", []) if str(item).strip()],
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
                    [str(item.get("skill_id") or "")],
                )
                normalized_selected_ids = self.hub.skills.normalize_selected_skill_ids(
                    [str(item.get("selected_skill_id") or "")],
                )
                normalized_skill_id = normalized_skill_ids[0] if normalized_skill_ids else ""
                normalized_selected_skill_id = (
                    normalized_selected_ids[0] if normalized_selected_ids else ""
                )
                if normalized_skill_id and normalized_skill_id != str(item.get("skill_id") or ""):
                    normalized_item["skill_id"] = normalized_skill_id
                    changed = True
                if normalized_selected_skill_id and normalized_selected_skill_id != str(
                    item.get("selected_skill_id") or "",
                ):
                    normalized_item["selected_skill_id"] = normalized_selected_skill_id
                    changed = True
                normalized_triggered.append(normalized_item)
            if normalized_triggered != list(payload.get("triggered", [])):
                payload["triggered"] = normalized_triggered
                changed = True
            if changed:
                # Drop the synthetic `path` field before persisting so
                # the store doesn't carry it as a real column; it gets
                # added back on every read.
                to_store = {k: v for k, v in payload.items() if k != "path"}
                store.set(project_root, session_id, to_store)
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
        self,
        project_root: Path,
        session_id: str,
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

    def skill_provider_status(self, project_root: Path, provider_id: str) -> dict[str, object]:
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
        self,
        project_root: Path,
        provider_id: str,
        choice: str | None,
    ) -> dict[str, object]:
        provider = self.hub.skills.set_external_provider_override(project_root, provider_id, choice)
        self._refresh_all_host_skill_states(project_root)
        return {
            "provider_id": provider.provider_id,
            "provider_state": provider.compatibility_state,
            "override": provider.user_choice,
            "choices": list(provider.choices),
        }

    def set_session_skills(
        self,
        project_root: Path,
        session_id: str,
        selected_skills: list[str],
    ) -> dict[str, object]:
        result = self.hub.skills.try_set_selected_skills(project_root, session_id, selected_skills)
        if result.get("ok"):
            snapshot = self._refresh_host_skill_state_for_session(project_root, session_id)
            result["imported_skill_state"] = snapshot
        return result

    def _normalize_skill_trigger_token(self, value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return normalized or None

    def _skill_trigger_text_matches(self, value: str | None, expected: set[str]) -> bool:
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
                "brainstorming",
                project_root=project_root,
                session_id=session_id,
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
                "writing-plans",
                project_root=project_root,
                session_id=session_id,
            )["intent"],
        ):
            return "planning"
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "deep-retrieval",
                project_root=project_root,
                session_id=session_id,
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
        # #130: "activate castle doctrine" / "kingdom law" → the bundled
        # empire-doctrine lawbook. Last specific rung — work-skill intents
        # (debug/plan/design) win over doctrine mentions in work prompts.
        if self._skill_trigger_text_matches(
            request,
            self._configured_skill_trigger_rule(
                "empire-doctrine",
                project_root=project_root,
                session_id=session_id,
            )["intent"],
        ):
            return "castle-doctrine"
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
            str(skill.get("skill_id") or "").split("/")[-1],
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
                self._normalize_skill_trigger_token(str(item)) for item in skill.get("tags", [])
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
                skill_id.rsplit("/", maxsplit=1)[-1],
            )
            override_mode = override.mode
            if override.mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                skill_id = str(override.skill_id or skill_id.rsplit("/", maxsplit=1)[-1])
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
            runtime_provider = str(item.get("runtime_provider") or provider).strip() or provider
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
                },
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
                resolved_skill_id not in active_skills and selected_skill_id not in active_skills
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
                },
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
            intent_token,
            rule.get("intent", set()),
        ):
            reasons.append(f"intent:{intent_token}")
            rank += 20
        if workflow_token and self._skill_trigger_text_matches(
            workflow_token,
            rule.get("workflow", set()),
        ):
            reasons.append(f"workflow:{workflow_token}")
            rank += 10
        if selected_rank == 0 and str(trigger_skill.get("source") or "") == "external_provider":
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
            project_root,
            session_id,
            intent=intent,
            workflow_state=workflow_state,
        )
        if payload.get("triggered"):
            logger.info(
                "Skill trigger state resolved for session %s: %s",
                session_id,
                payload.get("triggered"),
            )
        self._persist_host_skill_state(
            project_root,
            session_id,
            intent=intent,
            workflow_state=workflow_state,
        )
        payload["skills_overview"] = self._build_skills_overview(
            session_id=session_id,
            selected_skills={"selected_skills": list(payload.get("selected_skills", []))},
            active_skills=list(payload.get("active_skills", [])),
            imported_skill_state=payload.get("imported_skill_state")
            if isinstance(payload.get("imported_skill_state"), dict)
            else None,
            skill_trigger_state=payload,
        )
        return payload

    def ensure_claude_mcp_config(
        self, project_root: Path, interpreter: str | None = None
    ) -> dict[str, object]:
        return self._project_support.ensure_claude_mcp_config(project_root, interpreter)

    def project_origins(self, project_root: Path) -> dict[str, object]:
        return self._project_support.project_origins(project_root)

    def _load_project_rules(self, project_root: Path) -> dict[str, str]:
        """Bootstrap-law rules, served from the MemPalace bridge (canonical
        palace drawers) — NOT by reading .MEMORY/rules/*.md.

        get_bootstrap_context excludes retired/superseded drawers, so a
        lingering or edited markdown file cannot inject, override, or
        resurrect a rule; only content imported into the palace (via the
        memory_index -> palace ingest) is authority. Degrades closed
        (empty dict) if the palace is unavailable — never falls back to
        files. Returns {title: content} for the bootstrap renderer
        (title == the rule's room/stem, preserving the prior shape).
        """
        palace = getattr(self.hub, "palace", None)
        if palace is None:
            return {}
        try:
            from .palace_hub_extension import build_palace_context

            ctx = build_palace_context(
                self.hub,
                self,
                tool_name="bootstrap.load_rules",
            )
            bundle = palace.get_bootstrap_context(
                project_root=project_root,
                categories=("rules",),
                hub_ctx=ctx,
            )
        except Exception:
            logger.exception("rules bridge fetch failed")
            return {}
        records = getattr(bundle, "records", None)
        if not records:  # Refused (no .records) or empty bundle
            return {}
        return {rec.title: rec.content for rec in records if rec.content}

    def repo_summary(
        self,
        project_root: Path,
        *,
        freshness_mode: str = "deep",
        session_count: int | None = None,
    ) -> dict[str, object]:
        return self._project_support.repo_summary(
            project_root,
            freshness_mode=freshness_mode,
            session_count=session_count,
        )

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

    def _index_freshness_status(self, project_root: Path) -> tuple[str, dict[str, object]]:
        return self._project_support._index_freshness_status(project_root)

    def session_start_state(
        self,
        project_root: Path,
        session_id: str | None = None,
        *,
        verify_index: bool = True,
    ) -> dict[str, object]:
        # Delegates to session_start with hydrate=False (startup path).
        # For explicit hydration, use session_connect/conductor_enter.
        # verify_index=False (UPS only) skips the exact SHA freshness walks and
        # reports an honest "unverified" status instead — see the service impl.
        return self._session_state.session_start_state(
            project_root, session_id, verify_index=verify_index,
        )

    def host_state(
        self,
        project_root: Path,
        session_id: str | None = None,
        prompt_text: str | None = None,
        *,
        verify_index: bool = True,
        host_session_id: str = "",
    ) -> dict[str, object]:
        return self._session_state.host_state(
            project_root,
            session_id,
            prompt_text,
            verify_index=verify_index,
            host_session_id=host_session_id,
        )

    def project_init(
        self,
        project_root: Path,
        init_git: bool = True,
        create_remote: bool = False,
        interpreter: str | None = None,
    ) -> dict[str, object]:
        return self._bootstrap.project_init(
            project_root, init_git, create_remote, interpreter
        )

    def _environment_preflight(self) -> dict[str, object]:
        """#769 guard: verify the venv running aidocs_mcp satisfies its own
        declared dependency floors + pip's graph is consistent. Fail-open —
        a preflight must never block the session it protects. Process-memoized
        (pip check runs at most once per process)."""
        from . import env_floor_audit

        try:
            return env_floor_audit.cached_environment()
        except Exception:  # noqa: BLE001
            return {"ok": True, "below_floor": [], "missing": [], "pip_check": {"ok": True}, "summary": ""}

    def session_start(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
        hydrate: bool = False,
    ) -> dict[str, object]:
        # Session remains unhydrated on startup path unless hydrate=True.
        # MUST call session_connect/conductor_enter explicitly to hydrate.
        # This prevents ~100k token loads on trivial prompts.
        # Rule: startup/bind != hydrate
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)

        startup_files = [
            "/.MEMORY/.aidocs/index.aidocs",
            "/.MEMORY/.aidocs/global-instructions.aidocs",
            "/.MEMORY/.aidocs/coding-standards.aidocs",
            "/.MEMORY/.aidocs/memory-system.aidocs",
            # SQLite-only doctrine (2026-06): /.MEMORY/INDEX.md retired — durable
            # memory is canonical in the sqlite memory_index.
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
                    "environment": self._environment_preflight(),
                }
                response["report"] = self._build_session_start_report(response)
                return response

        # Only hydrate session data when hydrate=True (explicit session_connect/conductor_enter)
        # Rule: startup/bind != hydrate
        if hydrate:
            session = self.hub.sessions.read_session(project_root, session_id)
            context = self.hub.sessions.read_context(project_root, session_id)
            handoff = self.hub.sessions.read_handoff(project_root, session_id)
            handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
            selected_skills = self.hub.skills.get_selected_skills(project_root, session_id)
            imported_skill_state = self._persist_host_skill_state(
                project_root,
                session_id,
                intent="startup",
                workflow_state="session_start",
            )
            skill_trigger_state = self.skill_trigger_state(
                project_root,
                session_id,
                intent="startup",
                workflow_state="session_start",
            )
            compliance = self.session_compliance_summary(project_root, session_id)
        else:
            # Startup path: only load session metadata (id, title, status)
            session = self.hub.sessions.read_session(project_root, session_id)
            context = dict(_EMPTY_STARTUP_CONTEXT)
            handoff = dict(_EMPTY_STARTUP_HANDOFF)
            handoff_steps = []
            selected_skills = []
            imported_skill_state = {}
            skill_trigger_state = {}
            compliance = {}
            # Explicit hydration required: call session_connect/conductor_enter

        context_path = str(context.path) if hasattr(context, "path") and context.path else None
        context_sections = (
            context.sections
            if hasattr(context, "sections") and isinstance(context.sections, dict)
            else (
                context.get("sections", {})
                if isinstance(context, dict) and isinstance(context.get("sections"), dict)
                else {}
            )
        )
        handoff_path = str(handoff.path) if hasattr(handoff, "path") and handoff.path else None
        handoff_sections = (
            handoff.sections
            if hasattr(handoff, "sections") and isinstance(handoff.sections, dict)
            else (
                handoff.get("sections", {})
                if isinstance(handoff, dict) and isinstance(handoff.get("sections"), dict)
                else {}
            )
        )

        if sync_indexes:
            self.hub.code.sync_session_code(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
            )

        response: dict[str, object] = {
            # Always include minimal session metadata
            "startup_files": startup_files,
            "origins": self.project_origins(project_root),
            "repo_summary": self.repo_summary(project_root),
            "requires_session_selection": False,
            "selected_session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            # Hydrated content only when hydrate=True
            "context": {"path": context_path, "sections": context_sections},
            "handoff": {"path": handoff_path, "sections": handoff_sections},
            "handoff_steps": handoff_steps,
            "selected_skills": selected_skills,
            "imported_skill_state": imported_skill_state,
            "active_imported_skills": list(imported_skill_state.get("active_skills", [])),
            "runtime_owned_capabilities": [
                item
                for item in (skill_trigger_state.get("runtime_owned_capabilities") or [])
                if isinstance(item, dict)
            ],
            "skill_trigger_state": skill_trigger_state,
            "active_skills": list(skill_trigger_state.get("active_skills", [])),
            "compliance": compliance,
            "sessions": session_summaries,
            "environment": self._environment_preflight(),
            # Indicate hydration state to client
            "hydrated": hydrate,
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
            context_sections=context_sections,
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
            response["ai_bundle"] = self.hub.code.get_context_bundle(
                project_root,
                session_id=session_id,
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


    # Public dashboard-panel readers — the control-plane ``dashboard_view`` op
    # (outer_gate_transport) serves the Settings / Bash-policy / RBAC panels by
    # calling these. They delegate to the SAME RuntimePresentationService
    # readers that dashboard_snapshot uses for its ``config`` section, so a
    # per-panel view is byte-identical to the corresponding snapshot slice.
    def dashboard_config_entries(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        return self._presentation.dashboard_config_entries(project_root, session_id)

    def dashboard_bash_policy(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> dict[str, object]:
        return self._presentation.dashboard_bash_policy(project_root, session_id)

    def dashboard_rbac(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        return self._presentation.dashboard_rbac(project_root)


    def dashboard_snapshot(
        self,
        project_root: Path,
        session_id: str | None = None,
        event_limit: int = 200,
        *,
        with_timings: bool = False,
        live_only: bool = False,
    ) -> dict[str, object]:
        return self._presentation.dashboard_snapshot(
            project_root,
            session_id=session_id,
            event_limit=event_limit,
            with_timings=with_timings,
            live_only=live_only,
        )

    def session_compliance_summary(self, project_root: Path, session_id: str) -> dict[str, object]:
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
        components = getattr(getattr(server, "_local_provider", None), "_components", {})
        return [component for key, component in components.items() if str(key).startswith("tool:")]

    def _sync_bootstrap_indexes(self, project_root: Path, include_tests: bool) -> dict[str, object]:
        workflow = self.hub.workflow.compile_project_rules(project_root)
        capabilities = self.hub.capabilities.sync_capabilities(
            project_root,
            self._registered_tools_snapshot(),
        )
        procedures = self.hub.procedures.sync_procedures(
            project_root,
            self.hub.workflow.read_compiled(project_root),
        )
        links = self.hub.procedure_links.sync_links(
            project_root,
            self.hub.procedures.find_procedures(project_root, query=None, limit=1000),
            self.hub.capabilities.find_capabilities(project_root, query=None, limit=1000),
        )
        memory_sync = self.hub.index.sync_all(project_root)
        # Phase 3 memory-home flip: project every eligible memory body into
        # palace drawers through the stamped, read-back-verified migrator.
        # The stable report keeps ingested/skipped/failed counters while the
        # attached migration receipt proves each body_home transition. Any
        # failure remains staged in sqlite and is retried on resume/capture.
        palace_ingest = {"ingested": 0, "skipped": 0, "failed": 0}
        palace = getattr(self.hub, "palace", None)
        if palace is not None:
            try:
                from .memory_home_migrator import migrate_bodies_with_ingest_summary
                from .palace_hub_extension import build_palace_context

                ctx = build_palace_context(
                    self.hub,
                    self,
                    tool_name="bootstrap.palace_ingest",
                )
                palace_ingest = migrate_bodies_with_ingest_summary(
                    project_root,
                    palace,
                    hub_ctx=ctx,
                )
            except Exception:
                logger.exception("bootstrap palace ingest failed")
        return {
            "memory": memory_sync,
            "palace_ingest": palace_ingest,
            "code_manifest": {
                "code_files": self.hub.code.sync_code_files(
                    project_root,
                    include_tests=include_tests,
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
        *,
        host_session_id: str = "",
    ) -> dict[str, object]:
        # Bug #234-1: resolve by THIS host session's per-conductor binding, not
        # the global singleton (which returns the session ANY agent last set).
        # #1027 authority door: `active` alone can name a session that no
        # longer exists, and EVERYTHING below scopes on it (preflight,
        # skill-trigger state, the recommended flow).
        _managed_sid, _managed_reason = explain_managed_session(
            self.hub.managed_mode, project_root, host_session_id=host_session_id
        )
        # A binding that names no usable session is still MANAGED here. This
        # site's original shape returned the rich payload with a null
        # session_id rather than the unmanaged one, and collapsing that into
        # "unmanaged" would hand an unbound caller the free-inspection reply.
        _binding_without_session = not _managed_sid and (
            _managed_reason.startswith("stale_bind:")
            or _managed_reason == "managed_binding_names_no_session"
        )
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if not (_managed_sid or _binding_without_session):
            return {
                "managed_mode": False,
                "action_kind": action_kind,
                "allowed_direct_inspection": bool(explicit_targets),
                "requires_session": False,
                "requires_task_lifecycle": False,
                "recommended_mcp_flow": ["/aidocs"],
                "blocked_reason": None,
            }

        session_id = _managed_sid or None
        skill_trigger_state = None
        if session_id:
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
        active_managed_session = bool(session_id)
        recommended = ["runtime_preflight"]
        # 2026-05-12 collapse: plan_conductor_status → ai_plan_status;
        # session_list/session_connect → ai_session (mode-dispatched);
        # task_begin → ai_task (mode-dispatched).
        if active_managed_session:
            recommended.append("ai_plan_status")
        elif preflight.get("requires_session"):
            recommended.append("ai_session")
        if requires_task_lifecycle:
            recommended.append("ai_task")
        if action_kind in {"understand", "trace", "edit", "ai_bundle"}:
            recommended.append("orchestrate")

        blocked_reason = None
        if preflight.get("allowed") is False:
            blocked_reason = str(preflight.get("reason"))

        return {
            "managed_mode": True,
            "action_kind": action_kind,
            "session_id": session_id,
            "skill_trigger_state": skill_trigger_state,
            "active_skills": list((skill_trigger_state or {}).get("active_skills", [])),
            "runtime_owned_capabilities": [
                item
                for item in ((skill_trigger_state or {}).get("runtime_owned_capabilities") or [])
                if isinstance(item, dict)
            ],
            "imported_skill_state": (skill_trigger_state or {}).get("imported_skill_state"),
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
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if explicit_targets:
            if any(
                token in text
                for token in ("error", "stack trace", "traceback", "log", "logs", "why")
            ):
                action_kind = "read_error"
            else:
                action_kind = "inspect"
            result: dict[str, object] = {
                "action_kind": action_kind,
                "why": ["explicit_targets"],
            }
            self._enrich_domain_hints(result, text, project_root, session_id)
            self._enrich_tool_discovery(result, text, project_root)
            return result

        mapping = self._get_intent_tokens(project_root=project_root, session_id=session_id)
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
        result: dict[str, Any],
        text: str,
        project_root: Path | None = None,
    ) -> None:
        """Match __tool_discovery_* keywords and add recommended_tools.

        Reads from the empire intent-tokens store (kind='tool_discovery').
        Each row: parent_key=<category>, token=<alias>, attrs={tools: [...]}.
        Surfaces tools whose category alias appears in `text`.
        """
        try:
            from . import intent_tokens_store as _its

            rows = _its.get_rows_by_kind("en", "tool_discovery", include_attrs=True)
        except Exception:
            return
        text_lower = text.lower()
        discovered: list[str] = []
        matched_categories: list[str] = []
        cats_seen: set[str] = set()
        cat_tools: dict[str, list[str]] = {}
        for r in rows:
            cat = r.get("parent_key") or ""
            token = (r.get("token") or "").lower()
            if not cat or not token:
                continue
            if cat not in cat_tools:
                tools = r.get("attrs", {}).get("tools") or []
                cat_tools[cat] = [str(t).strip() for t in tools if str(t).strip()]
            if token in text_lower and cat not in cats_seen:
                cats_seen.add(cat)
                matched_categories.append(cat)
                for tool_str in cat_tools[cat]:
                    if tool_str and tool_str not in discovered:
                        discovered.append(tool_str)
        if discovered:
            result["recommended_tools"] = discovered[:15]
            result["tool_discovery_categories"] = matched_categories

    def _get_domain_hint_defs(
        self,
        project_root: Path | None = None,
    ) -> dict[str, dict[str, object]]:
        """Load __domain_hint_* definitions from TOML action token files."""
        cache_key = str(project_root.resolve()) if project_root else "_global"
        cached = getattr(self, "_domain_hint_cache", {})
        if cache_key in cached:
            return cached[cache_key]
        try:
            from . import intent_tokens_store as _its

            rows = _its.get_rows_by_kind("en", "domain_hint", include_attrs=True)
        except Exception:
            rows = []
        defs: dict = {}
        for r in rows:
            domain = r.get("parent_key") or ""
            if not domain:
                continue
            entry = defs.setdefault(domain, {"tokens": [], "tools": ""})
            token = r.get("token") or ""
            if token and token not in entry["tokens"]:
                entry["tokens"].append(token)
            tools_attr = r.get("attrs", {}).get("tools", "")
            if tools_attr and not entry.get("tools"):
                entry["tools"] = tools_attr
        if not hasattr(self, "_domain_hint_cache"):
            self._domain_hint_cache = {}
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
        host_session_id: str = "",
    ) -> dict[str, object]:
        return self._prompt_handling.aidocs_handle_prompt(
            project_root,
            user_request,
            action_kind,
            explicit_targets=explicit_targets,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            host_session_id=host_session_id,
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
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> None:
        self._conductor_state._require_plan_conductor_lane_id(project_root, session_id, lane_id)

    def _read_plan_conductor_state(self, project_root: Path, session_id: str) -> dict[str, object]:
        return self._conductor_state._read_plan_conductor_state(project_root, session_id)

    def _write_plan_conductor_state(
        self,
        project_root: Path,
        session_id: str,
        state: dict[str, object],
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

    def lane_update_state(
        self,
        project_root: Path,
        worker_id: str,
        state: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Transition a lane worker's state; emit registry + event telemetry.

        Composes SessionLaneAgentsStore.update_worker_state with
        ExecutionIndexStore.record_run / record_event so dashboards see
        the transition both in the session_lane_agents row and in the
        execution_events / execution_runs log.
        """
        from .execution_index_store import ExecutionIndexStore
        from .session_lane_agents_store import SessionLaneAgentsStore

        if not worker_id:
            return {"ok": False, "error": "missing_worker_id"}

        store = SessionLaneAgentsStore()
        store.init_db(project_root)
        with store.connect(project_root) as conn:
            row = conn.execute(
                "SELECT session_id, lane_id FROM session_lane_agents WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
        if row is None:
            return {
                "ok": False,
                "error": "unknown_worker_id",
                "worker_id": worker_id,
            }
        session_id = row[0]
        lane_id = row[1]

        ok = store.update_worker_state(project_root, worker_id, state, metadata=metadata)
        if not ok:
            return {
                "ok": False,
                "error": "update_failed",
                "worker_id": worker_id,
            }

        exec_store = ExecutionIndexStore()
        payload: dict[str, object] = {
            "worker_id": worker_id,
            "lane_id": lane_id,
            "state": state,
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        terminal = state in {"done", "failed", "crashed"}
        run_status = "completed" if terminal else "started"
        completed_at = exec_store._timestamp() if terminal else None
        run_id = exec_store.record_run(
            project_root,
            run_kind="lane_worker",
            source_kind="lane_agent",
            session_id=session_id,
            target_entity=lane_id,
            status=run_status,
            metadata=payload,
            completed_at=completed_at,
        )
        event_id = exec_store.record_event(
            project_root,
            event_kind="lane_worker_state",
            source_kind="lane_agent",
            session_id=session_id,
            action_kind=state,
            target_entity=lane_id,
            status=state,
            payload=payload,
            run_id=run_id,
        )
        return {
            "ok": True,
            "worker_id": worker_id,
            "lane_id": lane_id,
            "session_id": session_id,
            "state": state,
            "run_id": run_id,
            "event_id": event_id,
        }

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

    def _find_plan_lane(self, project_root: Path, session_id: str, lane_id: str) -> object | None:
        return self._conductor_dispatch._find_plan_lane(project_root, session_id, lane_id)

    def _lane_open_steps(self, lane: object) -> list[str]:
        return self._conductor_dispatch._lane_open_steps(lane)

    def _build_subagent_task_packet(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        return self._conductor_dispatch._build_subagent_task_packet(
            project_root,
            session_id,
            lane_id,
        )

    def plan_dispatch_next(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str | None = None,
    ) -> dict[str, object]:
        return self._conductor_dispatch.plan_dispatch_next(
            project_root,
            session_id,
            lane_id=lane_id,
        )

    def _plan_lanes_complete(self, project_root: Path, session_id: str) -> bool:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        reopened_lane_ids = set(
            self._read_plan_conductor_state(project_root, session_id).get("reopened_lane_ids", []),
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
                ),
            )
            if session_id:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.bound_session",
                        project_root=project_root,
                        session_id=session_id,
                        bound_session_id=session_id,
                    ),
                )
                prompt_lines.append(
                    self._interaction_text(
                        "managed.stay_in_session",
                        project_root=project_root,
                        session_id=session_id,
                    ),
                )
            if prompt_action_kind:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.active_short",
                        project_root=project_root,
                        session_id=session_id,
                        action_kind=prompt_action_kind,
                    ),
                )
            prompt_lines.append(
                self._interaction_text(
                    "managed.use_mcp_first",
                    project_root=project_root,
                    session_id=session_id,
                ),
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
                    ),
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
                    ),
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
                    ),
                )
            if helper_skill_guidance:
                # Once-per-epoch dedup (mirrors DNT banner contract).
                # Empty list = all skills already shown this epoch;
                # suppress the header line too so we don't dangle a
                # "guidance:" label with no content.
                from .helper_skill_injector import maybe_helper_skill_blocks

                skill_blocks = maybe_helper_skill_blocks(
                    project_root,
                    helper_skill_guidance,
                )
                if skill_blocks:
                    prompt_lines.append(
                        self._interaction_text(
                            "managed.helper_guidance_header",
                            project_root=project_root,
                            session_id=session_id,
                        ),
                    )
                    prompt_lines.extend(skill_blocks)
            if workflow_summary:
                prompt_lines.append(
                    self._interaction_text(
                        "managed.workflow_actions",
                        project_root=project_root,
                        session_id=session_id,
                        workflow_summary=workflow_summary,
                    ),
                )
            if lifecycle_state:
                last_lifecycle_tool = lifecycle_state.get("last_lifecycle_tool")
                has_open_task = last_lifecycle_tool in {"task_begin", "task_update"}
                if lifecycle_state.get("needs_task_complete"):
                    key = (
                        "lifecycle.task_complete"
                        if has_open_task
                        else "lifecycle.task_complete_no_open_task"
                    )
                    prompt_lines.append(
                        self._interaction_text(
                            key,
                            project_root=project_root,
                            session_id=session_id,
                        ),
                    )
                elif lifecycle_state.get("needs_task_update"):
                    key = (
                        "lifecycle.task_update"
                        if has_open_task
                        else "lifecycle.task_update_no_open_task"
                    )
                    prompt_lines.append(
                        self._interaction_text(
                            key,
                            project_root=project_root,
                            session_id=session_id,
                        ),
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

    # AIDOCS shell provider lock — Invariant #38 Path A Step 4
    # (canonical 2026-04-29). "bash" is REMOVED from the lane-grantable
    # raw tool set: native shell tools are not delegable authority in
    # managed AIDOCS sessions. Lanes use ai_run for shell execution;
    # provider selection (Git Bash / native bash) is internal plumbing
    # resolved per-spawn by shell_resolver. The matching managed-mode
    # hard-stop in grant_raw_tools_for_lane below provides
    # belt-and-suspenders against any future caller that bypasses the
    # whitelist.
    _GRANTABLE_RAW_TOOLS: frozenset[str] = frozenset(
        {
            "read",
            "grep",
            "glob",
            "edit",
            "write",
            "patch",
            "apply_patch",
            "multiedit",
        },
    )

    # grant_raw_tools_for_turn removed 2026-04-19: self-grant primitive
    # obsoleted by NLP grant parsing in UserPromptSubmit. The heuristic
    # detector in claude_hook._grant_user_intent_tools is the single
    # grant path. Widen its vocabulary rather than reintroducing a
    # bypass tool.

    def grant_raw_tools_for_lane(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        tools: list[str],
        reason: str,
    ) -> dict[str, object]:
        """Conductor-only: delegate raw-tool access to a specific lane.

        Writes {lane_id: [tool, ...]} into the session's query gate under
        `lane_raw_tools_granted`. The raw-tool gate consults this when
        `current_lane_id` matches, allowing agents running under that
        lane to use the granted raw tools. Destructive-command guards
        (heuristic judge, bash denylist) still apply — the grant only
        lifts the raw-file-tool block, not the safety guards.

        Agents cannot call this tool: `lane_grant_raw_tools` is in
        `AccessGate._CONDUCTOR_ONLY_TOOLS`. See
        `tests/security/test_lane_raw_tool_grant.py`.
        """
        cleaned_reason = (reason or "").strip()
        if not cleaned_reason:
            return {
                "ok": False,
                "error": "reason required — lane raw-tool grants must be justified for audit.",
                "granted": [],
                "rejected": [],
            }

        lane_key = (lane_id or "").strip()
        if not lane_key:
            return {
                "ok": False,
                "error": "lane_id required.",
                "granted": [],
                "rejected": [],
            }

        normalized_tools: list[str] = []
        rejected: list[str] = []
        for item in tools or []:
            tool = str(item or "").strip().lower()
            if not tool:
                continue
            if tool in self._GRANTABLE_RAW_TOOLS:
                if tool not in normalized_tools:
                    normalized_tools.append(tool)
            else:
                rejected.append(tool)

        if rejected:
            return {
                "ok": False,
                "error": (
                    "Unknown raw tools: "
                    + ", ".join(rejected)
                    + ". Grantable tools: "
                    + ", ".join(sorted(self._GRANTABLE_RAW_TOOLS))
                ),
                "granted": [],
                "rejected": rejected,
            }

        # AIDOCS shell provider lock — Invariant #38 Path A Step 4
        # (canonical 2026-04-29). Belt-and-suspenders managed-mode
        # hard-stop. _GRANTABLE_RAW_TOOLS no longer contains "bash"
        # (Layer 1 above), so the rejected-list path already catches
        # bash today. This second layer protects against future
        # callers that bypass the whitelist (e.g. a future _GRANTABLE
        # set extended with non-shell tools that happen to share a
        # name, or a direct write to lane_raw_tools_granted). In
        # managed AIDOCS sessions, native shell tools are NEVER
        # delegable authority — lanes use ai_run for shell execution;
        # provider selection is internal plumbing.
        try:
            from .access_gate import _RAW_SHELL_TOOLS as _SHELL_FORBIDDEN
        except Exception:
            _SHELL_FORBIDDEN = frozenset()
        try:
            _sid, _reason = explain_managed_session(self.hub.managed_mode, project_root)
            # #1027: a binding that names no usable session must NOT unlock
            # native shell delegation. The deny side of the door is still
            # "managed" for this gate -- collapsing it into unmanaged would
            # turn a T0 refusal into a grant.
            in_managed_mode = bool(
                _sid
                or _reason.startswith("stale_bind:")
                or _reason == "managed_binding_names_no_session",
            )
        except Exception:
            in_managed_mode = False
        if in_managed_mode:
            shell_refused = [t for t in normalized_tools if t in _SHELL_FORBIDDEN]
            if shell_refused:
                return {
                    "ok": False,
                    "error": (
                        "Native shell tools cannot be delegated to "
                        "lanes in managed AIDOCS sessions: "
                        + ", ".join(shell_refused)
                        + ". Lanes use ai_run for shell execution; "
                        "provider selection is internal plumbing, "
                        "not delegable authority. (Invariant #38: "
                        "native shell tools are T0-blocked in "
                        "managed AIDOCS sessions; no flag, grant, "
                        "or lane delegation lifts this.)"
                    ),
                    "granted": [],
                    "rejected": shell_refused,
                }

        # Invariant: lane delegation can only pass through tools the
        # current turn's user intent already granted the conductor.
        # Without this, lane_grant_raw_tools becomes a self-grant
        # laundering path (conductor grants Lane A bash, spawns Lane A,
        # runs bash from there) that bypasses NLP-parsed user intent.
        current_intent = set(
            self.hub.query_gate.get_user_intent_tools(project_root, session_id) or [],
        )
        missing_intent = [t for t in normalized_tools if t not in current_intent]
        if missing_intent:
            return {
                "ok": False,
                "error": (
                    "Cannot delegate tools the current turn's user intent did "
                    "not grant: " + ", ".join(missing_intent) + ". Ask the user "
                    "to authorize these tools in their current prompt, then "
                    "retry delegation."
                ),
                "granted": [],
                "rejected": missing_intent,
            }
        if not normalized_tools:
            return {
                "ok": False,
                "error": "no valid tools specified.",
                "granted": [],
                "rejected": [],
            }

        # Merge into existing grants so multiple calls accumulate.
        current = self.hub.query_gate.get(project_root, session_id)
        grants: dict[str, list[str]] = dict(current.get("lane_raw_tools_granted") or {})
        existing = list(grants.get(lane_key) or [])
        for tool in normalized_tools:
            if tool not in existing:
                existing.append(tool)
        grants[lane_key] = existing
        self.hub.query_gate.set(
            project_root,
            session_id,
            lane_raw_tools_granted=grants,
        )
        # Audit trail: execution_events already records the grant through
        # the orchestrator/hook path; the duplicate journal write was
        # deleted 2026-04-20 as part of the audit-surface deslop.
        return {
            "ok": True,
            "session_id": session_id,
            "lane_id": lane_key,
            "granted": normalized_tools,
            "scope": "lane",
            "reason": cleaned_reason,
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
        # TDD mode is the only regime where verification_gate blocks.
        # Off by default — we can't force tests onto a project that
        # doesn't have them. When on (dashboard toggle), the gate
        # derives required test commands from sibling test files for
        # the edited sources.
        try:
            from .config import get_setting as _get_setting_tdd

            tdd_mode = bool(
                _get_setting_tdd(
                    "workflow.tdd_mode",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            tdd_mode = False
        # #593: ONE normalization point for the evidence argument, hoisted
        # above the tdd_mode branch. The tdd-off branch used to call
        # `(verification_evidence or {}).get(...)` directly, which raises
        # AttributeError on any non-dict a caller hands the method, while
        # the enforcing branch below already guarded with isinstance — the
        # two branches disagreed about what a malformed argument means.
        evidence = verification_evidence if isinstance(verification_evidence, dict) else {}
        if not tdd_mode:
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": True,
                "status": "verified_tdd_off",
                "reason": (
                    "workflow.tdd_mode=false — verification gate is "
                    "advisory only. Enable it in the dashboard to "
                    "enforce that sibling tests run before task_complete."
                ),
                "required_commands": [],
                "commands_run": [
                    str(item).strip()
                    for item in (evidence.get("commands_run") or [])
                    if str(item).strip()
                ],
                "command_results": [],
            }
        context = self.hub.sessions.read_context(project_root, session_id)
        context_sections = context.sections if isinstance(context.sections, dict) else {}
        # #772: a required "command" must be a COMMAND, not an expected OUTPUT.
        # `Relevant Commands` is SESSION-WIDE, so a line written during an earlier
        # task is inherited verbatim by every later one — and on 2026-08-16 that
        # line was a prior task's MEASUREMENT ("... 512 passed, 1 skipped ..."),
        # pass counts included. Because satisfaction is substring matching against
        # agent-typed strings, the only way to clear it was to emit that literal.
        # An agent that honestly re-ran and measured 510 could not close; one that
        # pasted the expected string without running anything could. The incentive
        # pointed at fabrication.
        #
        # Demoting these is NOT a loosening: such a requirement is unsatisfiable
        # except by fabrication, so it supplies no verification to lose. Every
        # command-shaped requirement still hard-blocks, the audit cross-check still
        # credits only exit_code==0 events, and Audit hardening D still demands a
        # real test run for code-touching tasks. Demoted lines are REPORTED rather
        # than dropped — an unenforced line the operator cannot see is how this
        # survived unnoticed.
        _declared_relevant = self._clean_bullets(context_sections.get("Relevant Commands", []))
        required_commands = [
            c for c in _declared_relevant if not self._is_unsatisfiable_requirement(c)
        ]
        advisory_notes = [
            c for c in _declared_relevant if self._is_unsatisfiable_requirement(c)
        ]
        # Auto-derive required test commands from the task's actually-
        # touched files (via edit_history). Augments the session-wide
        # `Relevant Commands` list with scope-aware suites so a task
        # that edits an outline extractor is held to tests/indexing/
        # without the operator having to pin it in context.md. Derived
        # commands are deduped against anything the operator already
        # pinned (substring match).
        try:
            derived = self._derive_required_commands_for_touched_files(project_root, session_id)
        except Exception:
            derived = []
        for cmd in derived:
            if not any(cmd in existing or existing in cmd for existing in required_commands):
                required_commands.append(cmd)
        commands_run = [
            str(item).strip() for item in (evidence.get("commands_run") or []) if str(item).strip()
        ]
        command_results = [
            str(item).strip()
            for item in (evidence.get("command_results") or [])
            if str(item).strip()
        ]
        # Auto-record from execution_events: every ai_run invocation
        # stamps `command` into the tool_call_completed
        # event's payload. Harvesting from there means agents don't
        # have to re-report what the audit chain already captured.
        # (Diagnosed 2026-04-20: gate did literal substring match on
        # agent-reported commands_run, false-negatived when one shell
        # invocation covered multiple required commands.)
        #
        # Tightened 2026-04-20: only count events from
        # tool_call_completed (started events have no exit info) AND
        # exit_code == 0. A failing test or build no longer satisfies
        # the gate even if the command ran. Agent-reported commands
        # still flow through via evidence.commands_run but are now
        # cross-checked against the audit stream — a command appearing
        # only in agent evidence with no matching passing-exit event
        # counts as unverified.
        try:
            recent_events = self.hub.execution.list_events(
                project_root,
                session_id=session_id,
                limit=200,
            )
        except Exception:
            recent_events = []
        audited_commands: list[str] = []
        for ev in recent_events:
            if not isinstance(ev, dict):
                continue
            if ev.get("event_kind") != "tool_call_completed":
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            cmd = payload.get("command") or ""
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            exit_code = payload.get("exit_code")
            # Accept None (legacy events without exit_code capture) as
            # trust-the-agent until post-fix events accumulate. Strict-
            # mode (exit_code required) can flip on via a future config
            # knob once the project has recorded ≥N events with the new
            # shape.
            if exit_code is not None and exit_code != 0:
                continue
            audited_commands.append(cmd.strip())
        # Merge — agent-reported wins if both present (agent may have
        # run something outside the MCP tool layer).
        for ac in audited_commands:
            if ac not in commands_run:
                commands_run.append(ac)
        full_suite_failed = bool(evidence.get("full_suite_failed"))
        failure_evidence = (
            evidence.get("failure_evidence")
            if isinstance(evidence.get("failure_evidence"), dict)
            else {}
        )

        if full_suite_failed:
            attributed_lanes = self._attribute_failure_to_lane(
                project_root,
                session_id,
                failure_evidence,
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
                "reason": self._missing_evidence_reason(
                    required_commands,
                    verification_evidence,
                ),
                "required_commands": required_commands,
                "commands_run": commands_run,
                "command_results": command_results,
                # #593: the refusal must describe what ACTUALLY ARRIVED, not
                # only what was expected. Without this a caller cannot tell
                # "you sent nothing" from "you sent the wrong key", which is
                # how one agent retried the identical malformed call four
                # times and concluded the transport had eaten its argument.
                "received_evidence": self._received_evidence_report(verification_evidence),
            }

        # "Covers" matcher: a required command is satisfied if any
        # actually-run command line contains it as a substring. Lets
        # `pytest tests/a.py tests/b.py` cover both
        # `pytest tests/a.py` and `pytest tests/b.py` — which is what
        # an agent does when batching tests.
        def _covers(req: str, runs: list[str]) -> bool:
            req_norm = req.strip()
            if not req_norm:
                return True
            return any(req_norm in r or r in req_norm for r in runs)

        missing_required = [cmd for cmd in required_commands if not _covers(cmd, commands_run)]
        if missing_required:
            preview = "; ".join(missing_required[:3])
            if len(missing_required) > 3:
                preview += f"; …(+{len(missing_required) - 3} more)"
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": False,
                "status": "blocked_missing_required_commands",
                "reason": (
                    f"missing required verification commands "
                    f"({len(missing_required)}/{len(required_commands)}): {preview}"
                ),
                "required_commands": required_commands,
                "advisory_notes": advisory_notes,
                "commands_run": commands_run,
                "command_results": command_results,
                "missing_required_commands": missing_required,
            }

        # Audit hardening D (2026-04-19): code-touching tasks must show
        # a pytest / test-runner invocation in commands_run. Trusting
        # "it worked" without a test command was the rainwater problem —
        # an operator can verify-and-commit with zero regression coverage
        # and nobody can tell weeks later. Inspect edit_history for this
        # session; if any .py or .ts/.tsx file was touched, require at
        # least one "pytest" / "npm test" / "vitest" / "jest" invocation.
        code_files_touched = self._session_code_files_touched(project_root, session_id)
        if code_files_touched and not self._evidence_has_test_run(commands_run):
            return {
                "session_id": session_id,
                "lane_id": lane_id,
                "verified": False,
                "status": "blocked_code_touched_without_test_run",
                "reason": (
                    "task touched code files but verification_evidence "
                    "contains no test-runner invocation (pytest / npm test "
                    "/ vitest / jest). Run the relevant test suite and "
                    "pass its command in commands_run."
                ),
                "required_commands": required_commands,
                "commands_run": commands_run,
                "command_results": command_results,
                "code_files_touched": code_files_touched[:20],
            }

        return {
            "session_id": session_id,
            "lane_id": lane_id,
            "verified": True,
            "status": "verified",
            "reason": "fresh verification evidence is present",
            "required_commands": required_commands,
            # #772: `Relevant Commands` lines that state an OUTCOME rather than
            # name a command. Reported, never enforced — an unenforced line the
            # operator cannot see is how the fabrication incentive survived.
            "advisory_notes": advisory_notes,
            "commands_run": commands_run,
            "command_results": command_results,
        }

    # Evidence keys verification_gate actually reads. Anything else in the
    # dict is inert, and #593 says the refusal has to SAY so rather than let
    # the caller guess.
    _EVIDENCE_UNDERSTOOD_KEYS: tuple[str, ...] = (
        "commands_run",
        "command_results",
        "full_suite_failed",
        "failure_evidence",
    )

    @classmethod
    def _received_evidence_report(cls, verification_evidence: object) -> dict[str, object]:
        """Describe the verification_evidence argument AS RECEIVED (#593).

        A refusal that only states the expected shape cannot distinguish
        "the caller sent nothing" from "the caller sent the wrong key" from
        "the caller sent a non-dict". All three read as
        'commands_run is empty', which is what made one agent retry the same
        malformed call four times and blame the transport. This report is
        purely diagnostic — it never relaxes what the gate requires.
        """
        report: dict[str, object] = {"type": type(verification_evidence).__name__}
        if verification_evidence is None:
            report["passed"] = False
            return report
        report["passed"] = True
        if not isinstance(verification_evidence, dict):
            report["is_object"] = False
            return report
        report["is_object"] = True
        keys = [str(k) for k in verification_evidence]
        report["keys"] = keys
        report["understood_keys"] = [k for k in keys if k in cls._EVIDENCE_UNDERSTOOD_KEYS]
        report["ignored_keys"] = [k for k in keys if k not in cls._EVIDENCE_UNDERSTOOD_KEYS]
        raw = verification_evidence.get("commands_run")
        report["commands_run_type"] = type(raw).__name__
        report["commands_run_len"] = len(raw) if isinstance(raw, (list, tuple, str)) else None
        return report

    @classmethod
    def _missing_evidence_reason(
        cls,
        required_commands: list[str],
        verification_evidence: object = None,
    ) -> str:
        """Missing-evidence refusal text that NAMES the expected shape
        (#474 legible refusals): the agent should never have to guess which
        keys the evidence dict wants.

        #593: it now also names what ARRIVED. Stating only the expectation
        made an empty argument and a misspelled key indistinguishable.
        """
        shape = (
            "verification_evidence={'commands_run': ['<command you ran>', ...], "
            "'command_results': ['<short outcome per command>', ...]}"
        )
        reason = (
            "evidence.commands_run is empty — no verification commands "
            f"recorded for this task. Expected shape: {shape}."
        )
        received = cls._received_evidence_report(verification_evidence)
        if not received.get("passed"):
            reason += " RECEIVED: verification_evidence was not passed at all."
        elif not received.get("is_object"):
            reason += (
                f" RECEIVED: verification_evidence arrived as {received['type']}, "
                f"not an object — it was ignored entirely."
            )
        else:
            keys = received.get("keys") or []
            if not keys:
                reason += " RECEIVED: an EMPTY object {} — no keys at all."
            else:
                reason += f" RECEIVED: object with keys {sorted(keys)}"
                ignored = received.get("ignored_keys") or []
                if ignored:
                    reason += (
                        f"; keys this gate does not read (ignored): {sorted(ignored)} — "
                        f"it reads {list(cls._EVIDENCE_UNDERSTOOD_KEYS)}"
                    )
                reason += (
                    f"; commands_run arrived as {received.get('commands_run_type')} "
                    f"with {received.get('commands_run_len')} entries."
                )
        if required_commands:
            preview = "; ".join(required_commands[:3])
            if len(required_commands) > 3:
                preview += f"; …(+{len(required_commands) - 3} more)"
            reason += f" Required commands to cover: {preview}"
        return reason

    # ── Audit hardening helpers (D) ──

    # File suffixes that trigger the "code touched" test-run requirement.
    # Data/config suffixes (.md, .json, .toml, .yaml) intentionally absent —
    # touching those is common for docs/spec work that doesn't need a
    # pytest run to land cleanly.
    _CODE_TOUCHED_SUFFIXES: tuple[str, ...] = (
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".vb",
        ".rb",
        ".php",
    )

    # Any commands_run entry containing one of these tokens counts as a
    # test invocation. Case-insensitive whole-word-ish match — we look
    # at the raw command string the agent ran, not a parsed AST.
    _TEST_RUNNER_TOKENS: tuple[str, ...] = (
        "pytest",
        "unittest",
        "npm test",
        "npm run test",
        "yarn test",
        "pnpm test",
        "pnpm run test",
        "vitest",
        "jest",
        "mocha",
        "cargo test",
        "go test",
        "mvn test",
        "gradle test",
        "dotnet test",
        "rspec",
        "phpunit",
    )

    def _session_code_files_touched(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """Return session-scoped edit_history files whose suffix marks
        them as code. Returns [] on any failure so verification_gate
        stays resilient to edit_history being unavailable.
        """
        try:
            from .edit_history import EditHistoryStore

            summary = EditHistoryStore().files_touched_summary(
                project_root,
                session_id=session_id,
            )
        except Exception:
            return []
        suffixes = self._CODE_TOUCHED_SUFFIXES
        out: list[str] = []
        for item in summary or []:
            path = str(item.get("file") or "").strip()
            if not path:
                continue
            lower = path.lower()
            if any(lower.endswith(s) for s in suffixes):
                out.append(path)
        return out

    def _derive_required_commands_for_touched_files(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """Derive pytest commands from sibling test files for the
        source files this task edited. Project-agnostic — follows
        filesystem conventions, never hardcodes project-specific paths.

        For each touched .py source file `<pkg>/foo/bar.py`, look for
        sibling test files by walking up to the nearest `tests/` or
        `test/` directory at or above `<pkg>/` and probing for
        `test_<name>.py`, `<name>_test.py`, or a test file whose name
        contains `<name>`. Every hit becomes a pytest command rooted
        at the test-dir's parent so it runs regardless of cwd. Returns
        [] when no sibling tests exist — callers fall back to the
        broader "any pytest invocation" audit-D check downstream.
        """
        touched = self._session_code_files_touched(project_root, session_id)
        if not touched:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in touched:
            path_norm = raw.replace("\\", "/")
            if not path_norm.endswith(".py"):
                continue
            if "/tests/" in path_norm or "/test/" in path_norm:
                # Touching a test file? Require running that file itself.
                cmd = f"pytest {path_norm}"
                if cmd not in seen:
                    seen.add(cmd)
                    out.append(cmd)
                continue
            cmds = self._locate_sibling_tests(project_root, path_norm)
            for cmd in cmds:
                if cmd not in seen:
                    seen.add(cmd)
                    out.append(cmd)
        return out

    @staticmethod
    def _locate_sibling_tests(
        project_root: Path,
        touched_rel: str,
    ) -> list[str]:
        """Walk up from the touched file's directory looking for the
        nearest `tests/` or `test/` directory. Inside it, probe for
        test files whose name is `test_<stem>.py`, `<stem>_test.py`,
        or contains `<stem>` as a substring. Return pytest commands
        rooted at the tests-dir's parent (so `cd parent && pytest tests/`
        style invocations work). Project-agnostic — no hardcoded paths.
        """
        from pathlib import PurePosixPath

        touched = PurePosixPath(touched_rel)
        stem = touched.stem
        parts = touched.parts
        if not parts:
            return []
        # Walk from the file's directory upward to project root.
        parent_chain: list[str] = []
        for i in range(len(parts) - 1, -1, -1):
            parent_chain.append("/".join(parts[:i]))
        out: list[str] = []
        for parent in parent_chain:
            for tests_name in ("tests", "test"):
                candidate_rel = f"{parent}/{tests_name}" if parent else tests_name
                tests_dir = project_root / candidate_rel
                if not tests_dir.is_dir():
                    continue
                hits: list[str] = []
                try:
                    for child in tests_dir.rglob("*.py"):
                        name = child.stem
                        if name == f"test_{stem}" or name == f"{stem}_test" or stem in name:
                            rel = child.relative_to(project_root).as_posix()
                            hits.append(rel)
                except Exception:
                    continue
                if hits:
                    # Build a single pytest command covering the hits.
                    # Run from project_root context — absolute-ish rel
                    # paths keep the command self-contained.
                    out.append("pytest " + " ".join(sorted(set(hits))))
                # Stop walking up once we found a tests dir at this
                # level, whether or not it had hits — outer tests/
                # dirs aren't closer ancestors.
                return out
        return out

    def _evidence_has_test_run(self, commands_run: list[str]) -> bool:
        """True iff any commands_run string mentions a recognized
        test runner. Lowercase contains-match — strict tokenization
        would reject legitimate shapes like `python -m pytest ...`.
        """
        if not commands_run:
            return False
        joined = " ".join(str(c).lower() for c in commands_run)
        return any(token in joined for token in self._TEST_RUNNER_TOKENS)

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
            item for item in (packet_result.get("undeclared_files") or []) if isinstance(item, dict)
        ]
        for item in undeclared_files:
            target_lane_id = str(item.get("target_lane_id") or lane_id).strip() or lane_id
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
                project_root,
                session_id,
                lane_id,
                packet_result,
            )
            if not validation.get("valid"):
                issues = validation.get("issues", [])
                has_scope_violation = any(i.get("category") == "scope_violation" for i in issues)
                has_test_failure = any(
                    i.get("category") in ("test_failure", "missing_test_evidence", "no_tests_run")
                    for i in issues
                )
                has_syntax_error = any(i.get("category") == "syntax_error" for i in issues)

                # Build actionable instructions for the agent
                instructions: list[str] = []
                if has_scope_violation:
                    instructions.append(
                        "STOP: You modified files outside your lane scope. "
                        "Revert those changes immediately. Only modify files listed in your lane's allowed_files. "
                        "If you need a file from another lane, use plan_conductor_record_lane_signal with signal_kind='undeclared_file_needed'.",
                    )
                if has_syntax_error:
                    instructions.append(
                        "FIX: Your edits introduced syntax errors. Fix the broken files before reporting done again.",
                    )
                if has_test_failure:
                    instructions.append(
                        "FIX: Your tests are failing. Debug and fix the failures, then re-run tests and report done with passing test evidence.",
                    )
                if any(i.get("category") == "missing_test_evidence" for i in issues):
                    instructions.append(
                        "REQUIRED: conductor.require_agent_tests is enabled. Write tests for your changes, run them, "
                        "and include the results in your dispatch report under 'test_evidence': {'commands_run': [...], 'command_results': [...]}.",
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
        self._require_plan_conductor_lane_id(project_root, session_id, conflicting_lane_id)
        state = self._read_plan_conductor_state(project_root, session_id)
        display = file_path.replace("\\", "/").lower()
        paused_lanes = dict(state["paused_lanes"])
        paused_lanes[paused_lane_id] = f"inflight-file-overlap:{display}:{conflicting_lane_id}"
        paused_lanes[conflicting_lane_id] = f"inflight-file-overlap:{display}:{paused_lane_id}"
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
        # #107: resuming a lane also clears its blocked_on_conflict stamp —
        # the resumed worker re-attempts the tool call; if the peer still
        # holds the file the gate refuses (and re-stamps) again.
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore as _SLA

            _sla = _SLA()
            for _row in _sla.get_lane_agents(
                project_root, session_id, state_filter="blocked_on_conflict"
            ):
                if str(_row.get("lane_id") or "") == lane_id:
                    _sla.update_worker_state(
                        project_root,
                        str(_row.get("worker_id") or ""),
                        "running",
                        metadata={"blocked_on_conflict": None},
                    )
        except Exception:
            pass  # best-effort: the conductor-state resume below still runs
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
            (plan_lane for plan_lane in conductor.plan.lanes if plan_lane.lane_id == lane_id),
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
            str(f).replace("\\", "/").lower() for f in failure_evidence.get("failed_files", [])
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
                    if any(lf.split("/")[-1].replace(".py", "") in test_path for lf in lane_files):
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
            project_root,
            session_id,
            failure_evidence,
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
                project_root,
                session_id,
            )
            result = self._connect_existing_plan(
                project_root,
                session_id,
                plan,
                run_preflight=run_preflight,
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
                "plan_source": "roadmap_summary" if roadmap_steps else "session_open_work",
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
            session_id,
            end_goal=goal_values[0] if goal_values else None,
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
        self,
        session_id: str,
        end_goal: str | None = None,
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "connected": True,
            "plan_source": "none",
            "roadmap_steps": [],
            "open_work": [],
            "plan_overview": self._build_default_plan_overview(
                session_id=session_id,
                end_goal=end_goal,
            ),
            "next_action": "create_plan_or_roadmap",
            "instruction": self._interaction_text(
                "runtime.no_plan_or_roadmap",
                session_id=session_id,
            ),
        }

    def _collect_session_open_work(
        self,
        project_root: Path,
        session_id: str,
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

        for step in self.hub.sessions.read_handoff_steps_optional(project_root, session_id):
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
            for blocker in self._clean_bullets(handoff.sections.get("Risks and Blockers", [])):
                if blocker.lower() != "none":
                    add_item("handoff_blocker", "blocked", blocker)
                    if self._looks_like_pending_user_input(blocker):
                        add_item("pending_feedback", "pending_user_feedback", blocker)
            feedback_sections.extend(
                [
                    handoff.sections.get("What Matters Now", []),
                    handoff.sections.get("Open Questions", []),
                    handoff.sections.get("Suggested Next Steps", []),
                ],
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
            and self._normalize_state_text(str(step.get("text") or "")) in normalized_candidates
        ]
        if len(matches) != 1:
            return None
        return self.mark_roadmap_step_pending_feedback(project_root, str(matches[0]["text"]))

    def _clean_bullet_value(self, lines: list[str]) -> str:
        cleaned = self._clean_bullets(lines)
        return cleaned[0] if cleaned else ""

    def _normalize_state_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()

    def _execution_state(self, goal: str | None, state: list[str] | None) -> list[str] | None:
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
            ),
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
            if normalized_files and all(path in lane_files for path in normalized_files):
                matches.append((lane.lane_id, lane_files))
        if len(matches) != 1:
            return None, []
        return matches[0]

    def archive_sessions_now(
        self,
        project_root: Path,
        session_ids: list[str] | None = None,
        stale_after_days: int = 30,
        dry_run: bool = True,
    ) -> dict[str, object]:
        """Move stale done-sessions into .MEMORY/archive/sessions/.

        Bonus 2026-04-19. Companion to list_archive_candidates: this
        actually performs the move. dry_run=True (default) returns the
        plan without touching disk so the operator can preview. Pass
        an explicit session_ids list to override the staleness filter.

        Returns {planned: [...], moved: [...], skipped: [...]} with
        a per-session reason for any skip (collision, missing source,
        etc.).
        """
        import shutil

        candidates_resp = self.list_archive_candidates(
            project_root,
            stale_after_days=int(stale_after_days),
        )
        all_candidates = [
            c
            for c in (candidates_resp.get("candidates") or [])
            if isinstance(c, dict) and c.get("session_id")
        ]
        if session_ids is not None:
            wanted = {s.strip() for s in session_ids if s and s.strip()}
            targets = [c for c in all_candidates if c["session_id"] in wanted]
        else:
            targets = all_candidates
        sessions_root = project_root / ".MEMORY" / "sessions"
        archive_root = project_root / ".MEMORY" / "archive" / "sessions"
        planned: list[dict[str, str]] = []
        moved: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for c in targets:
            sid = str(c["session_id"])
            src = sessions_root / sid
            dst = archive_root / sid
            entry = {"session_id": sid, "src": str(src), "dst": str(dst)}
            planned.append(entry)
            if dry_run:
                continue
            if not src.is_dir():
                skipped.append({**entry, "reason": "source_missing"})
                continue
            if dst.exists():
                skipped.append({**entry, "reason": "destination_exists"})
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(entry)
            except Exception as exc:
                skipped.append({**entry, "reason": f"move_failed: {exc}"})
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "stale_after_days": int(stale_after_days),
            "planned": planned,
            "moved": moved,
            "skipped": skipped,
            "planned_count": len(planned),
            "moved_count": len(moved),
            "skipped_count": len(skipped),
        }

    def lane_workers_status_summary(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Aggregate lane-worker states across sessions.

        Bonus 2026-04-19. SessionLaneAgentsStore tracks per-worker
        state (spawned/running/done/crashed); this sums by state and
        lane, surfaces running counts per lane, and reports the
        longest-running worker. Useful for "who's still working?"
        dashboard widgets.
        """
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore

            store = SessionLaneAgentsStore()
            # reap first so "running" reflects reality, not zombies.
            if session_id:
                try:
                    store.reap_crashed(project_root, session_id=session_id)
                except Exception:
                    pass
            rows = (
                store.get_lane_agents(
                    project_root,
                    session_id=session_id,
                )
                or []
            )
        except Exception:
            return {"ok": False, "workers": [], "error": "store_unavailable"}
        by_state: dict[str, int] = {}
        by_lane: dict[str, dict[str, int]] = {}
        longest_running: dict[str, object] | None = None
        longest_elapsed = 0.0
        from datetime import datetime

        now = datetime.now(UTC)
        for row in rows:
            state = str(row.get("state", "unknown"))
            by_state[state] = by_state.get(state, 0) + 1
            lane = str(row.get("lane_id", ""))
            lane_entry = by_lane.setdefault(
                lane,
                {
                    "running": 0,
                    "done": 0,
                    "crashed": 0,
                    "spawned": 0,
                    "total": 0,
                },
            )
            lane_entry[state] = lane_entry.get(state, 0) + 1
            lane_entry["total"] += 1
            # Longest-running: among state=running, pick the oldest
            # started_at.
            if state == "running":
                started = row.get("started_at")
                if started:
                    try:
                        clean = str(started).rstrip("Z")
                        if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                            clean = clean + "+00:00"
                        ts = datetime.fromisoformat(clean)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        elapsed = (now - ts).total_seconds()
                        if elapsed > longest_elapsed:
                            longest_elapsed = elapsed
                            longest_running = {
                                "worker_id": row.get("worker_id"),
                                "lane_id": lane,
                                "started_at": str(started),
                                "elapsed_seconds": round(elapsed, 1),
                            }
                    except Exception:
                        pass
        return {
            "ok": True,
            "session_id": session_id,
            "total_workers": len(rows),
            "by_state": by_state,
            "by_lane": [{"lane_id": lane, **stats} for lane, stats in sorted(by_lane.items())],
            "longest_running": longest_running,
        }

    def recent_commits_touching_file(
        self,
        project_root: Path,
        file_path: str,
        limit: int = 10,
    ) -> dict[str, object]:
        """Recent git commits that modified a given file.

        Bonus 2026-04-19. Operator asks "when did this file last
        change?" and wants commit hash + date + subject. Shells out
        to `git log` via ai_run's backing primitive but
        goes through GitPython/subprocess directly to avoid the
        runtime's bash-policy filter (since this is a read-only
        metadata call on a single file). Returns last N commits
        newest-first.
        """
        import subprocess

        if not file_path or not file_path.strip():
            return {"ok": False, "commits": [], "error": "empty_path"}
        try:
            target = (project_root / file_path).resolve()
            target.relative_to(project_root.resolve())
        except (ValueError, OSError):
            return {"ok": False, "commits": [], "error": "path_outside_project"}
        try:
            # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
            from .shell_egress_service import audited_run

            result = audited_run(
                [
                    "git",
                    "log",
                    f"-{int(limit)}",
                    "--pretty=format:%H\t%ai\t%an\t%s",
                    "--",
                    file_path,
                ],
                fingerprint=("runtime_service.py", "recent_commits_touching_file", "subprocess.run"),
                reason="git-log-file-history",
                run=lambda *a, **kw: subprocess.run(*a, **kw),
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=15,
                # #333 Phase 2: daemon runs console-less (pythonw); without
                # this flag every spawn allocates a NEW visible console window.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return {"ok": False, "commits": [], "error": f"git_log_failed: {exc}"}
        if result.returncode != 0:
            return {
                "ok": False,
                "commits": [],
                "error": f"git_log_exit_{result.returncode}: {result.stderr.strip()[:200]}",
            }
        commits: list[dict[str, str]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            commits.append(
                {
                    "sha": parts[0],
                    "date": parts[1],
                    "author": parts[2],
                    "subject": parts[3][:200],
                },
            )
        return {
            "ok": True,
            "file": file_path,
            "count": len(commits),
            "commits": commits,
        }

    def project_progress_dashboard(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """One-call conductor overview: roadmap progress + audit + activity.

        Bonus 2026-04-19 — composite of roadmap_layer_progress,
        project_audit_snapshot.headline, backlog_inbox count, and
        lane_workers_status_summary. Designed for "glance and know" —
        conductor pastes output into handoff, operator sees the whole
        state in one scroll.

        Returns {ok, headline, roadmap, backlog, workers}. Each
        component is best-effort; partial breakage doesn't kill the
        dashboard.
        """
        out: dict[str, object] = {
            "ok": True,
            "project_root": str(project_root),
        }

        def _safe(name: str, fn):
            try:
                out[name] = fn()
            except Exception as exc:
                out[name] = {"ok": False, "error": str(exc)[:200]}

        _safe("roadmap", lambda: self.roadmap_layer_progress(project_root))
        _safe("audit", lambda: self.project_audit_snapshot(project_root))
        _safe("backlog", lambda: self.backlog_inbox(project_root, limit=10))
        _safe("workers", lambda: self.lane_workers_status_summary(project_root))
        _safe("freshness", lambda: self.project_freshness(project_root))

        # Headline rollup — one screen of key numbers.
        roadmap = out.get("roadmap") or {}
        audit = out.get("audit") or {}
        audit_headline = audit.get("headline", {}) if isinstance(audit, dict) else {}
        workers = out.get("workers") or {}
        backlog = out.get("backlog") or {}
        out["headline"] = {
            "health_score": int(audit_headline.get("score", 0)),
            "roadmap_percent": (
                roadmap.get("overall_percent", 0) if isinstance(roadmap, dict) else 0
            ),
            "total_violations": int(audit_headline.get("total_violations", 0)),
            "backlog_count": int(backlog.get("count", 0)) if isinstance(backlog, dict) else 0,
            "active_workers": int(workers.get("total_workers", 0))
            if isinstance(workers, dict)
            else 0,
            "open_sessions": int(audit_headline.get("open_sessions", 0)),
            "blocked_sessions": int(audit_headline.get("blocked_sessions", 0)),
            "stale_sessions": int(audit_headline.get("stale_sessions", 0)),
        }
        return out

    def list_protected_files(
        self,
        project_root: Path,
        limit: int = 200,
    ) -> dict[str, object]:
        """All files carrying the DO NOT TOUCH sentinel header.

        Bonus 2026-04-19 — Layer 6 DO-NOT-TOUCH tranche deliverable.
        Walks the project tree (skipping node_modules / .git / .venv /
        __pycache__ / .MEMORY / build / dist / target), reads the
        first 20 lines of every text file, flags those with the
        PROTECTION_SENTINEL. Returns the list sorted alphabetically
        plus per-file pair_files (other files the sentinel chains to).
        """
        from .protected_file import (
            PROTECTION_SENTINEL,
            has_protection_sentinel,
            parse_pair_files,
        )

        skip_dirs = {
            "node_modules",
            ".git",
            ".venv",
            "venv",
            "env",
            "__pycache__",
            ".MEMORY",
            "build",
            "dist",
            "target",
            ".pytest-tmp",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".next",
            ".cache",
            "coverage",
            "htmlcov",
        }
        # Extensions worth scanning — code/docs/config. Skip binaries
        # explicitly to keep the walk fast.
        text_suffixes = {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".cs",
            ".cpp",
            ".c",
            ".h",
            ".hpp",
            ".md",
            ".txt",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
            ".sh",
            ".ps1",
            ".sql",
            ".rb",
            ".php",
            ".lua",
        }
        found: list[dict[str, object]] = []
        try:
            for path in project_root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    rel_parts = path.relative_to(project_root).parts
                except ValueError:
                    continue
                # Skip checks only apply to paths INSIDE the project tree.
                # Looking at absolute path parts (the original bug) meant
                # any tmp dir sitting under a skip-named ancestor like
                # `.pytest-tmp/` was wrongly excluded.
                if any(part in skip_dirs for part in rel_parts):
                    continue
                if path.suffix.lower() not in text_suffixes:
                    continue
                try:
                    # Read only head to keep the walk fast.
                    with path.open("r", encoding="utf-8", errors="ignore") as fh:
                        head_lines: list[str] = []
                        for _ in range(20):
                            line = fh.readline()
                            if not line:
                                break
                            head_lines.append(line)
                        head = "".join(head_lines)
                except OSError:
                    continue
                if not has_protection_sentinel(head):
                    continue
                try:
                    rel = path.relative_to(project_root)
                except ValueError:
                    rel = path
                pairs = parse_pair_files(head)
                found.append(
                    {
                        "path": str(rel).replace("\\", "/"),
                        "pair_files": pairs,
                    },
                )
                if len(found) >= int(limit):
                    break
        except Exception:
            pass
        found.sort(key=lambda e: str(e["path"]))
        return {
            "ok": True,
            "sentinel": PROTECTION_SENTINEL,
            "count": len(found),
            "files": found,
        }

    def roadmap_layer_progress(
        self,
        project_root: Path,
        roadmap_path: str | None = None,
    ) -> dict[str, object]:
        """Roadmap progress from the ai_backlog SQL store.

        No-file-layer doctrine (2026-05-21): roadmap.md is RETIRED — the
        roadmap is the project backlog (ai_backlog). Progress is grouped by
        priority band (the dashboard's progress bands, replacing the old
        ``# Layer N`` checkbox sections); ``done`` items are the "checked"
        ones, active items (open/in_progress/blocked) the "unchecked".
        ``roadmap_path`` is accepted for back-compat and ignored — nothing
        is read from disk. Return shape is preserved so the dashboard chart
        keeps working.
        """
        from . import project_backlog_store as _bk

        try:
            items = _bk.list_backlog(project_root)  # excludes 'removed'
        except Exception:
            return {"ok": False, "layers": [], "error": "backlog_unavailable"}

        # Canonical band order from the store (#101): a hardcoded copy here
        # silently DROPPED items whose band it didn't know (urgent/normal
        # vanished from progress after the enum change) — truth bug class.
        bands = list(_bk.PRIORITY_ORDER)
        active = {"open", "in_progress", "blocked"}
        layers: list[dict[str, object]] = []
        for band in bands:
            checked = sum(
                1 for i in items if i.get("priority") == band and i.get("status") == "done"
            )
            unchecked = sum(
                1 for i in items if i.get("priority") == band and i.get("status") in active
            )
            total = checked + unchecked
            if total == 0:
                continue
            layers.append(
                {
                    "layer": band,
                    "checked": checked,
                    "unchecked": unchecked,
                    "total": total,
                    "percent": round(100 * checked / total, 1) if total else None,
                },
            )

        all_checked = sum(int(l["checked"]) for l in layers)
        all_unchecked = sum(int(l["unchecked"]) for l in layers)
        all_total = all_checked + all_unchecked
        return {
            "ok": True,
            "source": "ai_backlog",
            "overall_checked": all_checked,
            "overall_unchecked": all_unchecked,
            "overall_percent": (round(100 * all_checked / all_total, 1) if all_total else None),
            "layers": layers,
        }

    def untouched_code_files(
        self,
        project_root: Path,
        limit: int = 100,
    ) -> dict[str, object]:
        """Indexed source files with no entries in edit_history.

        Bonus 2026-04-19. Hotspot's cold twin — these files live in
        the index but no session has touched them through AIDOCS.
        Either dead code, or code the team intentionally avoids,
        or files edited through raw tools before tier-0 landed.
        Useful for dead-code cleanup triage.
        """
        self.hub.code.init_db(project_root)
        try:
            with self.hub.code.connect(project_root) as conn:
                indexed_rows = conn.execute(
                    "SELECT path, language, line_count FROM code_files "
                    "WHERE (role IS NULL OR role NOT IN ('test', 'fixture')) "
                    "ORDER BY line_count DESC",
                ).fetchall()
        except Exception:
            return {"ok": False, "files": [], "error": "index_unavailable"}
        indexed_paths = {str(row["path"]) for row in indexed_rows}
        # Files that have edit history.
        touched_paths: set[str] = set()
        try:
            from .edit_history import EditHistoryStore

            edits = EditHistoryStore().list_edits(project_root, limit=20000)
            for e in edits:
                if e.file_path:
                    touched_paths.add(str(e.file_path))
        except Exception:
            pass
        untouched: list[dict[str, object]] = []
        for row in indexed_rows:
            fp = str(row["path"])
            # Match either exact or endswith, same as hot_files_with_no_test.
            if fp in touched_paths:
                continue
            if any(fp.endswith(t) or t.endswith(fp) for t in touched_paths):
                continue
            untouched.append(
                {
                    "path": fp,
                    "language": row["language"] or "",
                    "line_count": int(row["line_count"] or 0),
                },
            )
        untouched.sort(key=lambda f: -int(f["line_count"]))
        return {
            "ok": True,
            "total_indexed": len(indexed_paths),
            "total_touched": len(touched_paths),
            "untouched_count": len(untouched),
            "files": untouched[: int(limit)],
        }

    def session_handoff_completeness(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Check whether a session's handoff has the shape a reader needs.

        Bonus 2026-04-19. Handoffs that ship with "-" placeholders or
        missing sections leave the next operator guessing. Reads the
        session's HANDOFF.md, counts meaningful lines per section,
        flags sections that are empty or placeholder-only. Returns
        per-section shape + rollup score (0-100).
        """
        try:
            handoff = self.hub.sessions.read_handoff(project_root, session_id)
        except Exception:
            return {"ok": False, "session_id": session_id, "error": "handoff_unavailable"}
        if not handoff or not getattr(handoff, "sections", None):
            return {
                "ok": True,
                "session_id": session_id,
                "completeness": 0,
                "sections": [],
                "reason": "handoff_missing_or_empty",
            }
        # Sections we expect an operator-written handoff to fill beyond
        # the auto-populated scaffold. Skipping Purpose/Freshness/Steps
        # because the scaffold already populates those; flagging the
        # others keeps the score meaningful.
        expected_sections = [
            "Current State",
            "What Was Done",
            "What Failed / Dead Ends",
            "What Matters Now",
            "Open Questions",
            "Risks and Blockers",
            "Relevant Files",
            "Suggested Next Steps",
        ]
        per_section: list[dict[str, object]] = []
        filled_count = 0
        for name in expected_sections:
            body = handoff.sections.get(name, []) or []
            meaningful_lines = 0
            for line in body:
                cleaned = str(line).lstrip("- ").strip()
                if cleaned and cleaned != "-":
                    meaningful_lines += 1
            if meaningful_lines > 0:
                filled_count += 1
            per_section.append(
                {
                    "section": name,
                    "meaningful_lines": meaningful_lines,
                    "status": "filled" if meaningful_lines > 0 else "placeholder",
                },
            )
        score = int(round(100 * filled_count / len(expected_sections)))
        return {
            "ok": True,
            "session_id": session_id,
            "completeness": score,
            "filled_sections": filled_count,
            "total_sections": len(expected_sections),
            "sections": per_section,
        }

    def dependency_freshness(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Age of dependency lockfiles + package manifests.

        Bonus 2026-04-19. Stale lockfiles ship old CVEs. This walks
        known dep manifests (requirements.txt, pyproject.toml,
        poetry.lock, uv.lock, package.json, pnpm-lock.yaml, yarn.lock,
        Cargo.lock, go.sum, Gemfile.lock) at the project root and
        first-level package dirs, returns {path, mtime_iso, age_days,
        band}. Bands: fresh (<30d), aging (30-180d), stale (>180d).
        """
        import time
        from datetime import datetime

        manifests = [
            "requirements.txt",
            "requirements-dev.txt",
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "Pipfile.lock",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lockb",
            "Cargo.lock",
            "go.sum",
            "Gemfile.lock",
            "composer.lock",
        ]
        now = time.time()
        results: list[dict[str, object]] = []
        # Scan project root + one level deep (common for monorepos like
        # AIDOCS/mcp/, ui/, dashboard/).
        candidates: list[Path] = [project_root]
        try:
            for child in project_root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    candidates.append(child)
        except OSError:
            pass
        for root in candidates:
            for name in manifests:
                path = root / name
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                age_days = (now - mtime) / 86400.0
                if age_days < 30:
                    band = "fresh"
                elif age_days < 180:
                    band = "aging"
                else:
                    band = "stale"
                try:
                    rel = path.relative_to(project_root)
                except ValueError:
                    rel = path
                results.append(
                    {
                        "path": str(rel).replace("\\", "/"),
                        "mtime_iso": datetime.fromtimestamp(
                            mtime,
                            tz=UTC,
                        ).isoformat(),
                        "age_days": round(age_days, 1),
                        "band": band,
                    },
                )
        results.sort(key=lambda r: -float(r["age_days"]))
        summary = {"fresh": 0, "aging": 0, "stale": 0}
        for r in results:
            summary[str(r["band"])] += 1
        return {
            "ok": True,
            "total_manifests": len(results),
            "summary": summary,
            "manifests": results,
        }

    # rule_orphan_finder was REMOVED 2026-05-21 (no-file-layer doctrine).
    # It scanned .MEMORY/rules|standards|system|domains/*.md for markdown
    # cross-link hygiene — a markdown-era diagnostic with no SQL-canonical
    # equivalent. Rule relevance is now status (active) + routing, not
    # markdown back-links.

    def config_validation_report(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Check every setting in effective_config against type/range sanity.

        Bonus 2026-04-19. Operators set values through the dashboard
        but there's no single "is my config sane?" check. Walks
        effective_config vs _DEFAULT_CONFIG, flags: (a) type mismatches
        (operator set a string where default is bool), (b) out-of-range
        ints (negative timeouts, impossibly large caps). Per-entry
        {path, current, expected_type, issue} so dashboards render the
        offender list directly.
        """
        from .config import _DEFAULT_CONFIG

        try:
            effective = self._config_resolver.effective_config(
                project_root=project_root,
            )
        except Exception:
            return {"ok": False, "issues": [], "error": "config_unavailable"}
        issues: list[dict[str, object]] = []

        def _walk(path: str, current: object, default: object) -> None:
            # Recurse into dicts; compare leaves.
            if isinstance(default, dict):
                if not isinstance(current, dict):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "dict",
                            "issue": "type_mismatch",
                        },
                    )
                    return
                for k in default:
                    sub = f"{path}.{k}" if path else k
                    _walk(sub, current.get(k), default[k])
                return
            # Leaf — compare types.
            if default is None:
                return  # no opinion
            if current is None:
                return  # missing is tolerated (falls back to default)
            # Allow bool↔int (python treats bool as int, but we want distinct).
            if isinstance(default, bool):
                if not isinstance(current, bool):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "bool",
                            "issue": "type_mismatch",
                        },
                    )
                return
            if isinstance(default, int) and not isinstance(default, bool):
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "int",
                            "issue": "type_mismatch",
                        },
                    )
                    return
                if current < 0 and "timeout" in path.lower():
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "int >= 0",
                            "issue": "negative_timeout",
                        },
                    )
                elif current > 86400 and ("seconds" in path.lower() or "timeout" in path.lower()):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "int (typical < 86400 = 1 day)",
                            "issue": "suspiciously_large",
                        },
                    )
                return
            if isinstance(default, str):
                if not isinstance(current, str):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "str",
                            "issue": "type_mismatch",
                        },
                    )
                return
            if isinstance(default, list):
                if not isinstance(current, list):
                    issues.append(
                        {
                            "path": path,
                            "current": current,
                            "expected_type": "list",
                            "issue": "type_mismatch",
                        },
                    )
                return

        try:
            _walk("", effective, _DEFAULT_CONFIG)
        except Exception:
            pass
        # Skip action_hooks / languages / interaction noise.
        skip_prefixes = ("action_hooks.", "languages.", "interaction.")
        issues = [i for i in issues if not any(str(i["path"]).startswith(p) for p in skip_prefixes)]
        issues.sort(key=lambda i: str(i["path"]))
        return {
            "ok": True,
            "issue_count": len(issues),
            "issues": issues,
        }

    def plan_step_drift(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """PLAN checkbox items that never triggered a task lifecycle event.

        Bonus 2026-04-19. Session PLANs carry `- [ ] step X` items;
        task_lifecycle entries carry intent text. When a step never
        shows up in the journal, it was likely skipped/forgotten.
        Returns unchecked PLAN items with no matching task_lifecycle
        intent substring match.
        """
        try:
            plan = self.hub.sessions.read_plan(project_root, session_id)
            plan_sections = plan.sections if plan else {}
        except Exception:
            return {"ok": False, "drifted": [], "error": "plan_unavailable"}
        steps_raw = plan_sections.get("Steps", []) or []
        steps: list[str] = []
        for line in steps_raw:
            stripped = str(line).strip()
            if stripped.startswith("- [ ]"):
                steps.append(stripped[5:].strip())
        try:
            entries = self.hub.sessions.read_journal(project_root, session_id) or []
        except Exception:
            entries = []
        intents = [str(entry.get("intent", "")).lower() for entry in entries]
        drifted: list[str] = []
        touched: list[str] = []
        for step in steps:
            # Score — a PLAN step was touched if any journal intent
            # contains a substantive fragment (first 30 chars).
            fragment = step[:30].lower().strip()
            if not fragment:
                continue
            matched = any(fragment in intent for intent in intents)
            if matched:
                touched.append(step)
            else:
                drifted.append(step)
        return {
            "ok": True,
            "session_id": session_id,
            "total_unchecked": len(steps),
            "drifted_count": len(drifted),
            "touched_count": len(touched),
            "drifted": drifted[:20],
            "touched": touched[:20],
        }

    def memory_doc_word_count(
        self,
        project_root: Path,
        memory_root: str = ".MEMORY",
    ) -> dict[str, object]:
        """Word-count per memory doc — rule hygiene signal.

        Bonus 2026-04-19. Rule files should be terse (≤200 words of
        durable guidance) — sprawling docs are usually journals that
        slipped in. Returns per-file word counts with heuristic band
        tags: sparse (<30 words — likely placeholder), healthy
        (30-500), bloated (>500 — split candidate). Skips
        sessions/archive/.aidocs/.index to stay focused on durable
        memory.
        """
        mem_root = project_root / memory_root
        if not mem_root.is_dir():
            return {"ok": False, "docs": [], "error": "memory_root_not_found"}
        skip_dirs = {"sessions", "archive", ".aidocs", ".index"}
        docs: list[dict[str, object]] = []
        for path in mem_root.rglob("*.md"):
            try:
                rel = path.relative_to(mem_root)
            except ValueError:
                continue
            if any(part in skip_dirs for part in rel.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            words = len(text.split())
            if words < 30:
                band = "sparse"
            elif words > 500:
                band = "bloated"
            else:
                band = "healthy"
            docs.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "words": words,
                    "band": band,
                },
            )
        docs.sort(key=lambda d: -int(d["words"]))
        summary = {"sparse": 0, "healthy": 0, "bloated": 0}
        for d in docs:
            summary[str(d["band"])] += 1
        return {
            "ok": True,
            "memory_root": memory_root,
            "total_docs": len(docs),
            "summary": summary,
            "docs": docs,
        }

    def file_age_histogram(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Distribution of indexed-file mtimes by age bucket.

        Bonus 2026-04-19. Answers "how much of this codebase is old?"
        at a glance. Reads code_files.mtime_ns and buckets into
        recency bands: last 24h, last 7d, last 30d, last 90d, last
        year, older. Count + fraction per bucket. Sparkline-friendly.
        """
        import time

        self.hub.code.init_db(project_root)
        try:
            with self.hub.code.connect(project_root) as conn:
                rows = conn.execute("SELECT mtime_ns FROM code_files WHERE mtime_ns > 0").fetchall()
        except Exception:
            return {"ok": False, "buckets": [], "error": "index_unavailable"}
        now_ns = int(time.time() * 1_000_000_000)
        # Bucket thresholds in nanoseconds.
        NS = 1_000_000_000
        thresholds = [
            ("< 24h", 24 * 3600 * NS),
            ("< 7d", 7 * 24 * 3600 * NS),
            ("< 30d", 30 * 24 * 3600 * NS),
            ("< 90d", 90 * 24 * 3600 * NS),
            ("< 1yr", 365 * 24 * 3600 * NS),
        ]
        counts: dict[str, int] = {label: 0 for label, _ in thresholds}
        counts["older"] = 0
        total = 0
        for row in rows:
            mtime_ns = int(row["mtime_ns"] or 0)
            if mtime_ns <= 0:
                continue
            age_ns = now_ns - mtime_ns
            age_ns = max(age_ns, 0)
            placed = False
            for label, limit_ns in thresholds:
                if age_ns < limit_ns:
                    counts[label] += 1
                    placed = True
                    break
            if not placed:
                counts["older"] += 1
            total += 1
        buckets = []
        for label, _ in thresholds:
            count = counts[label]
            buckets.append(
                {
                    "bucket": label,
                    "count": count,
                    "fraction": round(count / total, 3) if total else 0.0,
                },
            )
        buckets.append(
            {
                "bucket": "older",
                "count": counts["older"],
                "fraction": round(counts["older"] / total, 3) if total else 0.0,
            },
        )
        return {
            "ok": True,
            "total_indexed": total,
            "buckets": buckets,
        }

    def session_compare(
        self,
        project_root: Path,
        session_a: str,
        session_b: str,
    ) -> dict[str, object]:
        """Diff two sessions: action_kind counts + unique-to-each.

        Bonus 2026-04-19. "How did session X differ from Y?" was a
        manual side-by-side read of two journals. This aggregates
        by action_kind, returns per-kind counts for both sessions
        plus the set of kinds that appeared in only one. Useful for
        "this re-run session looks similar to the original, right?"
        verification.
        """

        def _scan(sid: str) -> dict[str, int]:
            counts: dict[str, int] = {}
            try:
                entries = self.hub.sessions.read_journal(project_root, sid) or []
            except Exception:
                return counts
            for entry in entries:
                kind = str(entry.get("action_kind", "")).strip().lower()
                if not kind:
                    continue
                counts[kind] = counts.get(kind, 0) + 1
            return counts

        a_counts = _scan(session_a)
        b_counts = _scan(session_b)
        all_kinds = sorted(set(a_counts) | set(b_counts))
        by_kind = []
        for kind in all_kinds:
            a = a_counts.get(kind, 0)
            b = b_counts.get(kind, 0)
            by_kind.append(
                {
                    "action_kind": kind,
                    "a": a,
                    "b": b,
                    "diff": a - b,
                },
            )
        only_a = sorted(k for k in a_counts if k not in b_counts)
        only_b = sorted(k for k in b_counts if k not in a_counts)
        return {
            "ok": True,
            "session_a": session_a,
            "session_b": session_b,
            "by_kind": by_kind,
            "only_in_a": only_a,
            "only_in_b": only_b,
            "a_total": sum(a_counts.values()),
            "b_total": sum(b_counts.values()),
        }

    def most_denied_commands(
        self,
        project_root: Path,
        limit: int = 20,
    ) -> dict[str, object]:
        """Top shell commands by gate-block count.

        Bonus 2026-04-19. denial_tier_stats aggregates by tier;
        this aggregates by the actual command the agent reached
        for. Helps operators spot patterns ("agents keep trying
        `find . -delete`") and decide whether to relax the policy
        or train the workflow around the friction.
        """
        self.hub.execution.init_db(project_root)
        try:
            with self.hub.execution.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT payload_json, event_kind, COUNT(*) AS n "
                    "FROM execution_events "
                    "WHERE event_kind IN ('raw_shell_block', 'bash_policy_block', "
                    "'heuristic_judge_block', 'test_retry_block') "
                    "AND payload_json IS NOT NULL "
                    "GROUP BY payload_json, event_kind "
                    "ORDER BY n DESC LIMIT ?",
                    (int(limit) * 3,),  # overfetch, dedup below
                ).fetchall()
        except Exception:
            return {"ok": False, "commands": [], "error": "execution_index_unavailable"}
        per_command: dict[str, dict[str, object]] = {}
        for row in rows:
            payload_raw = row["payload_json"]
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                continue
            command = str(payload.get("command", "")).strip()
            if not command:
                continue
            # Normalize to the verb-only form for coarser grouping.
            # Full command stays visible as the first example.
            entry = per_command.setdefault(
                command,
                {
                    "count": 0,
                    "tiers": set(),
                },
            )
            entry["count"] = int(entry["count"]) + int(row["n"])
            kind = str(row["event_kind"])
            tier = kind.removesuffix("_block")
            entry["tiers"].add(tier)
        commands = [
            {
                "command": cmd[:200],
                "count": int(stats["count"]),
                "tiers": sorted(stats["tiers"]),
            }
            for cmd, stats in per_command.items()
        ]
        commands.sort(key=lambda c: -int(c["count"]))
        return {
            "ok": True,
            "total_unique_commands": len(commands),
            "commands": commands[: int(limit)],
        }

    def inactive_session_nudge(
        self,
        project_root: Path,
        stale_after_days: int = 7,
    ) -> dict[str, object]:
        """Non-terminal sessions with no recent activity.

        Bonus 2026-04-19. Complement to list_archive_candidates
        (which finds DONE sessions); this finds ACTIVE sessions
        that have gone quiet — the "stuck / forgotten / abandoned"
        sessions that need a nudge. Uses journal freshness as the
        activity signal; sessions with no journal fall through to
        session mtime.
        """
        from datetime import datetime, timedelta

        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
        except Exception:
            return {"ok": False, "inactive": [], "error": "list_sessions_failed"}
        cutoff = datetime.now(UTC) - timedelta(days=int(stale_after_days))
        cutoff_iso = cutoff.isoformat()
        inactive: list[dict[str, object]] = []
        for s in sessions:
            sid = getattr(s, "session_id", None) or (
                s.get("session_id") if isinstance(s, dict) else None
            )
            if not sid:
                continue
            status = (
                getattr(s, "status", None) or (s.get("status") if isinstance(s, dict) else "") or ""
            )
            status_str = str(status or "").lower().strip("- ").strip()
            # Skip terminal sessions — list_archive_candidates handles those.
            if any(status_str.startswith(t) for t in ("done", "abandoned", "closed", "archived")):
                continue
            # Find the latest journal timestamp.
            latest_ts = None
            try:
                entries = self.hub.sessions.read_journal(project_root, sid) or []
                for entry in entries:
                    ts = str(entry.get("timestamp", ""))
                    if ts and (latest_ts is None or ts > latest_ts):
                        latest_ts = ts
            except Exception:
                pass
            if latest_ts is None or latest_ts < cutoff_iso:
                stale_days = None
                if latest_ts:
                    try:
                        clean = latest_ts.rstrip("Z")
                        if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                            clean = clean + "+00:00"
                        last = datetime.fromisoformat(clean)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=UTC)
                        stale_days = round(
                            (datetime.now(UTC) - last).total_seconds() / 86400,
                            1,
                        )
                    except Exception:
                        pass
                inactive.append(
                    {
                        "session_id": str(sid),
                        "status": status_str or "active",
                        "title": str(getattr(s, "title", "") or ""),
                        "last_activity": latest_ts,
                        "stale_days": stale_days,
                    },
                )
        inactive.sort(
            key=lambda x: x["last_activity"] or "",
        )
        return {
            "ok": True,
            "stale_after_days": int(stale_after_days),
            "count": len(inactive),
            "inactive": inactive,
        }

    def edit_session_overlap(
        self,
        project_root: Path,
        window_hours: int = 24,
    ) -> dict[str, object]:
        """Files edited by multiple sessions within a recent window.

        Bonus 2026-04-19. Conflict-risk surface — when two parallel
        sessions edit the same file, merge conflicts or stale reads
        are a hazard. Walks edit_history, buckets by file_path,
        flags files with edits from >1 distinct session_id in the
        last `window_hours`. Sorted by conflict severity (number of
        distinct sessions desc, then edit count desc).
        """
        from datetime import datetime, timedelta

        try:
            from .edit_history import EditHistoryStore

            edits = EditHistoryStore().list_edits(project_root, limit=5000)
        except Exception:
            return {"ok": False, "files": [], "error": "edit_history_unavailable"}
        cutoff_iso = (datetime.now(UTC) - timedelta(hours=int(window_hours))).isoformat()
        per_file: dict[str, dict[str, object]] = {}
        for e in edits:
            if str(e.created_at) < cutoff_iso:
                continue
            entry = per_file.setdefault(
                e.file_path,
                {
                    "sessions": set(),
                    "edit_count": 0,
                    "last_edit": "",
                },
            )
            if e.session_id:
                entry["sessions"].add(e.session_id)
            entry["edit_count"] = int(entry["edit_count"]) + 1
            if str(e.created_at) > str(entry["last_edit"]):
                entry["last_edit"] = e.created_at
        overlapping = []
        for fp, stats in per_file.items():
            sessions = stats["sessions"]
            if len(sessions) > 1:
                overlapping.append(
                    {
                        "file": fp,
                        "session_count": len(sessions),
                        "sessions": sorted(sessions),
                        "edit_count": int(stats["edit_count"]),
                        "last_edit": stats["last_edit"],
                    },
                )
        overlapping.sort(
            key=lambda f: (-int(f["session_count"]), -int(f["edit_count"])),
        )
        return {
            "ok": True,
            "window_hours": int(window_hours),
            "count": len(overlapping),
            "files": overlapping,
        }

    def recent_errors_scan(
        self,
        project_root: Path,
        limit: int = 50,
    ) -> dict[str, object]:
        """Recent execution-events with status=error across all sessions.

        Bonus 2026-04-19. Quick forensic surface — operators hit a
        bug and want "what's been erroring today?" without walking
        the full execution trail. Filters execution_events for
        status='error' (or starting with 'error'), newest first.
        """
        self.hub.execution.init_db(project_root)
        try:
            with self.hub.execution.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT event_kind, capability_name, session_id, "
                    "action_kind, target_entity, status, payload_json, "
                    "observed_at "
                    "FROM execution_events "
                    "WHERE status LIKE 'error%' OR status = 'failed' "
                    "ORDER BY observed_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except Exception:
            return {"ok": False, "events": [], "error": "execution_index_unavailable"}
        events = [
            {
                "event_kind": row["event_kind"],
                "capability_name": row["capability_name"] or "",
                "session_id": row["session_id"] or "",
                "action_kind": row["action_kind"] or "",
                "target_entity": row["target_entity"] or "",
                "status": row["status"],
                "observed_at": row["observed_at"],
            }
            for row in rows
        ]
        return {
            "ok": True,
            "count": len(events),
            "events": events,
        }

    def session_owner_summary(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Aggregate sessions/tasks per owner.

        Bonus 2026-04-19. Walks list_sessions, reads the Owner section
        from each, aggregates {owner → {total, active, done, blocked}}.
        Useful for multi-operator projects to see who's working on what.
        """
        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
        except Exception:
            return {"ok": False, "owners": {}, "error": "list_sessions_failed"}
        owners: dict[str, dict[str, int]] = {}
        for s in sessions:
            sid = getattr(s, "session_id", None) or (
                s.get("session_id") if isinstance(s, dict) else None
            )
            if not sid:
                continue
            owner = "(none)"
            status_str = ""
            try:
                full = self.hub.sessions.read_session(project_root, sid)
                if full and getattr(full, "sections", None):
                    for line in full.sections.get("Owner") or []:
                        cleaned = str(line).lstrip("- ").strip()
                        if cleaned and cleaned != "-":
                            owner = cleaned[:60]
                            break
                    for line in full.sections.get("Status") or []:
                        cleaned = str(line).lstrip("- ").strip().lower()
                        if cleaned:
                            status_str = cleaned
                            break
            except Exception:
                pass
            entry = owners.setdefault(
                owner,
                {
                    "total": 0,
                    "active": 0,
                    "done": 0,
                    "blocked": 0,
                },
            )
            entry["total"] += 1
            if any(status_str.startswith(t) for t in ("done", "abandoned", "closed", "archived")):
                entry["done"] += 1
            elif "block" in status_str:
                entry["blocked"] += 1
            else:
                entry["active"] += 1
        summary = [
            {"owner": name, **counts}
            for name, counts in sorted(owners.items(), key=lambda kv: -kv[1]["total"])
        ]
        return {
            "ok": True,
            "owners": summary,
            "owner_count": len(summary),
            "total_sessions": sum(o["total"] for o in summary),
        }

    def tool_use_leaderboard(
        self,
        project_root: Path,
        limit: int = 30,
    ) -> dict[str, object]:
        """Most-called MCP tools from the execution-events trail.

        Bonus 2026-04-19. operator observability — which tools do
        agents reach for most? Aggregates capability_name from every
        tool_call_started event. Highest call count first.
        """
        self.hub.execution.init_db(project_root)
        try:
            with self.hub.execution.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT capability_name, COUNT(*) AS n "
                    "FROM execution_events "
                    "WHERE event_kind = 'tool_call_started' "
                    "AND capability_name IS NOT NULL "
                    "GROUP BY capability_name "
                    "ORDER BY n DESC",
                ).fetchall()
        except Exception:
            return {"ok": False, "tools": [], "error": "execution_index_unavailable"}
        leaderboard = [{"tool": row["capability_name"], "calls": int(row["n"])} for row in rows]
        return {
            "ok": True,
            "total_unique_tools": len(leaderboard),
            "total_calls": sum(t["calls"] for t in leaderboard),
            "tools": leaderboard[: int(limit)],
        }

    def task_progress_streak(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Consecutive-days streak of task_complete activity.

        Bonus 2026-04-19. Streak metric: how many consecutive UTC
        days have at least one task_complete event? Optionally
        scoped to a single session (None = project-wide). Returns
        current_streak, longest_streak, last_active_date, and the
        full set of active dates (capped at 30) for sparkline use.
        """
        from datetime import datetime

        active_dates: set[str] = set()
        try:
            if session_id:
                sessions_to_scan = [session_id]
            else:
                sessions = self.hub.sessions.list_sessions(project_root) or []
                sessions_to_scan = []
                for s in sessions:
                    sid = getattr(s, "session_id", None) or (
                        s.get("session_id") if isinstance(s, dict) else None
                    )
                    if sid:
                        sessions_to_scan.append(str(sid))
            for sid in sessions_to_scan:
                try:
                    entries = self.hub.sessions.read_journal(project_root, sid) or []
                except Exception:
                    continue
                for entry in entries:
                    if str(entry.get("action_kind", "")).strip().lower() != "task_complete":
                        continue
                    ts_raw = str(entry.get("timestamp", ""))
                    try:
                        clean = ts_raw.rstrip("Z")
                        if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                            clean = clean + "+00:00"
                        ts = datetime.fromisoformat(clean)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                    except Exception:
                        continue
                    active_dates.add(ts.date().isoformat())
        except Exception:
            return {"ok": False, "error": "scan_failed", "current_streak": 0}
        if not active_dates:
            return {
                "ok": True,
                "current_streak": 0,
                "longest_streak": 0,
                "last_active_date": None,
                "active_dates": [],
            }
        sorted_dates = sorted(active_dates)
        # Longest streak — walk sorted, count consecutive runs.
        longest = current = 1
        for i in range(1, len(sorted_dates)):
            prev = datetime.fromisoformat(sorted_dates[i - 1]).date()
            curr = datetime.fromisoformat(sorted_dates[i]).date()
            if (curr - prev).days == 1:
                current += 1
                longest = max(longest, current)
            else:
                current = 1
        # Current streak — backward from today.
        today = datetime.now(UTC).date()
        last = datetime.fromisoformat(sorted_dates[-1]).date()
        gap = (today - last).days
        if gap > 1:
            current_streak = 0
        else:
            current_streak = 1
            for i in range(len(sorted_dates) - 1, 0, -1):
                prev = datetime.fromisoformat(sorted_dates[i - 1]).date()
                curr = datetime.fromisoformat(sorted_dates[i]).date()
                if (curr - prev).days == 1:
                    current_streak += 1
                else:
                    break
        return {
            "ok": True,
            "session_id": session_id,
            "current_streak": current_streak,
            "longest_streak": longest,
            "last_active_date": sorted_dates[-1],
            "active_dates": sorted_dates[-30:],
        }

    def backlog_inbox(
        self,
        project_root: Path,
        limit: int = 20,
    ) -> dict[str, object]:
        """Top unchecked lane items across all session PLAN.md files.

        Bonus 2026-04-19. Walks every session's plans/PLAN.md and
        collects `- [ ] <text>` items as backlog candidates. Skips
        archive/ sessions and items in done-status sessions. Useful
        for "what should I work on next?" widgets — surfaces the
        actual inbox instead of forcing the operator to remember
        which sessions are still open.
        """
        sessions_root = project_root / ".MEMORY" / "sessions"
        if not sessions_root.is_dir():
            return {"ok": False, "items": [], "error": "sessions_root_not_found"}
        items: list[dict[str, str]] = []
        try:
            for session_dir in sorted(sessions_root.iterdir()):
                if not session_dir.is_dir():
                    continue
                sid = session_dir.name
                # Skip done sessions.
                try:
                    full = self.hub.sessions.read_session(project_root, sid)
                    status = ""
                    if full and getattr(full, "sections", None):
                        for line in full.sections.get("Status") or []:
                            cleaned = str(line).lstrip("- ").strip().lower()
                            if cleaned:
                                status = cleaned
                                break
                    if any(
                        status.startswith(t) for t in ("done", "abandoned", "closed", "archived")
                    ):
                        continue
                except Exception:
                    pass
                plan_file = session_dir / "plans" / "PLAN.md"
                if not plan_file.is_file():
                    continue
                try:
                    text = plan_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- [ ]"):
                        items.append(
                            {
                                "session_id": sid,
                                "item": stripped[5:].strip()[:200],
                            },
                        )
        except Exception:
            pass
        return {
            "ok": True,
            "count": len(items),
            "items": items[: int(limit)],
        }

    def task_velocity(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Tasks-per-day + first/last activity timestamps for a session.

        Bonus 2026-04-19. Operators want to know "is this session
        moving?" without grepping the journal. Counts task_lifecycle
        + task_complete entries, computes age in days, returns
        velocity metrics. Useful for stale-session detection and
        progress reports.
        """
        from datetime import datetime

        try:
            entries = self.hub.sessions.read_journal(project_root, session_id) or []
        except Exception:
            return {"ok": False, "session_id": session_id, "error": "journal_unavailable"}
        starts = 0
        completes = 0
        first_ts = None
        last_ts = None
        for entry in entries:
            kind = str(entry.get("action_kind", "")).strip().lower()
            if kind == "task_lifecycle":
                starts += 1
            elif kind == "task_complete":
                completes += 1
            ts_raw = str(entry.get("timestamp", ""))
            try:
                clean = ts_raw.rstrip("Z")
                if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                    clean = clean + "+00:00"
                ts = datetime.fromisoformat(clean)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        days_active = 0.0
        if first_ts and last_ts:
            days_active = max((last_ts - first_ts).total_seconds() / 86400.0, 0.0)
        # Velocity: completes per active-day, capped at 1-day floor so
        # a brand-new session doesn't divide by ~zero.
        velocity = round(completes / max(days_active, 1.0), 2) if completes else 0.0
        return {
            "ok": True,
            "session_id": session_id,
            "starts": starts,
            "completes": completes,
            "completion_ratio": round(completes / starts, 2) if starts else None,
            "days_active": round(days_active, 2),
            "first_activity": first_ts.isoformat() if first_ts else None,
            "last_activity": last_ts.isoformat() if last_ts else None,
            "tasks_per_day": velocity,
        }

    def recent_denials_for_session(
        self,
        project_root: Path,
        session_id: str,
        limit: int = 50,
    ) -> dict[str, object]:
        """Recent gate-block events scoped to a single session.

        Bonus 2026-04-19. denial_tier_stats is global; this filters
        per-session and returns ordered events {tier, timestamp,
        action_kind, target} for forensic "why did this lane keep
        getting blocked?" walks. Newest first.
        """
        try:
            stats = self.hub.execution.denial_tier_stats(
                project_root,
                session_id=session_id,
                limit_per_tier=int(limit),
            )
        except Exception:
            return {"ok": False, "session_id": session_id, "events": [], "error": "stats_failed"}
        events: list[dict[str, str]] = []
        for tier_name, tier_data in (stats.get("by_tier") or {}).items():
            for ts in tier_data.get("recent_timestamps") or []:
                events.append(
                    {
                        "tier": tier_name,
                        "timestamp": str(ts),
                    },
                )
        events.sort(key=lambda e: e["timestamp"], reverse=True)
        return {
            "ok": True,
            "session_id": session_id,
            "total_denials": int(stats.get("total_denials", 0)),
            "events": events[: int(limit)],
        }

    def hot_files_with_no_test(
        self,
        project_root: Path,
        heatmap_limit: int = 100,
        untested_limit: int = 200,
    ) -> dict[str, object]:
        """Files that are both heavily edited AND lack a matching test.

        Bonus 2026-04-19. Composite of files_touched_heatmap +
        find_untested_files. The interesting set is the intersection:
        files we keep editing without test coverage are the highest
        regression-risk surface in the project. Sorted by edits desc
        so the most-churned untested file ranks first.
        """
        try:
            heatmap = self.files_touched_heatmap(project_root, limit=int(heatmap_limit))
        except Exception:
            return {"ok": False, "files": [], "error": "heatmap_failed"}
        try:
            untested = self.hub.code.find_untested_files(
                project_root,
                limit=int(untested_limit),
            )
        except Exception:
            return {"ok": False, "files": [], "error": "untested_lookup_failed"}
        untested_paths = {u.get("path") for u in untested if u.get("path")}
        intersection: list[dict[str, object]] = []
        for hot in heatmap.get("files") or []:
            fp = hot.get("file")
            if not fp:
                continue
            # Exact match OR endswith — heatmap stores history paths,
            # untested uses indexed relative paths; match either.
            matched = fp in untested_paths or any(
                fp.endswith(p) or p.endswith(fp) for p in untested_paths
            )
            if matched:
                intersection.append(
                    {
                        "file": fp,
                        "edits": hot.get("edits", 0),
                        "session_count": hot.get("session_count", 0),
                        "last_edit": hot.get("last_edit", ""),
                    },
                )
        return {
            "ok": True,
            "count": len(intersection),
            "files": intersection,
        }

    def project_size_report(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """One-call size report: indexed files, total lines, sessions, edits.

        Bonus 2026-04-19. Operators want the project-scale numbers
        without grepping. Returns indexed_files, total_lines (sum),
        languages (top 5 by line count), session_count, edit_count
        (lifetime), denial_count (lifetime). Single read from each
        store; no per-file work.
        """
        report: dict[str, object] = {
            "ok": True,
            "project_root": str(project_root),
        }
        # Code index — files + lines + languages
        try:
            with self.hub.code.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(line_count), 0) AS lines FROM code_files",
                ).fetchone()
                report["indexed_files"] = int(row["n"] or 0)
                report["total_lines"] = int(row["lines"] or 0)
                lang_rows = conn.execute(
                    "SELECT language, COUNT(*) AS n, COALESCE(SUM(line_count), 0) AS lines "
                    "FROM code_files WHERE language IS NOT NULL "
                    "GROUP BY language ORDER BY lines DESC LIMIT 5",
                ).fetchall()
                report["languages"] = [
                    {
                        "language": row["language"],
                        "files": int(row["n"]),
                        "lines": int(row["lines"]),
                    }
                    for row in lang_rows
                ]
        except Exception:
            report["indexed_files"] = None
            report["total_lines"] = None
            report["languages"] = []
        # Sessions
        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
            report["session_count"] = len(sessions)
        except Exception:
            report["session_count"] = None
        # Edits + denials
        try:
            from .edit_history import EditHistoryStore

            with EditHistoryStore()._connect(project_root) as conn:  # type: ignore[attr-defined]
                report["edit_count_lifetime"] = int(
                    conn.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0],
                )
        except Exception:
            report["edit_count_lifetime"] = None
        try:
            stats = self.hub.execution.denial_tier_stats(project_root)
            report["denial_count_lifetime"] = int(stats.get("total_denials", 0))
        except Exception:
            report["denial_count_lifetime"] = None
        return report

    def task_open_or_blocked(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Sessions in non-terminal state — what's awaiting attention.

        Bonus 2026-04-19. Operators forget which sessions still need
        work. This walks list_sessions and returns: (a) `open` for
        active/in-progress, (b) `blocked` where Status mentions
        blocked OR Blockers section has content. Sorted by Last
        Updated descending so the most-recent demand attention first.
        """
        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
        except Exception:
            return {"ok": False, "open": [], "blocked": [], "error": "list_sessions_failed"}
        open_list: list[dict[str, str]] = []
        blocked_list: list[dict[str, str]] = []
        for s in sessions:
            sid = getattr(s, "session_id", None) or (
                s.get("session_id") if isinstance(s, dict) else None
            )
            if not sid:
                continue
            status = (
                getattr(s, "status", None) or (s.get("status") if isinstance(s, dict) else "") or ""
            )
            status_str = str(status or "").lower().strip("- ").strip()
            if any(status_str.startswith(t) for t in ("done", "abandoned", "closed", "archived")):
                continue
            entry: dict[str, str] = {
                "session_id": str(sid),
                "status": status_str or "unknown",
                "title": str(getattr(s, "title", "") or ""),
            }
            # Inspect Blockers section for content
            blockers_meaningful = False
            try:
                full = self.hub.sessions.read_session(project_root, sid)
                if full and getattr(full, "sections", None):
                    blockers = full.sections.get("Blockers") or []
                    for line in blockers:
                        cleaned = str(line).lstrip("- ").strip()
                        if cleaned and cleaned != "-":
                            blockers_meaningful = True
                            entry["first_blocker"] = cleaned[:120]
                            break
            except Exception:
                pass
            if blockers_meaningful or "block" in status_str:
                blocked_list.append(entry)
            else:
                open_list.append(entry)
        return {
            "ok": True,
            "open": open_list,
            "blocked": blocked_list,
            "open_count": len(open_list),
            "blocked_count": len(blocked_list),
        }

    def project_audit_snapshot(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """One-call composite of every guardrail + health signal.

        Bonus 2026-04-19. Replaces 8+ separate calls (project_health_score
        + reserved_filename_check + memory_shape_check +
        memory_content_check + list_archive_candidates + memory_stale_finder
        + denial_tier_stats + task_open_or_blocked) with a single audit
        pass for compliance/dashboard surfaces.

        Each component is best-effort — if one fails, the rest still
        return so the audit is robust to partial breakage.
        """
        snapshot: dict[str, object] = {
            "ok": True,
            "project_root": str(project_root),
        }

        def _safe(name: str, fn):
            try:
                snapshot[name] = fn()
            except Exception as exc:
                snapshot[name] = {"ok": False, "error": str(exc)[:200]}

        _safe("health", lambda: self.project_health_score(project_root))
        _safe("reserved_filenames", lambda: self.reserved_filename_check(project_root))
        _safe("memory_shape", lambda: self.memory_shape_check(project_root))
        _safe("memory_content", lambda: self.memory_content_check(project_root))
        _safe("memory_stale", lambda: self.memory_stale_finder(project_root))
        _safe("archive_candidates", lambda: self.list_archive_candidates(project_root))
        _safe("denial_tiers", lambda: self.hub.execution.denial_tier_stats(project_root))
        _safe("open_or_blocked", lambda: self.task_open_or_blocked(project_root))
        # Headline summary at the top so dashboards can render the banner
        # without walking the full tree.
        score = 0
        try:
            score = int((snapshot.get("health") or {}).get("score", 0))
        except Exception:
            pass
        violation_count = 0
        for key in ("reserved_filenames", "memory_shape", "memory_content"):
            comp = snapshot.get(key) or {}
            if isinstance(comp, dict):
                violation_count += int(comp.get("count", 0) or comp.get("total", 0) or 0)
        snapshot["headline"] = {
            "score": score,
            "total_violations": violation_count,
            "stale_sessions": int((snapshot.get("archive_candidates") or {}).get("count", 0)),
            "open_sessions": int((snapshot.get("open_or_blocked") or {}).get("open_count", 0)),
            "blocked_sessions": int(
                (snapshot.get("open_or_blocked") or {}).get("blocked_count", 0),
            ),
        }
        return snapshot

    def files_touched_heatmap(
        self,
        project_root: Path,
        limit: int = 50,
    ) -> dict[str, object]:
        """Cross-session edit-frequency heatmap of project files.

        Bonus 2026-04-19. files_touched_summary scopes per-session;
        this aggregates across the entire audit trail. Returns hot
        files by total edit count + per-session breakdown — useful
        for "which files do we keep churning on?" hotspot analysis.
        """
        from .edit_history import EditHistoryStore

        store = EditHistoryStore()
        try:
            edits = store.list_edits(project_root, limit=10000)
        except Exception:
            return {"ok": False, "files": [], "error": "edit_history_unavailable"}
        per_file: dict[str, dict[str, object]] = {}
        for e in edits:
            entry = per_file.setdefault(
                e.file_path,
                {
                    "edits": 0,
                    "sessions": set(),
                    "tools": set(),
                    "last_edit": "",
                },
            )
            entry["edits"] = int(entry["edits"]) + 1
            if e.session_id:
                entry["sessions"].add(e.session_id)
            if e.tool_name:
                entry["tools"].add(e.tool_name)
            if str(e.created_at) > str(entry["last_edit"]):
                entry["last_edit"] = e.created_at
        # Materialize sets to lists, sort by edit count.
        files = []
        for fp, stats in per_file.items():
            files.append(
                {
                    "file": fp,
                    "edits": int(stats["edits"]),
                    "session_count": len(stats["sessions"]),
                    "sessions": sorted(stats["sessions"])[:10],
                    "tools": sorted(stats["tools"])[:10],
                    "last_edit": stats["last_edit"],
                },
            )
        files.sort(key=lambda f: int(f["edits"]), reverse=True)
        return {
            "ok": True,
            "total_files": len(files),
            "files": files[: int(limit)],
        }

    def config_diff_from_default(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Operator-overridden settings — what differs from defaults.

        Bonus 2026-04-19. config_get returns values; this returns the
        delta between effective_config and the shipped defaults so
        operators can answer "what did I change?" without reading two
        configs side-by-side. Returns flat list of {path, current,
        default} entries; nested dicts walked recursively.
        """
        from .config import _DEFAULT_CONFIG  # type: ignore

        try:
            effective = self.hub.config_resolver.effective_config(
                project_root=project_root,
            )
        except Exception:
            try:
                effective = self.effective_config(project_root)
            except Exception:
                return {"ok": False, "diffs": [], "error": "config_unavailable"}
        diffs: list[dict[str, object]] = []

        def _walk(path: str, current: object, default: object) -> None:
            if isinstance(current, dict) and isinstance(default, dict):
                keys = set(current.keys()) | set(default.keys())
                for k in sorted(keys):
                    sub = f"{path}.{k}" if path else k
                    _walk(sub, current.get(k), default.get(k))
                return
            if current != default:
                diffs.append(
                    {
                        "path": path,
                        "current": current,
                        "default": default,
                    },
                )

        try:
            _walk("", effective, _DEFAULT_CONFIG)
        except Exception:
            pass
        # Skip obviously-noisy paths (action_hooks, languages bundles
        # — those ship as overlays, not user overrides).
        skip_prefixes = ("action_hooks.", "languages.", "interaction.")
        diffs = [d for d in diffs if not any(d["path"].startswith(p) for p in skip_prefixes)]
        diffs.sort(key=lambda d: str(d["path"]))
        return {
            "ok": True,
            "diff_count": len(diffs),
            "diffs": diffs,
        }

    def workflow_step_chronograph(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Action-kind sequence + per-kind timing stats for a session.

        Bonus 2026-04-19. Looks at journal entries in chrono order and
        returns: (a) the ordered sequence of action_kinds (compressed
        runs — repeated kinds collapse to N×kind), (b) per-kind stats
        (count, mean gap to next entry in seconds). Useful for spotting
        loops ("agent did test_progress 12 times in a row") or
        bottlenecks ("task_complete waited 47m on average").
        """
        from datetime import datetime

        try:
            entries = self.hub.sessions.read_journal(project_root, session_id) or []
        except Exception:
            return {"ok": False, "session_id": session_id, "error": "journal_unavailable"}
        # Parse timestamps once.
        parsed: list[tuple[datetime | None, str]] = []
        for entry in entries:
            ts_raw = str(entry.get("timestamp", ""))
            ts: datetime | None = None
            try:
                clean = ts_raw.rstrip("Z")
                if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                    clean = clean + "+00:00"
                ts = datetime.fromisoformat(clean)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                ts = None
            parsed.append((ts, str(entry.get("action_kind", "")).strip()))
        # Run-length compressed sequence.
        sequence: list[str] = []
        if parsed:
            cur_kind = parsed[0][1]
            cur_count = 1
            for _, kind in parsed[1:]:
                if kind == cur_kind:
                    cur_count += 1
                else:
                    sequence.append(f"{cur_count}x{cur_kind}" if cur_count > 1 else cur_kind)
                    cur_kind = kind
                    cur_count = 1
            sequence.append(f"{cur_count}x{cur_kind}" if cur_count > 1 else cur_kind)
        # Per-kind timing stats.
        per_kind: dict[str, dict[str, float | int]] = {}
        for i, (ts, kind) in enumerate(parsed):
            entry = per_kind.setdefault(kind, {"count": 0, "_gap_total": 0.0, "_gap_n": 0})
            entry["count"] += 1
            if ts is not None and i + 1 < len(parsed):
                next_ts = parsed[i + 1][0]
                if next_ts is not None:
                    entry["_gap_total"] += (next_ts - ts).total_seconds()
                    entry["_gap_n"] += 1
        for k, v in per_kind.items():
            n = v.pop("_gap_n", 0)
            total = v.pop("_gap_total", 0.0)
            v["mean_gap_seconds"] = round(total / n, 1) if n else None
        return {
            "ok": True,
            "session_id": session_id,
            "total_entries": len(parsed),
            "sequence_compressed": sequence,
            "per_kind": per_kind,
        }

    def denial_trend_24h(
        self,
        project_root: Path,
        bucket_hours: int = 1,
    ) -> dict[str, object]:
        """Rolling 24-hour denial-event histogram bucketed by hour.

        Bonus 2026-04-19. denial_tier_stats gives totals + recent
        timestamps; this turns those timestamps into a histogram so
        dashboards can chart "denials per hour over the last day"
        without parsing client-side. Bucket size adjustable
        (default 1h → 24 buckets).
        """
        from datetime import datetime, timedelta

        try:
            stats = self.hub.execution.denial_tier_stats(project_root, limit_per_tier=2000)
        except Exception:
            return {"ok": False, "buckets": [], "error": "execution_index_unavailable"}
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=24)
        bucket_size = max(1, int(bucket_hours)) * 3600
        # Init buckets keyed by hour-bucket index from cutoff.
        bucket_count = (24 * 3600) // bucket_size
        buckets: list[dict[str, object]] = []
        for i in range(bucket_count):
            bucket_start = cutoff + timedelta(seconds=i * bucket_size)
            buckets.append(
                {
                    "bucket_start": bucket_start.isoformat(),
                    "by_tier": {},
                    "total": 0,
                },
            )
        for tier_name, tier_data in (stats.get("by_tier") or {}).items():
            for ts_raw in tier_data.get("recent_timestamps") or []:
                try:
                    clean = str(ts_raw).rstrip("Z")
                    if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                        clean = clean + "+00:00"
                    ts = datetime.fromisoformat(clean)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                except Exception:
                    continue
                if ts < cutoff or ts > now:
                    continue
                idx = int((ts - cutoff).total_seconds() // bucket_size)
                if 0 <= idx < bucket_count:
                    bucket = buckets[idx]
                    bucket["by_tier"][tier_name] = bucket["by_tier"].get(tier_name, 0) + 1
                    bucket["total"] = int(bucket["total"]) + 1
        return {
            "ok": True,
            "now": now.isoformat(),
            "bucket_hours": int(bucket_hours),
            "bucket_count": bucket_count,
            "buckets": buckets,
            "total_in_window": sum(int(b["total"]) for b in buckets),
        }

    def session_export_markdown(
        self,
        project_root: Path,
        session_id: str,
        include_journal: bool = True,
        include_plan: bool = True,
        include_handoff: bool = True,
        max_journal_entries: int = 50,
    ) -> dict[str, object]:
        """Bundle a session into one self-contained markdown export.

        Bonus 2026-04-19. Sessions are 4-6 separate files (SESSION,
        PLAN, context, journal, handoff, agents/). Sharing one outside
        AIDOCS — for a PR description, a postmortem, an external
        review — meant manual copy-paste. This returns a single
        markdown blob with section headers per source file, suitable
        for GitHub gist / paste into a doc / archive snapshot.

        Returns {markdown: str, byte_count, sections_included}.
        """
        sections: list[tuple[str, str]] = []
        try:
            session = self.hub.sessions.read_session(project_root, session_id)
            if session and getattr(session, "sections", None):
                lines = [f"# Session: {session_id}\n"]
                for header, body in session.sections.items():
                    lines.append(f"## {header}\n")
                    for item in body:
                        lines.append(str(item))
                    lines.append("")
                sections.append(("session", "\n".join(lines)))
        except Exception:
            pass
        if include_plan:
            try:
                plan = self.hub.sessions.read_plan(project_root, session_id)
                if plan and getattr(plan, "sections", None):
                    lines = ["\n---\n", "# Plan\n"]
                    for header, body in plan.sections.items():
                        lines.append(f"## {header}\n")
                        for item in body:
                            lines.append(str(item))
                        lines.append("")
                    sections.append(("plan", "\n".join(lines)))
            except Exception:
                pass
        if include_journal:
            try:
                entries = self.hub.sessions.read_journal(project_root, session_id) or []
                if entries:
                    lines = ["\n---\n", "# Journal (newest first)\n"]
                    recent = list(reversed(entries))[: int(max_journal_entries)]
                    for entry in recent:
                        lines.append(
                            f"- `{entry.get('timestamp', '')}` "
                            f"**{entry.get('action_kind', '')}**: "
                            f"{entry.get('intent', '')} "
                            f"→ {entry.get('outcome', '')}",
                        )
                    sections.append(("journal", "\n".join(lines)))
            except Exception:
                pass
        if include_handoff:
            try:
                handoff = self.hub.sessions.read_handoff(project_root, session_id)
                if handoff and getattr(handoff, "sections", None):
                    lines = ["\n---\n", "# Handoff\n"]
                    for header, body in handoff.sections.items():
                        lines.append(f"## {header}\n")
                        for item in body:
                            lines.append(str(item))
                        lines.append("")
                    sections.append(("handoff", "\n".join(lines)))
            except Exception:
                pass
        markdown = "".join(text for _, text in sections)
        return {
            "ok": True,
            "session_id": session_id,
            "markdown": markdown,
            "byte_count": len(markdown.encode("utf-8")),
            "sections_included": [name for name, _ in sections],
        }

    def task_breadcrumbs(
        self,
        project_root: Path,
        session_id: str,
        last_n: int = 10,
    ) -> dict[str, object]:
        """Recent decisions/state-changes for a session in compact form.

        Bonus 2026-04-19. session_timeline returns ALL lifecycle events;
        breadcrumbs compress to the last N "what changed" markers
        suitable for a hover tooltip / status-bar long-press: each
        entry is {when_relative, action, intent_short}. Designed for
        UI that says "10 minutes ago: completed login flow tests".
        """
        from datetime import datetime

        try:
            entries = self.hub.sessions.read_journal(project_root, session_id) or []
        except Exception:
            return {
                "ok": False,
                "session_id": session_id,
                "trail": [],
                "error": "journal_unavailable",
            }
        # Newest first, limited.
        recent = list(reversed(entries))[: int(last_n)]
        now = datetime.now(UTC)
        trail: list[dict[str, str]] = []
        for entry in recent:
            ts_raw = str(entry.get("timestamp", ""))
            when_relative = "unknown"
            try:
                # Strip trailing Z; fromisoformat handles +00:00 in 3.11+.
                clean = ts_raw.rstrip("Z")
                if clean and "+" not in clean[-6:] and "-" not in clean[-6:]:
                    clean = clean + "+00:00"
                ts = datetime.fromisoformat(clean)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                delta = (now - ts).total_seconds()
                if delta < 60:
                    when_relative = "just now"
                elif delta < 3600:
                    when_relative = f"{int(delta // 60)}m ago"
                elif delta < 86400:
                    when_relative = f"{int(delta // 3600)}h ago"
                else:
                    when_relative = f"{int(delta // 86400)}d ago"
            except Exception:
                pass
            intent = str(entry.get("intent", "")).strip()
            trail.append(
                {
                    "when_relative": when_relative,
                    "timestamp": ts_raw,
                    "action": str(entry.get("action_kind", "")),
                    "intent_short": intent[:80] + ("…" if len(intent) > 80 else ""),
                },
            )
        return {"ok": True, "session_id": session_id, "trail": trail, "count": len(trail)}

    def memory_content_check(
        self,
        project_root: Path,
        memory_root: str = ".MEMORY",
    ) -> dict[str, object]:
        """Layer 7 G4-G8 memory guardrails in one traversal.

        Bonus 2026-04-19. Five content-shape detectors that catch
        common memory-misuse patterns:

        - **G4 tabular code-inventory**: rules/standards docs that
          devolve into `| Path | Lines | Owner |` tables — the agent
          dumped a directory listing into a rule. Code inventory
          belongs in the index, not memory.
        - **G5 feedback-log**: docs that look like feedback transcripts
          (`### User said:`, `> User: ...`, `## Reply:`).
        - **G6 bug-report**: docs structured as bug reports outside
          sessions/ (`## Steps to reproduce`, `## Expected vs actual`).
        - **G7 wrong-project**: docs referencing project names that
          don't match the current project (heuristic: search for the
          word "project" near a name that isn't the current one).
          Conservative — only flags the obvious cases.
        - **G8 trivial/duplicate**: rule files smaller than 100 bytes
          OR sharing a basename with another rule file.

        Returns per-rule violation lists plus a summary count. Run
        alongside reserved_filename_check + memory_shape_check for
        the full Layer 7 sweep.
        """
        import re

        mem_root = project_root / memory_root
        if not mem_root.is_dir():
            return {
                "ok": False,
                "violations": [],
                "error": "memory_root_not_found",
            }
        skip_dirs = {"sessions", "archive", ".aidocs", ".index"}
        # Patterns
        g4_pattern = re.compile(
            r"^\|\s*(?:path|file|module|class|symbol|name)\s*\|",
            re.IGNORECASE | re.MULTILINE,
        )
        g5_pattern = re.compile(
            r"^(?:>\s*(?:user|operator|me)\s*:|#+\s+(?:user said|reply|"
            r"feedback|response from|chat log)\b)",
            re.IGNORECASE | re.MULTILINE,
        )
        g6_pattern = re.compile(
            r"^#+\s+(?:steps to reproduce|expected vs actual|expected\s*:|"
            r"actual\s*:|reproduction|repro steps?)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        project_name = project_root.name.lower()
        # Detect references to OTHER projects — looks for "project X"
        # phrasing where X is not our project name.
        g7_pattern = re.compile(r"\bproject\s+([A-Za-z0-9_-]{3,40})\b", re.IGNORECASE)
        g4: list[dict[str, str]] = []
        g5: list[dict[str, str]] = []
        g6: list[dict[str, str]] = []
        g7: list[dict[str, str]] = []
        g8: list[dict[str, str]] = []
        # G8 second pass — basename collisions across the tree.
        basenames: dict[str, list[str]] = {}
        for path in mem_root.rglob("*.md"):
            try:
                rel = path.relative_to(mem_root)
            except ValueError:
                continue
            parts = rel.parts
            if any(p in skip_dirs for p in parts):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel_str = str(rel).replace("\\", "/")
            # G4
            if g4_pattern.search(text):
                g4.append({"path": rel_str, "kind": "tabular_code_inventory"})
            # G5
            if g5_pattern.search(text):
                g5.append({"path": rel_str, "kind": "feedback_log"})
            # G6
            if g6_pattern.search(text):
                g6.append({"path": rel_str, "kind": "bug_report"})
            # G7 — conservative: only flag if "project X" appears AND X
            # isn't the current project name AND isn't a common English
            # noun we actually use in rule prose ("project memory",
            # "project state", "project agents", etc.). The stopword
            # list below came from real false-positives in the 2026-04-19
            # audit — treat it as the minimum viable list, extend as
            # new false-positives surface.
            _G7_STOPWORDS = {
                "root",
                "name",
                "the",
                "this",
                "that",
                "a",
                "an",
                # English words that legitimately follow "project" in
                # durable rules without implying another project.
                "memory",
                "memorystore",
                "state",
                "status",
                "setup",
                "structure",
                "updates",
                "workflow",
                "workflows",
                "agents",
                "agent",
                "routers",
                "router",
                "routing",
                "index",
                "indexes",
                "indices",
                "issue",
                "issues",
                "links",
                "link",
                "collaboration",
                "behavior",
                "overrides",
                "override",
                "targeting",
                "knowledge",
                "tabs",
                "tab",
                "session",
                "sessions",
                "plans",
                "plan",
                "rules",
                "rule",
                "bundle",
                "bundles",
                "code",
                "codes",
                "detector",
                "detectors",
                "acls",
                "acl",
                "blocks",
                "block",
                "bootstrap",
                "resolution",
                "judge",
                "judges",
                # Found in the 2026-04-19 re-audit after MCP restart.
                "other",
                "owner",
                "work",
                "context",
                "task",
                "tasks",
                "scope",
                "goal",
                "goals",
                "artifacts",
                "artifact",
            }
            other_projects = {
                m.group(1).lower()
                for m in g7_pattern.finditer(text)
                if m.group(1).lower() != project_name and m.group(1).lower() not in _G7_STOPWORDS
            }
            if other_projects:
                g7.append(
                    {
                        "path": rel_str,
                        "kind": "wrong_project_reference",
                        "names": ", ".join(sorted(other_projects)[:5]),
                    },
                )
            # G8 part 1 — trivial size
            if size < 100:
                g8.append(
                    {
                        "path": rel_str,
                        "kind": "trivial_size",
                        "size_bytes": str(size),
                    },
                )
            # G8 part 2 — collect basenames for duplicate detection
            basenames.setdefault(path.name, []).append(rel_str)
        # G8 finalize — report basenames with >1 location (excludes
        # already-trivial files to avoid double-flagging). A basename
        # that co-exists in distinct top-level dirs (rules/workflow.md
        # vs system/workflow.md vs domains/workflow.md) is NOT drift;
        # each role dir serves a distinct purpose. Only flag true drift:
        # same basename inside the same top-level dir (copy-paste or
        # ambiguous-scope failure).
        seen_paths = {v["path"] for v in g8}
        for name, paths_list in basenames.items():
            if len(paths_list) <= 1:
                continue
            # Group by top-level dir; only flag groups with >1 entry.
            by_top: dict[str, list[str]] = {}
            for p in paths_list:
                top = p.split("/", 1)[0] if "/" in p else ""
                by_top.setdefault(top, []).append(p)
            for top, same_dir_paths in by_top.items():
                if len(same_dir_paths) <= 1:
                    continue
                for p in same_dir_paths:
                    if p in seen_paths:
                        continue
                    g8.append(
                        {
                            "path": p,
                            "kind": "duplicate_basename",
                            "name": name,
                        },
                    )
        # Stable sort
        for lst in (g4, g5, g6, g7, g8):
            lst.sort(key=lambda v: v["path"])
        return {
            "ok": True,
            "memory_root": memory_root,
            "g4_violations": g4,
            "g5_violations": g5,
            "g6_violations": g6,
            "g7_violations": g7,
            "g8_violations": g8,
            "total": len(g4) + len(g5) + len(g6) + len(g7) + len(g8),
        }

    def memory_shape_check(
        self,
        project_root: Path,
        memory_root: str = ".MEMORY",
    ) -> dict[str, object]:
        """Layer 7 G2 + G3: detect agent-exploration headers and plan-shape misuse.

        Bonus 2026-04-19. Two related guardrails folded into one
        scanner because they share traversal:

        - **G2 agent-exploration headers**: rules/standards docs that
          look like an agent's exploration log (`# Investigating X`,
          `# Reading Y`, `# Trying Z`) instead of durable rules. These
          slip in when an agent journals into the wrong file.
        - **G3 plan/phase-shape**: files outside `sessions/<id>/plans/`
          whose first heading matches a plan/phase shape (`# Plan`,
          `## Phase 1`, `## Steps`). Plans that drift out of session
          scope become orphan roadmaps.

        Returns separate g2/g3 lists so dashboards can theme them; an
        empty `violations` summary list means clean.
        """
        import re

        mem_root = project_root / memory_root
        if not mem_root.is_dir():
            return {
                "ok": False,
                "g2_violations": [],
                "g3_violations": [],
                "error": "memory_root_not_found",
            }
        # Header signatures
        g2_pattern = re.compile(
            r"^#+\s+(investigating|reading|trying|exploring|debugging|"
            r"checking|figuring|attempting|wip|todo:|notes?:)\b",
            re.IGNORECASE,
        )
        g3_pattern = re.compile(
            r"^#+\s+(plan|phase\s*\d|step\s*\d|steps|tasks|backlog|"
            r"roadmap|milestone)\b",
            re.IGNORECASE,
        )
        # Skip dirs where these shapes are EXPECTED
        skip_for_g2 = {"sessions", "archive", ".aidocs", ".index"}
        skip_for_g3 = {"sessions", "archive", ".aidocs", ".index", "roadmaps"}
        g2_violations: list[dict[str, str]] = []
        g3_violations: list[dict[str, str]] = []
        for path in mem_root.rglob("*.md"):
            try:
                rel = path.relative_to(mem_root)
            except ValueError:
                continue
            parts = rel.parts
            try:
                with path.open("r", encoding="utf-8") as fh:
                    # Read at most first 20 non-empty lines — enough to
                    # spot the leading heading without slurping huge files.
                    head_lines: list[str] = []
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        head_lines.append(line)
                        if len(head_lines) >= 20:
                            break
            except OSError:
                continue
            for line in head_lines:
                if not line.startswith("#"):
                    continue
                if g2_pattern.match(line) and not any(p in skip_for_g2 for p in parts):
                    g2_violations.append(
                        {
                            "path": str(rel).replace("\\", "/"),
                            "header": line[:120],
                            "kind": "agent_exploration_header",
                        },
                    )
                    break
                if g3_pattern.match(line) and not any(p in skip_for_g3 for p in parts):
                    g3_violations.append(
                        {
                            "path": str(rel).replace("\\", "/"),
                            "header": line[:120],
                            "kind": "plan_shape_outside_sessions",
                        },
                    )
                    break
        g2_violations.sort(key=lambda v: v["path"])
        g3_violations.sort(key=lambda v: v["path"])
        return {
            "ok": True,
            "memory_root": memory_root,
            "g2_violations": g2_violations,
            "g3_violations": g3_violations,
            "g2_count": len(g2_violations),
            "g3_count": len(g3_violations),
            "total": len(g2_violations) + len(g3_violations),
        }

    def edit_rollback_batch(
        self,
        project_root: Path,
        session_id: str | None = None,
        file_path: str | None = None,
        last_n: int = 10,
        dry_run: bool = True,
    ) -> dict[str, object]:
        """Roll back the last N edits in a session/file in one call.

        Bonus 2026-04-19. edit_rollback handles one edit at a time;
        scripted operator unwinds (e.g. "undo the last 5 changes that
        broke the build") had to chain N calls. Returns the planned
        edit_ids in dry-run mode (default) so the operator can confirm
        before destructive execution.
        """
        from .edit_history import EditHistoryStore

        store = EditHistoryStore()
        edits = store.list_edits(
            project_root,
            file_path=file_path,
            session_id=session_id,
            limit=int(last_n),
        )
        planned = [
            {
                "edit_id": e.edit_id,
                "file_path": e.file_path,
                "tool_name": e.tool_name,
                "created_at": e.created_at,
            }
            for e in edits
        ]
        rolled_back: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        if not dry_run:
            for plan in planned:
                try:
                    result = store.rollback(project_root, plan["edit_id"])
                    if result and getattr(result, "success", False):
                        rolled_back.append(plan)
                    else:
                        skipped.append(
                            {
                                **plan,
                                "reason": str(getattr(result, "message", "unknown")),
                            },
                        )
                except Exception as exc:
                    skipped.append({**plan, "reason": f"exception: {exc}"})
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "planned": planned,
            "rolled_back": rolled_back,
            "skipped": skipped,
            "planned_count": len(planned),
            "rolled_back_count": len(rolled_back),
            "skipped_count": len(skipped),
        }

    def reserved_filename_check(
        self,
        project_root: Path,
        memory_root: str = ".MEMORY",
    ) -> dict[str, object]:
        """Layer 7 G1: catch reserved-filename violations under .MEMORY.

        Bonus 2026-04-19. Memory guardrail: protect canonical filenames
        from being silently overwritten by random files. The convention
        reserves names like INDEX.md, SESSION.md, PLAN.md, and
        agent-specific globals. A file matching one of those names but
        located outside its expected directory signals an accidental
        scaffold or copy-paste mistake.

        Returns {violations: [{path, expected_location, kind}]} sorted
        alphabetically. Empty list = clean.
        """
        # Reserved names → expected parent-directory pattern.
        # `INDEX.md` belongs at .MEMORY root; `SESSION.md`/`PLAN.md`/
        # `journal.md`/`context.md` belong inside .MEMORY/sessions/<id>/
        # (or its plans/ subdir for PLAN.md). Anything matching outside
        # those locations gets flagged.
        rules = {
            "INDEX.md": "directly under .MEMORY/",
            "SESSION.md": "inside .MEMORY/sessions/<id>/",
            "PLAN.md": "inside .MEMORY/sessions/<id>/plans/",
            "journal.md": "inside .MEMORY/sessions/<id>/",
            "context.md": "inside .MEMORY/sessions/<id>/",
        }
        mem_root = project_root / memory_root
        if not mem_root.is_dir():
            return {
                "ok": False,
                "violations": [],
                "error": "memory_root_not_found",
            }
        violations: list[dict[str, str]] = []
        # Dirs whose contents are scaffold/template — files with
        # canonical names here are legitimate (they get copied into
        # session dirs on create_session). Skip silently.
        template_dirs = {".aidocs", ".index"}
        for path in mem_root.rglob("*.md"):
            name = path.name
            if name not in rules:
                continue
            try:
                rel = path.relative_to(mem_root)
            except ValueError:
                continue
            if any(part in template_dirs for part in rel.parts):
                continue
            parts = rel.parts
            ok = False
            if name == "INDEX.md":
                # Must be exactly .MEMORY/INDEX.md (1 segment).
                ok = len(parts) == 1
            elif name in ("SESSION.md", "journal.md", "context.md"):
                # Must be .MEMORY/sessions/<id>/<name> (3 segments)
                # or .MEMORY/archive/sessions/<id>/<name> (4 segments).
                ok = (len(parts) == 3 and parts[0] == "sessions") or (
                    len(parts) == 4 and parts[:2] == ("archive", "sessions")
                )
            elif name == "PLAN.md":
                # .MEMORY/sessions/<id>/plans/PLAN.md (4 segments) or
                # archive/sessions/<id>/plans/PLAN.md (5 segments).
                ok = (len(parts) == 4 and parts[0] == "sessions" and parts[2] == "plans") or (
                    len(parts) == 5 and parts[:2] == ("archive", "sessions") and parts[3] == "plans"
                )
            if not ok:
                violations.append(
                    {
                        "path": str(rel).replace("\\", "/"),
                        "name": name,
                        "expected_location": rules[name],
                    },
                )
        violations.sort(key=lambda v: v["path"])
        return {
            "ok": True,
            "memory_root": memory_root,
            "violations": violations,
            "count": len(violations),
        }

    def memory_stale_finder(
        self,
        project_root: Path,
        stale_after_days: int = 90,
        memory_root: str = ".MEMORY",
    ) -> dict[str, object]:
        """Memory files (rules/standards/lessons) untouched for N days.

        Bonus 2026-04-19. Memory drifts: rules written months ago for
        a now-dead workflow are worse than no rules — they actively
        mislead. This walks the project's .MEMORY tree (excluding
        sessions/, archive/, .aidocs/, .index/) and returns files with
        mtime older than the cutoff, sorted oldest-first so review
        prioritization is obvious.
        """
        from datetime import datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=int(stale_after_days))
        cutoff_ts = cutoff.timestamp()
        skip_dirs = {"sessions", "archive", ".aidocs", ".index", "config"}
        mem_root = project_root / memory_root
        if not mem_root.is_dir():
            return {
                "ok": False,
                "stale_after_days": int(stale_after_days),
                "candidates": [],
                "count": 0,
                "error": "memory_root_not_found",
            }
        candidates: list[dict[str, object]] = []
        for path in mem_root.rglob("*.md"):
            try:
                rel = path.relative_to(mem_root)
            except ValueError:
                continue
            parts = rel.parts
            if any(part in skip_dirs for part in parts):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff_ts:
                continue
            candidates.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "mtime_iso": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
                    "stale_days": round(
                        (datetime.now(UTC).timestamp() - mtime) / 86400,
                        1,
                    ),
                },
            )
        candidates.sort(key=lambda c: float(c.get("stale_days", 0)), reverse=True)
        return {
            "ok": True,
            "stale_after_days": int(stale_after_days),
            "memory_root": memory_root,
            "candidates": candidates,
            "count": len(candidates),
        }

    def edit_diff_summary(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Per-file diff stats for a session: edits, lines added/removed.

        Bonus 2026-04-19. files_touched_summary returns net_chars per
        file (good for sorting); this adds line-level deltas computed
        from the audit trail's old_content/new_content. Useful for
        PR-style "+47 -12 in 3 files" headlines.
        """
        try:
            from .edit_history import EditHistoryStore

            edits = EditHistoryStore().list_edits(
                project_root,
                session_id=session_id,
                limit=1000,
            )
        except Exception:
            return {
                "ok": False,
                "session_id": session_id,
                "files": [],
                "totals": {},
                "error": "edit_history_unavailable",
            }
        per_file: dict[str, dict[str, int]] = {}
        for e in edits:
            old_lines = (e.old_content or "").count("\n") + (
                1 if (e.old_content or "").strip() else 0
            )
            new_lines = (e.new_content or "").count("\n") + (
                1 if (e.new_content or "").strip() else 0
            )
            entry = per_file.setdefault(
                e.file_path,
                {
                    "edits": 0,
                    "lines_added": 0,
                    "lines_removed": 0,
                },
            )
            entry["edits"] += 1
            # Conservative diff approximation — actual SequenceMatcher
            # would give better numbers but is too heavy for a summary
            # surface. Treat the asymmetric line delta as net add/remove.
            delta = new_lines - old_lines
            if delta > 0:
                entry["lines_added"] += delta
            elif delta < 0:
                entry["lines_removed"] += -delta
        files_list = [
            {"file": fp, **stats}
            for fp, stats in sorted(
                per_file.items(),
                key=lambda kv: kv[1]["edits"] + kv[1]["lines_added"] + kv[1]["lines_removed"],
                reverse=True,
            )
        ]
        totals = {
            "files": len(files_list),
            "edits": sum(f["edits"] for f in files_list),
            "lines_added": sum(f["lines_added"] for f in files_list),
            "lines_removed": sum(f["lines_removed"] for f in files_list),
        }
        return {
            "ok": True,
            "session_id": session_id,
            "files": files_list,
            "totals": totals,
        }

    def list_archive_candidates(
        self,
        project_root: Path,
        stale_after_days: int = 30,
    ) -> dict[str, object]:
        """Sessions that look ripe for archive: status=done + no recent edits.

        Bonus 2026-04-19. Operators accumulate finished sessions that
        nobody remembers to archive. This surfaces them as a sortable
        list so the dashboard can prompt "archive these N stale done
        sessions?" without scanning by hand.

        Heuristic: session.status starts with "done"/"abandoned"/"closed"
        AND no edit_history rows for that session newer than the cutoff.
        Returns ordered by staleness (oldest-last-edit first).
        """
        from datetime import datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=int(stale_after_days))
        cutoff_iso = cutoff.isoformat()
        candidates: list[dict[str, object]] = []
        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
        except Exception:
            return {"ok": False, "candidates": [], "error": "session_list_failed"}
        try:
            from .edit_history import EditHistoryStore

            edit_store = EditHistoryStore()
        except Exception:
            edit_store = None
        for s in sessions:
            sid = getattr(s, "session_id", None) or (
                s.get("session_id") if isinstance(s, dict) else None
            )
            status = getattr(s, "status", None) or (s.get("status") if isinstance(s, dict) else "")
            status_str = str(status or "").lower().strip("- ").strip()
            if not any(status_str.startswith(t) for t in ("done", "abandoned", "closed")):
                continue
            last_edit_at = None
            if edit_store is not None and sid:
                try:
                    edits = edit_store.list_edits(
                        project_root,
                        session_id=sid,
                        limit=1,
                    )
                    if edits:
                        last_edit_at = edits[0].created_at
                except Exception:
                    last_edit_at = None
            if last_edit_at and str(last_edit_at) > cutoff_iso:
                continue
            candidates.append(
                {
                    "session_id": sid,
                    "status": status_str,
                    "title": str(getattr(s, "title", "") or ""),
                    "last_edit_at": str(last_edit_at) if last_edit_at else None,
                    "stale_days": int(stale_after_days),
                },
            )
        candidates.sort(
            key=lambda c: str(c.get("last_edit_at") or ""),
            reverse=False,
        )
        return {
            "ok": True,
            "stale_after_days": int(stale_after_days),
            "candidates": candidates,
            "count": len(candidates),
        }

    def project_health_score(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Composite 0-100 health score from index/coverage/sessions/denials.

        Bonus 2026-04-19. Operators want a single number to track
        "is this project getting healthier?" over time. Score weights:
        - 30 pts: indexed-files presence (binary: indexed at all)
        - 25 pts: test coverage (1 - untested_ratio)
        - 25 pts: session hygiene (1 - stale_done_ratio, 30d cutoff)
        - 20 pts: low denial rate (cap at 100 denials = 0 pts)

        Each component is reported individually so the dashboard can
        chart the breakdown, not just the headline.
        """
        score = 0.0
        breakdown: dict[str, object] = {}

        # Index presence (30 pts) — having an index at all is the
        # foundational signal; a project with zero indexed files can't
        # even be searched, never mind tested.
        try:
            idx = self.hub.code.code_status(project_root)
            count = int(
                idx.get("code_files") or idx.get("file_count") or idx.get("total_files") or 0,
            )
            if count > 0:
                score += 30
                breakdown["index"] = {"score": 30, "files": count}
            else:
                breakdown["index"] = {"score": 0, "files": 0}
        except Exception:
            breakdown["index"] = {"score": 0, "files": 0, "error": True}

        # Test coverage (25 pts) — 1 - (untested / total_source).
        try:
            untested = self.hub.code.find_untested_files(project_root, limit=1000)
            untested_count = len(untested)
            with self.hub.code.connect(project_root) as conn:
                total_src = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM code_files "
                        "WHERE (role IS NULL OR role NOT IN ('test', 'fixture'))",
                    ).fetchone()[0]
                    or 0,
                )
            if total_src > 0:
                ratio = max(0.0, 1.0 - (untested_count / total_src))
                cov_pts = round(25 * ratio, 1)
                score += cov_pts
                breakdown["coverage"] = {
                    "score": cov_pts,
                    "untested": untested_count,
                    "total_source": total_src,
                }
            else:
                breakdown["coverage"] = {
                    "score": 0,
                    "untested": 0,
                    "total_source": 0,
                }
        except Exception:
            breakdown["coverage"] = {"score": 0, "error": True}

        # Session hygiene (25 pts) — done sessions older than 30d that
        # nobody archived count against the score.
        try:
            archive = self.list_archive_candidates(project_root, stale_after_days=30)
            stale_count = int(archive.get("count", 0))
            sessions = self.hub.sessions.list_sessions(project_root) or []
            total_sessions = len(sessions)
            if total_sessions > 0:
                ratio = max(0.0, 1.0 - (stale_count / total_sessions))
                hyg_pts = round(25 * ratio, 1)
                score += hyg_pts
                breakdown["sessions"] = {
                    "score": hyg_pts,
                    "stale_done": stale_count,
                    "total": total_sessions,
                }
            else:
                # No sessions yet — neutral 25 (nothing to be stale).
                score += 25
                breakdown["sessions"] = {"score": 25, "total": 0}
        except Exception:
            breakdown["sessions"] = {"score": 0, "error": True}

        # Denial pressure (20 pts) — cap at 100 total denials = 0 pts.
        # A project with hundreds of gate-blocks is either misconfigured
        # or under attack; either way the operator should look.
        try:
            stats = self.hub.execution.denial_tier_stats(project_root)
            total = int(stats.get("total_denials", 0))
            denial_ratio = min(1.0, total / 100.0)
            den_pts = round(20 * (1 - denial_ratio), 1)
            score += den_pts
            breakdown["denials"] = {"score": den_pts, "total": total}
        except Exception:
            breakdown["denials"] = {"score": 0, "error": True}

        return {
            "ok": True,
            "score": round(score, 1),
            "max_score": 100,
            "breakdown": breakdown,
        }

    def project_freshness(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """Single-call dashboard heartbeat: index, edits, denials, sessions.

        Bonus 2026-04-19. Gap: dashboards make 4-5 separate calls to
        compose a status banner (index_status, edit_history_list,
        denial_tier_stats, project_list_sessions). This one collapses
        them into a single read so the status bar refresh is one
        round-trip.
        """
        from datetime import datetime, timedelta

        out: dict[str, object] = {
            "ok": True,
            "project_root": str(project_root),
        }
        # Index freshness
        try:
            idx_status = self.hub.code_index.code_status(project_root)
            out["indexed_files"] = (
                idx_status.get("code_files")
                or idx_status.get("file_count")
                or idx_status.get("total_files")
                or 0
            )
            out["last_index_sync"] = idx_status.get("last_sync") or idx_status.get("synced_at")
        except Exception:
            out["indexed_files"] = None
            out["last_index_sync"] = None
        # Edit history (last 24h)
        try:
            from .edit_history import EditHistoryStore

            cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            edits = EditHistoryStore().list_edits(project_root, limit=500)
            out["edit_count_24h"] = sum(1 for e in edits if str(e.created_at) > cutoff)
        except Exception:
            out["edit_count_24h"] = None
        # Denial blocks (all-time, scoped to total)
        try:
            stats = self.hub.execution.denial_tier_stats(project_root)
            out["total_denials"] = stats.get("total_denials", 0)
            out["denial_tiers_active"] = len(stats.get("by_tier") or {})
        except Exception:
            out["total_denials"] = None
            out["denial_tiers_active"] = None
        # Active sessions
        try:
            sessions = self.hub.sessions.list_sessions(project_root) or []
            out["session_count_total"] = len(sessions)
            active = 0
            for s in sessions:
                status = ""
                if hasattr(s, "status"):
                    status = str(s.status or "")
                elif isinstance(s, dict):
                    status = str(s.get("status", ""))
                if "active" in status.lower():
                    active += 1
            out["session_count_active"] = active
        except Exception:
            out["session_count_total"] = None
            out["session_count_active"] = None
        return out

    def session_timeline(
        self,
        project_root: Path,
        session_id: str,
        action_kinds: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Chronological timeline of task-lifecycle events for a session.

        Bonus 2026-04-19. Gap: journal entries are flat lines; agents
        and dashboards needed a structured "what happened when" view
        without grepping the markdown. Default filter is the lifecycle
        triad (task_lifecycle/task_progress/task_complete) — pass
        action_kinds=[] to get everything.
        """
        try:
            entries = self.hub.sessions.read_journal(project_root, session_id) or []
        except Exception:
            return {
                "ok": False,
                "session_id": session_id,
                "events": [],
                "error": "session_not_found",
            }
        if action_kinds is None:
            action_kinds = ["task_lifecycle", "task_progress", "task_complete"]
        wanted = {k.strip().lower() for k in action_kinds if k.strip()}
        events: list[dict[str, str]] = []
        for entry in entries:
            kind = str(entry.get("action_kind", "")).strip().lower()
            if wanted and kind not in wanted:
                continue
            events.append(
                {
                    "timestamp": str(entry.get("timestamp", "")),
                    "action_kind": kind,
                    "intent": str(entry.get("intent", "")),
                    "outcome": str(entry.get("outcome", "")),
                },
            )
        # Newest first (read_journal already returns chrono — flip for UI).
        events.sort(key=lambda e: e["timestamp"], reverse=True)
        return {
            "ok": True,
            "session_id": session_id,
            "total_matched": len(events),
            "events": events[: int(limit) if limit else None],
        }

    def _task_actor_identity(self, project_root: Path) -> tuple[str, str, bool]:
        """Resolve actor ownership without consulting shared session task state.

        Delegates to the shared #463/#457/#483 seam so every task-slot
        consumer (task_*, the universal gate, event attribution, todo
        ownership) agrees on the caller's (actor, lane, worker) triple.
        #483: non-workers with a host-derived identity get their OWN
        actor id too (lane_id="") so their task slot is per-actor, not
        the shared session-level one.
        """
        from .task_actor_identity import resolve_slot_actor

        return resolve_slot_actor(project_root)

    @staticmethod
    def _worker_task_store():
        from .todo_state_store import ActorTaskStateStore

        return ActorTaskStateStore()

    def _worker_task_begin(
        self,
        project_root: Path,
        session_id: str,
        actor_id: str,
        lane_id: str,
        *,
        goal: str | None,
        state: list[str] | None,
        upcoming: list[str] | None,
        partial_goals: list[str] | None,
        end_goal: str | None,
        blockers: list[str] | None,
        relevant_files: list[str] | None,
        relevant_commands: list[str] | None,
        relevant_snippets: list[str] | None,
        session_facts: list[str] | None,
        constraints: list[str] | None,
    ) -> dict[str, object]:
        if not actor_id:
            return {
                "ok": False,
                "blocked": True,
                "status": "missing_actor_identity",
                "session_id": session_id,
                "error": "worker task_begin requires canonical agent_context_id",
            }
        import hashlib as _hashlib
        import time as _time

        task_id = "t_" + _hashlib.sha256(
            f"{session_id}|{actor_id}|{lane_id}|{goal or ''}|{int(_time.time())}".encode(),
        ).hexdigest()[:16]
        worker_state: dict[str, object] = {
            "goal": goal,
            "state": list(state or []),
            "upcoming": list(upcoming or []),
            "partial_goals": list(partial_goals or []),
            "end_goal": end_goal,
            "blockers": list(blockers or []),
            "relevant_files": list(relevant_files or []),
            "relevant_commands": list(relevant_commands or []),
            "relevant_snippets": list(relevant_snippets or []),
            "session_facts": list(session_facts or []),
            "constraints": list(constraints or []),
        }
        stored = self._worker_task_store().set(
            project_root,
            session_id,
            actor_id,
            lane_id,
            task_id=task_id,
            state=worker_state,
            status="active",
        )
        # #457 lane auto-bind: the agent's first governed task_begin
        # guarantees a registry binding for (session, lane, actor) —
        # audited on first creation, idempotent after. Best-effort so a
        # registry hiccup never fails the begin. NOTE: the worker path
        # deliberately writes NO .MEMORY/sessions/<session_id>/ files —
        # that was the #457 stale-bind / orphan-session-dir residue.
        try:
            from .task_actor_identity import ensure_worker_lane_binding

            lane_binding = ensure_worker_lane_binding(
                project_root,
                session_id,
                actor_id,
                lane_id,
                source="task_begin",
            )
        except Exception:
            lane_binding = {"bound": False}
        return {
            "ok": True,
            "worker_scoped": True,
            "session_id": session_id,
            "agent_context_id": actor_id,
            "lane_id": lane_id,
            "task_id": task_id,
            "goal": goal,
            "files_in_scope": list(relevant_files or []),
            "actor_task": stored,
            "lane_binding": lane_binding,
        }

    def _worker_task_update(
        self,
        project_root: Path,
        session_id: str,
        actor_id: str,
        lane_id: str,
        **updates: object,
    ) -> dict[str, object]:
        """Merge progress into the worker's actor slot.

        ONE WRITE LOCK AROUND THE READ AND THE WRITE. This was `store.get(...)`
        on one connection, a merge in Python, then `store.set(...)` on a SECOND
        connection issuing `INSERT OR REPLACE` over the whole `state_json` blob
        -- the failure ledger's lost-update shape verbatim, and reachable because
        `stable_actor_id` still hashes only (project, host_kind,
        host_session_id), so N subagents of one conversation share ONE slot
        (#650 A1, open). Two concurrent updates lost one WHOLESALE. The merge now
        happens inside `ActorTaskStateStore.merge_state`'s transaction, so what
        is written is always derived from what is stored.
        """
        if not actor_id:
            return {
                "ok": False,
                "blocked": True,
                "status": "missing_actor_identity",
                "session_id": session_id,
            }
        # Only the fields the caller actually supplied. A None is "not sent",
        # never "clear it" -- filtering HERE keeps the store ignorant of the
        # tool's argument conventions.
        supplied = {key: value for key, value in updates.items() if value is not None}
        stored = self._worker_task_store().merge_state(
            project_root,
            session_id,
            actor_id,
            lane_id,
            updates=supplied,
        )
        if stored is None:
            return {
                "ok": False,
                "blocked": True,
                "status": "no_open_task",
                "session_id": session_id,
                "agent_context_id": actor_id,
                "lane_id": lane_id,
            }
        return {
            "ok": True,
            "worker_scoped": True,
            "session_id": session_id,
            "agent_context_id": actor_id,
            "lane_id": lane_id,
            "task_id": str(stored.get("task_id") or ""),
            "updated_fields": list(supplied),
            "actor_task": stored,
        }

    def _worker_task_status(
        self,
        project_root: Path,
        session_id: str,
        actor_id: str,
        lane_id: str,
    ) -> dict[str, object]:
        if not actor_id:
            return {
                "ok": False,
                "blocked": True,
                "status": "missing_actor_identity",
                "session_id": session_id,
            }
        current = self._worker_task_store().get(project_root, session_id, actor_id, lane_id)
        if not current:
            return {
                "ok": True,
                "worker_scoped": True,
                "session_id": session_id,
                "agent_context_id": actor_id,
                "lane_id": lane_id,
                "current_task": None,
                "task_id": "",
            }
        state = current.get("state") if isinstance(current.get("state"), list) else []
        goal = str(current.get("goal") or "").strip()
        current_task = goal or (str(state[0]) if state else "")
        active = str(current.get("status") or "") == "active"
        if not active:
            current_task = ""
        return {
            "ok": True,
            "worker_scoped": True,
            "session_id": session_id,
            "agent_context_id": actor_id,
            "lane_id": lane_id,
            "task_id": str(current.get("task_id") or ""),
            "status": str(current.get("status") or ""),
            "goal": goal or None,
            "current_task": current_task or None,
            "partial_goals": list(current.get("partial_goals") or [])[:5],
            # #353: a closed task's blockers are retracted with it — status
            # never reports a blocker whose owning task is no longer active.
            "blockers": (list(current.get("blockers") or [])[:5] if active else []),
            "actor_task": current,
        }

    def _worker_task_complete(
        self,
        project_root: Path,
        session_id: str,
        actor_id: str,
        lane_id: str,
        *,
        result_summary: str,
        next_status: str,
        verification_evidence: dict[str, object] | None,
    ) -> dict[str, object]:
        if not actor_id:
            return {
                "ok": False,
                "blocked": True,
                "status": "missing_actor_identity",
                "session_id": session_id,
            }
        store = self._worker_task_store()
        current = store.get(project_root, session_id, actor_id, lane_id)
        if not current or str(current.get("status") or "") != "active":
            # #599: fall back to this ACTOR's active row on any lane before
            # declaring it task-less — the lane a caller presents can move
            # between two of its own requests, and refusing an agent's own
            # open task is the same wound from the other side.
            wider = store.active_row_for_actor(project_root, session_id, actor_id)
            if wider is not None:
                current = wider
                lane_id = str(wider.get("lane_id") or "")
        if not current or str(current.get("status") or "") != "active":
            return {
                "session_id": session_id,
                "blocked": True,
                "status": "no_open_task",
                "error": "task_complete refused: no actor-owned task is open",
            }
        task_id = str(current.get("task_id") or "")
        worker_state = {
            key: value
            for key, value in current.items()
            if key
            not in {
                "task_id",
                "status",
                "updated_at",
                "session_id",
                "agent_context_id",
                "lane_id",
            }
        }
        worker_state["result_summary"] = result_summary
        if verification_evidence is not None:
            worker_state["verification_evidence"] = verification_evidence
        # #353: blockers are OWNED by this actor's task; the owner's
        # task_complete RETRACTS them. A blocker that outlives the task it
        # blocked is a lie that outlives its truth — the audit copy stays in
        # retracted_blockers, the live field empties.
        open_blockers = [b for b in list(worker_state.get("blockers") or []) if str(b).strip()]
        if open_blockers:
            worker_state["retracted_blockers"] = open_blockers
            worker_state["blockers"] = []
        stored = store.set(
            project_root,
            session_id,
            actor_id,
            lane_id,
            task_id=task_id,
            state=worker_state,
            status=str(next_status or "done").strip().lower(),
        )
        return {
            "ok": True,
            "worker_scoped": True,
            "session_id": session_id,
            "agent_context_id": actor_id,
            "lane_id": lane_id,
            "task_id": task_id,
            "status": str(next_status or "done").strip().lower(),
            "actor_task": stored,
        }

    def task_status(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Quick-peek read-only status of the active task in a session.

        Gap this fills: task_begin/update WRITE, but there was no read
        surface for "what is this session working on right now?" — useful
        for status bars, conductor dashboards, and external monitors that
        shouldn't trigger any side effects.

        Returns a small (<500B typical) dict with goal, current state,
        partial goals, blockers, lane scope. No journal write, no plan
        write, no code bundle.
        """
        actor_id, lane_id, is_worker = self._task_actor_identity(project_root)
        if is_worker:
            return self._worker_task_status(
                project_root,
                session_id,
                actor_id,
                lane_id,
            )
        try:
            session = self.hub.sessions.read_session(project_root, session_id)
            sections = session.sections if session else {}
        except Exception:
            return {"ok": False, "session_id": session_id, "error": "session_not_found"}
        try:
            plan = self.hub.sessions.read_plan(project_root, session_id)
            plan_sections = plan.sections if plan else {}
        except Exception:
            plan_sections = {}
        gate_state = {}
        try:
            gate_state = self.hub.query_gate.get(project_root, session_id) or {}
        except Exception:
            pass

        def _first(s: list[str] | None) -> str | None:
            if not s:
                return None
            for line in s:
                stripped = str(line).lstrip("- ").strip()
                if stripped:
                    return stripped
            return None

        return {
            "ok": True,
            "session_id": session_id,
            "title": _first(sections.get("Title")),
            "status": _first(sections.get("Status")),
            "goal": _first(sections.get("Goal")),
            "current_task": _first(plan_sections.get("Current State"))
            or _first(sections.get("State")),
            "partial_goals": [
                str(line).lstrip("- ").strip()
                for line in (plan_sections.get("Partial Goals") or [])
                if str(line).strip() and str(line).strip() != "-"
            ][:5],
            "blockers": [
                str(line).lstrip("- ").strip()
                for line in (sections.get("Blockers") or [])
                if str(line).strip() and str(line).strip() != "-"
            ][:5],
            "current_lane_id": gate_state.get("current_lane_id"),
            "current_lane_id_short": _short_lane_id(gate_state.get("current_lane_id")),
            "lane_files_count": len(gate_state.get("lane_exact_paths") or []),
            "last_tool": gate_state.get("last_tool"),
        }

    def auto_task_begin(
        self,
        project_root: Path,
        session_id: str,
        *,
        goal: str,
        kind: str = "work",
        origin_prompt: str | None = None,
    ) -> dict[str, object] | None:
        """Lightweight, SQL-only task open for the UserPromptSubmit auto-task.

        Removes the "call task_begin first" friction: opens a task row in
        sqlite (task_lifecycle_store) AND sets the query_gate current task
        so the agent is genuinely "in a task" (task_complete and gated ops
        work) WITHOUT the agent calling task_begin and WITHOUT writing
        SESSION.md/PLAN.md (no-file-layer doctrine).

        No-op (returns None) when a task is already active for the session
        — one active task at a time; subsequent imperatives attach to it.
        """
        from . import task_lifecycle_store as _tls

        opened = _tls.begin_task_if_none_active(
            project_root,
            session_id=session_id,
            goal=goal,
            kind=kind,
            source="auto_ups",
            origin_prompt=origin_prompt,
        )
        if opened is None:
            return None
        # Make the agent "in a task" for the gate (the part of task_begin
        # that isn't markdown), so task_complete's open-task guard passes.
        # Split-brain guard: if the gate write fails, the SQL task exists
        # but no gate state does — so abandon the SQL task and report None
        # rather than tell the agent it is "in a task" when task_complete /
        # gated ops would refuse. The two stores must not diverge.
        try:
            self.hub.query_gate.set_current_task_id(
                project_root,
                session_id,
                str(opened["task_id"]),
            )
        except Exception:
            try:
                from . import task_lifecycle_store as _tls_rollback

                _tls_rollback.complete_task(
                    project_root,
                    session_id=session_id,
                    task_id=str(opened["task_id"]),
                    next_status="abandoned",
                )
            except Exception:
                pass
            return None
        # #599: the auto-task must be OWNED by the actor it was opened
        # for. Before this, a UPS-opened task lived ONLY in the shared
        # session slot, so it belonged to nobody — and an unowned task
        # is exactly what another actor's task_complete adopts and
        # closes. Writing the actor slot here is what makes the
        # conductor's auto-task defensible by the ownership check in
        # task_complete. Best-effort: an actor-less host (no host
        # identity to derive from) keeps the pre-#599 session-slot-only
        # behaviour rather than losing its auto-task.
        try:
            _actor_id, _lane_id, _ = self._task_actor_identity(project_root)
            if _actor_id:
                self._worker_task_store().set(
                    project_root,
                    session_id,
                    _actor_id,
                    _lane_id,
                    task_id=str(opened["task_id"]),
                    state={"goal": goal, "source": "auto_ups"},
                    status="active",
                )
        except Exception:
            pass
        return opened

    def _persist_active_task_id(
        self,
        project_root: Path,
        session_id: str,
        task_id: str,
    ) -> dict[str, object] | None:
        """Persist the active task id and VERIFY it landed (#474 lifecycle truth).

        Pre-#474 this write was swallowed (``task_id = ""`` on error) and
        task_begin still returned ok=True — the caller believed a task was
        open while every subsequent tool call refused with "no active task".
        NEVER ok-but-noop: a failed or unverifiable persist returns an
        explicit error dict naming the state and the next action; the caller
        must return it verbatim. Returns None when the persist verified.
        """
        try:
            self.hub.query_gate.set_current_task_id(project_root, session_id, task_id)
            readback = self.hub.query_gate.get_current_task_id(project_root, session_id)
        except Exception as exc:
            return {
                "ok": False,
                "blocked": True,
                "status": "lifecycle_write_failed",
                "session_id": session_id,
                "task_id": task_id,
                "error": (
                    f"task_begin could NOT persist the active task id for session "
                    f"'{session_id}' ({exc!r}). Without it every next tool call fails "
                    f"'no active task' — refusing to pretend the task opened. "
                    f"Check the session store (.MEMORY/.index) and retry `ai_task(mode='begin')`."
                ),
            }
        if str(readback or "") != task_id:
            return {
                "ok": False,
                "blocked": True,
                "status": "lifecycle_write_failed",
                "session_id": session_id,
                "task_id": task_id,
                "error": (
                    f"task_begin persisted task '{task_id}' for session '{session_id}' "
                    f"but read back '{readback or ''}' — the write did not land "
                    f"(silent no-op guard, #474). Retry `ai_task(mode='begin')`; if it repeats, "
                    f"the session store is unhealthy."
                ),
            }
        return None

    def mint_session_scaffold_grant(
        self,
        project_root: Path,
        pattern: str,
        ttl_seconds: int = 900,
        host_session_id: str = "",
    ) -> dict[str, object]:
        """#475 (2): conductor-minted work-grant permitting task_begin to
        scaffold sessions matching ``pattern`` (fnmatch) for ``ttl_seconds``.

        Authority: the caller must be a bound conductor — managed mode
        active for a session that is a SQL member. The mint is
        audit-stamped (record_event) to the granting conductor session.
        """
        cleaned = (pattern or "").strip()
        if not cleaned:
            return {
                "ok": False,
                "blocked": True,
                "rule_id": "invalid_pattern",
                "error": "session-scaffold grant requires a non-empty session_id pattern (e.g. 'war-*')",
                "next_action": "call again with a concrete fnmatch pattern naming the sessions the dispatch may create",
            }
        mode = self.hub.managed_mode.get_mode(
            project_root,
            host_session_id=host_session_id,
        )
        # #1027: the session that may GRANT comes from the authority door;
        # `mode` above is retained only for the separate membership_valid fact.
        granting_session = resolve_managed_session(
            self.hub.managed_mode,
            project_root,
            host_session_id=host_session_id,
        )
        is_conductor = bool(granting_session and mode.get("membership_valid"))
        if not is_conductor:
            return {
                "ok": False,
                "blocked": True,
                "rule_id": "conductor_required",
                "error": (
                    "minting a session-scaffold work-grant requires an active "
                    "conductor binding: managed mode must be active for a "
                    "member session (the dispatching conductor is the "
                    "operator-chain authority for the grant)"
                ),
                "next_action": (
                    "bind a conductor session first (ai_session mode='connect' "
                    "/ conductor_mode_enter), then mint the grant from that "
                    "bound context"
                ),
            }
        from .session_scaffold_grant_store import SessionScaffoldGrantStore

        grant = SessionScaffoldGrantStore().mint(
            project_root,
            pattern=cleaned,
            granted_by_session=granting_session,
            host_session_id=host_session_id or "",
            ttl_seconds=ttl_seconds,
        )
        # Audit-stamp the mint to the granting conductor session.
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="session_scaffold",
                source_kind="mcp",
                session_id=granting_session,
                action_kind="scaffold_grant_minted",
                target_entity=cleaned,
                status="ok",
                payload={
                    "grant_id": grant["grant_id"],
                    "pattern": cleaned,
                    "ttl_seconds": grant["ttl_seconds"],
                    "expires_at_epoch": grant["expires_at_epoch"],
                    "granted_by_session": granting_session,
                    "host_session_id": host_session_id or "",
                },
            )
        except Exception:
            pass
        return {"ok": True, **grant}

    def _scaffold_or_refuse_missing_session(
        self,
        project_root: Path,
        session_id: str,
        goal: str | None,
    ) -> dict[str, object] | None:
        """#475 (1): task_begin hit a session with no SESSION.md.

        Scaffold it (returning None so task_begin proceeds) when either
        a conductor work-grant covers this session_id or the
        ``session.task_begin_autoscaffold`` config flag permits;
        otherwise return the structured refusal dict. Scaffolding
        reuses ``SessionStore.create_session`` — the SAME writer behind
        ai_session create — so the directory shape and membership
        registration stay one definition.
        """
        grant: dict[str, object] | None = None
        try:
            from .session_scaffold_grant_store import SessionScaffoldGrantStore

            grant = SessionScaffoldGrantStore().find_active(project_root, session_id)
        except Exception:
            grant = None
        config_allows = False
        if grant is None:
            try:
                from .config import get_setting

                config_allows = bool(
                    get_setting(
                        "session.task_begin_autoscaffold",
                        project_root=project_root,
                        session_id=session_id,
                        default=False,
                    ),
                )
            except Exception:
                config_allows = False
        if grant is None and not config_allows:
            return {
                "ok": False,
                "blocked": True,
                "rule_id": "session_missing",
                "status": "session_missing",
                "session_id": session_id,
                "error": (
                    f"task_begin precondition failed: session '{session_id}' "
                    f"does not exist (no SESSION.md under "
                    f".MEMORY/sessions/{session_id}/ and no scaffold "
                    f"authority). It can be satisfied by: an operator "
                    f"creating the session (ai_session mode='create' "
                    f"session_id='{session_id}'), or a bound conductor "
                    f"minting a session-scaffold work-grant covering "
                    f"'{session_id}', or the "
                    f"session.task_begin_autoscaffold config flag."
                ),
                "who_can_satisfy": [
                    f"operator: ai_session mode='create' session_id='{session_id}'",
                    "conductor: mint_session_scaffold_grant with a pattern covering this session_id",
                    "config: set session.task_begin_autoscaffold=true",
                ],
                "next_action": (
                    "create the session via ai_session mode='create', or have "
                    "the dispatching conductor mint a scaffold work-grant, "
                    "then retry task_begin"
                ),
            }
        # Scaffold with THE writer ai_session create uses (one definition).
        self.hub.sessions.create_session(
            project_root,
            session_id,
            title=session_id,
            owner="agent",
            goal=(goal or f"Scaffolded by task_begin for {session_id}").strip(),
        )
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="session_scaffold",
                source_kind="mcp",
                session_id=session_id,
                action_kind="session_scaffolded",
                target_entity=session_id,
                status="ok",
                payload={
                    "via": "work_grant" if grant is not None else "config_flag",
                    "grant_id": grant.get("grant_id") if grant is not None else None,
                    "granted_by_session": grant.get("granted_by_session")
                    if grant is not None
                    else None,
                    "pattern": grant.get("pattern") if grant is not None else None,
                },
            )
        except Exception:
            pass
        return None

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
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if goal is not None and goal.strip() == session_id.strip():
            return {
                "ok": False,
                "blocked": True,
                "status": "placeholder_goal_refused",
                "session_id": session_id,
                "error": "task_begin requires a concrete goal, not the session id",
            }
        actor_id, lane_id, is_worker = self._task_actor_identity(project_root)
        if is_worker:
            return self._worker_task_begin(
                project_root,
                session_id,
                actor_id,
                lane_id,
                goal=goal,
                state=state,
                upcoming=upcoming,
                partial_goals=partial_goals,
                end_goal=end_goal,
                blockers=blockers,
                relevant_files=relevant_files,
                relevant_commands=relevant_commands,
                relevant_snippets=relevant_snippets,
                session_facts=session_facts,
                constraints=constraints,
            )
        # #475: a task_begin against a NONEXISTENT session used to leak
        # SessionStore.read_session's FileNotFoundError — a bare path
        # string with no rule_id and no next action. Now: scaffold when
        # authority exists (conductor work-grant / config flag), else a
        # structured legible refusal.
        if not self.hub.sessions.session_file(project_root, session_id).exists():
            refusal = self._scaffold_or_refuse_missing_session(
                project_root,
                session_id,
                goal,
            )
            if refusal is not None:
                return refusal
        current_lane_id, lane_exact_paths = self._resolve_task_lane_scope(
            project_root,
            session_id,
            relevant_files,
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
        session = self.hub.sessions.update_session(project_root, session_id, session_patch)

        plan_patch: dict[str, list[str]] = {}
        session_scope = self.hub.sessions.read_session(project_root, session_id).sections.get(
            "Scope",
            ["-"],
        )
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
                    existing_plan.sections.get("Constraints", []),
                )
            except Exception:
                existing_constraints = []
            merged_constraints = [
                item for item in existing_constraints if item and not item.startswith("Blockers: ")
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

        # Audit hardening (2026-04-19): mint a stable task_id so every
        # execution_event between here and task_complete can be
        # attributed to this task. Content-addressed: sha256 of
        # (session_id | goal | current time bucket) truncated. Stored
        # in query_gate so record_event reads it without an extra join.
        import hashlib as _hashlib
        import time as _time

        task_id = (
            "t_"
            + _hashlib.sha256(
                f"{session_id}|{goal or ''}|{int(_time.time())}".encode(),
            ).hexdigest()[:16]
        )
        # #474 lifecycle truth: persist-and-verify. A failed persist is an
        # EXPLICIT error (never ok-but-noop — the old path swallowed the
        # failure, returned ok=True, and the next tool call refused with
        # "no active task" out of nowhere).
        _persist_err = self._persist_active_task_id(project_root, session_id, task_id)
        if _persist_err is not None:
            return _persist_err
        # #483 per-host-actor slots: the caller's ACTIVE task lives in its
        # own actor slot (lane_id="" for non-workers), so another actor's
        # task_complete on the same session can never clobber this begin.
        # The session-level slot above stays written too — it is the
        # fallback for actor-less legacy callers and the audit-stamp
        # source for record_event. Best-effort: if this write fails the
        # gate still passes via the session slot.
        if actor_id:
            try:
                self._worker_task_store().set(
                    project_root,
                    session_id,
                    actor_id,
                    "",
                    task_id=task_id,
                    state={"goal": goal or ""},
                    status="active",
                )
            except Exception:
                pass
        try:
            from .session_response_ledger import record_lifecycle as _srl_record

            _srl_record(project_root, session_id, task_id=task_id, status="active")
        except Exception:
            pass

        # Journal the task_begin intent so file-backed views
        # (session_timeline, breadcrumbs, compare, velocity, streak,
        # inactive-nudge) have a record regardless of whether the
        # orchestrator hook ran. The orchestrator translation path
        # writes execution_events in a hooked Claude session, but
        # direct RuntimeService callers (tests, dashboard, CLI, codex
        # backend) bypass that path — journal writes here keep both
        # surfaces honest. write_journal_entry drops trivial actions
        # and enforces max_entries internally.
        try:
            self.hub.sessions.write_journal_entry(
                project_root,
                session_id,
                action_kind="task_lifecycle",
                intent=(goal or "task begin").strip(),
                outcome="started",
            )
        except Exception:
            pass

        # Session + plan + context dicts are foundational — callers (dashboard,
        # tests, conductor) read fresh sections to verify the write landed.
        # The slop we were cutting was ai_bundle (~100KB), not these.
        result: dict[str, object] = {
            "ok": True,
            "session_id": session.session_id,
            "lane_id": current_lane_id,
            "task_id": task_id,
            "goal": goal,
            "files_in_scope": list(lane_exact_paths or []),
            "session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "plan": {"path": str(plan.path), "sections": plan.sections},
            "context": {"path": str(context.path), "sections": context.sections},
        }
        # Phase 4 (2026-05-15): king-field vs agent-crop diff. Compute
        # the agent-crop from `goal`, fetch the cached king-field for
        # this session, compare via the 3-tier verdict (BLOCK / WARN /
        # ALLOW). Best-effort: any failure silently drops the diff so
        # task_begin still completes.
        if goal:
            try:
                from .anchor_field import (
                    compute_diff_verdict,
                    extract_field,
                    get_king_field,
                )

                king = get_king_field(project_root, session_id)
                if king is not None:
                    crop = extract_field(goal, project_root)
                    verdict, details = compute_diff_verdict(king, crop)
                    result["diff_verdict"] = verdict
                    result["diff_details"] = details
                    result["king_field"] = king.to_dict()
                    result["agent_crop"] = crop.to_dict()
                    if verdict == "block":
                        result["ok"] = False
                        result["reason"] = (
                            "king-field/agent-crop divergence (BLOCK): "
                            f"king named {king.anchors!r} root="
                            f"{king.root_anchor!r}; you inferred "
                            f"{crop.anchors!r} root={crop.root_anchor!r}. "
                            f"Zero shared anchor/domain/symbol. Re-read "
                            f"the prompt or ask for clarification."
                        )
                    elif verdict == "warn":
                        warnings = result.setdefault("warnings", [])
                        if isinstance(warnings, list):
                            warnings.append(
                                "king-field/agent-crop divergence (WARN): "
                                f"king root={king.root_anchor!r} vs crop "
                                f"root={crop.root_anchor!r}; shared "
                                f"anchors={details.get('anchor_overlap', [])}",
                            )
            except Exception:
                pass

        if include_code_bundle:
            result["ai_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
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
        summary_only: bool = False,
    ) -> dict[str, object]:
        actor_id, lane_id, is_worker = self._task_actor_identity(project_root)
        if is_worker:
            result = self._worker_task_update(
                project_root,
                session_id,
                actor_id,
                lane_id,
                state=state,
                upcoming=upcoming,
                partial_goals=partial_goals,
                end_goal=end_goal,
                blockers=blockers,
                relevant_files=relevant_files,
                relevant_commands=relevant_commands,
                relevant_snippets=relevant_snippets,
                session_facts=session_facts,
                constraints=constraints,
            )
            if summary_only and result.get("ok"):
                return {
                    "ok": True,
                    "worker_scoped": True,
                    "session_id": session_id,
                    "agent_context_id": actor_id,
                    "lane_id": lane_id,
                    "task_id": result.get("task_id"),
                    "updated_fields": result.get("updated_fields", []),
                }
            return result
        effective_relevant_files = relevant_files
        if effective_relevant_files is None:
            try:
                existing_context = self.hub.sessions.read_context(project_root, session_id)
                effective_relevant_files = self._clean_file_bullets(
                    existing_context.sections.get("Relevant Files", []),
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
        # Audit trail: execution_events records task_update automatically
        # via the orchestrator. Duplicate journal write deleted 2026-04-20.
        if summary_only:
            updated_fields: list[str] = []
            if state is not None:
                updated_fields.append("state")
            if upcoming is not None:
                updated_fields.append("upcoming")
            if partial_goals is not None:
                updated_fields.append("partial_goals")
            if end_goal is not None:
                updated_fields.append("end_goal")
            if blockers is not None:
                updated_fields.append("blockers")
            if relevant_files is not None:
                updated_fields.append("relevant_files")
            if relevant_commands is not None:
                updated_fields.append("relevant_commands")
            if relevant_snippets is not None:
                updated_fields.append("relevant_snippets")
            if session_facts is not None:
                updated_fields.append("session_facts")
            if constraints is not None:
                updated_fields.append("constraints")
            # Include session + plan dicts so callers can verify the patch
            # landed without issuing a separate read. ai_bundle is the
            # only payload actually excluded by summary_only — it was the
            # 100KB slop driving this setting in the first place.
            session_dict = result.get("session") if isinstance(result, dict) else None
            plan_dict = result.get("plan") if isinstance(result, dict) else None
            return {
                "session_id": session_id,
                "updated_fields": updated_fields,
                "ok": True,
                "session": session_dict,
                "plan": plan_dict,
            }
        return result

    _TASK_TERMINAL_STATUSES = frozenset({"done", "abandoned", "blocked"})

    def task_complete(
        self,
        project_root: Path,
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        verification_evidence: dict[str, object] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        tool_report: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        actor_id, lane_id, is_worker = self._task_actor_identity(project_root)
        # War HH (Emperor charter 2026-07-19): optional structured tool
        # feedback, honored ONLY for superadmin/dev principals. Shape and
        # audience are gated UP FRONT — a refused payload blocks the call
        # legibly (retry without the param); it is never silently dropped
        # and never reaches the privileged store.
        _tool_reports: list[dict[str, object]] = []
        _tool_principal = ""
        if tool_report:
            from . import tool_usage_report_store as _turs

            _cleaned, _schema_error = _turs.validate_tool_report(tool_report)
            if _schema_error:
                return {
                    "session_id": session_id,
                    "blocked": True,
                    "status": "tool_report_schema",
                    "rule_id": _turs.RULE_ID_SCHEMA,
                    "reason": _schema_error,
                    "error": _schema_error,
                }
            _privileged, _principal = _turs.caller_tool_report_authority(project_root)
            if not _privileged:
                _audience_error = (
                    "tool_report refused: this parameter is honored only for "
                    "superadmin (super_admin) and dev RBAC principals — the "
                    "authenticated caller does not carry those roles. Nothing "
                    "was stored. Retry task_complete WITHOUT tool_report and "
                    "put the tool feedback in your report text instead."
                )
                return {
                    "session_id": session_id,
                    "blocked": True,
                    "status": "tool_report_unauthorized",
                    "rule_id": _turs.RULE_ID_AUDIENCE,
                    "reason": _audience_error,
                    "error": _audience_error,
                }
            _tool_reports = list(_cleaned)
            _tool_principal = _principal
        if is_worker:
            _worker_result = self._worker_task_complete(
                project_root,
                session_id,
                actor_id,
                lane_id,
                result_summary=result_summary,
                next_status=next_status,
                verification_evidence=verification_evidence,
            )
            if _tool_reports and _worker_result.get("ok"):
                # Already shape-checked and audience-gated above; ingest
                # keyed to the worker's own task. Fail-quiet by contract.
                from . import tool_usage_report_store as _turs

                _worker_result["tool_report"] = _turs.record_tool_reports(
                    project_root,
                    session_id=session_id,
                    task_id=str(_worker_result.get("task_id") or ""),
                    principal=_tool_principal,
                    reports=_tool_reports,
                )
            return _worker_result
        # Protocol guard (2026-04-24): refuse task_complete when no
        # task is open. Without this, a lane worker that skips
        # task_begin can still "complete" — closing the audit chain
        # without ever stamping mutations. MiniMax-free observed
        # doing exactly this on oc-smoke. Return a structured refusal
        # the worker's prompt can parse and retry from.
        #
        # #483 per-host-actor slots: the CALLER's own actor slot is the
        # source of truth for "which task am I completing" — the shared
        # session-level slot is only the fallback for actor-less legacy
        # callers (and for actors whose begin predates the actor-slot
        # write, e.g. the UPS auto-task). This is what makes complete
        # close the completing actor's OWN task instead of whatever
        # task last won the session slot.
        _actor_slot_task = ""
        _actor_slot_lane = ""
        if actor_id:
            try:
                # #599: ANY lane. The row key carries a lane, but the task
                # belongs to the ACTOR — and whether a caller presents as a
                # lane worker can differ between two of its own requests, so
                # a lane-exact read can miss the caller's own open task and
                # send it to the shared session slot (the theft path).
                _actor_row = self._worker_task_store().active_row_for_actor(
                    project_root,
                    session_id,
                    actor_id,
                )
                if _actor_row is None:
                    _actor_row = self._worker_task_store().get(
                        project_root,
                        session_id,
                        actor_id,
                        "",
                    )
                if _actor_row and str(_actor_row.get("status") or "") == "active":
                    _actor_slot_task = str(_actor_row.get("task_id") or "").strip()
                    _actor_slot_lane = str(_actor_row.get("lane_id") or "")
            except Exception:
                _actor_slot_task = ""
                _actor_slot_lane = ""
        try:
            _session_slot_task = str(
                self.hub.query_gate.get_current_task_id(
                    project_root,
                    session_id,
                )
                or "",
            ).strip()
        except Exception:
            _session_slot_task = ""
        _open_task = _actor_slot_task
        if not _open_task and _session_slot_task:
            # #599 THE THEFT, and the end of it. The old code was
            # `_actor_slot_task or _session_slot_task` — an unconditional
            # fallback, the same permissive-on-empty shape as the freeze
            # store's `OR actor = ''` (#588 D1). A caller with no task of
            # its own adopted whatever stood in the shared session slot
            # and closed it; the victim only found out when its next call
            # refused "no active task". Ownership is now consulted: a task
            # standing in ANOTHER actor's active slot is not ours to close.
            #
            # An UNOWNED session-slot task is still adoptable, and that is
            # deliberate rather than an oversight — it is the pre-#483
            # actor-less contract (pinned by
            # test_actorless_legacy_caller_still_works_via_session_slot)
            # and the single-agent flow on any host whose identity cannot
            # be derived. Narrowing it would silently strand live tasks,
            # the same reason #588 D1 left legacy freeze rows
            # session-scoped. What is refused is the case where there is
            # something to steal.
            _slot_owner = ""
            try:
                _slot_owner = self._worker_task_store().active_owner_of_task(
                    project_root,
                    session_id,
                    _session_slot_task,
                )
            except Exception:
                _slot_owner = ""
            # An empty actor id is not one situation but TWO, and #599 wants
            # opposite answers for them. They are separated by whether the call
            # arrived inside a REQUEST IDENTITY SCOPE:
            #
            #   scoped, empty  -> a REAL request from a host that stamps no
            #     identity ("unknown"). Refusing it compares an unresolved
            #     identity as if it were a value and locks the rightful owner
            #     out — measured live, every tool_call_started carrying
            #     host_id "unknown", no agent able to complete its own task,
            #     reopening no help because the new task was foreign next call.
            #     These callers keep the pre-#483 session-slot contract exactly
            #     as the UNOWNED case above does.
            #     (test_an_unidentifiable_caller_is_not_treated_as_a_foreign_actor)
            #
            #   not scoped     -> no request identity scope at all. This did not
            #     come through the request path and makes no claim to the
            #     session's work, so it may ride an UNOWNED slot task (the
            #     pre-#483 contract) but may NOT adopt one another actor
            #     actively owns.
            #     (test_unresolvable_actor_cannot_adopt_an_owned_task)
            #
            # A RESOLVED foreign actor is refused either way — that is the case
            # where there is genuinely something to steal
            # (test_a_resolved_foreign_actor_is_still_refused).
            from .mcp_server_runtime_helpers import request_identity_is_scoped

            _present_but_unidentified = not actor_id and request_identity_is_scoped()
            if _slot_owner and _slot_owner != actor_id and not _present_but_unidentified:
                return {
                    "session_id": session_id,
                    "blocked": True,
                    "status": "task_owned_by_other_actor",
                    "task_id": _session_slot_task,
                    "error": (
                        f"task_complete refused: you have no open task, and the "
                        f"session slot holds task '{_session_slot_task}', which is "
                        f"OWNED by another actor on session '{session_id}'. "
                        f"Completing it would close that agent's task out from "
                        f"under it (#599). If this is your work, call task_begin to "
                        f"open your own task; to report progress use task_update."
                    ),
                }
            _open_task = _session_slot_task
        if not _open_task:
            return {
                "session_id": session_id,
                "blocked": True,
                "status": "no_open_task",
                "error": (
                    "task_complete refused: no task is open. Call "
                    "`ai_task(mode='begin')` first, do the work, then `ai_task(mode='complete')`."
                ),
            }
        normalized_status = str(next_status).strip().lower()
        is_terminal = normalized_status in self._TASK_TERMINAL_STATUSES
        gate = None
        if normalized_status == "done":
            # Workflow forcing seam (todo #52): compiled workflow rules with
            # trigger `before_task_complete` BLOCK completion until each
            # action is verified (its [verify:...] spec passes) or satisfied
            # with evidence via workflow_action_satisfy. Same forcing shape
            # as the git_ops before_git_commit/before_git_push gate — the
            # rule forces the action, it is not merely stated to the agent.
            _wf_unsatisfied = [
                _wf_action
                for _wf_action in self.hub.workflow.pending_actions_for_trigger(
                    project_root,
                    "before_task_complete",
                )
                if not bool(
                    self.hub.workflow.verify_action(project_root, _wf_action).get("verified"),
                )
            ]
            if _wf_unsatisfied:
                _wf_text = "; ".join(
                    str(a.get("source_segment") or a.get("kind") or "action")
                    for a in _wf_unsatisfied
                )
                return {
                    "session_id": session_id,
                    "blocked": True,
                    "status": "workflow_actions_pending",
                    "error": (
                        f"task_complete refused: workflow rule requires: {_wf_text} — "
                        "do the required work, then call "
                        "workflow_action_satisfy(action_id, evidence) and retry."
                    ),
                    "pending_actions": [
                        {
                            "id": a.get("id"),
                            "trigger": a.get("trigger"),
                            "kind": a.get("kind"),
                            "source_rule": a.get("source_rule"),
                        }
                        for a in _wf_unsatisfied
                    ],
                }
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
        # #483: close the completing actor's OWN slot (mirror of the
        # worker path — status flips to the closing status, so the row
        # keeps the audit story). Never another actor's slot.
        if actor_id and _actor_slot_task:
            try:
                self._worker_task_store().set(
                    project_root,
                    session_id,
                    actor_id,
                    _actor_slot_lane,
                    task_id=_actor_slot_task,
                    state={"result_summary": result_summary},
                    status=(normalized_status if is_terminal else "done"),
                )
            except Exception:
                pass
        # Clear the audit-stamp task_id. Later mutations lose their task
        # linkage until another task_begin fires — which is exactly what
        # we want: mutations outside a task are visible as "no task_id"
        # in the event log.
        #
        # #483: clear the SESSION slot only when it still holds the task
        # this caller is completing. If another actor's begin has since
        # won the session slot, that task is NOT ours to drain — leaving
        # it is precisely the end of the cross-actor clobber.
        if _session_slot_task and _session_slot_task == _open_task:
            try:
                # #599: close THIS task only. `set_current_task_id(..., "")` is
                # documented as a WHOLESALE reset that purges every open-task
                # holder, so using it here drained a co-hosted peer's still-open
                # task along with our own — the exact clobber this war is about.
                # `close_session_task` retires one holder and returns the most
                # recent survivor (or "" when this was the last), which is what
                # keeps actor A's task standing when sibling B completes.
                self.hub.query_gate.close_session_task(
                    project_root,
                    session_id,
                    _open_task,
                )
            except Exception:
                pass
        # #474 lifecycle truth (War FF, begin-race): record WHICH task this
        # complete closed and with what status — never task_id="". The
        # session-level current_task_id slot is shared by every non-worker
        # actor on the session, so a complete by actor B legitimately
        # clears actor A's slot; when A's next call then refuses "no
        # active task", the refusal reads THIS snapshot to say which task
        # vanished, when, and why. The old ("", "done") write erased that
        # story and left the refusal illegibly bare.
        try:
            from .session_response_ledger import record_lifecycle as _srl_record

            _srl_record(
                project_root,
                session_id,
                task_id=_open_task,
                status=(normalized_status if is_terminal else "done"),
            )
        except Exception:
            pass
        # War HH: ingest the (already gated) tool feedback — durable
        # rows + ONE batched digest annotation on backlog #469. And when
        # a privileged session completes WITHOUT feedback, carry a
        # gentle one-line nudge once per session (notify-on-change via
        # the response ledger). Feedback is never load-bearing: every
        # branch here is fail-quiet.
        tool_report_receipt: dict[str, object] | None = None
        tool_report_nudge: str | None = None
        try:
            from . import tool_usage_report_store as _turs

            if _tool_reports:
                tool_report_receipt = _turs.record_tool_reports(
                    project_root,
                    session_id=session_id,
                    task_id=_open_task,
                    principal=_tool_principal,
                    reports=_tool_reports,
                )
            else:
                _nudge_privileged, _ = _turs.caller_tool_report_authority(project_root)
                if _nudge_privileged:
                    from .session_response_ledger import dedupe_state_notice as _srl_dedupe

                    tool_report_nudge = _srl_dedupe(
                        project_root,
                        session_id,
                        "tool_report_nudge",
                        _turs.NUDGE_TEXT,
                    )
        except Exception as _turs_exc:
            logger.warning("tool_report handling failed (non-fatal): %s", _turs_exc)
            tool_report_receipt = None
            tool_report_nudge = None
        # Close the SQL-only auto-task (if any) opened by the UPS
        # auto-task path, so the next imperative prompt can open a fresh
        # one. Tombstone transition; best-effort.
        #
        # #599: this call names no task_id, so it closes whatever SQL task
        # the session has open — including one another actor is holding.
        # Guarded the same way as the slot above: skip when the row is
        # ACTIVELY owned by a different actor. An unowned row (the legacy
        # shape, and every row minted before the auto-task learned to
        # write an actor slot) still closes, so the single-agent flow is
        # byte-identical.
        try:
            from . import task_lifecycle_store as _tls

            _sql_task = _tls.active_task(project_root, session_id) or {}
            _sql_task_id = str(_sql_task.get("task_id") or "")
            _sql_owner = ""
            if _sql_task_id:
                try:
                    _sql_owner = self._worker_task_store().active_owner_of_task(
                        project_root,
                        session_id,
                        _sql_task_id,
                    )
                except Exception:
                    _sql_owner = ""
            if not (_sql_owner and _sql_owner != actor_id):
                _tls.complete_task(
                    project_root,
                    session_id=session_id,
                    next_status=("abandoned" if normalized_status == "abandoned" else "done"),
                )
        except Exception:
            pass
        session = self.hub.sessions.read_session(project_root, session_id)
        existing_state = self._clean_bullets(session.sections.get("State", []))
        existing_state.append(result_summary)
        session_patch = {
            "Status": [f"- {next_status}"],
            "State": self._as_bullets(existing_state),
        }
        updated = self.hub.sessions.update_session(project_root, session_id, session_patch)
        try:
            existing_plan = self.hub.sessions.read_plan(project_root, session_id)
            existing_validation = self._clean_bullets(existing_plan.sections.get("Validation", []))
            existing_validation.append(f"Completion result: {result_summary}")
            plan_patch: dict[str, list[str]] = {
                "Current State": self._as_bullets(existing_state),
                "Validation": self._as_bullets(existing_validation),
            }
            if is_terminal:
                plan_patch["Next Steps"] = [
                    self._interaction_text(
                        "runtime.task_complete_next_steps",
                        project_root=project_root,
                        session_id=session_id,
                    ),
                ]
            plan = self.hub.sessions.update_plan(project_root, session_id, plan_patch)
        except Exception:
            plan = None
        handoff = None
        if is_terminal:
            try:
                handoff = self.hub.sessions.update_handoff(
                    project_root,
                    session_id,
                    {
                        "Current State": self._as_bullets(existing_state),
                        "What Was Done": self._as_bullets([result_summary]),
                        "Freshness": [
                            f"- Updated {self._timestamp()} — status {normalized_status}.",
                        ],
                    },
                )
            except Exception:
                handoff = None

        # Journal the task_complete outcome — see task_begin's note
        # above for why we write here rather than relying on the
        # orchestrator translation path.
        try:
            self.hub.sessions.write_journal_entry(
                project_root,
                session_id,
                action_kind="task_complete",
                intent=result_summary.strip(),
                outcome=normalized_status,
            )
        except Exception:
            pass

        # Detect "all steps done" deterministically from the plan's
        # checkbox list — `[ ]` = pending, `[x]` = done. Fires the
        # archive trigger for the conductor to consume without any
        # agent-driven decision.
        all_steps_done = False
        remaining_steps: list[str] = []
        if plan is not None:
            steps = plan.sections.get("Steps", []) if hasattr(plan, "sections") else []
            for line in steps:
                stripped = str(line).strip()
                if stripped.startswith("- [ ]") or stripped.startswith("[ ]"):
                    remaining_steps.append(stripped)
            all_steps_done = is_terminal and not remaining_steps

        try:
            roadmap_feedback = self._mark_matching_roadmap_step_pending_feedback(
                project_root,
                session_id,
                plan,
            )
        except Exception:
            roadmap_feedback = None

        # Session + plan dicts are foundational output — callers read
        # fresh sections to verify completion landed. Only ai_bundle
        # (~100KB) is gated behind include_code_bundle.
        terse: dict[str, object] = {
            "ok": True,
            "session_id": updated.session_id,
            "status": next_status,
            "all_steps_done": all_steps_done,
            "remaining_steps": len(remaining_steps),
            "session": {
                "session_id": updated.session_id,
                "path": str(updated.path),
                "sections": updated.sections,
            },
            "plan": (
                {"path": str(plan.path), "sections": plan.sections} if plan is not None else None
            ),
        }
        if handoff is not None:
            terse["handoff"] = {
                "path": str(handoff.path),
                "sections": handoff.sections,
            }
        if gate is not None:
            terse["verified"] = bool(gate.get("verified"))
        if roadmap_feedback is not None:
            terse["roadmap_feedback"] = roadmap_feedback
        if tool_report_receipt is not None:
            terse["tool_report"] = tool_report_receipt
        if tool_report_nudge:
            terse["tool_report_nudge"] = tool_report_nudge
        if not include_code_bundle:
            return terse

        result: dict[str, object] = {
            "ok": True,
            "all_steps_done": all_steps_done,
            "session": {
                "session_id": updated.session_id,
                "path": str(updated.path),
                "sections": updated.sections,
            },
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
        if roadmap_feedback is not None:
            result["roadmap_feedback"] = roadmap_feedback
        if tool_report_receipt is not None:
            result["tool_report"] = tool_report_receipt
        if tool_report_nudge:
            result["tool_report_nudge"] = tool_report_nudge
        result["ai_bundle"] = self._refresh_session_code_bundle(
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
            project_root,
            session_id=session_id,
            include_tests=include_tests,
        )
        return self.hub.code.get_context_bundle(project_root, session_id=session_id)

    def _as_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- {item}" for item in cleaned] or ["-"]

    def mark_roadmap_step_pending_feedback(
        self,
        project_root: Path,
        step_text: str,
    ) -> dict[str, object]:
        return self.hub.sessions.update_roadmap_step_state(
            project_root,
            step_text,
            "pending_user_feedback",
        )

    def update_roadmap_feedback_state(
        self,
        project_root: Path,
        step_text: str,
        feedback: str,
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
        status = "completed" if self._feedback_confirms_completion(feedback) else "in_progress"
        result = self.hub.sessions.update_roadmap_step_state(project_root, step_text, status)
        result["ok"] = True
        result["feedback"] = feedback
        return result

    def normalize_plan_prose(self, project_root: Path, session_id: str) -> dict[str, object]:
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
            or 24,
        )
        freshness_lines = sections.get("Freshness", []) if isinstance(sections, dict) else []
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
                    "status": "stale" if age_hours > resolved_stale_after_hours else "fresh",
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
            or 24,
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

    #: A line REPORTING A RESULT rather than naming a command (#772): "512 passed",
    #: "1 skipped", "3 failed". Deliberately narrow — it requires a DIGIT
    #: immediately before the outcome word, so `pytest tests/test_512.py` (number,
    #: no outcome) and `pytest -k 'passed_through'` (outcome word, no number) are
    #: both left alone. Over-matching here would silently drop a real requirement
    #: and weaken the verification gate, so the rule fails toward REQUIRING.
    _EXPECTED_OUTPUT_RE = re.compile(
        r"\b\d+\s+(passed|failed|skipped|errors?|xfailed|xpassed|warnings?)\b",
        re.IGNORECASE,
    )

    def _is_expected_output(self, line: str) -> bool:
        """True when a `Relevant Commands` bullet states an OUTCOME, not a command.

        Such a line cannot be honestly satisfied: the gate matches it by substring
        against what the agent says it ran, so clearing it means retyping a prior
        task's numbers. See #772 — the incentive points at fabrication, which is
        the exact behaviour the gate exists to prevent.
        """
        return bool(self._EXPECTED_OUTPUT_RE.search(str(line or "")))

    #: An UNSUBSTITUTED PLACEHOLDER (#772 shape 2, 2026-08-24): `<fix-sha>`,
    #: `{sha}` — a fill-me-in token in an otherwise command-shaped line. Requires
    #: an identifier INSIDE the brackets, so real shell survives untouched:
    #: `sort < input.txt` (space after `<`), `2>&1`, `-exec rm {} ;` (empty
    #: braces) and `-k 'a<b'` (no closing token) all fail to match. Same
    #: discipline as _EXPECTED_OUTPUT_RE — over-matching would silently drop a
    #: real requirement, so the rule fails toward REQUIRING.
    _PLACEHOLDER_RE = re.compile(
        r"<[A-Za-z][A-Za-z0-9_.-]*>|\{[A-Za-z][A-Za-z0-9_.-]*\}",
    )

    def _has_unsubstituted_placeholder(self, line: str) -> bool:
        """True when a bullet is a command TEMPLATE rather than a command.

        Satisfaction is substring matching against what the agent says it ran, so
        a placeholder no invocation can carry is clearable only by typing the
        placeholder itself — reporting a command that cannot be run.
        """
        return bool(self._PLACEHOLDER_RE.search(str(line or "")))

    def _is_unsatisfiable_requirement(self, line: str) -> bool:
        """True when no honest agent could ever clear this bullet.

        THE PRINCIPLE, not the shapes (#772). A verification gate must require
        that the work WAS verified; a requirement clearable only by fabrication
        supplies no verification to lose, so demoting it removes an incentive
        rather than a safeguard.

        Two shapes are known, and the split exists so a third can be added here
        instead of at the call site:
          1. an expected OUTPUT — "512 passed, 1 skipped" (the 2026-08-16 incident);
          2. an unsubstituted TEMPLATE — "git merge-base ... <fix-sha> ..."
             (the 2026-08-24 recurrence, which blocked every agent on a session
             because required_commands are read session-wide).
        """
        return self._is_expected_output(line) or self._has_unsubstituted_placeholder(line)

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

    def _collect_pending_workflow(self, action_kind: str | None, project_root: Path | None) -> str:
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
            rule_defs = compiled.get("rules", []) if isinstance(compiled.get("rules"), list) else []
            for trigger in triggers:
                pending.extend(self.hub.workflow.pending_actions_for_trigger(project_root, trigger))
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
                logger.debug("Failed to record workflow trigger evaluation event: %s", exc)
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

        def add_file_section(name: str, relative_dir: str, legacy: bool = False) -> None:
            directory = root / relative_dir
            if not directory.exists():
                return
            files = sorted(path.name for path in directory.glob("*.md") if path.is_file())
            if not files and relative_dir != "config":
                return
            sections.append(
                {
                    "name": name,
                    "file_count": len(files),
                    "samples": files[:3],
                    "legacy": legacy,
                },
            )

        sessions = self.hub.sessions.list_sessions(project_root)
        archived_sessions_root = root / "archive" / "sessions"
        archived_sessions = 0
        if archived_sessions_root.exists():
            archived_sessions = sum(1 for path in archived_sessions_root.iterdir() if path.is_dir())
        sections.append(
            {
                "name": "sessions",
                "active_count": len(sessions),
                "archived_count": archived_sessions,
                "legacy": False,
            },
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
            # SQLite-only doctrine (2026-06): /.MEMORY/INDEX.md retired.
            "router_files": ["/.MEMORY/.aidocs/index.aidocs"],
            "sections": sections,
        }
