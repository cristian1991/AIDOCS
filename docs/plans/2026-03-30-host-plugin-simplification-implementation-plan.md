# Host And Plugin Simplification Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify OpenCode and Claude integration by making AIDOCS runtime the single producer of canonical host-visible state, reducing plugin reconstruction logic and clearly separating prompt-time live state from cached session/startup state.

**Architecture:** Build one canonical runtime host-state payload that includes session, skill, prompt, inspection, and host-action sections. Move host-facing state decisions into runtime, keep only delivery/rendering logic in the OpenCode plugin and Claude hook path, and remove stale or duplicated reconstruction paths. Preserve current behavior where possible while reducing complexity.

**Tech Stack:** Python, Node.js plugin code, pytest, AIDOCS MCP/runtime/session state, Claude hook integration.

---

## File Structure

**Create**
- `mcp/tests/test_host_state_contract.py`
- `mcp/tests/test_opencode_plugin_simplification.py`
- `mcp/tests/test_claude_host_contract.py`

**Modify**
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `core/plugins/aidocs.js`
- `mcp/server/aidocs_mcp/claude_hook.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_claude_external_skill_integration.py`
- `mcp/tests/test_claude_hook.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_host_state_contract.py mcp/tests/test_runtime_service.py -v`

Expected:
- one canonical host-state payload exists
- prompt-time state is live
- session/startup state is clearly separated and cacheable

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_opencode_plugin_simplification.py mcp/tests/test_opencode_external_skill_integration.py -v`

Expected:
- OpenCode plugin consumes canonical runtime payload
- plugin reconstruction logic is reduced
- prompt-time route failures do not leak stale state

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_claude_host_contract.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected:
- Claude path consumes the same core decision layer as OpenCode
- startup/prompt/tool-time host behavior aligns with runtime payloads

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add The Canonical Runtime Host-State Contract

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Create: `mcp/tests/test_host_state_contract.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_runtime_host_state_contract_contains_session_skill_prompt_and_inspection_sections(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.host_state(project, session_id, prompt_text="debug the issue")
    assert set(result.keys()) >= {"session_state", "skill_state", "prompt_state", "inspection_state", "host_actions"}


def test_prompt_state_is_live_and_not_sourced_from_cached_snapshot(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    first = runtime.host_state(project, session_id, prompt_text="brainstorm a feature")
    second = runtime.host_state(project, session_id, prompt_text="run the tests")
    assert first["prompt_state"] != second["prompt_state"]


def test_session_state_can_be_cached_without_affecting_prompt_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.host_state(project, session_id, prompt_text="write the plan")
    assert result["session_state"]["session_id"] == session_id
    assert result["prompt_state"]["intent"] == "planning"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_host_state_contract.py mcp/tests/test_runtime_service.py -k "host_state or prompt_state or session_state" -v`

Expected: FAIL because no single canonical host-state payload exists yet.

- [ ] **Step 3: Write full production implementation**

Add a canonical runtime contract, for example:

```python
def host_state(self, project_root: Path, session_id: str | None = None, prompt_text: str | None = None) -> dict[str, object]:
    return {
        "session_state": {"managed": True, "session_id": session_id, "plan_ready": True},
        "skill_state": {"selected_skills": ["superpowers_external/brainstorming"], "active_skills": []},
        "prompt_state": {"intent": "debugging", "triggered_skills": []},
        "inspection_state": {"provider_states": [{"provider_id": "superpowers_external", "state": "detected_compatible"}]},
        "host_actions": {"inject_context": ["Use indexed retrieval first."]},
    }
```

Requirements:
- prompt-time skill activation must always be computed live
- session/startup information may be cached/persisted when appropriate
- no stale prompt-state fallback

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_host_state_contract.py mcp/tests/test_runtime_service.py -k "host_state or prompt_state or session_state" -v`

Expected: PASS

### Task 2: Simplify The OpenCode Plugin To Consume Runtime Host State

**Files:**
- Modify: `core/plugins/aidocs.js`
- Create: `mcp/tests/test_opencode_plugin_simplification.py`
- Modify: `mcp/tests/test_opencode_external_skill_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_opencode_plugin_consumes_runtime_host_state_payload(tmp_path: Path) -> None:
    state = _run_plugin_with_runtime_host_state(tmp_path, prompt="brainstorm homepage")
    assert state["source"] == "runtime_host_state"


def test_opencode_plugin_does_not_reconstruct_prompt_state_from_startup_snapshot(tmp_path: Path) -> None:
    state = _run_plugin_with_route_failure_and_stale_snapshot(tmp_path)
    assert "brainstorming" not in state["prompt_context"].lower()


def test_opencode_plugin_thins_mode_reconstruction_logic(tmp_path: Path) -> None:
    state = _run_plugin_with_provider_content_override(tmp_path)
    assert state["mode_metadata_source"] == "runtime"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_opencode_plugin_simplification.py mcp/tests/test_opencode_external_skill_integration.py -k "runtime_host_state or stale_snapshot or mode_metadata_source" -v`

Expected: FAIL because the plugin still reconstructs too much host state itself.

- [ ] **Step 3: Write full production implementation**

Refactor the plugin to consume the canonical runtime payload, for example:

```javascript
async function runAidocsHostState(projectRoot, sessionID, promptText) {
  return await runAidocsPromptRoute(projectRoot, promptText, "host_state", sessionID)
}

function buildPromptContext(state, promptText, activeCommand, activeCommandMeta, promptHostState) {
  const hostState = promptHostState || state.hostState
  return renderHostStatePromptContext(hostState, promptText, activeCommand, activeCommandMeta)
}
```

Requirements:
- plugin remains responsible for delivery/rendering only
- prompt-time route failure must not fall back to stale prompt-specific state
- startup/session state may still come from cached runtime state

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_opencode_plugin_simplification.py mcp/tests/test_opencode_external_skill_integration.py -k "runtime_host_state or stale_snapshot or mode_metadata_source" -v`

Expected: PASS

### Task 3: Align Claude With The Same Runtime Host-State Contract

**Files:**
- Modify: `mcp/server/aidocs_mcp/claude_hook.py`
- Create: `mcp/tests/test_claude_host_contract.py`
- Modify: `mcp/tests/test_claude_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_hook.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_claude_hook_consumes_runtime_host_state_contract_for_session_start(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    result = handler.handle({"hook_event_name": "SessionStart", "cwd": str(project_root)})
    payload = result["hookSpecificOutput"]
    assert "AIDOCS startup check" in payload["additionalContext"]


def test_claude_hook_consumes_runtime_host_state_contract_for_user_prompt_submit(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    result = handler.handle({"hook_event_name": "UserPromptSubmit", "cwd": str(project_root), "prompt": "brainstorm the homepage"})
    assert result is not None


def test_claude_hook_and_opencode_share_same_prompt_level_skill_decision_source(tmp_path: Path) -> None:
    runtime, project_root, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    host_state = runtime.host_state(project_root, session_id=session_id, prompt_text="brainstorm the homepage")
    assert host_state["prompt_state"]["intent"] == "brainstorming"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_claude_host_contract.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: FAIL because Claude still assembles host-facing state separately from the new canonical contract.

- [ ] **Step 3: Write full production implementation**

Refactor Claude host behavior to consume the same runtime contract, for example:

```python
host_state = self.runtime.host_state(project_root, session_id=resolved_session_id, prompt_text=prompt)
```

Requirements:
- Claude remains a hook-based delivery path
- but the state it renders should come from the same runtime decision layer as OpenCode

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_claude_host_contract.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: PASS

### Task 4: Full Verification And Host Simplification Check

**Files:**
- Modify: `docs/specs/2026-03-30-host-plugin-simplification-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_host_state_contract.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_opencode_plugin_simplification.py mcp/tests/test_opencode_external_skill_integration.py -v`

`pytest mcp/tests/test_claude_host_contract.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm milestone goals are met**

Verify all of these are true:

- one canonical runtime host-state payload exists
- OpenCode plugin is thinner and less reconstructive
- Claude/OpenCode consume the same core decision layer
- prompt-time state is live and not replaced by stale snapshot fallbacks
- session/startup state is clearly separated from prompt-time state
- host behavior is simpler to reason about than before
