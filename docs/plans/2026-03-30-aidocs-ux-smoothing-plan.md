# AIDOCS UX Smoothing Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce remaining workflow friction in AIDOCS by smoothing exact-path reads, improving new-file visibility after edits, stabilizing session goal/purpose handling, and adding artifact normalization tooling for older session state.

**Architecture:** Keep the current deterministic AIDOCS model. Improve the query-gate and file lifecycle ergonomics without weakening safety, separate durable session goal from transient task execution state, and add explicit normalization/cleanup tools for legacy or drifted session artifacts instead of silently rewriting them during unrelated reads.

**Tech Stack:** Python, pytest, MCP server tools, markdown session artifacts, SQLite-backed query gate and indexes.

---

## File Structure

**Modify**
- `mcp/server/aidocs_mcp/mcp_server.py`
  - Refine indexed-read gate behavior and post-create/post-edit visibility flow.
- `mcp/server/aidocs_mcp/file_ops.py`
  - Add any file-read/create metadata needed for smoother exact-path follow-up reads.
- `mcp/server/aidocs_mcp/runtime_service.py`
  - Separate long-lived session goal/purpose from transient task updates.
- `mcp/server/aidocs_mcp/session_store.py`
  - Add normalization/cleanup helpers for session artifacts and handoff/plan cleanup entrypoints.
- `mcp/server/aidocs_mcp/mcp_server.py`
  - Expose normalization/cleanup MCP tools if needed.

**Create**
- `mcp/tests/test_query_gate_ux.py`
- `mcp/tests/test_runtime_session_goal_stability.py`
- `mcp/tests/test_session_normalization_tools.py`

**Test**
- `mcp/tests/test_mcp_server_trace_depth.py`
- `mcp/tests/test_file_ops_create_and_read.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_session_store.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_query_gate_ux.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_trace_depth.py -v`

Expected:
- exact-path reads are smoother for known project-relative paths
- newly created files are easier to inspect immediately after creation/edit flows
- query-gate safety still holds for broad unguided reads

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_runtime_session_goal_stability.py mcp/tests/test_runtime_service.py -v`

Expected:
- task_begin/task_update/task_complete no longer overwrite durable session goal/purpose incorrectly
- task execution state is still visible without drifting the higher-level session objective

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_session_normalization_tools.py mcp/tests/test_session_store.py -v`

Expected:
- legacy/drifted session artifacts can be normalized explicitly
- normalization is deterministic and reviewable
- unrelated read paths do not auto-mutate old artifacts

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Smooth Exact-Path Reads And New-File Visibility

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py:102-123,1161-1204`
- Modify: `mcp/server/aidocs_mcp/file_ops.py:297-341`
- Create: `mcp/tests/test_query_gate_ux.py`
- Modify: `mcp/tests/test_file_ops_create_and_read.py`
- Modify: `mcp/tests/test_mcp_server_trace_depth.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_known_exact_path_allows_followup_read_after_native_file_create(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_minimal_project(project)
    server = create_server()
    server.call_tool("code_create_file", {"project_root": str(project), "path": "notes/todo.md", "content": "- item\n"})

    result = server.call_tool("code_get_lines", {"project_root": str(project), "path": "notes/todo.md", "known_exact_path": True})

    assert "item" in result["content"]

def test_known_exact_path_requires_project_relative_path_even_after_grant(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_minimal_project(project)
    server = create_server()
    server.call_tool("code_create_file", {"project_root": str(project), "path": "notes/todo.md", "content": "- item\n"})

    result = server.call_tool("code_get_lines", {"project_root": str(project), "path": str((project / 'notes' / 'todo.md').resolve()), "known_exact_path": True})

    assert "error" in result

def test_edit_or_create_flow_can_grant_narrow_followup_read_without_unlocking_broad_reads(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_minimal_project(project)
    server = create_server()
    server.call_tool("code_create_file", {"project_root": str(project), "path": "notes/todo.md", "content": "- item\n"})

    exact = server.call_tool("code_get_lines", {"project_root": str(project), "path": "notes/todo.md", "known_exact_path": True})
    broad = server.call_tool("code_get_lines", {"project_root": str(project), "path": "other/file.md"})

    assert "item" in exact["content"]
    assert "error" in broad
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_query_gate_ux.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_trace_depth.py -k "known_exact_path or followup_read or query_gate" -v`

Expected: FAIL because current gate behavior is still too awkward for immediate follow-up inspection after create/edit workflows.

- [ ] **Step 3: Write full production implementation**

Implement a narrow follow-up-read capability, for example:

```python
def _grant_exact_path_followup(project_root: Path, session_id: str, path: str) -> None:
    state = _CURRENT_HUB.query_gate.get(project_root, session_id)
    allowed = set(state.get("allowed_paths") or [])
    allowed.add(path.replace("\\", "/"))
    _CURRENT_HUB.query_gate.set(project_root, session_id, allow_read=False, last_tool="exact_path_followup", allowed_paths=sorted(allowed))

def _require_indexed_read_gate(project_root: Path, path: str | None = None) -> dict[str, Any] | None:
    # allow exact known paths explicitly granted by create/edit flows
    state = _CURRENT_HUB.query_gate.get(project_root, str(session_id))
    if path and path.replace("\\", "/") in set(state.get("allowed_paths") or []):
        return None
    if state.get("allow_read"):
        return None
    return {"error": "Indexed-query prerequisite not satisfied."}
```

The implementation must not turn known-path mode into a broad read bypass.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_query_gate_ux.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_trace_depth.py -k "known_exact_path or followup_read or query_gate" -v`

Expected: PASS

### Task 2: Stabilize Durable Session Goal And Purpose

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py:1876-2045`
- Create: `mcp/tests/test_runtime_session_goal_stability.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_task_begin_does_not_overwrite_session_goal_when_working_subtask_starts(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path, goal="Ship host hooks")
    runtime.task_begin(project, session_id, goal="Fix one failing test")

    session = runtime.hub.sessions.read_session(project, session_id)
    assert session.sections["Goal"] == ["- Ship host hooks"]

def test_task_update_preserves_original_session_purpose(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path, goal="Ship host hooks")
    runtime.task_update(project, session_id, state=["Investigating query gate"])

    plan = runtime.hub.sessions.read_plan(project, session_id)
    assert plan.sections["Purpose"] == ["- Ship host hooks"]

def test_task_complete_updates_execution_state_without_rewriting_high_level_goal(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_session(tmp_path, goal="Ship host hooks")
    runtime.task_complete(project, session_id, result_summary="Fixed query gate")

    session = runtime.hub.sessions.read_session(project, session_id)
    assert session.sections["Goal"] == ["- Ship host hooks"]
    assert any("Fixed query gate" in line for line in session.sections["State"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_runtime_session_goal_stability.py mcp/tests/test_runtime_service.py -k "goal or purpose or task_complete" -v`

Expected: FAIL because current task_begin/task_complete behavior rewrites high-level goal text too aggressively.

- [ ] **Step 3: Write full production implementation**

Separate durable session purpose from transient execution state, for example:

```python
# keep Goal/Title durable
# write current task intent into State / Current State / Validation / execution-specific sections
```

Do not lose visibility into what the current task is doing.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_runtime_session_goal_stability.py mcp/tests/test_runtime_service.py -k "goal or purpose or task_complete" -v`

Expected: PASS

### Task 3: Add Explicit Session Artifact Normalization Tools

**Files:**
- Modify: `mcp/server/aidocs_mcp/session_store.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Create: `mcp/tests/test_session_normalization_tools.py`
- Modify: `mcp/tests/test_session_store.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_normalize_handoff_steps_converts_legacy_open_done_mix_deterministically(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_legacy_handoff(tmp_path)

    result = store.normalize_session_artifacts(project, session_id)

    assert result["normalized"]["handoff_steps"] >= 1
    assert result["changed"] is True

def test_normalize_session_artifacts_does_not_run_implicitly_on_read(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_legacy_handoff(tmp_path)
    before = store.handoff_file(project, session_id).read_text(encoding="utf-8")
    store.read_handoff_optional(project, session_id)
    after = store.handoff_file(project, session_id).read_text(encoding="utf-8")

    assert before == after

def test_normalize_plan_feedback_sections_preserves_user_prose(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_prose_plan(tmp_path)

    result = store.normalize_session_artifacts(project, session_id)

    assert result["preserved_prose"]
    assert result["normalized"]["plan_feedback"] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_session_normalization_tools.py mcp/tests/test_session_store.py -k "normalize or legacy" -v`

Expected: FAIL because normalization/cleanup is not yet exposed as an explicit workflow.

- [ ] **Step 3: Write full production implementation**

Add explicit normalization helpers/tools rather than mutating artifacts during read paths, for example:

```python
def normalize_session_artifacts(project_root: Path, session_id: str) -> dict[str, object]:
    handoff = self.read_handoff_optional(project_root, session_id)
    plan = self.read_plan_optional(project_root, session_id)
    changes = []
    if handoff is not None:
        changes.extend(self._normalize_handoff_steps(project_root, session_id, handoff))
    if plan is not None:
        changes.extend(self._normalize_plan_feedback_sections(project_root, session_id, plan))
    return {"session_id": session_id, "changed": bool(changes), "changes": changes}
```

The result should report what changed and what remained untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_session_normalization_tools.py mcp/tests/test_session_store.py -k "normalize or legacy" -v`

Expected: PASS

### Task 4: Full Verification And Updated Tool Report

**Files:**
- Modify: `docs/specs/2026-03-30-aidocs-tooling-followup-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_query_gate_ux.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_trace_depth.py -v`

`pytest mcp/tests/test_runtime_session_goal_stability.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_session_normalization_tools.py mcp/tests/test_session_store.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Capture updated AIDOCS tool findings**

Confirm whether these remaining UX issues improved:

- indexed-read gate ceremony
- new-file visibility after creation/edit
- session goal/purpose drift
- old session artifact cleanup burden
