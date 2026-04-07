# AIDOCS Tooling Follow-Up Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the remaining AIDOCS tooling issues around plan/roadmap fallback, feedback-state workflows, freshness/status semantics, file creation/read ergonomics, and CLI help correctness while preserving fresh per-file replacement indexing.

**Architecture:** Extend the existing session and runtime layer instead of creating parallel workflow state. Session plan resolution, roadmap fallback, and feedback-state transitions should live in the session/runtime path; freshness and edit ergonomics should extend the current indexed file and code-store workflows; CLI help should be fixed at the entrypoint without changing server behavior. Strict exact-span indexing is explicitly deferred to `2.0.0`.

**Tech Stack:** Python, markdown session files, pytest, MCP server tools, SQLite-backed index stores.

---

## File Structure

**Modify**
- `mcp/server/aidocs_mcp/session_store.py`
  - Add resilient plan loading, roadmap parsing helpers, feedback-state support, and handoff step completed-state support.
- `mcp/server/aidocs_mcp/runtime_service.py`
  - Add merged plan/roadmap resolution behavior and roadmap-feedback transition logic.
- `mcp/server/aidocs_mcp/types.py`
  - Extend typed plan/roadmap resolution results if needed.
- `mcp/server/aidocs_mcp/code_index_store.py`
  - Expose explicit freshness metadata and preserve handled per-file replacement semantics.
- `mcp/server/aidocs_mcp/index_store.py`
  - Expose memory index freshness metadata to match code status.
- `mcp/server/aidocs_mcp/file_ops.py`
  - Add native file creation support and exact-path read ergonomics.
- `mcp/server/aidocs_mcp/mcp_server.py`
  - Expose any new file operation/read options and fix CLI `--help` behavior.
- `ROADMAP_2_0_0.md`
  - Normalize actionable roadmap bullets into a deterministic mutable state format where needed.

**Create**
- `mcp/tests/test_plan_resolution.py`
- `mcp/tests/test_roadmap_feedback.py`
- `mcp/tests/test_index_freshness.py`
- `mcp/tests/test_file_ops_create_and_read.py`
- `mcp/tests/test_mcp_server_cli.py`

**Test**
- `mcp/tests/test_plan_tools.py`
- `mcp/tests/test_runtime_service.py`
- `mcp/tests/test_file_ops.py`
- `mcp/tests/test_host_integration.py`

---

## Phase Verification

### Phase 1 Checkpoint

Run:

`pytest mcp/tests/test_plan_tools.py mcp/tests/test_plan_resolution.py mcp/tests/test_roadmap_feedback.py -v`

Expected:
- missing session plans no longer hard-fail
- roadmap fallback summary works
- `pending_user_feedback` and `awaiting_feedback` transitions work

### Phase 2 Checkpoint

Run:

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_index_freshness.py -v`

Expected:
- freshness metadata is explicit
- handled per-file replacement sync stays fresh
- stale state reflects real unhandled drift or missing state

### Phase 3 Checkpoint

Run:

`pytest mcp/tests/test_file_ops.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_cli.py -v`

Expected:
- native file creation works
- exact-path reads are easier without fake discovery requirements
- `python -m aidocs_mcp.mcp_server --help` exits with help text

### Final Verification

Run:

`pytest mcp/tests -v`

and:

`python -m aidocs_mcp.mcp_server --help`

Expected:
- full suite passes
- help prints usage and does not start the server

---

### Task 1: Make Session Plan Resolution Resilient

**Files:**
- Modify: `mcp/server/aidocs_mcp/session_store.py:173-189`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py:1579-1647`
- Create: `mcp/tests/test_plan_resolution.py`
- Modify: `mcp/tests/test_plan_tools.py`

- [ ] **Step 1: Write the failing tests**

Add tests covering these cases:

```python
def test_plan_connect_uses_session_plan_when_present(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-plan", "Plan", "user", "Use session plan")
    _write_plan(project, "2026-03-30-plan", [(False, "Implement session step")])

    result = runtime.plan_connect(project, "2026-03-30-plan", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "session_plan"
    assert result["next_steps"] == ["Implement session step"]

def test_plan_connect_without_session_plan_summarizes_roadmap_and_asks_user(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-roadmap", "Roadmap", "user", "Use roadmap")
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [ ] Ship roadmap fallback\n", encoding="utf-8")

    result = runtime.plan_connect(project, "2026-03-30-roadmap", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "roadmap_summary"
    assert "ask the user" in result["instruction"].lower()
    assert "Ship roadmap fallback" in result["roadmap_steps"][0]["text"]

def test_plan_connect_without_plan_or_roadmap_asks_user_for_next_steps(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-empty", "Empty", "user", "Need direction")

    result = runtime.plan_connect(project, "2026-03-30-empty", run_preflight=False)

    assert result["connected"] is True
    assert result["plan_source"] == "none"
    assert "ask the user" in result["instruction"].lower()
    assert result["next_action"] == "create_plan_or_roadmap"
```

Required assertions:
- session plan wins when present
- no session plan + roadmap returns a merged remaining-work summary and asks the user what to work on
- no plan and no roadmap returns a structured “define next work / create plan” response instead of a hard failure

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_plan_tools.py mcp/tests/test_plan_resolution.py -k "plan_connect or roadmap" -v`

Expected: FAIL because `read_plan()` currently raises and `plan_connect()` assumes a session plan must exist.

- [ ] **Step 3: Write full production implementation**

Implement these concrete changes:

```python
# session_store.py
def read_plan_optional(self, project_root: Path, session_id: str) -> PlanData | None:
    path = self.plan_file(project_root, session_id)
    if not path.exists():
        return None
    return self.read_plan(project_root, session_id)

def roadmap_candidates(self, project_root: Path) -> list[Path]:
    return [
        project_root / "ROADMAP_2_0_0.md",
        project_root / "ROADMAP.md",
        project_root / "mcp" / "ROADMAP.md",
    ]

def read_roadmap_steps(self, project_root: Path) -> list[dict[str, str]]:
    # parse only actionable checkbox bullets; preserve prose untouched
    # return [{"text": "Ship roadmap fallback", "status": "open", "line_number": 12}]
```

```python
# runtime_service.py
def plan_connect(self, project_root: Path, session_id: str, run_preflight: bool = True) -> dict[str, object]:
    plan = self.hub.sessions.read_plan_optional(project_root, session_id)
    if plan is not None:
        return self._connect_existing_plan(project_root, session_id, plan, run_preflight=run_preflight)
    roadmap_steps = self.hub.sessions.read_roadmap_steps(project_root)
    if roadmap_steps:
        return self._build_roadmap_summary_result(project_root, session_id, roadmap_steps)
    return self._build_no_plan_no_roadmap_result(session_id)
```

Session-local open work must include at least:
- handoff open steps
- unresolved blockers
- pending feedback items

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_plan_tools.py mcp/tests/test_plan_resolution.py -k "plan_connect or roadmap" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/session_store.py mcp/server/aidocs_mcp/runtime_service.py mcp/tests/test_plan_tools.py mcp/tests/test_plan_resolution.py
git commit -m "feat: add resilient session plan and roadmap fallback"
```

### Task 2: Add Feedback-State Workflow For Roadmap And Plans

**Files:**
- Modify: `mcp/server/aidocs_mcp/session_store.py`
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Create: `mcp/tests/test_roadmap_feedback.py`
- Modify: `ROADMAP_2_0_0.md`

- [ ] **Step 1: Write the failing tests**

Add tests for these behaviors:

```python
def test_completed_plan_sequence_moves_matching_roadmap_step_to_pending_user_feedback(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-feedback", "Feedback", "user", "Close a roadmap step")
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [~] Ship startup flow\n", encoding="utf-8")

    result = runtime.mark_roadmap_step_pending_feedback(project, "Ship startup flow")

    assert result["status"] == "pending_user_feedback"
    assert "[>] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")

def test_user_feedback_can_move_pending_step_back_to_in_progress(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "ROADMAP_2_0_0.md").write_text("# Roadmap\n\n- [>] Ship startup flow\n", encoding="utf-8")

    result = runtime.update_roadmap_feedback_state(project, "Ship startup flow", feedback="needs fixes")

    assert result["status"] == "in_progress"
    assert "[~] Ship startup flow" in (project / "ROADMAP_2_0_0.md").read_text(encoding="utf-8")

def test_prose_only_plan_addition_creates_awaiting_feedback_structure(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-prose", "Prose", "user", "Normalize prose")
    runtime.hub.sessions.update_plan(project, "2026-03-30-prose", {"Steps": ["- The agent should validate the roadmap state and then continue with CLI fixes."]})

    result = runtime.normalize_plan_prose(project, "2026-03-30-prose")

    assert result["status"] == "awaiting_feedback"
    assert result["original_prose"]
    assert any("[>]" in line for line in result["normalized_lines"])
```

Required assertions:
- roadmap step state supports `- [>]`
- plan step state supports `- [>]`
- prose-only plan additions are preserved and proposed as normalized structured steps marked awaiting feedback

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_roadmap_feedback.py -v`

Expected: FAIL because roadmap and plan feedback states are not implemented yet.

- [ ] **Step 3: Write full production implementation**

Implement deterministic state parsing and rendering:

```python
ROADMAP_STEP_MARKERS = {
    "open": "[ ]",
    "in_progress": "[~]",
    "pending_user_feedback": "[>]",
    "completed": "[x]",
    "blocked": "[!]",
}

PLAN_STEP_MARKERS = {
    "open": "[ ]",
    "in_progress": "[~]",
    "awaiting_feedback": "[>]",
    "completed": "[x]",
    "blocked": "[!]",
}
```

The implementation must:
- mutate only recognized actionable roadmap bullets
- preserve roadmap prose untouched
- preserve original prose-only plan text until feedback confirms cleanup

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_roadmap_feedback.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/session_store.py mcp/server/aidocs_mcp/runtime_service.py mcp/tests/test_roadmap_feedback.py ROADMAP_2_0_0.md
git commit -m "feat: add roadmap and plan feedback states"
```

### Task 3: Support Completed Handoff Steps Consistently

**Files:**
- Modify: `mcp/server/aidocs_mcp/session_store.py:24-30,131-171,520-545`
- Modify: `mcp/tests/test_plan_tools.py`

- [ ] **Step 1: Write the failing test**

Add a test like:

```python
def test_handoff_step_update_accepts_completed_status(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    runtime.hub.sessions.create_session(project, "2026-03-30-handoff", "Handoff", "user", "Complete step")

    handoff = runtime.hub.sessions.upsert_handoff_step(project, "2026-03-30-handoff", text="Finish verification", status="completed")

    assert any("[x]" in line and "Finish verification" in line for line in handoff.sections["Steps"])
```

Required assertions:
- the tool accepts `completed`
- it renders deterministically
- legacy `done` is either preserved as alias or normalized safely

- [ ] **Step 2: Run test to verify it fails**

Run:

`pytest mcp/tests/test_plan_tools.py -k handoff_step -v`

Expected: FAIL because `completed` is not currently accepted.

- [ ] **Step 3: Write full production implementation**

Implement one canonical mapping:

```python
HANDOFF_STEP_MARKERS = {
    "open": "[ ]",
    "completed": "[x]",
    "failed": "[!]",
    "reset": "[~]",
    "stale": "[?]",
}

HANDOFF_STEP_ALIASES = {
    "done": "completed",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

`pytest mcp/tests/test_plan_tools.py -k handoff_step -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/session_store.py mcp/tests/test_plan_tools.py
git commit -m "fix: support completed handoff step state"
```

### Task 4: Expose Explicit Freshness Metadata Without Breaking Per-File Sync

**Files:**
- Modify: `mcp/server/aidocs_mcp/code_index_store.py:777-812`
- Modify: `mcp/server/aidocs_mcp/index_store.py`
- Create: `mcp/tests/test_index_freshness.py`
- Modify: `mcp/tests/test_runtime_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests for these cases:

```python
def test_code_status_reports_missing_stale_and_ready_states(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    store = CodeIndexStore()

    missing = store.code_status(project)
    assert missing["freshness"] == "missing"

def test_handled_file_edit_replacement_sync_keeps_index_fresh(tmp_path: Path) -> None:
    project = tmp_path / "project"
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    target = src / "app.py"
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    store = CodeIndexStore()
    store.sync_code_files(project)
    target.write_text("def run():\n    return 2\n", encoding="utf-8")
    store.sync_code_files(project, paths=["src/app.py"])

    ready = store.code_status(project)
    assert ready["freshness"] == "ready"
    assert ready["replacement_sync"] is True

def test_execution_event_write_does_not_change_code_freshness_state(tmp_path: Path) -> None:
    runtime, project = _make_runtime(tmp_path)
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    runtime.hub.code.sync_code_files(project)
    before = runtime.hub.code.code_status(project)
    runtime.hub.execution.record_event(project, event_kind="tool", source_kind="test", payload={"ok": True})
    after = runtime.hub.code.code_status(project)

    assert before["freshness"] == after["freshness"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_index_freshness.py -v`

Expected: FAIL because freshness metadata is currently mostly counts and DB paths.

- [ ] **Step 3: Write full production implementation**

Expose metadata like:

```python
{
    "freshness": "ready",
    "last_indexed_at": "2026-03-30T12:34:56Z",
    "latest_source_mtime_ns": 1711792496000000000,
    "index_mtime_ns": 1711792496000000000,
    "drift_reasons": [],
    "replacement_sync": True,
}
```

Do not remove the intentional handled-edit freshness model.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_index_freshness.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/code_index_store.py mcp/server/aidocs_mcp/index_store.py mcp/tests/test_runtime_service.py mcp/tests/test_index_freshness.py
git commit -m "feat: expose deterministic index freshness metadata"
```

### Task 5: Add Native File Creation And Exact-Path Read Ergonomics

**Files:**
- Modify: `mcp/server/aidocs_mcp/file_ops.py`
- Modify: `mcp/server/aidocs_mcp/mcp_server.py:1123-1185`
- Create: `mcp/tests/test_file_ops_create_and_read.py`
- Modify: `mcp/tests/test_file_ops.py`

- [ ] **Step 1: Write the failing tests**

Add tests for:

```python
def test_create_file_writes_new_content_and_returns_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    result = create_file(project, "docs/new.md", "# New\n")

    assert result["success"] is True
    assert (project / "docs" / "new.md").read_text(encoding="utf-8") == "# New\n"

def test_code_get_lines_allows_known_exact_path_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    target = project / "notes.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = get_lines(project, "notes.txt", start_line=2, count=1, show_line_numbers=False)

    assert result["content"] == "two"

def test_create_file_respects_sensitive_path_guardrails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    result = create_file(project, ".env", "SECRET=1\n")

    assert result["success"] is False
    assert "sensitive" in result["error"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

`pytest mcp/tests/test_file_ops.py mcp/tests/test_file_ops_create_and_read.py -v`

Expected: FAIL because native create support and easier exact-path read behavior do not exist yet.

- [ ] **Step 3: Write full production implementation**

Add one explicit creation API rather than overloading normal line edits too aggressively.

Target shape:

```python
def create_file(project_root: Path, path: str, content: str, *, expect_missing: bool = True, dry_run: bool = False) -> dict[str, object]:
    abs_path = _resolve_path(project_root, path, write=True)
    if expect_missing and abs_path.exists():
        return {"success": False, "path": path, "error": "File already exists"}
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        abs_path.write_text(content, encoding="utf-8")
    return {"success": True, "path": path, "created": not dry_run, "bytes_written": len(content.encode("utf-8"))}
```

And expose a narrow exact-path read escape hatch such as:

```python
def code_get_lines(project_root: str, path: str, start_line: int = 1, count: int = 50, show_line_numbers: bool = True, known_path: bool = False) -> dict[str, Any]:
    root = Path(project_root)
    if not known_path:
        gate = _require_indexed_read_gate(root)
        if gate:
            return gate
    return _file_get_lines(root, path, start_line=start_line, count=count, show_line_numbers=show_line_numbers)
```

where `known_path=True` only bypasses discovery gating for an exact relative path inside the project root.

- [ ] **Step 4: Run tests to verify they pass**

Run:

`pytest mcp/tests/test_file_ops.py mcp/tests/test_file_ops_create_and_read.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/file_ops.py mcp/server/aidocs_mcp/mcp_server.py mcp/tests/test_file_ops.py mcp/tests/test_file_ops_create_and_read.py
git commit -m "feat: add native file creation and exact-path line reads"
```

### Task 6: Fix CLI Help Behavior

**Files:**
- Modify: `mcp/server/aidocs_mcp/mcp_server.py:2573-2592`
- Create: `mcp/tests/test_mcp_server_cli.py`

- [ ] **Step 1: Write the failing test**

Add a subprocess test like:

```python
def test_mcp_server_help_prints_usage_and_exits() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aidocs_mcp.mcp_server", "--help"],
        capture_output=True,
        text=True,
        cwd=str(MCP_ROOT),
    )

    assert result.returncode == 0
    assert "usage" in (result.stdout + result.stderr).lower()
    assert "listening" not in (result.stdout + result.stderr).lower()
```

Required assertions:
- exit code is `0`
- output contains `usage` or equivalent help text
- output does not indicate server startup

- [ ] **Step 2: Run test to verify it fails**

Run:

`pytest mcp/tests/test_mcp_server_cli.py -v`

Expected: FAIL because `main()` currently starts the server directly.

- [ ] **Step 3: Write full production implementation**

Introduce a real CLI parser:

```python
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m aidocs_mcp.mcp_server", description="Run the AIDOCS MCP server")
    parser.parse_args(argv)
    server.run()
```

Do not change server runtime behavior when no arguments are passed.

- [ ] **Step 4: Run test to verify it passes**

Run:

`pytest mcp/tests/test_mcp_server_cli.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/server/aidocs_mcp/mcp_server.py mcp/tests/test_mcp_server_cli.py
git commit -m "fix: make mcp server help deterministic"
```

### Task 7: Full Verification And Tooling Report Refresh

**Files:**
- Modify: `docs/specs/2026-03-30-aidocs-tooling-followup-design.md` (only if implementation reality diverges)

- [ ] **Step 1: Run the phase-targeted suites**

Run:

`pytest mcp/tests/test_plan_tools.py mcp/tests/test_plan_resolution.py mcp/tests/test_roadmap_feedback.py -v`

`pytest mcp/tests/test_runtime_service.py mcp/tests/test_index_freshness.py -v`

`pytest mcp/tests/test_file_ops.py mcp/tests/test_file_ops_create_and_read.py mcp/tests/test_mcp_server_cli.py -v`

Expected: PASS

- [ ] **Step 2: Run the full MCP suite**

Run:

`pytest mcp/tests -v`

Expected: PASS

- [ ] **Step 3: Run the final CLI verification**

Run:

`python -m aidocs_mcp.mcp_server --help`

Expected: usage/help output and clean exit without server startup

- [ ] **Step 4: Capture final AIDOCS tool findings**

Record whether these issues are resolved:

- plan/roadmap fallback hard-fail
- missing handoff completed status
- unclear freshness metadata
- no native file creation
- awkward exact-path line reads
- broken CLI help

- [ ] **Step 5: Commit**

```bash
git add mcp/tests docs/specs/2026-03-30-aidocs-tooling-followup-design.md
git commit -m "test: verify tooling follow-up behavior end to end"
```
