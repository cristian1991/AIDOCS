# Tool Space Reorganization Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the AIDOCS MCP tool surface into a cleaner, user-oriented taxonomy with better names, overview vs precision layering, primary vs debug separation, and reduced UI noise in OpenCode/Claude traces.

**Architecture:** Perform a breaking taxonomy-first refactor. Keep the `aidocs_` namespace, but replace backend-shaped public tool names with product-oriented names. Introduce a cleaner primary surface, preserve precision tools, add better overview tools, and demote low-level plumbing into debug/specialist surfaces. Update all internal callers, tests, and host integrations in one coordinated pass.

**Tech Stack:** Python, MCP server tools, pytest, AIDOCS runtime/session/code/memory/project services.

---

## File Structure

**Modify**
- `mcp/server/aidocs_mcp/mcp_server.py`
  - rename/reorganize exposed tools into the new taxonomy
- `mcp/server/aidocs_mcp/runtime_service.py`
  - add/reshape overview payloads that back the new primary tools
- `core/plugins/aidocs.js`
  - switch host-facing calls to the new tool names/surfaces
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_host_integration.py`
- `mcp/tests/test_opencode_external_skill_integration.py`
- `mcp/tests/test_claude_external_skill_integration.py`
- `mcp/tests/test_claude_hook.py`

**Create**
- `mcp/tests/test_tool_surface_taxonomy.py`
- `mcp/tests/test_tool_overview_payloads.py`
- `mcp/tests/test_tool_precision_paths.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_tool_surface_taxonomy.py -v`

Expected:
- new primary tool names exist
- deprecated backend-shaped public names are removed or demoted
- debug/specialist tools are clearly separated

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`

Expected:
- overview tools produce compact high-level payloads
- context-aware defaults work where intended
- repeated `project_root` / `session_id` noise is reduced for common flows

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_tool_precision_paths.py mcp/tests/test_host_integration.py mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected:
- precision tools still exist for narrow agent needs
- host/plugin callers use the new tool names
- host UI traces become cleaner without losing deterministic behavior

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Define And Enforce The New Tool Taxonomy

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_tool_surface_taxonomy.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_primary_surface_uses_new_taxonomy_names() -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_skills_overview" in tool_names
    assert "aidocs_runtime_host_state" in tool_names
    assert "aidocs_session_overview" in tool_names


def test_backend_shaped_primary_names_are_removed() -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_skill_provider_status_get" not in tool_names
    assert "aidocs_skill_registry_get" not in tool_names
    assert "aidocs_skill_trigger_state_get" not in tool_names


def test_debug_tools_are_named_as_debug_or_specialist_tools() -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_debug_execution_events" in tool_names or "aidocs_execution_events" in tool_names
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_tool_surface_taxonomy.py -v`

Expected: FAIL because the old surface is still exposed.

- [ ] **Step 3: Write full production implementation**

Refactor the exposed tool names in `mcp_server.py` into the new taxonomy, grouped roughly as:

```text
aidocs_project_*
aidocs_session_*
aidocs_skills_*
aidocs_plan_*
aidocs_runtime_*
aidocs_code_*
aidocs_memory_*
aidocs_debug_*
```

Keep the `aidocs_` prefix. Remove `_get` from primary tools where possible.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_tool_surface_taxonomy.py -v`

Expected: PASS

### Task 2: Add User-Facing Overview Tools And Context Defaults

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_tool_overview_payloads.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_skills_overview_returns_provider_selected_active_and_override_state(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_selected_superpowers(tmp_path)
    result = runtime.skills_overview(project, session_id=session_id)
    assert result["provider_states"]["superpowers_external"] == "compatible"
    assert result["selected_skills"]
    assert result["active_skills"]


def test_session_overview_returns_blockers_handoff_and_compliance_summary(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path)
    result = runtime.session_overview(project, session_id=session_id)
    assert "blockers" in result
    assert "handoff" in result
    assert "compliance" in result


def test_project_overview_can_use_current_managed_context_without_explicit_session(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path)
    runtime.hub.managed_mode.set_mode(project, session_id)
    result = runtime.project_overview(project)
    assert result["managed"] is True
    assert result["session_id"] == session_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`

Expected: FAIL because overview tools and cleaner context-aware defaults do not exist yet.

- [ ] **Step 3: Write full production implementation**

Add overview payload builders and expose tools such as:

```python
def project_overview(self, project_root: Path) -> dict[str, object]:
    return {"managed": True, "session_id": "2026-03-25-tool-usage-enforcement-and-gaps", "index_freshness": "ready"}

def session_overview(self, project_root: Path, session_id: str | None = None) -> dict[str, object]:
    return {"session_id": session_id, "blockers": [], "handoff": {}, "compliance": {}}

def skills_overview(self, project_root: Path, session_id: str | None = None) -> dict[str, object]:
    return {"provider_states": {"superpowers_external": "compatible"}, "selected_skills": [], "active_skills": [], "override_modes": {}}

def plan_overview(self, project_root: Path, session_id: str | None = None) -> dict[str, object]:
    return {"session_id": session_id, "current_phase": None, "current_lanes": [], "pending_feedback": []}
```

Rules:
- primary overview tools should infer managed project/session where safe
- precision/debug tools may still require explicit parameters
- payloads should be concise and UI-friendly

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`

Expected: PASS

### Task 3: Preserve Precision Tools And Update Host Callers

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `core/plugins/aidocs.js`
- Modify: `mcp/tests/test_host_integration.py`
- Modify: `mcp/tests/test_opencode_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_external_skill_integration.py`
- Modify: `mcp/tests/test_claude_hook.py`
- Create: `mcp/tests/test_tool_precision_paths.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_precision_tools_still_exist_for_exact_queries() -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_code_find" in tool_names
    assert "aidocs_code_trace" in tool_names
    assert "aidocs_code_read" in tool_names


def test_host_paths_use_new_overview_tools_for_common_views(tmp_path: Path) -> None:
    tool_names = _registered_tool_names()
    assert "aidocs_runtime_host_state" in tool_names
    assert "aidocs_skills_overview" in tool_names
    assert "aidocs_session_overview" in tool_names


def test_debug_tools_are_not_required_for_normal_host_flow(tmp_path: Path) -> None:
    state = _run_opencode_common_flow(tmp_path, prompt="brainstorm homepage")
    assert "aidocs_debug_" not in state["used_tools_text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_tool_precision_paths.py mcp/tests/test_host_integration.py mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: FAIL because host callers still reference the old surface and precision-vs-overview layering is not fully locked in.

- [ ] **Step 3: Write full production implementation**

Update host/plugin/runtime callers to use the new overview tools where common-path UI is intended, while preserving precision tools for narrow agent needs.

Requirements:
- do not force agents into broad tools when exact tools are better
- do not leave normal host flows dependent on plumbing-shaped debug tools

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_tool_precision_paths.py mcp/tests/test_host_integration.py mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: PASS

### Task 4: Full Verification And Final Surface Check

**Files:**
- Modify: `docs/specs/2026-03-30-tool-space-reorganization-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_tool_surface_taxonomy.py -v`

`pytest mcp/tests/test_tool_overview_payloads.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_tool_precision_paths.py mcp/tests/test_host_integration.py mcp/tests/test_opencode_external_skill_integration.py mcp/tests/test_claude_external_skill_integration.py mcp/tests/test_claude_hook.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm reorganization goals are met**

Verify all of these are true:

- primary surface is cleaner and more product-oriented
- debug/specialist tools are distinct
- overview tools reduce visible plumbing calls
- precision tools still exist for narrow needs
- host/plugin traces look materially cleaner than before
