# Conductor Pilot Tool-Space Reorganization Implementation Plan

> **For conductor validation:** This is a lane-native pilot intended to be executed by the AIDOCS conductor rather than by task-by-task external orchestration. The goal is to test conductor authority, not merely to finish the feature.

**Goal:** Validate whether AIDOCS conductor can safely and usefully execute a real lane-based plan on the tool-space reorganization feature.

**Architecture:** The pilot uses one foundational phase, two parallel host lanes, and two final integration/verification lanes. AIDOCS conductor should own lane scheduling, conflict blocking, lane progress, and user-interrupt handling. Behavioral `superpowers` skills may still assist inside lanes, but orchestration-critical skills must not control execution.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime, OpenCode plugin, Claude hook integration.

---

## Conductor Rules For This Pilot

- AIDOCS conductor is the execution authority.
- Do not let the following external process skills control the run:
  - `writing-plans`
  - `subagent-driven-development`
  - `executing-plans`
- One agent owns one lane.
- Lane review happens after lane completion.
- Overlapping-file lanes must never run together.
- If emergent overlap appears, the conductor pauses affected lanes.

---

## Phase 1: Taxonomy Foundation

### Lane `taxonomy-core`

Files:
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_tool_surface_taxonomy.py`

- [ ] **Step 1: Write the failing test**

```python
def test_primary_surface_uses_new_taxonomy_names() -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_skills_overview" in tool_names
    assert "aidocs_runtime_host_state" in tool_names
    assert "aidocs_session_overview" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_tool_surface_taxonomy.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Rename the public tool surface in `mcp_server.py` to the new taxonomy while preserving a clean split between primary and debug/specialist tools.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_tool_surface_taxonomy.py -v`
Expected: PASS

### Lane `overview-payloads`

depends_on:
- `taxonomy-core`

Files:
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Create: `mcp/tests/test_tool_overview_payloads.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_skills_overview_returns_provider_selected_active_and_override_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skills_overview(project, session_id=session_id)
    assert result["provider_states"]["superpowers_external"] == "compatible"
    assert result["selected_skills"]
    assert result["active_skills"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add overview payload builders (`project_overview`, `session_overview`, `skills_overview`, `plan_overview`) with safe context-aware defaults.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`
Expected: PASS

---

## Phase 2: Host Integrations

### Lane `opencode-host`

depends_on:
- `overview-payloads`

Files:
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/tests/test_opencode_external_skill_integration.py`
- Modify: `mcp/tests/test_tool_precision_paths.py`

- [ ] **Step 1: Write the failing test**

```python
def test_host_paths_use_new_overview_tools_for_common_views(tmp_path: Path) -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_runtime_host_state" in tool_names
    assert "aidocs_skills_overview" in tool_names
    assert "aidocs_session_overview" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_tool_precision_paths.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Switch OpenCode common-path calls to the new overview tools while preserving precision-path behavior for narrow reads.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_tool_precision_paths.py -v`
Expected: PASS

### Lane `claude-host`

depends_on:
- `overview-payloads`

Files:
- Modify: `mcp/server/aidocs_mcp/claude_hook.py`
- Modify: `mcp/tests/test_claude_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_hook.py`

- [ ] **Step 1: Write the failing test**

```python
def test_claude_runtime_state_uses_new_overview_tools(tmp_path: Path) -> None:
    handler, project_root = _make_handler(tmp_path)
    result = handler.handle({"hook_event_name": "UserPromptSubmit", "cwd": str(project_root), "prompt": "brainstorm the homepage"})
    assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Switch Claude common-path state to the new overview/runtime tools while preserving hook safety behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`
Expected: PASS

---

## Phase 3: Integration And Verification

### Lane `integration`

depends_on:
- `opencode-host`
- `claude-host`

Files:
- Modify: `mcp/tests/test_host_integration.py`
- Modify: `mcp/tests/test_tool_precision_paths.py`
- Modify: `mcp/tests/test_tool_surface_taxonomy.py`

- [ ] **Step 1: Write the failing integration test**

```python
def test_debug_tools_are_not_required_for_normal_host_flow(tmp_path: Path) -> None:
    state = _run_opencode_common_flow(tmp_path, prompt="brainstorm homepage")
    assert "aidocs_debug_" not in state["used_tools_text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_host_integration.py mcp/tests/test_tool_precision_paths.py mcp/tests/test_tool_surface_taxonomy.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Run cross-host verification on the new tool surface, fix integration regressions, and verify overview tools reduced visible plumbing without harming precision tools.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_host_integration.py mcp/tests/test_tool_precision_paths.py mcp/tests/test_tool_surface_taxonomy.py -v`
Expected: PASS

### Lane `full-suite`

depends_on:
- `integration`

Files:
- `mcp/tests/**`

- [ ] **Step 1: Run the full MCP suite**

Run: `pytest mcp/tests -v`
Expected: PASS

- [ ] **Step 2: Record conductor pilot outcome**

Assess whether:
- conductor remained the execution authority
- `opencode-host` and `claude-host` truly ran in parallel
- lane progress/status was useful
- the final integrated result passed the full suite
