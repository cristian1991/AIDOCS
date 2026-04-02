"""Settings catalog for aidocs.toml metadata.

This stays intentionally flat: each entry is keyed by the dotted TOML path and
describes the setting without introducing a second nested config model.
"""

from __future__ import annotations

from typing import Literal, TypedDict


SettingType = Literal["integer", "boolean", "string", "string_list"]
SettingScope = Literal["project", "global"]
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
    scope: SettingScope = "project",
) -> SettingMetadata:
    allowed_scopes: list[SettingScope] = [scope]
    agent_editable_scopes: list[SettingScope] = (
        [] if scope == "global" or security_sensitive else ["project"]
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
    ),
    "journal.evict_batch": _setting(
        type="integer",
        default=20,
        description="How many oldest journal entries to archive when the journal is full.",
    ),
    "journal.trivial_actions": _setting(
        type="string_list",
        default=["task_begin", "task_update", "project_update"],
        description="Action kinds that are too trivial to journal.",
    ),
    "journal.min_intent_length": _setting(
        type="integer",
        default=10,
        description="Minimum intent length required before a journal entry is recorded.",
    ),
    "index.extra_skip_dirs": _setting(
        type="string_list",
        default=[],
        description="Extra directories to skip during indexing.",
    ),
    "index.extra_module_hints": _setting(
        type="string_list",
        default=[],
        description="Extra directory names that hint at project modules.",
    ),
    "index.max_json_size": _setting(
        type="integer",
        default=100_000,
        description="Maximum JSON file size in bytes before the indexer skips the file.",
    ),
    "index.enabled_languages": _setting(
        type="string",
        default="all",
        description="Language set used by index-side language filtering.",
    ),
    "languages.enabled": _setting(
        type="string",
        default="all",
        description="Comma-separated language descriptors to load for prompt classification.",
    ),
    "tools.tool_call_timeout": _setting(
        type="integer",
        default=10,
        description="Default timeout in seconds for general MCP tool calls.",
    ),
    "tools.sync_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Default timeout in seconds for sync and indexing operations.",
    ),
    "tools.git_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Default timeout in seconds for git-related operations.",
    ),
    "tools.max_timeout": _setting(
        type="integer",
        default=120,
        description="Maximum timeout in seconds allowed for any tool call.",
    ),
    "agent.directive_style": _setting(
        type="string",
        default="short",
        description="Controls how much directive detail AIDOCS injects into agent context.",
        allowed_values=["short", "detailed"],
        value_descriptions={
            "short": "Inject concise agent-facing directives.",
            "detailed": "Inject the full directive payload when a host needs maximum detail.",
        },
    ),
    "agent.inject_message_directives": _setting(
        type="boolean",
        default=True,
        description="Whether tool directives are injected into user messages for supported hosts.",
    ),
    "agent.inject_rules_on_bootstrap": _setting(
        type="boolean",
        default=True,
        description="Whether project workflow and standards rules are loaded during bootstrap.",
    ),
    "global.aidocs_core_version": _setting(
        type="string",
        default="1.0.0",
        description="AIDOCS core version. Global setting that agents must never modify.",
        scope="global",
    ),
    "dev.dev_mode": _setting(
        type="boolean",
        default=False,
        description="Allows agents to edit AIDOCS MCP source files through guarded edit tools.",
        security_sensitive=True,
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
    ),
    "presentation.helper_skill_excerpt_lines": _setting(
        type="integer",
        default=12,
        description="Maximum non-empty lines injected from a helper skill into host context.",
    ),
    "presentation.helper_skill_excerpt_chars": _setting(
        type="integer",
        default=1200,
        description="Maximum characters injected from a helper skill into host context.",
    ),
    "presentation.workflow_summary_limit": _setting(
        type="integer",
        default=3,
        description="Maximum workflow actions shown in compact workflow summaries.",
    ),
    "presentation.resume_journal_last_n": _setting(
        type="integer",
        default=10,
        description="Default journal entry count returned by session resume bundles.",
    ),
    "presentation.handoff_stale_after_hours": _setting(
        type="integer",
        default=24,
        description="Hours after which handoff freshness is considered stale.",
    ),
    "presentation.handoff_recent_hours": _setting(
        type="integer",
        default=24,
        description="Hours during which a handoff step counts as recently changed.",
    ),
}


def available_config_edit_modes(profile: str = "release") -> list[ConfigEditMode]:
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    return ["explicit_user_permitted"]


def self_edit_available_in_profile(profile: str = "release") -> bool:
    """Check if self-editing is allowed. Returns True only when dev_mode=true in aidocs.toml."""
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    from .config import DEV_MODE
    return DEV_MODE


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
    if scope == "global":
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
