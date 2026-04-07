# Tiered Read Access Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current one-size-fits-all query-before-read gate with a tiered read model: strict discovery reads, lightweight exact known reads, easy conductor lane-owned reads, and still-blocked protected paths.

**Architecture:** Keep query-first behavior for discovery, but separate it from exact known reads and lane-owned reads. Extend the query-gate state model so conductor-owned lanes can receive deterministic read grants for declared files, and keep security-sensitive paths protected regardless of read tier.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime/conductor state, query-gate state in `mcp_server.py`.

---

## File Structure

**Modify**
- `mcp/server/aidocs_mcp/mcp_server.py`
  - refine read-gate behavior, exact-path grants, and lane-owned read logic
- `mcp/server/aidocs_mcp/runtime_service.py`
  - if needed, surface conductor/lane read grant information through runtime helpers
- `mcp/server/aidocs_mcp/plan_conductor.py`
  - grant lane-scoped read access for declared files
- `mcp/tests/test_query_gate_ux.py`
- `mcp/tests/test_mcp_server_trace_depth.py`
- `mcp/tests/test_plan_conductor_interaction.py`

**Create**
- `mcp/tests/test_tiered_read_access.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_tiered_read_access.py mcp/tests/test_mcp_server_trace_depth.py -v`

Expected:
- discovery reads still require indexed/query-first behavior
- exact known reads are lighter for safe project-relative targets
- protected reads remain blocked

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`

Expected:
- lane-owned files get automatic read access
- undeclared-file requests can be surfaced to the conductor instead of silently expanding scope
- no stale or overly broad read grants

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Separate Discovery Reads From Exact Known Reads

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_tiered_read_access.py`
- Modify: `mcp/tests/test_mcp_server_trace_depth.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_discovery_read_still_requires_indexed_query(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    result = server.call_tool("aidocs_code_get_lines", {
        "project_root": str(tmp_path / "project"),
        "path": "src/unknown.py"
    })
    assert "Indexed-query prerequisite" in result["error"]


def test_exact_known_relative_read_skips_discovery_gate(tmp_path: Path) -> None:
    project = _write_project_with_file(tmp_path, "src/exact.py", "x = 1\n")
    server = _make_server(tmp_path)
    result = server.call_tool("aidocs_code_get_lines", {
        "project_root": str(project),
        "path": "src/exact.py",
        "known_exact_path": true
    })
    assert result["content"]


def test_protected_path_stays_blocked_even_with_known_exact_path(tmp_path: Path) -> None:
    project = _write_project_with_file(tmp_path, ".MEMORY/config/security.json", "{}\n")
    server = _make_server(tmp_path)
    result = server.call_tool("aidocs_code_get_lines", {
        "project_root": str(project),
        "path": ".MEMORY/config/security.json",
        "known_exact_path": true
    })
    assert "error" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_tiered_read_access.py mcp/tests/test_mcp_server_trace_depth.py -v`

Expected: FAIL because the current gate still applies too uniformly.

- [ ] **Step 3: Write full production implementation**

Refine the query gate so it distinguishes:

- discovery read
- exact known read
- protected read

Use exact relative path checks instead of broad exemptions.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_tiered_read_access.py mcp/tests/test_mcp_server_trace_depth.py -v`

Expected: PASS

### Task 2: Add Conductor Lane-Owned Read Grants

**Files:**
- Modify: `mcp/server/aidocs_mcp/plan_conductor.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_plan_conductor_interaction.py`
- Modify: `mcp/tests/test_query_gate_ux.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_lane_owned_file_read_is_granted_automatically(tmp_path: Path) -> None:
    conductor, server = _make_conductor_and_server_with_lane(tmp_path, files=["src/owned.py"])
    result = server.call_tool("aidocs_code_get_lines", {
        "project_root": str(conductor.project_root),
        "path": "src/owned.py"
    })
    assert result["content"]


def test_lane_agent_request_for_undeclared_file_requires_conductor_signal(tmp_path: Path) -> None:
    conductor, server = _make_conductor_and_server_with_lane(tmp_path, files=["src/owned.py"])
    result = server.call_tool("aidocs_code_get_lines", {
        "project_root": str(conductor.project_root),
        "path": "src/not-owned.py"
    })
    assert "Indexed-query prerequisite" in result["error"] or "undeclared" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`

Expected: FAIL because lane-owned read grants are not yet automatic.

- [ ] **Step 3: Write full production implementation**

When a lane starts, the conductor should grant read access for that lane’s declared files only. If another file is needed, the lane should require explicit conductor expansion or a structured `undeclared_file_needed` signal.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`

Expected: PASS

### Task 3: Final Verification And Policy Check

**Files:**
- Modify: `docs/specs/2026-03-31-tiered-read-access-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_tiered_read_access.py mcp/tests/test_mcp_server_trace_depth.py -v`

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_query_gate_ux.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm policy goals are met**

Verify all of these are true:

- discovery still requires indexed/query-first behavior
- exact known safe reads are easier
- conductor-owned lane reads do not suffer repeated query friction
- security-sensitive paths remain blocked
- no stale or over-broad read grants exist
