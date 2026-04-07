# Conductor Multi-Agent Execution Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add conductor-style multi-agent plan execution to AIDOCS so dependency-aware, file-safe lanes can run in parallel while related work stays in one contextual lane.

**Architecture:** Introduce a dedicated conductor layer that parses lane-aware plans, computes runnable lanes from plan structure plus indexed code relationships, blocks unsafe parallelism on file overlap or obvious coupling, and tracks live lane state while remaining interactive to user corrections. Keep plan syntax lean: `Phase`, `Lane`, mandatory `Files`, optional sparse `depends_on`.

**Tech Stack:** Python, markdown session plans, pytest, AIDOCS runtime/session stores, indexed code query tools, MCP server tools.

---

## File Structure

**Create**
- `mcp/server/aidocs_mcp/plan_conductor.py`
  - Conductor graph model, lane parsing helpers, runnable-lane computation, conflict checks, and lane-state transitions.
- `mcp/tests/test_plan_conductor_parse.py`
- `mcp/tests/test_plan_conductor_runtime.py`
- `mcp/tests/test_plan_conductor_interaction.py`

**Modify**
- `mcp/server/aidocs_mcp/session_store.py`
  - Parse lane/phase metadata from `PLAN.md` while preserving existing checkbox-plan support.
- `mcp/server/aidocs_mcp/runtime_service.py`
  - Integrate conductor summary into `plan_connect` and add runtime methods for conductor state, lane holds, and lane resume.
- `mcp/server/aidocs_mcp/mcp_server.py`
  - Expose conductor MCP tools for graph/status/control.
- `mcp/server/aidocs_mcp/types.py`
  - Add typed structures for phases, lanes, dependencies, conflicts, and conductor state.
- `mcp/tests/test_plan_tools.py`
  - Cover lane-aware plan parsing and connect behavior.
- `mcp/tests/test_runtime_service.py`
  - Cover runtime-side conductor behavior and state transitions.

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_plan_conductor_parse.py mcp/tests/test_plan_tools.py -v`

Expected:
- lane-aware plan syntax parses deterministically
- mandatory `Files` are enforced
- optional `depends_on` is supported sparsely

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_plan_conductor_runtime.py mcp/tests/test_runtime_service.py -v`

Expected:
- runnable lanes are computed correctly
- overlapping-file lanes are blocked from parallel execution
- conductor remains query-first and does not rely on low-level file reads by default

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_runtime_service.py -v`

Expected:
- paused lanes can be resumed after user clarification
- user overrides can adjust lane state safely
- in-flight conflict detection pauses affected lanes instead of continuing unsafely

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Parse Lane-Aware Plans Without Breaking Existing Plans

**Files:**
- Create: `mcp/tests/test_plan_conductor_parse.py`
- Modify: `mcp/server/aidocs_mcp/session_store.py`
- Modify: `mcp/server/aidocs_mcp/types.py`
- Modify: `mcp/tests/test_plan_tools.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_read_plan_parses_phase_lane_files_and_optional_dependencies(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_lane_plan(tmp_path)

    plan = store.read_plan(project, session_id)

    assert plan.lanes[0].name == "homepage-hero"
    assert plan.lanes[0].files == ["src/components/home/Hero.tsx", "src/cms/hero-block.ts"]
    assert plan.lanes[0].depends_on == []


def test_lane_without_files_is_rejected(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_invalid_lane_plan(tmp_path)

    with pytest.raises(ValueError, match="Files are required"):
        store.read_plan(project, session_id)


def test_legacy_checkbox_plan_still_reads_without_lanes(tmp_path: Path) -> None:
    store, project, session_id = _make_session_store_with_legacy_plan(tmp_path)

    plan = store.read_plan(project, session_id)

    assert plan.lanes == []
    assert plan.sections["Steps"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_conductor_parse.py mcp/tests/test_plan_tools.py -k "lane or files or legacy_checkbox" -v`

Expected: FAIL because the current plan parser does not understand lane metadata.

- [ ] **Step 3: Write full production implementation**

Add typed lane/phase support and extend plan parsing, for example:

```python
@dataclass
class PlanLane:
    lane_id: str
    phase_id: str
    name: str
    files: list[str]
    depends_on: list[str]
    steps: list[PlanStep]

@dataclass
class PlanPhase:
    phase_id: str
    name: str
    lanes: list[PlanLane]
```

```python
def read_plan(self, project_root: Path, session_id: str) -> PlanData:
    # preserve existing section parsing
    # additionally parse phase/lane metadata when present
```

Legacy plans must continue to work unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_conductor_parse.py mcp/tests/test_plan_tools.py -k "lane or files or legacy_checkbox" -v`

Expected: PASS

### Task 2: Compute Runnable Lanes Safely

**Files:**
- Create: `mcp/server/aidocs_mcp/plan_conductor.py`
- Create: `mcp/tests/test_plan_conductor_runtime.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/types.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_conductor_marks_dependency_free_non_overlapping_lanes_runnable(tmp_path: Path) -> None:
    conductor = _make_conductor_with_lane_plan(tmp_path)

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == ["homepage-hero", "homepage-feature-grid"]


def test_conductor_blocks_parallel_lanes_that_share_a_file(tmp_path: Path) -> None:
    conductor = _make_conductor_with_overlapping_lanes(tmp_path)

    result = conductor.runnable_lanes()

    assert "shared-file-overlap" in result["blocked_reasons"]["lane-b"]


def test_conductor_respects_sparse_hard_depends_on(tmp_path: Path) -> None:
    conductor = _make_conductor_with_hard_dependency(tmp_path)

    result = conductor.runnable_lanes()

    assert "integration" not in result["runnable_lane_ids"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_conductor_runtime.py -k "runnable or overlap or depends_on" -v`

Expected: FAIL because conductor graph/runtime does not exist yet.

- [ ] **Step 3: Write full production implementation**

Add a conductor service, for example:

```python
class PlanConductor:
    def __init__(self, hub: AidocsServiceHub, project_root: Path, session_id: str):
        self.hub = hub
        self.project_root = project_root
        self.session_id = session_id
        self.plan = hub.sessions.read_plan(project_root, session_id)

    def graph(self) -> dict[str, Any]:
        return {
            "phase_order": ["phase-1", "phase-2"],
            "lanes": [{"lane_id": "homepage-hero", "phase_id": "phase-1", "files": ["src/components/home/Hero.tsx"]}],
            "dependencies": {"homepage-integration": ["homepage-hero", "homepage-shell"]},
        }

    def runnable_lanes(self) -> dict[str, Any]:
        return {
            "runnable_lane_ids": ["homepage-hero", "homepage-feature-grid"],
            "blocked_reasons": {"homepage-integration": ["waiting-on:homepage-hero", "waiting-on:homepage-shell"]},
            "waiting_on": {"homepage-integration": ["homepage-hero", "homepage-shell"]},
        }
```

Rules:
- block same-file overlap unconditionally
- respect explicit `depends_on`
- keep one agent per lane
- default to indexed/query-first reasoning for conflict analysis

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_conductor_runtime.py -k "runnable or overlap or depends_on" -v`

Expected: PASS

### Task 3: Surface Conductor State Through Runtime And MCP Tools

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`
- Modify: `mcp/tests/test_runtime_service.py`
- Modify: `mcp/tests/test_plan_tools.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_plan_connect_includes_lane_graph_summary_for_lane_aware_plan(tmp_path: Path) -> None:
    runtime, project, session_id = _make_runtime_with_lane_plan(tmp_path)

    result = runtime.plan_connect(project, session_id, run_preflight=False)

    assert result["plan_source"] == "session_plan"
    assert result["lane_summary"]["runnable"]


def test_mcp_tool_returns_conductor_graph(tmp_path: Path) -> None:
    server, project, session_id = _make_server_with_lane_plan(tmp_path)

    result = server.call_tool("plan_conductor_status", {"project_root": str(project), "session_id": session_id})

    assert result["lanes"]
    assert result["phase_order"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_plan_tools.py -k "lane_graph or conductor_status or lane_summary" -v`

Expected: FAIL because conductor state is not yet exposed.

- [ ] **Step 3: Write full production implementation**

Expose conductor-aware runtime and MCP entrypoints, for example:

```python
def plan_conductor_status(self, project_root: Path, session_id: str) -> dict[str, object]:
    conductor = PlanConductor(self.hub, project_root, session_id)
    return conductor.graph() | conductor.runnable_lanes()

@server.tool()
def plan_conductor_status(project_root: str, session_id: str) -> dict[str, Any]:
    return runtime.plan_conductor_status(Path(project_root), session_id)
```

`plan_connect()` should include lane-aware summary when a lane-aware plan is present, without regressing legacy plans.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_plan_tools.py -k "lane_graph or conductor_status or lane_summary" -v`

Expected: PASS

### Task 4: Support Interactive Pause/Resume And Emergent Conflict Holds

**Files:**
- Create: `mcp/tests/test_plan_conductor_interaction.py`
- Modify: `mcp/server/aidocs_mcp/plan_conductor.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py`

- [ ] **Step 1: Write the failing tests**

Add tests like:

```python
def test_conductor_pauses_lane_when_inflight_file_overlap_is_reported(tmp_path: Path) -> None:
    conductor = _make_running_conductor(tmp_path)
    result = conductor.report_lane_touch("lane-a", "src/shared.py")
    result = conductor.report_lane_touch("lane-b", "src/shared.py")

    assert result["lane_states"]["lane-b"] == "blocked"


def test_user_override_can_resume_paused_lane(tmp_path: Path) -> None:
    conductor = _make_paused_conductor(tmp_path)

    result = conductor.user_override_resume("lane-b", reason="shared dependency is stable")

    assert result["lane_states"]["lane-b"] == "ready"


def test_contract_compatible_lanes_can_run_together_when_conductor_marks_contract_ready(tmp_path: Path) -> None:
    conductor = _make_contract_coupled_conductor(tmp_path)

    result = conductor.mark_contract_ready("api-contract")

    assert set(result["runnable_lane_ids"]) == {"backend-api", "frontend-api-consumers"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py -v`

Expected: FAIL because pause/resume and in-flight conflict handling do not exist yet.

- [ ] **Step 3: Write full production implementation**

Add conductor interaction methods and MCP tools, for example:

```python
def report_lane_touch(self, lane_id: str, path: str) -> dict[str, Any]:
    # record actual touched file, recompute conflicts, and pause overlapping runnable lanes
    return {
        "lane_states": {"lane-a": "running", "lane-b": "blocked"},
        "paused": ["lane-b"],
        "conflicts": [{"type": "file_overlap", "path": "src/shared.py", "lanes": ["lane-a", "lane-b"]}],
    }

def pause_lane(self, lane_id: str, reason: str) -> dict[str, Any]:
    # set lane state to blocked or awaiting_user_feedback and record reason
    return {"lane_id": lane_id, "state": "blocked", "reason": reason}

def resume_lane(self, lane_id: str, reason: str) -> dict[str, Any]:
    # clear hold and recompute runnable set
    return {"lane_id": lane_id, "state": "ready", "reason": reason, "runnable_lane_ids": ["lane-b"]}
```

Rules:
- conductor stays interactive while lanes run
- in-flight overlap pauses affected lanes
- user can resume or restructure lanes explicitly
- contract-compatible parallelism is allowed only when the conductor establishes the contract first

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py -v`

Expected: PASS

### Task 5: Final Verification And Documentation Check

**Files:**
- Modify: `docs/specs/2026-03-30-conductor-multi-agent-execution-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_plan_conductor_parse.py mcp/tests/test_plan_tools.py -v`

`pytest mcp/tests/test_plan_conductor_runtime.py mcp/tests/test_runtime_service.py -v`

`pytest mcp/tests/test_plan_conductor_interaction.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm conductor goals are met**

Verify the implementation supports:

- one agent per lane
- dependency-driven runnable-lane computation
- mandatory file-based overlap blocking
- query-first conductor reasoning
- live pause/resume while lanes run
- sparse optional `depends_on`
