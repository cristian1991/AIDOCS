from __future__ import annotations

from typing import Any

from .skill_provider import BUNDLED_PROVIDER_ID

_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_RUNTIME_OWNED_OVERRIDE_MODES = frozenset({"aidocs_runtime_owned"})


def resolve_suggested_skill_handle(
    suggested_name: str,
    catalog: list[dict[str, Any]] | None,
) -> str:
    """The RESOLVABLE catalog id for a flat skill-trigger name (#620).

    Skill-trigger vocab keys are FLAT names ("writing-plans"). The skill
    catalog is not flat: bundled/empire rows carry a flat ``skill_id``, while
    external-provider rows carry a NAMESPACED one
    ("superpowers_external/writing-plans") whose ``name`` is the flat form. So
    a surfaced trigger name is not guaranteed to be dereferenceable as-is.

    This is the ONE place that maps a suggested name to an address — the
    identity module already owning skill-id resolution. It is a pure function
    over catalog rows (``skill_store.list_skills`` shape); it opens no
    database, so the sovereign-only exclusion of its input is enforced at the
    SQL layer by the catalog itself (``_load_empire_skills_from_sql``:
    ``WHERE read_access = 'public'``) and is never re-derived — let alone
    post-filtered — here.

    Resolution order (first win):
      1. exact ``skill_id`` match  -> that id (flat bundled/empire case)
      2. exact ``name`` match      -> that row's ``skill_id`` (namespaced case)

    Returns "" when the name resolves to nothing. Drop-on-doubt: an
    unresolvable name never gets a fabricated address.
    """
    name = str(suggested_name or "").strip()
    if not name or not catalog:
        return ""
    by_name = ""
    for row in catalog:
        if not isinstance(row, dict):
            continue
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        if skill_id == name:
            return skill_id
        if not by_name and str(row.get("name") or "").strip() == name:
            by_name = skill_id
    return by_name


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
        selected_skill_id.split("/", 1)[0] if "/" in selected_skill_id else BUNDLED_PROVIDER_ID
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
        not isinstance(provider_states, dict) or selected_skill_id not in provider_states
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
