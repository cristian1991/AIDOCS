# Skill Override Policy Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit AIDOCS skill override policy so orchestration-critical skills use AIDOCS-native implementations, provider-content/runtime-controlled skills use external content under AIDOCS authority, and provider-native skills continue to work normally.

**Architecture:** Introduce a small override registry owned by AIDOCS, integrate mode resolution into the skill trigger/runtime path, and make host/runtime surfaces expose which mode won and why. Keep the model intentionally small: `aidocs_native_override`, `provider_content_aidocs_runtime`, and `provider_native`.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime/session state, external skill provider metadata.

---

## File Structure

**Create**
- `mcp/server/aidocs_mcp/skill_override_store.py`
  - override registry defaults, mode resolution helpers, and inspectable override state.
- `mcp/tests/test_skill_override_policy.py`
- `mcp/tests/test_skill_override_runtime.py`

**Modify**
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/server/aidocs_mcp/types.py`
- `mcp/server/aidocs_mcp/skill_store.py`
- `mcp/tests/test_skill_trigger_runtime.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_claude_external_skill_integration.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_skill_override_policy.py -v`

Expected:
- override registry is inspectable
- the three modes resolve deterministically
- initial AIDOCS-native override set is enforced

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_skill_override_runtime.py mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -v`

Expected:
- runtime chooses the correct mode
- AIDOCS-native overrides win for orchestration-critical skills
- provider-content/runtime-controlled skills use external content but AIDOCS runtime authority
- provider-native skills still work normally

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected:
- OpenCode and Claude surfaces expose the resolved override mode where needed
- host behavior stays aligned with runtime decisions

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add The Override Registry And Resolution Model

**Files:**
- Create: `mcp/server/aidocs_mcp/skill_override_store.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Create: `mcp/tests/test_skill_override_policy.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_override_registry_marks_writing_plans_as_aidocs_native(tmp_path: Path) -> None:
    store = SkillOverrideStore()
    result = store.resolve("superpowers_external", "writing-plans")
    assert result.mode == "aidocs_native_override"


def test_override_registry_marks_brainstorming_as_provider_content_runtime_controlled(tmp_path: Path) -> None:
    store = SkillOverrideStore()
    result = store.resolve("superpowers_external", "brainstorming")
    assert result.mode == "provider_content_aidocs_runtime"


def test_override_registry_leaves_systematic_debugging_provider_native(tmp_path: Path) -> None:
    store = SkillOverrideStore()
    result = store.resolve("superpowers_external", "systematic-debugging")
    assert result.mode == "provider_native"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_override_policy.py -v`

Expected: FAIL because no override registry exists yet.

- [ ] **Step 3: Write full production implementation**

Add a dedicated override store, for example:

```python
@dataclass
class SkillOverrideDecision:
    skill_id: str
    provider_match: str
    mode: str
    reason: str

class SkillOverrideStore:
    def resolve(self, provider_id: str, skill_id: str) -> SkillOverrideDecision:
        for rule in DEFAULT_OVERRIDE_RULES:
            if provider_id.startswith(rule.provider_match) and skill_id == rule.skill_id:
                return SkillOverrideDecision(skill_id=skill_id, provider_match=rule.provider_match, mode=rule.mode, reason=rule.reason)
        return SkillOverrideDecision(skill_id=skill_id, provider_match=provider_id, mode="provider_native", reason="no override rule matched")
```

Initial override set should include:
- `writing-plans`
- `subagent-driven-development`
- `executing-plans`

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_override_policy.py -v`

Expected: PASS

### Task 2: Integrate Override Resolution Into Runtime Trigger Decisions

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Create: `mcp/tests/test_skill_override_runtime.py`
- Modify: `mcp/tests/test_skill_trigger_runtime.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_runtime_uses_aidocs_native_override_for_writing_plans(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="planning")
    assert any(item["mode"] == "aidocs_native_override" for item in result["triggered"])


def test_runtime_uses_external_brainstorming_content_under_aidocs_runtime_control(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="brainstorming")
    assert any(item["mode"] == "provider_content_aidocs_runtime" for item in result["triggered"])


def test_runtime_leaves_systematic_debugging_provider_native(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="debugging")
    assert any(item["mode"] == "provider_native" for item in result["triggered"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_skill_override_runtime.py mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -k "override or mode" -v`

Expected: FAIL because runtime currently has no override-policy resolution layer.

- [ ] **Step 3: Write full production implementation**

Integrate override resolution into the runtime trigger path, for example:

```python
def _resolve_skill_mode(self, provider_id: str, skill_id: str) -> SkillOverrideDecision:
    return self.hub.skill_overrides.resolve(provider_id, skill_id)

def skill_trigger_state(self, project_root: Path, session_id: str, intent: str, workflow_state: str | None = None):
    # attach override mode and reason to triggered skills
    triggered = self._build_skill_trigger_decision(project_root, session_id, intent, workflow_state)
    for item in triggered:
        decision = self._resolve_skill_mode(item["provider"], item["skill_id"])
        item["mode"] = decision.mode
        item["override_reason"] = decision.reason
    return {"intent": intent, "triggered": triggered, "active_skills": [item["skill_id"] for item in triggered]}
```

Requirements:
- AIDOCS-native override must win deterministically
- provider-content/runtime-controlled must still use external content IDs
- provider-native must remain unchanged except for logging/inspection

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_skill_override_runtime.py mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -k "override or mode" -v`

Expected: PASS

### Task 3: Expose Override Decisions Through MCP And Host Integrations

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/tests/test_opencode_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_external_skill_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_mcp_skill_trigger_tool_surfaces_override_modes(tmp_path: Path) -> None:
    server, project, session_id = _make_server_with_selected_superpowers(tmp_path)
    result = server.call_tool("skill_trigger_state_get", {"project_root": str(project), "session_id": session_id, "intent": "planning"})
    assert any(item["mode"] == "aidocs_native_override" for item in result["triggered"])


def test_opencode_runtime_state_can_include_override_mode_metadata(tmp_path: Path) -> None:
    state = _run_aidocs_opencode_plugin_with_external_skill(tmp_path, selected=["superpowers_external/writing-plans"], prompt="write the plan")
    assert "aidocs_native_override" in state["bootstrap"]


def test_claude_runtime_state_can_include_override_mode_metadata(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skill_trigger_state(project, session_id, intent="planning")
    assert any(item["mode"] == "aidocs_native_override" for item in result["triggered"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_runtime_service.py -k "override_mode or mode_metadata" -v`

Expected: FAIL because override decisions are not yet surfaced to user-facing/runtime tools.

- [ ] **Step 3: Write full production implementation**

Expose override decisions where appropriate, for example:

```python
@server.tool()
def skill_override_registry_get(project_root: str) -> dict[str, Any]:
    return {"rules": runtime.skill_override_registry(Path(project_root))}
```

And include mode metadata in host-facing imported-skill runtime state where it helps debugging/inspection.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_runtime_service.py -k "override_mode or mode_metadata" -v`

Expected: PASS

### Task 4: Final Verification And Policy Check

**Files:**
- Modify: `docs/specs/2026-03-30-skill-override-policy-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_skill_override_policy.py -v`

`pytest mcp/tests/test_skill_override_runtime.py mcp/tests/test_skill_trigger_runtime.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm policy goals are met**

Verify all of these are true:

- override registry is inspectable
- AIDOCS-native overrides win for orchestration-critical skills
- provider-content/runtime-controlled skills still use external content
- provider-native skills still run normally
- session-selected skills still participate correctly under override resolution
