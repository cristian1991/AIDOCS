"""Settings catalog for aidocs.toml metadata.

This stays intentionally flat: each entry is keyed by the dotted TOML path and
describes the setting without introducing a second nested config model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict


SettingType = Literal["integer", "boolean", "string", "string_list"]
SettingScope = Literal["project", "global", "session", "user"]
ConfigEditMode = Literal["explicit_user_permitted"]


class SettingMetadata(TypedDict):
    type: SettingType
    default: int | bool | str | list[str]
    allowed_values: list[str] | None
    description: str
    value_descriptions: dict[str, str]
    allowed_scopes: list[SettingScope]
    agent_editable_scopes: list[SettingScope]
    security_sensitive: bool
    requires_restart: bool


def _setting(
    *,
    type: SettingType,
    default: int | bool | str | list[str],
    description: str,
    allowed_values: list[str] | None = None,
    value_descriptions: dict[str, str] | None = None,
    security_sensitive: bool = False,
    scope: SettingScope | list[SettingScope] = "project",
) -> SettingMetadata:
    allowed_scopes: list[SettingScope] = (
        list(scope) if isinstance(scope, list) else [scope]
    )
    agent_editable_scopes: list[SettingScope] = (
        []
        if "global" in allowed_scopes and len(allowed_scopes) == 1
        or security_sensitive
        else [s for s in allowed_scopes if s not in ("global", "user")]
    )
    return {
        "type": type,
        "default": default,
        "allowed_values": allowed_values,
        "description": description,
        "value_descriptions": value_descriptions or {},
        "allowed_scopes": allowed_scopes,
        "agent_editable_scopes": agent_editable_scopes,
        "security_sensitive": security_sensitive,
        "requires_restart": True,
    }


SETTINGS_CATALOG: dict[str, SettingMetadata] = {
    "journal.max_entries": _setting(
        type="integer",
        default=100,
        description="Maximum journal entries kept per session before eviction starts.",
        scope=["user", "project", "session"],
    ),
    "journal.evict_batch": _setting(
        type="integer",
        default=20,
        description="How many oldest journal entries to archive when the journal is full.",
        scope=["user", "project", "session"],
    ),
    "journal.trivial_actions": _setting(
        type="string_list",
        default=["task_begin", "task_update", "project_update"],
        description="Action kinds that are too trivial to journal.",
        scope=["user", "project", "session"],
    ),
    "journal.min_intent_length": _setting(
        type="integer",
        default=10,
        description="Minimum intent length required before a journal entry is recorded.",
        scope=["user", "project", "session"],
    ),
    "index.extra_skip_dirs": _setting(
        type="string_list",
        default=[],
        description="Extra directories to skip during indexing.",
        scope=["user", "project", "session"],
    ),
    "index.extra_module_hints": _setting(
        type="string_list",
        default=[],
        description="Extra directory names that hint at project modules.",
        scope=["user", "project", "session"],
    ),
    "index.max_json_size": _setting(
        type="integer",
        default=100_000,
        description="Maximum JSON file size in bytes before the indexer skips the file.",
        scope=["user", "project", "session"],
    ),
    "index.enabled_languages": _setting(
        type="string",
        default="all",
        description="Language set used by index-side language filtering.",
        scope=["user", "project", "session"],
    ),
    "index.include_tests": _setting(
        type="boolean",
        default=False,
        description="Include test directories (tests/, test/, __tests__/) in the code index by default.",
        scope=["user", "project"],
    ),
    "languages.enabled": _setting(
        type="string",
        default="all",
        description="Comma-separated language descriptors to load for prompt classification.",
        scope=["user", "project", "session"],
    ),
    "tools.tool_call_timeout": _setting(
        type="integer",
        default=10,
        description="Default timeout in seconds for general MCP tool calls.",
        scope=["user", "project", "session"],
    ),
    "tools.sync_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Default timeout in seconds for sync and indexing operations.",
        scope=["user", "project", "session"],
    ),
    "tools.git_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Default timeout in seconds for git-related operations.",
        scope=["user", "project", "session"],
    ),
    "tools.max_timeout": _setting(
        type="integer",
        default=120,
        description="Maximum timeout in seconds allowed for any tool call.",
        scope=["user", "project", "session"],
    ),
    "agent.host_mode": _setting(
        type="string",
        default="enforced",
        description="How AIDOCS communicates with the agent. 'enforced' = gates block tools, minimal context injection. 'advisory' = verbose directives for hosts without PreToolUse hooks.",
        allowed_values=["enforced", "advisory"],
        value_descriptions={
            "enforced": "Gates enforce tool discipline. Minimal context injection (~30 tokens/turn).",
            "advisory": "Verbose directives injected every turn. For hosts without PreToolUse gate support.",
        },
        scope=["user", "project", "session"],
    ),
    "agent.inject_message_directives": _setting(
        type="boolean",
        default=True,
        description="Whether tool directives are injected into user messages for supported hosts.",
        scope=["user", "project", "session"],
    ),
    "agent.inject_rules_on_bootstrap": _setting(
        type="boolean",
        default=True,
        description="Whether project workflow and standards rules are loaded during bootstrap.",
        scope=["user", "project", "session"],
    ),
    "agent.directive_style": _setting(
        type="string",
        default="short",
        description="How action directives are delivered to the agent.",
        allowed_values=["short", "detailed"],
        value_descriptions={
            "short": "Concise 3-step directive chains.",
            "detailed": "Full directive lists with examples.",
        },
        scope=["user", "project", "session"],
    ),
    "global.aidocs_core_version": _setting(
        type="string",
        default="2.1.0b1",
        description="AIDOCS core version. Global setting that agents must never modify.",
        scope="global",
    ),
    "dev.dev_mode": _setting(
        type="boolean",
        default=False,
        description="Unlocks AIDOCS infrastructure source editing.",
        security_sensitive=True,
        scope=["user", "project"],
    ),
    "dev.allow_config_edit": _setting(
        type="boolean",
        default=False,
        description="Unlocks aidocs.toml editing via agent tools.",
        security_sensitive=True,
        scope=["user", "project"],
    ),
    "gate.enforce": _setting(
        type="boolean",
        default=True,
        description="Tool gates active: bash allowlist, raw tool blocking, destructive command blocking.",
        security_sensitive=True,
        scope=["user", "project"],
    ),
    "gate.output_guard": _setting(
        type="boolean",
        default=True,
        description="Scan tool results for credentials, prompt injections, and sensitive data before returning to the agent.",
        scope=["user", "project"],
    ),
    "gate.output_guard_redact": _setting(
        type="boolean",
        default=True,
        description="Auto-redact detected credentials in tool results. When false, findings are reported but text is not modified.",
        scope=["user", "project"],
    ),
    "gate.bash_allowed": _setting(
        type="string_list",
        default=["cd", "ls", "pwd", "echo", "python", "pytest", "git", "npm", "dotnet", "cargo", "go"],
        description="Commands allowed in bash. Agents can only run these. User intent can override.",
        scope=["user", "project", "session"],
    ),
    "agents.allow_subagents": _setting(
        type="boolean",
        default=False,
        description="Allow agent subprocess delegation. When false, the Agent tool is blocked and agents must use AIDOCS indexed tools directly.",
        security_sensitive=True,
        scope=["user", "project", "session"],
    ),
    "conductor.mode": _setting(
        type="string",
        default="normal",
        description="Conductor execution mode.",
        allowed_values=["normal", "parallel"],
        value_descriptions={
            "normal": "One agent, serial lane execution.",
            "parallel": "Multi-agent, concurrent lanes with isolation.",
        },
        scope=["user", "project", "session"],
    ),
    "conductor.backend": _setting(
        type="string",
        default="claude",
        description="Default agent backend for the conductor.",
        allowed_values=["claude", "codex"],
        value_descriptions={
            "claude": "Anthropic Claude agent via claude CLI.",
            "codex": "OpenAI Codex agent via codex CLI.",
        },
        scope=["user", "project", "session"],
    ),
    "conductor.require_agent_tests": _setting(
        type="boolean",
        default=False,
        description="Agents must write and run tests for their changes before reporting done. The conductor checks for test evidence in the dispatch report.",
        scope=["user", "project", "session"],
    ),
    "conductor.lane_allowed_tools": _setting(
        type="string_list",
        default=["code_*", "session_*", "memory_*", "schema_*", "index_*", "plan_*", "execution_*", "task_*", "verification_*", "code_build_project", "code_test_project", "code_run_command", "skill_*", "context_*", "edit_history_*"],
        description="Glob patterns for tools allowed in conductor lanes. Agents in lanes can only use matching tools.",
        scope=["user", "project", "session"],
    ),
    "conductor.lane_extra_tools": _setting(
        type="string_list",
        default=[],
        description="Additional tool patterns allowed in lanes (for custom MCP tools). Added on top of lane_allowed_tools.",
        scope=["user", "project", "session"],
    ),
    "execution.max_events": _setting(
        type="integer",
        default=10000,
        description="Maximum execution events in the database. Oldest events are pruned when this limit is exceeded.",
        scope=["user", "project"],
    ),
    "execution.auto_prune_days": _setting(
        type="integer",
        default=7,
        description="Auto-delete execution events older than this many days. Set 0 to disable.",
        scope=["user", "project"],
    ),
    "code_quality.comment_enforcement": _setting(
        type="string",
        default="advisory",
        description="Controls how strictly agent edits must follow comment-quality rules.",
        allowed_values=["strict", "advisory", "off"],
        value_descriptions={
            "strict": "Require comment-quality rules during agent edits.",
            "advisory": "Remind agents about comment-quality rules without blocking edits.",
            "off": "Disable comment-quality rule reminders and enforcement.",
        },
        scope=["user", "project", "session"],
    ),
    "presentation.helper_skill_excerpt_lines": _setting(
        type="integer",
        default=12,
        description="Maximum non-empty lines injected from a helper skill into host context.",
        scope=["user", "project"],
    ),
    "presentation.helper_skill_excerpt_chars": _setting(
        type="integer",
        default=1200,
        description="Maximum characters injected from a helper skill into host context.",
        scope=["user", "project"],
    ),
    "presentation.workflow_summary_limit": _setting(
        type="integer",
        default=3,
        description="Maximum workflow actions shown in compact workflow summaries.",
        scope=["user", "project"],
    ),
    "presentation.resume_journal_last_n": _setting(
        type="integer",
        default=10,
        description="Default journal entry count returned by session resume bundles.",
        scope=["user", "project", "session"],
    ),
    "presentation.handoff_stale_after_hours": _setting(
        type="integer",
        default=24,
        description="Hours after which handoff freshness is considered stale.",
        scope=["user", "project"],
    ),
    "presentation.handoff_recent_hours": _setting(
        type="integer",
        default=24,
        description="Hours during which a handoff step counts as recently changed.",
        scope=["user", "project"],
    ),
}


def available_config_edit_modes(profile: str = "release") -> list[ConfigEditMode]:
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    return ["explicit_user_permitted"]


def self_edit_available_in_profile(profile: str = "release", project_root: Path | None = None) -> bool:
    """Check if self-editing of AIDOCS source is allowed (dev_mode=true)."""
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    if project_root is not None:
        from .config import ConfigResolver
        return bool(
            ConfigResolver().effective_config(project_root=project_root)
            .get("dev", {}).get("dev_mode", False)
        )
    from .config import DEV_MODE
    return bool(DEV_MODE)


def is_setting_agent_editable(
    setting_path: str,
    *,
    scope: SettingScope = "project",
    edit_mode: ConfigEditMode | None = None,
) -> bool:
    if edit_mode != "explicit_user_permitted":
        return False

    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None or metadata["security_sensitive"]:
        return False
    if scope not in metadata["allowed_scopes"]:
        return False
    # Agents must never write to global or user configs — those are human-owned
    if scope in ("global", "user"):
        return False

    editable_scopes = metadata["agent_editable_scopes"] or [
        allowed_scope
        for allowed_scope in metadata["allowed_scopes"]
        if allowed_scope == scope
    ]
    return scope in editable_scopes


def validate_setting_value(setting_path: str, value: object) -> None:
    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None:
        raise ValueError(f"Unknown config setting: {setting_path}.")

    setting_type = metadata["type"]
    if setting_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"Config setting {setting_path} requires an integer value."
            )
    elif setting_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Config setting {setting_path} requires a boolean value.")
    elif setting_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"Config setting {setting_path} requires a string value.")
    elif setting_type == "string_list":
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError(
                f"Config setting {setting_path} requires a list of strings."
            )
    else:
        raise ValueError(
            f"Unsupported config setting type for {setting_path}: {setting_type}."
        )

    allowed_values = metadata["allowed_values"]
    if (
        allowed_values is not None
        and isinstance(value, str)
        and value not in allowed_values
    ):
        allowed = ", ".join(allowed_values)
        raise ValueError(f"Config setting {setting_path} must be one of: {allowed}.")
