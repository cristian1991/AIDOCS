# Conductor Hardening Implementation Plan

> **For agentic workers:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the AIDOCS conductor to reliably take single-project work to trustworthy green state with full-suite awareness, persistent lane ownership, deterministic failure attribution, and structured intra-project signaling.

**Architecture:** Extend the existing conductor with full-suite verification gates, persistent ownership tracking, failure attribution logic, and structured lane signaling. Keep conductor query-first and avoid raw file reads except for exceptional cases.

**Tech Stack:** Python, pytest, AIDOCS MCP/runtime/conductor state.

---

## File Structure

**Modify:**
- `mcp/server/aidocs_mcp/plan_conductor.py`
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/tests/test_plan_conductor_interaction.py`
- `mcp/tests/test_runtime_service.py`

**Create:**
- `mcp/tests/test_conductor_hardening.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_conductor_hardening.py -v`

Expected:
- full-suite verification gates work
- lane reopening on attribution works
- persistent ownership across reopen cycles works

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_runtime_service.py -v`

Expected:
- structured lane signals work
- failure attribution is deterministic
- conductor stays query-first

### Final Verification

Run:

`pytest mcp/tests -v`

Expected:
- full MCP suite passes

---

### Task 1: Add Full-Suite-Aware Verification

**Files:**
- Modify: `mcp/server/aidocs_mcp/plan_conductor.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Create: `mcp/tests/test_conductor_hardening.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_conductor_reopens_lane_on_fullsuite_failure_attribution(tmp_path: Path) -> None:
    ...

def test_conductor_tracks_persistent_lane_ownership_across_reopens(tmp_path: Path) -> None:
    ...

def test_conductor_fails_attribution_deterministically_using_runtime_evidence(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_conductor_hardening.py -v`

Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add full-suite verification gates and lane reopening logic.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_conductor_hardening.py -v`

Expected: PASS

### Task 2: Add Structured Intra-Project Lane Signals

**Files:**
- Modify: `mcp/server/aidocs_mcp/plan_conductor.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/tests/test_plan_conductor_interaction.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_lane_can_signal_hidden_dependency_found(tmp_path: Path) -> None:
    ...

def test_lane_can_signal_undeclared_file_needed(tmp_path: Path) -> None:
    ...

def test_conductor_enforces_structured_lane_signals(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py -v`

Expected: FAIL

- [ ] **Step 3: Write full production implementation**

Add structured signal model for intra-project communication.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_conductor_interaction.py -v`

Expected: PASS

### Task 3: Final Verification

**Files:**
- Modify: `docs/specs/2026-03-31-conductor-hardening-design.md` only if implementation reality diverges

- [ ] **Step 1: Run phase-targeted suites**

Run:

`pytest mcp/tests/test_conductor_hardening.py -v`

`pytest mcp/tests/test_plan_conductor_interaction.py mcp/tests/test_runtime_service.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Confirm conductor hardening goals are met**

Verify:
- full-suite-aware verification works
- persistent lane ownership works
- deterministic failure attribution works
- structured intra-project signals work
- conductor stays query-first
