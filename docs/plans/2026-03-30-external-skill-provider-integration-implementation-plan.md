# External Skill Provider Integration Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AIDOCS-native bundled skill-provider integration so curated `superpowers`-derived skill content ships with AIDOCS while AIDOCS owns the orchestration, compatibility handling, host integration, and enforcement for OpenCode and Claude.

**Architecture:** Extend the current `SkillStore` into a provider-aware registry centered on a bundled curated provider, add compatibility and provider-state handling in the runtime layer, build an AIDOCS-native trigger/orchestration layer for bundled skills, and update the OpenCode plugin and Claude runtime path to use AIDOCS-selected bundled skills instead of depending on the external provider plugin/runtime.

**Tech Stack:** Python, Node.js plugin code, pytest, markdown skill files with YAML frontmatter, AIDOCS MCP/runtime/session state.

---

## File Structure

**Create**
- `mcp/server/aidocs_mcp/skill_provider.py`
  - Provider abstractions and bundled-provider resolution helpers.
- `mcp/tests/test_skill_provider_registry.py`
- `mcp/tests/test_skill_provider_compatibility.py`
- `mcp/tests/test_skill_trigger_runtime.py`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_claude_external_skill_integration.py`

**Modify**
- `mcp/server/aidocs_mcp/skill_store.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/server/aidocs_mcp/types.py`
- `core/plugins/aidocs.js`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_plan_tools.py`

**Test**
- `mcp/tests/test_plan_tools.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_host_integration.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_skill_provider_compatibility.py -v`

Expected:
- bundled provider loads correctly by default
- bundled skills are listed with provider attribution
- compatibility status is evaluated by AIDOCS
- incompatible-version user choice path is covered

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -v`

Expected:
- bundled skills can be selected per session
- AIDOCS runtime chooses active bundled skills automatically based on intent/workflow state
- activation reasons are logged or returned deterministically

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_host_integration.py -v`

Expected:
- OpenCode plugin surfaces bundled skills without requiring the superpowers plugin
- Claude path uses bundled-skill state through AIDOCS runtime decisions
- missing/disabled optional providers degrade gracefully

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add Provider-Aware Skill Registry

**Files:**
- Create: `mcp/server/aidocs_mcp/skill_provider.py`
- Modify: `mcp/server/aidocs_mcp/skill_store.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Create: `mcp/tests/test_skill_provider_registry.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_skill_registry_lists_built_in_project_and_external_provider_skills(tmp_path: Path) -> None:
    store, project = _make_skill_store_with_external_provider(tmp_path)

    result = store.list_skills(project)

    assert any(item["provider"] == "superpowers_external" for item in result)
    assert any(item["origin"] for item in result)


def test_external_provider_requires_local_path(tmp_path: Path) -> None:
    store, project = _make_skill_store(tmp_path)

    with pytest.raises(ValueError, match="local path"):
        store.register_external_provider(project, provider_name="superpowers_external", path="")


def test_session_skill_selection_can_reference_imported_skill(tmp_path: Path) -> None:
    store, project, session_id = _make_skill_store_with_external_provider_and_session(tmp_path)

    result = store.set_selected_skills(project, session_id, ["superpowers_external/brainstorming"])

    assert "superpowers_external/brainstorming" in result["selected_skills"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_provider_registry.py -v`

Expected: FAIL because the current skill store only knows built-in and project-local skills.

- [ ] **Step 3: Write full production implementation**

Add provider-aware records, for example:

```python
@dataclass
class ExternalSkillProvider:
    provider_id: str
    root_path: Path
    version: str | None
    compatibility_state: str

@dataclass
class SkillRecord:
    provider: str
    skill_id: str
    name: str
    description: str
    path: str
    origin: str
```

And extend `SkillStore` so it can register and list local-path external providers.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_provider_registry.py -v`

Expected: PASS

### Task 2: Add Compatibility Handling And User Override State

**Files:**
- Modify: `mcp/server/aidocs_mcp/skill_store.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Create: `mcp/tests/test_skill_provider_compatibility.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_incompatible_external_provider_reports_versions_and_choices(tmp_path: Path) -> None:
    runtime, project = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.skill_provider_status(project)

    assert result["provider_state"] == "detected_incompatible"
    assert result["aidocs_version"]
    assert result["provider_version"]
    assert result["compatible_versions"]
    assert result["choices"] == ["disable", "keep_enabled_anyway"]


def test_user_can_override_incompatible_provider(tmp_path: Path) -> None:
    runtime, project = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.set_skill_provider_override(project, "superpowers_external", "keep_enabled_anyway")

    assert result["provider_state"] == "incompatible_but_user_override"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_provider_compatibility.py -v`

Expected: FAIL because compatibility state and override flow do not exist yet.

- [ ] **Step 3: Write full production implementation**

Implement AIDOCS-owned compatibility tracking and persistence, for example:

```python
def skill_provider_status(self, project_root: Path) -> dict[str, object]:
    provider = self.hub.skills.get_provider(project_root, "superpowers_external")
    return {
        "provider_id": provider.provider_id,
        "provider_state": provider.compatibility_state,
        "aidocs_version": self.repo_summary(project_root).get("version"),
        "provider_version": provider.version,
        "compatible_versions": [">=5.0.0", "<6.0.0"],
        "choices": ["disable", "keep_enabled_anyway"],
    }

def set_skill_provider_override(self, project_root: Path, provider_id: str, choice: str) -> dict[str, object]:
    provider = self.hub.skills.set_provider_override(project_root, provider_id, choice)
    return {"provider_id": provider.provider_id, "provider_state": provider.compatibility_state, "override": choice}
```

Use AIDOCS-owned compatibility tables rather than trusting provider metadata.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_provider_compatibility.py -v`

Expected: PASS

### Task 3: Add AIDOCS-Native Skill Trigger Runtime

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Create: `mcp/tests/test_skill_trigger_runtime.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_runtime_selects_external_brainstorming_for_creative_task(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)

    result = runtime.skill_trigger_state(project, session_id, intent="brainstorming")

    assert "superpowers_external/brainstorming" in result["active_skills"]


def test_runtime_selects_external_debugging_for_bug_work(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)

    result = runtime.skill_trigger_state(project, session_id, intent="debugging")

    assert "superpowers_external/systematic-debugging" in result["active_skills"]


def test_incompatible_provider_is_not_auto_activated_without_override(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_incompatible_superpowers(tmp_path)

    result = runtime.skill_trigger_state(project, session_id, intent="planning")

    assert result["active_skills"] == []


def test_runtime_logs_why_external_skill_was_triggered(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)

    result = runtime.skill_trigger_state(project, session_id, intent="verification")

    assert result["triggered"][0]["provider"] == "superpowers_external"
    assert result["triggered"][0]["why"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -k "skill_trigger or active_skills" -v`

Expected: FAIL because imported skills do not yet participate in runtime trigger logic.

- [ ] **Step 3: Write full production implementation**

Add AIDOCS-native trigger/orchestration state, for example:

```python
def skill_trigger_state(self, project_root: Path, session_id: str, intent: str) -> dict[str, object]:
    selected = self.hub.skills.get_selected_skills(project_root, session_id).get("selected_skills", [])
    available = self.hub.skills.list_skills(project_root)
    active = [item for item in available if item["skill_id"] in selected and item.get("compatibility_state") in {"detected_compatible", "incompatible_but_user_override"}]
    triggered = [item for item in active if intent in item.get("trigger_tags", []) or item["name"] in selected]
    return {
        "intent": intent,
        "active_skills": [item["skill_id"] for item in triggered],
        "triggered": [{"skill_id": item["skill_id"], "provider": item["provider"], "why": f"selected+intent:{intent}"} for item in triggered],
    }
```

The implementation must:
- use session-selected skills as strongest signal
- support automatic triggering by intent/workflow state
- rank/import only compatible or user-overridden providers
- log what triggered and why

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -k "skill_trigger or active_skills" -v`

Expected: PASS

### Task 4: Add OpenCode And Claude Integration Paths

**Files:**
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_opencode_external_skill_integration.py`
- Create: `mcp/tests/test_claude_external_skill_integration.py`
- Modify: `mcp/tests/test_host_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_opencode_plugin_surfaces_imported_skill_state_without_superpowers_plugin(tmp_path: Path) -> None:
    state = _run_aidocs_opencode_plugin_with_external_skill(tmp_path, selected=["superpowers_external/brainstorming"])
    assert "superpowers_external/brainstorming" in state["bootstrap"]

def test_claude_startup_context_can_include_imported_skill_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.session_start_state(project, session_id=session_id)
    assert "superpowers_external/brainstorming" in result.get("active_skills", [])

def test_missing_external_provider_degrades_gracefully(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_missing_provider(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="planning")
    assert result["active_skills"] == []
    assert result["provider_state"] == "missing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_host_integration.py -v`

Expected: FAIL because imported external-skill state is not yet integrated into OpenCode/Claude host behavior.

- [ ] **Step 3: Write full production implementation**

Integrate imported-skill state into host behavior:

```javascript
// core/plugins/aidocs.js
// include imported/active skill state in bootstrap context when compatible and enabled
const externalSkills = await readJsonIfExists(path.join(memoryRoot, "config", "external-skill-providers.json"))
if (externalSkills?.active_skills?.length) {
  blocks.push("Imported skills:\n" + externalSkills.active_skills.map((item) => `- ${item}`).join("\n"))
}
```

```python
# runtime_service / claude paths
# surface active imported skill state for Claude-side startup/prompt/tool-time guidance
response["active_imported_skills"] = self.skill_trigger_state(project_root, session_id, intent="planning")
```

OpenCode and Claude should differ only in delivery mechanism, not in core skill-selection logic.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_host_integration.py -v`

Expected: PASS

### Task 5: Full Verification And Integration-Ready Check

**Files:**
- Modify: `docs/specs/2026-03-30-external-skill-provider-integration-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_skill_provider_compatibility.py -v`

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_host_integration.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm integration-ready conditions**

Verify all of these are true:

- external provider registration works from disk
- compatibility state is enforced by AIDOCS
- imported skills can be selected and auto-triggered
- OpenCode and Claude can consume imported skill state without requiring the external provider plugin for normal operation
- missing/disabled providers degrade gracefully
