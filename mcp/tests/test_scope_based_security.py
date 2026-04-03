"""Tests for scope-based edit policy in config_schema.

Security is driven by config scope, not lane ownership.
- Global settings: never agent-editable
- Project/settings: conditionally editable based on policy
- Security settings: never agent-editable
"""

from aidocs_mcp.config_schema import (
    SETTINGS_CATALOG,
    SettingScope,
    is_setting_agent_editable,
)


def _get_settings_by_prefix(prefix: str) -> list[str]:
    """Return all setting paths that start with the given prefix."""
    return [path for path in SETTINGS_CATALOG if path.startswith(prefix)]


# --- Global settings are never agent-editable ---


def test_global_settings_are_never_agent_editable() -> None:
    """Settings with 'global' scope must never be editable by agents."""
    global_settings = [
        path
        for path, meta in SETTINGS_CATALOG.items()
        if "global" in meta.get("allowed_scopes", [])
    ]

    for setting_path in global_settings:
        assert not is_setting_agent_editable(
            setting_path,
            scope="global",
            edit_mode="explicit_user_permitted",
        ), f"Global setting {setting_path} must not be agent-editable"


# --- Project settings can be conditionally editable ---


def test_project_settings_can_be_agent_editable_when_policy_allows() -> None:
    """Project-scoped settings should be editable when policy permits and they're not security-sensitive."""
    project_settings = [
        path
        for path, meta in SETTINGS_CATALOG.items()
        if "project" in meta.get("allowed_scopes", [])
        and not meta.get("security_sensitive", False)
    ]

    assert len(project_settings) > 0, (
        "Should have at least one non-security project setting"
    )

    for setting_path in project_settings:
        assert is_setting_agent_editable(
            setting_path,
            scope="project",
            edit_mode="explicit_user_permitted",
        ), f"Project setting {setting_path} should be editable when policy allows"


# --- Security settings are never agent-editable ---


def test_security_settings_are_never_agent_editable() -> None:
    """Settings marked as security_sensitive must never be editable by agents, regardless of scope."""
    security_settings = [
        path
        for path, meta in SETTINGS_CATALOG.items()
        if meta.get("security_sensitive", False)
    ]

    assert len(security_settings) > 0, (
        "Should have at least one security-sensitive setting"
    )

    for setting_path in security_settings:
        # Test with all possible scopes
        for scope in ["project", "global"]:
            assert not is_setting_agent_editable(
                setting_path,
                scope=scope,  # type: ignore[arg-type]
                edit_mode="explicit_user_permitted",
            ), (
                f"Security setting {setting_path} must not be agent-editable in scope {scope}"
            )
