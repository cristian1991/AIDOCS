# Host Hooks And Multi-Host Integration Implementation Plan

> **For AIDOCS session work:** Execute from the active AIDOCS session using session plans, indexed retrieval, and targeted verification after each phase. Keep execution AIDOCS-native rather than relying on external workflow skills.

**Goal:** Add AIDOCS-native startup hook coverage and multi-host integration improvements across Claude, OpenCode, Cursor, and Codex without weakening the current layered routing and guardrail model.

**Architecture:** AIDOCS keeps a layered host integration model. `session_start` becomes the startup-state layer only, `UserPromptSubmit` remains the prompt-routing and `/aidocs` gate, and tool-time hooks remain responsible for MCP-first nudges, safety boundaries, and workflow follow-through. Each host gets the strongest implementation its March 2026 surface supports.

**Tech Stack:** Python, Node.js plugin code, shell/PowerShell wrappers, JSON hook manifests, pytest.

---

## Implementation Standards

- Do not introduce stub handlers, placeholder manifests, or documentation-only packaging. Every created file must participate in real host integration behavior.
- Do not stop at the smallest passing change if it leaves obvious gaps in portability, startup-state coverage, or host-specific response shaping.
- Every new host surface must get exhaustive tests for both positive and negative behavior, not just file-existence checks.
- Prefer shared runtime logic for startup-state computation and compact guidance generation so hosts differ only at the binding layer.
- Run targeted tests at the end of each phase before moving on.
- Run the full relevant integration suite when all phases complete.

---

## Host Matrix

| Host | Host Surface | Startup Entry | Prompt Entry | Tool Entry | Target AIDOCS Behavior |
|---|---|---|---|---|---|
| Claude Code | Native hooks | `SessionStart` | `UserPromptSubmit` | `PreToolUse`, later optional `PostToolUse` | Full layered model |
| OpenCode | Plugin events and transforms | plugin startup/session path | `chat.message` plus transforms | `tool.execute.before`, `tool.execute.after` | Plugin-native equivalent of layered model |
| Cursor | Plugin manifest plus hook file | `sessionStart` | none in first pass | none in first pass | Startup-only first pass |
| Codex | Experimental hooks | `SessionStart` | `UserPromptSubmit` | `PreToolUse`, `PostToolUse` for Bash only | Reduced layered model |

## Shared State Model

All startup implementations must compute the same internal startup state:

- `not_initialized`
- `not_bootstrapped`
- `no_session`
- `multiple_sessions`
- `stale_indexes`
- `ready`

State precedence:

1. Initialization
2. Bootstrap / managed mode
3. Session availability
4. Session ambiguity
5. Index freshness
6. Ready-state resume guidance

Rules:

- `multiple_sessions` must tell the agent to ask the user which session to connect to.
- `session_start` is advisory only. It does not run bootstrap, create sessions, or mutate index state.
- Startup output must stay compact. It should name the next action, not dump full session contents by default.

## Planned Files

**Create**

- `core/hooks/session-start`
- `core/hooks/run-hook.cmd`
- `core/hooks/hooks.json`
- `core/hooks/hooks-cursor.json`
- `core/.cursor-plugin/plugin.json`
- `core/.codex/hooks.json`
- `core/tests/host_integration/test_session_start_hook.py`
- `core/tests/host_integration/test_host_packaging.py`
- `core/tests/host_integration/test_opencode_startup_context.py`
- `core/tests/host_integration/test_codex_hook_packaging.py`

**Modify**

- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/claude_hook.py`
- `core/plugins/aidocs.js`
- `core/scripts/install-agent-routing.sh`
- `core/scripts/install-agent-routing.ps1`
- `core/scripts/claude-hook.sh`
- `core/scripts/claude-hook.ps1`
- `README_INSTALL.md`
- `mcp/HOST_INTEGRATION.md`
- `mcp/tests/test_claude_hook.py`

---

## Phase Verification

### Phase 1 Checkpoint

After Tasks 1 through 3 complete, run:

`pytest mcp/tests/test_claude_hook.py core/tests/host_integration/test_host_packaging.py core/tests/host_integration/test_session_start_hook.py -v`

This phase is only complete when:

- the full local `/.MEMORY/.aidocs/**` bundle copies during bootstrap
- startup-state computation passes all known state cases
- Claude `SessionStart` coexists with `UserPromptSubmit` and `PreToolUse`

### Phase 2 Checkpoint

After Tasks 4 through 6 complete, run:

`pytest core/tests/host_integration/test_opencode_startup_context.py core/tests/host_integration/test_host_packaging.py core/tests/host_integration/test_codex_hook_packaging.py -v`

This phase is only complete when:

- OpenCode startup behavior matches the shared startup-state model
- Cursor startup packaging is real and validated
- Codex startup and prompt packaging is real, documented, and validated against current host limits

### Phase 3 Checkpoint

After Tasks 7 through 9 complete, run:

`pytest mcp/tests/test_claude_hook.py core/tests/host_integration -v`

and then:

`python -m aidocs_mcp.mcp_server --help`

This phase is only complete when:

- portability wrappers are covered by tests
- layered startup/prompt/tool behavior is covered by behavior-compliance tests
- docs reflect the shipped host model and limitations

---

### Task 1: Fix Local AIDOCS Bundle Bootstrap

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Test: `core/tests/host_integration/test_host_packaging.py`

- [ ] **Step 1: Write the failing test**

Add a test that initializes a temp project and asserts these files exist afterward:

```text
<project>/.MEMORY/.aidocs/index.aidocs
<project>/.MEMORY/.aidocs/global-instructions.aidocs
<project>/.MEMORY/.aidocs/memory-system.aidocs
<project>/.MEMORY/.aidocs/coding-standards.aidocs
<project>/.MEMORY/.aidocs/templates/
<project>/.MEMORY/.aidocs/personalities/
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k local_bundle -v`

Expected: FAIL because `project_init()` currently copies only the top-level memory template output plus `index.aidocs`.

- [ ] **Step 3: Write full production implementation**

Update the bootstrap copy path so it preserves existing project files but fills the full local `/.MEMORY/.aidocs/**` tree.

Implementation target:

```python
# inside project_init()
copy_missing_tree(source_memory_root / ".aidocs", target_memory_root / ".aidocs")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k local_bundle -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add mcp/server/aidocs_mcp/runtime_service.py core/tests/host_integration/test_host_packaging.py && git commit -m "fix: copy full local aidocs bundle during init"`

### Task 2: Add Shared Startup State Computation

**Files:**
- Modify: `mcp/server/aidocs_mcp/runtime_service.py`
- Modify: `mcp/server/aidocs_mcp/claude_hook.py`
- Test: `core/tests/host_integration/test_session_start_hook.py`

- [ ] **Step 1: Write the failing tests**

Add tests for each startup state:

```text
not_initialized
not_bootstrapped
no_session
multiple_sessions
stale_indexes
ready
```

Required assertions:

- `multiple_sessions` asks for user choice
- `stale_indexes` directs the agent to re-sync before normal work
- `ready` does not emit broad bootstrap instructions

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest core/tests/host_integration/test_session_start_hook.py -k state_model -v`

Expected: FAIL because no shared startup-state API exists yet.

- [ ] **Step 3: Write full production implementation**

Add one runtime helper that computes the startup state and a compact message payload.

Target shape:

```python
{
    "state": "multiple_sessions",
    "message": "Multiple plausible sessions exist. Ask the user which session to connect to before normal work.",
    "session_id": None,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest core/tests/host_integration/test_session_start_hook.py -k state_model -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add mcp/server/aidocs_mcp/runtime_service.py mcp/server/aidocs_mcp/claude_hook.py core/tests/host_integration/test_session_start_hook.py && git commit -m "feat: add startup state computation"`

### Task 3: Add Claude `SessionStart` Without Replacing Existing Hooks

**Files:**
- Create: `core/hooks/session-start`
- Create: `core/hooks/run-hook.cmd`
- Create: `core/hooks/hooks.json`
- Modify: `mcp/server/aidocs_mcp/claude_hook.py`
- Modify: `core/scripts/install-agent-routing.sh`
- Modify: `core/scripts/install-agent-routing.ps1`
- Modify: `mcp/tests/test_claude_hook.py`

- [ ] **Step 1: Write the failing tests**

Add Claude hook tests proving:

- `SessionStart` returns startup guidance
- `UserPromptSubmit` still handles `/aidocs`
- `PreToolUse` still injects tool nudges and protection

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration/test_session_start_hook.py -k "session_start or SessionStart" -v`

Expected: FAIL because Claude hook dispatch does not yet support `SessionStart`.

- [ ] **Step 3: Write minimal implementation**

Add a new dispatch branch:

```python
if event_name == "SessionStart":
    return self._handle_session_start(project_root, payload)
```

Register the hook in the installer while keeping the current `UserPromptSubmit` and `PreToolUse` registrations intact.
Also ensure the startup handler returns host-valid output with no regressions in existing hook behavior.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration/test_session_start_hook.py -k "session_start or SessionStart" -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add core/hooks core/scripts/install-agent-routing.sh core/scripts/install-agent-routing.ps1 mcp/server/aidocs_mcp/claude_hook.py mcp/tests/test_claude_hook.py core/tests/host_integration/test_session_start_hook.py && git commit -m "feat: add claude session start hook"`

### Task 4: Bring OpenCode Startup Logic To Parity

**Files:**
- Modify: `core/plugins/aidocs.js`
- Test: `core/tests/host_integration/test_opencode_startup_context.py`

- [ ] **Step 1: Write the failing test**

Add a plugin test verifying startup behavior is state-aware, not just “inject current session context once.”

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest core/tests/host_integration/test_opencode_startup_context.py -v`

Expected: FAIL because current OpenCode startup injection is session-context based, not startup-state based.

- [ ] **Step 3: Write full production implementation**

Refactor the plugin to use the same startup-state helper before injecting the first-turn startup block.

Target behavior:

```text
If not managed: tell the model to run /aidocs first.
If multiple sessions: tell the model to ask the user which session to connect to.
If stale indexes: tell the model to resync before normal work.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest core/tests/host_integration/test_opencode_startup_context.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add core/plugins/aidocs.js core/tests/host_integration/test_opencode_startup_context.py && git commit -m "feat: align opencode startup routing"`

### Task 5: Add Cursor Startup Packaging

**Files:**
- Create: `core/.cursor-plugin/plugin.json`
- Create: `core/hooks/hooks-cursor.json`
- Reuse: `core/hooks/session-start`
- Test: `core/tests/host_integration/test_host_packaging.py`

- [ ] **Step 1: Write the failing test**

Add a packaging test that asserts:

```json
{
  "hooks": {
    "sessionStart": [
      { "command": "./hooks/session-start" }
    ]
  }
}
```

and that the Cursor plugin manifest points at that hooks file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k cursor -v`

Expected: FAIL because AIDOCS does not currently ship Cursor packaging.

- [ ] **Step 3: Write full production implementation**

Add startup-only Cursor support in the first pass. The implementation must be real, packaged, and testable. Do not invent broader Cursor hooks until they are explicitly verified, but make the shipped startup path complete and host-valid.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k cursor -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add core/.cursor-plugin/plugin.json core/hooks/hooks-cursor.json core/tests/host_integration/test_host_packaging.py && git commit -m "feat: add cursor startup packaging"`

### Task 6: Add Codex Startup And Prompt Packaging

**Files:**
- Create: `core/.codex/hooks.json`
- Modify: `mcp/HOST_INTEGRATION.md`
- Test: `core/tests/host_integration/test_codex_hook_packaging.py`

- [ ] **Step 1: Write the failing test**

Add tests asserting the Codex package includes:

```json
{
  "hooks": {
    "SessionStart": [...],
    "UserPromptSubmit": [...]
  }
}
```

and that docs mention:

- Bash-only `PreToolUse` / `PostToolUse`
- hooks experimental
- hooks disabled on Windows in March 2026

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest core/tests/host_integration/test_codex_hook_packaging.py -v`

Expected: FAIL because AIDOCS currently ships no Codex hook package.

- [ ] **Step 3: Write full production implementation**

Add startup and prompt-routing hooks first. Add Bash-only tool hooks only if the packaging/test layer can verify the limitation clearly. The shipped Codex package must reflect real host constraints rather than placeholders for future functionality.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest core/tests/host_integration/test_codex_hook_packaging.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add core/.codex/hooks.json mcp/HOST_INTEGRATION.md core/tests/host_integration/test_codex_hook_packaging.py && git commit -m "feat: add codex startup and prompt packaging"`

### Task 7: Harden Shared Portability Layer

**Files:**
- Create: `core/hooks/run-hook.cmd`
- Modify: `core/hooks/session-start`
- Modify: `README_INSTALL.md`
- Modify: `mcp/HOST_INTEGRATION.md`
- Test: `core/tests/host_integration/test_host_packaging.py`

- [ ] **Step 1: Write the failing test**

Add assertions that all startup packaging surfaces reference a shared, Windows-safe entry strategy and that host-specific output fields are handled deliberately.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k portability -v`

Expected: FAIL because AIDOCS does not yet ship a reusable wrapper layer.

- [ ] **Step 3: Write full production implementation**

Use one startup script and one wrapper strategy for Windows-safe invocation. Keep host-specific response-shape logic inside the startup script, not duplicated across manifests.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest core/tests/host_integration/test_host_packaging.py -k portability -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add core/hooks README_INSTALL.md mcp/HOST_INTEGRATION.md core/tests/host_integration/test_host_packaging.py && git commit -m "feat: harden startup hook portability"`

### Task 8: Add Behavior-Compliance Coverage

**Files:**
- Modify: `mcp/tests/test_claude_hook.py`
- Modify: `core/tests/host_integration/test_session_start_hook.py`
- Modify: `core/tests/host_integration/test_opencode_startup_context.py`
- Modify: `core/tests/host_integration/test_codex_hook_packaging.py`

- [ ] **Step 1: Write exhaustive failing integration checks**

Add behavior checks proving:

- `session_start` does not replace `UserPromptSubmit`
- `session_start` does not replace `PreToolUse`
- multiple-session state asks for user choice
- stale-index state routes to re-sync guidance
- ready-state output stays compact
- invalid or unknown startup state fails safely
- host-specific output formats remain valid for the target host
- installer or packaging changes do not silently drop existing hook coverage

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration -v`

Expected: FAIL until all host integrations honor the layered model.

- [ ] **Step 3: Write full production implementation**

Fill the remaining gaps until tests reflect real behavior, not just manifest presence.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration -v`

Expected: PASS

- [ ] **Step 5: Commit**

Run: `git add mcp/tests/test_claude_hook.py core/tests/host_integration && git commit -m "test: cover startup hook behavior across hosts"`

### Task 9: Final Verification

**Files:**
- Modify: `README_INSTALL.md`
- Modify: `mcp/HOST_INTEGRATION.md`

- [ ] **Step 1: Update installation and host docs**

Document exactly which hosts support:

- startup-only coverage
- layered prompt/tool coverage
- Bash-only tool interception
- known host limitations

- [ ] **Step 2: Run the full host integration suite**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration -v`

Expected: PASS

- [ ] **Step 3: Run the full host integration suite**

Run: `pytest mcp/tests/test_claude_hook.py core/tests/host_integration -v`

Expected: PASS

- [ ] **Step 4: Run runtime import sanity check**

Run: `python -m aidocs_mcp.mcp_server --help`

Expected: usage text or successful exit without import failures

- [ ] **Step 5: Commit**

Run: `git add README_INSTALL.md mcp/HOST_INTEGRATION.md && git commit -m "docs: describe multi-host startup hook model"`
