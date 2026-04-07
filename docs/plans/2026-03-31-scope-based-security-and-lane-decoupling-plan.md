# Scope-Based Security And Lane Decoupling Implementation Plan

> **For agentic workers:** Use this plan to remove the mistaken coupling between lane ownership and edit security. The security boundary becomes config scope driven (`global` never editable by agents, `project/session` conditionally editable), while lane files remain conductor hints for coordination and context preservation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple lane ownership from edit security so the conductor can use plan files as coordination hints, while the real editability boundary is enforced by config scope and security policy.

**Architecture:** Preserve conductor lane ownership for routing/context, but remove lane files from the primary security model. Security decisions should be made from normalized config scope and security policy: global settings never agent-editable, project/session settings conditionally editable, security settings never agent-editable. Update query/edit guardrails and conductor logic accordingly.

**Tech Stack:** Python, pytest, AIDOCS runtime/conductor/config schema/file ops.

---

## File Structure

**Modify:**
- `mcp/server/aidocs_mcp/file_ops.py`
- `mcp/server/aidocs_mcp/config_schema.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/plan_conductor.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/tests/test_file_ops.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_plan_conductor_interaction.py`
- `mcp/tests/test_query_gate_ux.py`

**Create:**
- `mcp/tests/test_scope_based_security.py`

---

### Task 1: Define Scope-Based Edit Policy

**Files:**
- Modify: `mcp/server/aidocs_mcp/config_schema.py`
- Create: `mcp/tests/test_scope_based_security.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_global_settings_are_never_agent_editable() -> None:
    policy = settings_catalog()["providers.bundled_superpowers.enabled"]
    assert "global" not in policy["agent_editable_scopes"]

def test_project_settings_can_be_agent_editable_when_policy_allows() -> None:
    policy = settings_catalog()["skills.activation_mode"]
    assert "project" in policy["agent_editable_scopes"]

def test_security_settings_are_never_agent_editable() -> None:
    policy = settings_catalog()["security.gui.agent_access"]
    assert policy["security_sensitive"] is True
    assert policy["agent_editable_scopes"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_scope_based_security.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add explicit scope/editability metadata so security is driven by config scope, not lane ownership.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_scope_based_security.py -v`
Expected: PASS

### Task 2: Enforce Scope-Based Security At Write Boundaries

**Files:**
- Modify: `mcp/server/aidocs_mcp/file_ops.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_file_ops.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_agent_cannot_edit_global_settings_file(tmp_path: Path) -> None:
    project = _write_project_with_config_files(tmp_path)
    result = edit_lines(project, ".MEMORY/config/global.json", 1, 1, '{"providers": {"bundled_superpowers": {"enabled": false}}}')
    assert result["success"] is False

def test_agent_can_edit_project_settings_when_policy_allows(tmp_path: Path) -> None:
    project = _write_project_with_config_files(tmp_path)
    result = edit_lines(project, ".MEMORY/config/project.json", 1, 1, '{"skills": {"activation_mode": "selected-only"}}', config_edit_mode="explicit_user_permitted")
    assert result["success"] is True

def test_agent_cannot_edit_security_settings_even_in_dev_mode(tmp_path: Path) -> None:
    project = _write_project_with_config_files(tmp_path)
    result = edit_lines(project, ".MEMORY/config/security.json", 1, 1, '{"security": {"gui": {"agent_access": "allow"}}}', config_edit_mode="explicit_user_permitted")
    assert result["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "global_settings or project_settings or security_settings" -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Use the config schema metadata and scope classification to enforce real editability rules at write boundaries.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "global_settings or project_settings or security_settings" -v`
Expected: PASS

### Task 3: Remove Lane Ownership From The Security Model

**Files:**
- Modify: `mcp/server/aidocs_mcp/plan_conductor.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/tests/test_plan_conductor_interaction.py`
- Modify: `mcp/tests/test_query_gate_ux.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_lane_files_are_conductor_hints_not_hard_edit_locks(tmp_path: Path) -> None:
    conductor, project = _make_conductor_with_two_lanes(tmp_path)
    result = conductor.assess_fix_request(requesting_lane="lane-a", target_file="src/lane-b-helper.py")
    assert result["security_decision"] != "blocked_by_lane_ownership"

def test_conductor_can_delegate_small_fix_outside_current_lane_ownership(tmp_path: Path) -> None:
    conductor, project = _make_conductor_with_two_lanes(tmp_path)
    result = conductor.delegate_fix_scope(requesting_lane="lane-a", target_file="src/lane-b-helper.py", reason="small regression fix")
    assert result["granted"] is True

def test_lane_context_still_helps_with_read_scope_without_becoming_security_policy(tmp_path: Path) -> None:
    conductor, project = _make_conductor_with_two_lanes(tmp_path)
    result = conductor.read_scope_for_lane("lane-a")
    assert "src/lane-a.py" in result["allowed_reads"]
    assert "src/lane-b.py" not in result["allowed_reads"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`
Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Refactor conductor semantics so:
- lane files remain context/routing hints
- conductor can delegate a narrow fix scope
- lane ownership is not the primary security boundary

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`
Expected: PASS

### Task 4: Full Verification

- [ ] **Step 1: Run targeted suites**

Run:
- `pytest mcp/tests/test_scope_based_security.py -v`
- `pytest mcp/tests/test_file_ops.py mcp/tests/test_runtime_service.py -k "global_settings or project_settings or security_settings" -v`
- `pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`

Expected: PASS

- [ ] **Step 2: Run full suite**

Run: `pytest mcp/tests -v`
Expected: PASS

- [ ] **Step 3: Record final policy outcome**

Confirm all of these are true:
- global settings are never agent-editable
- project/session settings are conditionally editable by policy
- security settings are never agent-editable
- lane files are conductor hints, not hard security locks
- conductor can still preserve context without over-blocking legitimate fixes
