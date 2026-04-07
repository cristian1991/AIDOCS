# Planning Artifact Indexing Implementation Plan

> **For agentic workers:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make roadmap, spec, and plan artifacts first-class indexed targets with retrieval quality comparable to code and safe editing that reduces hard-patch fragility.

**Architecture:** Add planning artifact indexing to the existing code/index infrastructure, expose structured retrieval APIs for sections/tasks/lanes, and implement safe structural editing operations.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime/index infrastructure.

---

## File Structure

**Modify:**
- `mcp/server/aidocs_mcp/code_index_store.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/tests/test_runtime_service.py`

**Create:**
- `mcp/server/aidocs_mcp/planning_index.py`
- `mcp/tests/test_planning_index.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_planning_index.py -v`

Expected:
- planning artifacts are indexed with structured sections
- tasks, lanes, and phases are directly queryable

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_runtime_service.py -v`

Expected:
- safe editing API reduces patch failures
- agents use indexed retrieval for planning work

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add Planning Artifact Indexing

**Files:**
- Create: `mcp/server/aidocs_mcp/planning_index.py`
- Modify: `mcp/server/aidocs_mcp/code_index_store.py`
- Create: `mcp/tests/test_planning_index.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_planning_index_indexes_roadmap_sections(tmp_path: Path) -> None:
    ...

def test_planning_index_indexes_plan_phases_and_lanes(tmp_path: Path) -> None:
    ...

def test_planning_index_supports_structured_task_queries(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_planning_index.py -v`

Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add planning artifact indexing infrastructure.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_planning_index.py -v`

Expected: PASS

### Task 2: Add Safe Planning Artifact Editing

**Files:**
- Modify: `mcp/server/aidocs_mcp/planning_index.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_planning_index.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_safe_section_update_preserves_structure(tmp_path: Path) -> None:
    ...

def test_safe_task_update_validates_before_write(tmp_path: Path) -> None:
    ...

def test_planning_edit_detects_concurrent_conflicts(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_planning_index.py -v`

Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add safe structural editing API for planning artifacts.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_planning_index.py -v`

Expected: PASS

### Task 3: Final Verification

**Files:**
- Modify: `docs/specs/2026-03-31-planning-artifact-indexing-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_planning_index.py -v`

`pytest mcp/tests/test_runtime_service.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm planning indexing goals are met**

Verify:
- planning artifacts are indexed with structured sections
- tasks, lanes, and phases are directly queryable
- safe editing API reduces patch failures
- agents use indexed retrieval for planning work
