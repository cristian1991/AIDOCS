# Config Normalization And Settings Catalog Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize AIDOCS configuration into clear scopes and domains, separate runtime state from editable config, and introduce a flat settings catalog that can drive validation, comments, and later GUI rendering.

**Architecture:** Replace the current ad hoc mix of `aidocs.toml`, `aidocs-plugin.json`, scattered `/.MEMORY/*.json`, and runtime snapshots with a scoped config model (`global`, `project`, `session`) plus explicit runtime-state artifacts. Keep actual config files simple, push descriptions/allowed values/editability into a flat settings catalog, and make the runtime resolve one effective config view.

**Tech Stack:** Python, TOML/JSON config loading, pytest, AIDOCS MCP/runtime/session state.

---

## File Structure

**Create**
- `mcp/server/aidocs_mcp/config_schema.py`
  - Flat settings catalog and validation metadata.
- `mcp/tests/test_config_schema.py`
- `mcp/tests/test_config_resolution.py`
- `mcp/tests/test_config_migration.py`

**Modify**
- `mcp/server/aidocs_mcp/config.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/skill_store.py`
- `core/plugins/aidocs.js`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/tests/test_runtime_service.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_config_schema.py -v`

Expected:
- flat settings catalog exists
- settings metadata includes descriptions, values, allowed scopes, editability, and security sensitivity

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_config_resolution.py mcp/tests/test_runtime_service.py -v`

Expected:
- effective config resolution works with global/project/session precedence
- runtime can expose the effective config cleanly
- runtime state stays out of editable config

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_config_migration.py mcp/tests/test_runtime_service.py -v`

Expected:
- legacy config/state files are migrated or normalized correctly
- skill provider registry and workflow artifacts are placed/classified correctly
- host/plugin consumers use the normalized config paths

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add The Settings Catalog

**Files:**
- Create: `mcp/server/aidocs_mcp/config_schema.py`
- Create: `mcp/tests/test_config_schema.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_settings_catalog_includes_required_domains() -> None:
    catalog = settings_catalog()
    assert "providers.bundled_superpowers.enabled" in catalog
    assert "skills.activation_mode" in catalog
    assert "security.gui.agent_access" in catalog


def test_setting_metadata_includes_value_descriptions_and_scopes() -> None:
    meta = settings_catalog()["skills.activation_mode"]
    assert meta["type"] == "enum"
    assert meta["allowed_values"] == ["auto", "selected-only", "off"]
    assert "value_descriptions" in meta
    assert "allowed_scopes" in meta
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_config_schema.py -v`

Expected: FAIL because no centralized settings catalog exists yet.

- [ ] **Step 3: Write full production implementation**

Add a flat settings catalog, for example:

```python
def settings_catalog() -> dict[str, dict[str, object]]:
    return {
        "skills.activation_mode": {
            "type": "enum",
            "default": "auto",
            "allowed_values": ["auto", "selected-only", "off"],
            "description": "Controls how AIDOCS activates skills.",
            "value_descriptions": {
                "auto": "Activate skills automatically from runtime intent and workflow state.",
                "selected-only": "Only activate explicitly selected skills.",
                "off": "Disable automatic skill activation.",
            },
            "allowed_scopes": ["global", "project", "session"],
            "agent_editable_scopes": ["project", "session"],
            "security_sensitive": False,
            "requires_restart": False,
        }
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_config_schema.py -v`

Expected: PASS

### Task 2: Add Effective Config Resolution By Scope

**Files:**
- Modify: `mcp/server/aidocs_mcp/config.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Create: `mcp/tests/test_config_resolution.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_effective_config_resolves_session_over_project_over_global(tmp_path: Path) -> None:
    resolver = _make_config_resolver(tmp_path)
    value = resolver.get("skills.activation_mode", project_root=tmp_path / "project", session_id="s1")
    assert value == "off"


def test_runtime_can_expose_effective_config_view(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_config_layers(tmp_path)
    result = runtime.effective_config(project, session_id=session_id)
    assert result["skills"]["activation_mode"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_config_resolution.py mcp/tests/test_runtime_service.py -v`

Expected: FAIL because config is still import-time/global and not scope-aware.

- [ ] **Step 3: Write full production implementation**

Refactor config loading into a resolver service instead of module-global constants, for example:

```python
class ConfigResolver:
    def get(self, key: str, *, project_root: Path | None = None, session_id: str | None = None) -> object:
        return self.effective_config(project_root=project_root, session_id=session_id).get(key)

    def effective_config(self, *, project_root: Path | None = None, session_id: str | None = None) -> dict[str, object]:
        return {
            "skills.activation_mode": "off",
            "conductor.reopen_lanes_on_fullsuite_failure": True,
        }
```

Requirements:
- precedence handled once in the engine
- actual config files remain simple
- runtime can expose a merged effective config view

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_config_resolution.py mcp/tests/test_runtime_service.py -v`

Expected: PASS

### Task 3: Normalize Config And Runtime-State Paths

**Files:**
- Modify: `mcp/server/aidocs_mcp/skill_store.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_config_migration.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_skill_provider_registry_moves_under_memory_config(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    path = runtime.hub.skills.external_provider_registry_path(project)
    assert path.as_posix().endswith("/.MEMORY/config/skill-providers.json")


def test_aidocs_managed_is_classified_as_runtime_binding_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path)
    runtime.hub.managed_mode.set_mode(project, session_id)
    result = runtime.project_overview(project)
    assert result["managed"] is True
    assert result["session_id"] == session_id


def test_workflow_actions_json_is_treated_as_compiled_runtime_artifact(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    result = runtime.project_overview(project)
    assert result["workflow"]["compiled"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_config_migration.py mcp/tests/test_runtime_service.py -v`

Expected: FAIL because paths/classification are still inconsistent.

- [ ] **Step 3: Write full production implementation**

Normalize paths and classification, including:

- move `/.MEMORY/skill-providers.json` -> `/.MEMORY/config/skill-providers.json`
- classify `aidocs-managed.json` as runtime/session-binding state
- classify `workflow-actions.json` as compiled runtime artifact
- update plugin/runtime callers to the normalized locations
- support legacy migration/reads where necessary during transition

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_config_migration.py mcp/tests/test_runtime_service.py -v`

Expected: PASS

### Task 4: Security-Aware Config Editing Policy

**Files:**
- Modify: `mcp/server/aidocs_mcp/config_schema.py`
- Modify: `mcp/server/aidocs_mcp/file_ops.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_file_ops.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_normal_config_can_be_edited_with_explicit_user_permitted_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    target = project / ".MEMORY" / "config" / "project.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"skills": {"activation_mode": "auto"}}\n', encoding="utf-8")
    result = edit_lines(project, ".MEMORY/config/project.json", 1, 1, '{"skills": {"activation_mode": "off"}}')
    assert result["success"] is True


def test_security_config_is_never_agent_editable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    target = project / ".MEMORY" / "config" / "security.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"security": {"gui": {"agent_access": "deny"}}}\n', encoding="utf-8")
    result = edit_lines(project, ".MEMORY/config/security.json", 1, 1, '{"security": {"gui": {"agent_access": "allow"}}}')
    assert result["success"] is False


def test_self_edit_mode_is_not_available_in_release_profile(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    result = runtime.effective_config(project_root=project)
    assert result["security.self_edit_available"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "config or security or self_edit" -v`

Expected: FAIL because the config/security editability distinctions are not yet fully normalized and enforced.

- [ ] **Step 3: Write full production implementation**

Use the settings catalog metadata to enforce:

- editable under controlled conditions for normal config
- never editable for security settings
- self-edit mode excluded from release profile behavior

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "config or security or self_edit" -v`

Expected: PASS

### Task 5: Full Verification And 1.9.0 Config Readiness Check

**Files:**
- Modify: `docs/specs/2026-03-30-config-normalization-and-settings-catalog-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_config_schema.py -v`

`pytest mcp/tests/test_config_resolution.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_config_migration.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "config or security or self_edit" -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm config readiness goals are met**

Verify all of these are true:

- config values stay simple
- metadata is complete enough for validation and GUI rendering
- scope precedence is centralized in code
- runtime state is not confused with config
- bundled skill/provider integration is represented in the normalized config model
- security editability rules are enforceable and visible
