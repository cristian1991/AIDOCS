from aidocs_mcp.config_schema import SETTINGS_CATALOG


def test_settings_catalog_includes_required_domains() -> None:
    required_domains = {
        "journal",
        "index",
        "languages",
        "tools",
        "agent",
        "dev",
        "code_quality",
    }

    actual_domains = {path.split(".", 1)[0] for path in SETTINGS_CATALOG}

    assert required_domains.issubset(actual_domains)
    assert "journal.max_entries" in SETTINGS_CATALOG
    assert "tools.max_timeout" in SETTINGS_CATALOG
    assert "agent.directive_style" in SETTINGS_CATALOG


def test_setting_metadata_includes_value_descriptions_and_scopes() -> None:
    setting = SETTINGS_CATALOG["agent.directive_style"]

    assert setting["type"] == "string"
    assert setting["default"] == "short"
    assert setting["allowed_values"] == ["short", "detailed"]
    assert setting["description"]
    assert setting["value_descriptions"] == {
        "short": "Inject concise agent-facing directives.",
        "detailed": "Inject the full directive payload when a host needs maximum detail.",
    }
    assert setting["allowed_scopes"] == ["project"]
    assert setting["agent_editable_scopes"] == []
    assert setting["security_sensitive"] is False
    assert setting["requires_restart"] is True


def test_every_setting_includes_required_metadata_fields() -> None:
    required_fields = {
        "type",
        "default",
        "allowed_values",
        "description",
        "value_descriptions",
        "allowed_scopes",
        "agent_editable_scopes",
        "security_sensitive",
        "requires_restart",
    }

    for path, metadata in SETTINGS_CATALOG.items():
        assert set(metadata) == required_fields, path
        assert isinstance(metadata["description"], str) and metadata["description"], path
        assert isinstance(metadata["security_sensitive"], bool), path
        assert isinstance(metadata["requires_restart"], bool), path


def test_catalog_metadata_invariants_hold_for_every_setting() -> None:
    for path, metadata in SETTINGS_CATALOG.items():
        allowed_scopes = metadata["allowed_scopes"]
        agent_editable_scopes = metadata["agent_editable_scopes"]

        assert isinstance(allowed_scopes, list), path
        assert allowed_scopes == ["project"], path
        assert isinstance(agent_editable_scopes, list), path
        assert set(agent_editable_scopes).issubset(set(allowed_scopes)), path

        allowed_values = metadata["allowed_values"]
        value_descriptions = metadata["value_descriptions"]
        assert isinstance(value_descriptions, dict), path

        if allowed_values is None:
            assert value_descriptions == {}, path
        else:
            assert isinstance(allowed_values, list), path
            assert set(value_descriptions) == set(allowed_values), path


def test_catalog_default_matches_declared_type_for_every_setting() -> None:
    for path, metadata in SETTINGS_CATALOG.items():
        default = metadata["default"]
        setting_type = metadata["type"]

        if setting_type == "integer":
            assert isinstance(default, int) and not isinstance(default, bool), path
        elif setting_type == "boolean":
            assert isinstance(default, bool), path
        elif setting_type == "string":
            assert isinstance(default, str), path
        elif setting_type == "string_list":
            assert isinstance(default, list), path
            assert all(isinstance(item, str) for item in default), path
        else:
            raise AssertionError(f"Unexpected setting type for {path}: {setting_type}")
