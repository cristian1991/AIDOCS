# Bundled Provider Completion Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bundled curated skill provider the default AIDOCS experience so bundled skills are available out of the box, use clean canonical names, and no external `superpowers` plugin/runtime is required for normal OpenCode/Claude operation.

**Architecture:** Promote a bundled provider in the runtime/registry layer, keep optional external-provider support secondary or deferable, migrate provider-qualified bundled skill IDs to canonical names, and wire OpenCode + Claude to the bundled provider path by default. Preserve the existing AIDOCS override policy and runtime authority.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime/session state, bundled skill files, OpenCode plugin, Claude hook integration.

---

## File Structure

**Modify**
- `mcp/server/aidocs_mcp/skill_store.py`
- `mcp/server/aidocs_mcp/skill_provider.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `core/plugins/aidocs.js`
- `mcp/tests/test_skill_provider_registry.py`
- `mcp/tests/test_skill_provider_compatibility.py`
- `mcp/tests/test_skill_trigger_runtime.py`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_claude_external_skill_integration.py`

**Create**
- `mcp/tests/test_bundled_provider_completion.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_bundled_provider_completion.py -v`

Expected:
- bundled provider loads by default
- no manual registration is required
- bundled skills appear with canonical names in normal use

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_skill_provider_compatibility.py mcp/tests/test_skill_trigger_runtime.py -v`

Expected:
- bundled skills participate in trigger runtime
- override policy still works
- provider-qualified legacy IDs are migrated cleanly

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected:
- OpenCode works with bundled skills without the external plugin
- Claude works with bundled skills without the external plugin
- host-visible state uses clean canonical bundled names

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Make The Bundled Provider The Default Registry Path

**Files:**
- Modify: `mcp/server/aidocs_mcp/skill_store.py`
- Modify: `mcp/server/aidocs_mcp/skill_provider.py`
- Modify: `mcp/tests/test_skill_provider_registry.py`
- Create: `mcp/tests/test_bundled_provider_completion.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_bundled_provider_is_available_without_manual_registration(tmp_path: Path) -> None:
    store, project = _make_skill_store(tmp_path)
    result = store.list_skills(project)
    assert any(item["provider"] == "aidocs_bundled_superpowers" for item in result)


def test_bundled_provider_is_primary_in_registry_listing(tmp_path: Path) -> None:
    store, project = _make_skill_store(tmp_path)
    result = store.list_skills(project)
    assert result[0]["provider"] == "aidocs_bundled_superpowers"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_bundled_provider_completion.py -v`

Expected: FAIL because the registry still assumes explicit external-provider registration.

- [ ] **Step 3: Write full production implementation**

Implement a bundled-provider-first registry model, for example:

```python
def list_external_providers(self, project_root: Path) -> list[ExternalSkillProvider]:
    providers = [self._bundled_provider(project_root)]
    providers.extend(self._optional_external_providers(project_root))
    return providers
```

Requirements:
- bundled provider available by default
- no manual registration required for the primary experience
- optional external providers remain secondary

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_bundled_provider_completion.py -v`

Expected: PASS

### Task 2: Add Canonical Bundled Skill Names And Migration

**Files:**
- Modify: `mcp/server/aidocs_mcp/skill_store.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/tests/test_skill_trigger_runtime.py`
- Modify: `mcp/tests/test_skill_provider_compatibility.py`
- Modify: `mcp/tests/test_bundled_provider_completion.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_bundled_skill_names_are_clean_in_default_runtime_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="brainstorming")
    assert "brainstorming" in result["active_skills"]


def test_provider_qualified_legacy_id_migrates_to_canonical_name(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_legacy_selected_skill(tmp_path, "superpowers_external/brainstorming")
    result = runtime.skill_trigger_state(project, session_id, intent="brainstorming")
    assert "brainstorming" in result["active_skills"]
    assert "superpowers_external/brainstorming" not in result["active_skills"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_skill_provider_compatibility.py mcp/tests/test_bundled_provider_completion.py -v`

Expected: FAIL because provider-qualified IDs still leak into normal runtime use.

- [ ] **Step 3: Write full production implementation**

Implement bundled-provider canonical naming and migration, for example:

```python
def canonical_skill_id(self, provider_id: str, skill_id: str) -> str:
    if provider_id == "aidocs_bundled_superpowers":
        return skill_id.split("/")[-1]
    return skill_id
```

Requirements:
- clean canonical names in default user-facing/runtime-facing state
- provenance still available in debug/inspection metadata
- migration from provider-qualified legacy IDs is safe and deterministic

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_skill_provider_compatibility.py mcp/tests/test_bundled_provider_completion.py -v`

Expected: PASS

### Task 3: Keep Override Policy And Trigger Runtime Correct Under Bundled Mode

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_skill_trigger_runtime.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_bundled_writing_plans_still_resolves_to_aidocs_native_override(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="planning")
    assert any(item["skill_id"] == "writing-plans" and item["override_mode"] == "aidocs_native_override" for item in result["triggered"])


def test_bundled_brainstorming_still_uses_provider_content_runtime_control(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="brainstorming")
    assert any(item["skill_id"] == "brainstorming" and item["override_mode"] == "provider_content_aidocs_runtime" for item in result["triggered"])


def test_bundled_systematic_debugging_remains_provider_native(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="debugging")
    assert any(item["skill_id"] == "systematic-debugging" and item["override_mode"] == "provider_native" for item in result["triggered"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py -v`

Expected: FAIL because the override/trigger runtime still assumes provider-qualified external naming in some paths.

- [ ] **Step 3: Write full production implementation**

Adapt the runtime so bundled canonical names still resolve through the same override policy correctly.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_trigger_runtime.py -v`

Expected: PASS

### Task 4: Make OpenCode And Claude Use Bundled Skills By Default

**Files:**
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_opencode_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_external_skill_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_opencode_uses_bundled_skills_without_external_plugin(tmp_path: Path) -> None:
    state = _run_aidocs_opencode_plugin_with_bundled_skills(tmp_path, prompt="brainstorm the homepage")
    assert "brainstorming" in state["bootstrap"]


def test_claude_uses_bundled_skills_without_external_plugin(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    result = runtime.host_state(project, session_id=session_id, prompt_text="debug the issue")
    assert "systematic-debugging" in result["skill_state"]["selected_skills"] or result["prompt_state"]["triggered_skills"]


def test_host_views_show_canonical_bundled_skill_names(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_bundled_skills(tmp_path)
    state = runtime.host_state(project, session_id=session_id, prompt_text="brainstorm the homepage")
    flattened = json.dumps(state)
    assert "superpowers_external/brainstorming" not in flattened
    assert "brainstorming" in flattened
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected: FAIL because host integration still reflects the older external-provider framing.

- [ ] **Step 3: Write full production implementation**

Update host/runtime paths so OpenCode and Claude treat the bundled provider as the default skill source and expose clean canonical names.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected: PASS

### Task 5: Final Verification And Completion Check

**Files:**
- Modify: `docs/specs/2026-03-30-bundled-provider-completion-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_skill_provider_registry.py mcp/tests/test_bundled_provider_completion.py -v`

`pytest mcp/tests/test_skill_provider_compatibility.py mcp/tests/test_skill_trigger_runtime.py -v`

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm bundled-provider completion goals are met**

Verify all of these are true:

- bundled provider is available by default
- no manual registration is required
- bundled skills resolve with canonical clean names
- provider-qualified legacy IDs are migrated cleanly
- OpenCode and Claude work without external plugin dependency for normal bundled-skill use
