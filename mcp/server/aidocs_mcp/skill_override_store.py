from __future__ import annotations

from aidocs_mcp.types import SkillOverrideDecision, SkillOverrideRule

DEFAULT_OVERRIDE_RULES: tuple[SkillOverrideRule, ...] = (
    SkillOverrideRule(
        provider_match="superpowers_external",
        skill_id="writing-plans",
        mode="aidocs_native_override",
        reason="planning orchestration stays AIDOCS-native",
    ),
    SkillOverrideRule(
        provider_match="superpowers_external",
        skill_id="subagent-driven-development",
        mode="aidocs_native_override",
        reason="subagent orchestration stays AIDOCS-native",
    ),
    SkillOverrideRule(
        provider_match="superpowers_external",
        skill_id="executing-plans",
        mode="aidocs_native_override",
        reason="plan execution control stays AIDOCS-native",
    ),
    SkillOverrideRule(
        provider_match="superpowers_external",
        skill_id="brainstorming",
        mode="provider_content_aidocs_runtime",
        reason="provider skill content runs under AIDOCS workflow authority",
    ),
)


class SkillOverrideStore:
    def __init__(self, rules: tuple[SkillOverrideRule, ...] = DEFAULT_OVERRIDE_RULES) -> None:
        self._rules = rules

    def list_rules(self) -> list[SkillOverrideRule]:
        return list(self._rules)

    def resolve(self, provider_id: str, skill_id: str) -> SkillOverrideDecision:
        for rule in self._rules:
            if provider_id.startswith(rule.provider_match) and skill_id == rule.skill_id:
                return SkillOverrideDecision(
                    skill_id=skill_id,
                    provider=provider_id,
                    provider_match=rule.provider_match,
                    mode=rule.mode,
                    reason=rule.reason,
                )
        return SkillOverrideDecision(
            skill_id=skill_id,
            provider=provider_id,
            provider_match=provider_id,
            mode="provider_native",
            reason="no override rule matched",
        )
