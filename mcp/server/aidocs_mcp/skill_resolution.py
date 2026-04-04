from __future__ import annotations

from typing import Any

from .skill_provider import BUNDLED_PROVIDER_ID


_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_RUNTIME_OWNED_OVERRIDE_MODES = frozenset({"aidocs_runtime_owned"})


def selected_skill_override_identity(
    selected_skill_id: str,
    provider_states: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    if "/" in selected_skill_id:
        return tuple(selected_skill_id.split("/", 1))
    if isinstance(provider_states, dict) and BUNDLED_PROVIDER_ID in provider_states:
        return _BUNDLED_OVERRIDE_PROVIDER_ID, selected_skill_id
    return None


def selected_skill_trigger_identity(
    selected_skill_id: str,
    *,
    provider_states: dict[str, Any] | None = None,
    override_store: Any = None,
) -> tuple[str, str, str] | None:
    override_target = selected_skill_override_identity(
        selected_skill_id,
        provider_states=provider_states,
    )
    if override_target is None:
        return None
    policy_provider_id, selected_name = override_target
    source_provider_id = (
        selected_skill_id.split("/", 1)[0]
        if "/" in selected_skill_id
        else BUNDLED_PROVIDER_ID
    )
    resolved_skill_id = selected_skill_id
    provider = source_provider_id
    runtime_provider = source_provider_id
    if override_store is not None:
        decision = override_store.resolve(policy_provider_id, selected_name)
        override_mode = str(decision.mode or "").strip()
        if override_mode in _RUNTIME_OWNED_OVERRIDE_MODES:
            resolved_skill_id = str(decision.skill_id or selected_name)
            runtime_provider = "aidocs_runtime"
        elif override_mode == "provider_content_aidocs_runtime":
            runtime_provider = "aidocs"
    elif selected_name == selected_skill_id and (
        not isinstance(provider_states, dict)
        or selected_skill_id not in provider_states
    ):
        return None
    return resolved_skill_id, provider, runtime_provider


def match_selected_skill_id_for_trigger(
    *,
    selected_skills: list[str],
    skill_id: str,
    provider: str,
    runtime_provider: str,
    provider_states: dict[str, Any] | None = None,
    override_store: Any = None,
) -> str | None:
    if skill_id in selected_skills:
        return skill_id
    matches = [
        selected_skill_id
        for selected_skill_id in selected_skills
        if selected_skill_trigger_identity(
            selected_skill_id,
            provider_states=provider_states,
            override_store=override_store,
        )
        == (skill_id, provider, runtime_provider)
    ]
    return sorted(matches)[0] if matches else None
