from pathlib import Path

from aidocs_mcp.skill_override_store import SkillOverrideStore


def test_override_registry_marks_writing_plans_as_aidocs_native(tmp_path: Path) -> None:
    store = SkillOverrideStore()

    result = store.resolve("superpowers_external", "writing-plans")

    assert result.mode == "runtime_owned"
    assert result.runtime_capability_id == "planning"


def test_override_registry_marks_verification_before_completion_as_runtime_owned() -> (
    None
):
    store = SkillOverrideStore()

    result = store.resolve("superpowers_external", "verification-before-completion")

    assert result.mode == "runtime_owned"
    assert result.runtime_capability_id == "completion_verification"


def test_override_registry_marks_brainstorming_as_provider_content_runtime_controlled(
    tmp_path: Path,
) -> None:
    store = SkillOverrideStore()

    result = store.resolve("superpowers_external", "brainstorming")

    assert result.mode == "provider_content_aidocs_runtime"


def test_override_registry_leaves_systematic_debugging_provider_native(
    tmp_path: Path,
) -> None:
    store = SkillOverrideStore()

    result = store.resolve("superpowers_external", "systematic-debugging")

    assert result.mode == "provider_native"


def test_override_registry_is_inspectable_and_deterministic() -> None:
    store = SkillOverrideStore()

    rules = store.list_rules()
    result_a = store.resolve("superpowers_external", "executing-plans")
    result_b = store.resolve("superpowers_external", "executing-plans")

    assert [rule.skill_id for rule in rules[:3]] == [
        "writing-plans",
        "subagent-driven-development",
        "executing-plans",
    ]
    assert any(
        rule.skill_id == "brainstorming"
        and rule.mode == "provider_content_aidocs_runtime"
        for rule in rules
    )
    assert result_a == result_b
