# Test Suite Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `mcp/tests/` into stable domain folders and add a concise `mcp/tests/INDEX.md` so humans and agents can find relevant tests faster without changing the trust model of the full suite.

**Architecture:** Keep pytest and test behavior unchanged while changing filesystem structure in small, reviewable move clusters. Create the destination folders and `INDEX.md` first, then move obvious files by domain, splitting or deferring only files whose ownership is genuinely mixed, and verify collection/domain slices after each cluster before running the full suite.

**Tech Stack:** Python, pytest, Markdown, git file moves

---

## File Structure

- Create: `mcp/tests/INDEX.md`
  - Operational guide for folder purposes, non-obvious file placement, change-to-test hints, and broad cross-domain tests.
- Create: `mcp/tests/host/`
  - Host/plugin/hook/host-contract tests.
- Create: `mcp/tests/runtime/`
  - Runtime/session/orchestration tests.
- Create: `mcp/tests/planning/`
  - Plan/conductor/execution/verification tests.
- Create: `mcp/tests/security/`
  - Query gate/file guardrail/intent/scope tests.
- Create: `mcp/tests/indexing/`
  - Code index/analysis/outlines/descriptors/modules tests.
- Create: `mcp/tests/config/`
  - Config schema/resolution/migration/provider tests.
- Create: `mcp/tests/project/`
  - Project init/status/related-project/roadmap wiring tests.
- Modify: `mcp/tests/conftest.py` if imports or local fixture paths need path-safe updates after moves.
- Move: obvious `mcp/tests/test_*.py` files into one of the new domain folders.

### Task 1: Create domain folders and the test index skeleton

**Files:**
- Create: `mcp/tests/INDEX.md`
- Create: `mcp/tests/host/.gitkeep`
- Create: `mcp/tests/runtime/.gitkeep`
- Create: `mcp/tests/planning/.gitkeep`
- Create: `mcp/tests/security/.gitkeep`
- Create: `mcp/tests/indexing/.gitkeep`
- Create: `mcp/tests/config/.gitkeep`
- Create: `mcp/tests/project/.gitkeep`

- [ ] **Step 1: Write the index and create the destination folders**

Create `mcp/tests/INDEX.md` with concrete folder guidance.

```md
# MCP Test Index

## Folder Map

- `host/`: Claude/OpenCode/Codex hooks, plugin integrations, host-facing runtime contracts
- `runtime/`: session lifecycle, managed mode, runtime services, orchestration state
- `planning/`: plans, conductor, execution loop, verification gate, subagent/task flow
- `security/`: query gate, file guardrails, intent guard, scope-based access behavior
- `indexing/`: code sync, analysis, outlines, descriptors, modules, freshness/index behavior
- `config/`: config schema, resolution, migration, provider compatibility
- `project/`: project init, status, related-project wiring, roadmap/project-level flows

## Non-Obvious File Notes

- `host/test_host_state_contract.py`: stays under `host/` because it verifies host-consumed runtime payload contracts.
- `security/test_mcp_server_trace_depth.py`: keep here only if its primary purpose remains indexed-read gating; otherwise move to `indexing/` if code-trace semantics dominate.

## If You Changed X, Start Here

- plugin, host hook, host runtime payload changes -> `host/`
- runtime startup, session resume, managed mode changes -> `runtime/`
- plan, conductor, execution, verification changes -> `planning/`
- query gate, file ops, intent guard, scope changes -> `security/`
- code index, symbol search, outlines, descriptors, modules changes -> `indexing/`
- config schema, config loading, config migration changes -> `config/`
- project bootstrap, related project, roadmap/project status changes -> `project/`

## Broad Cross-Domain Tests

- `host/test_host_integration.py`
- `runtime/test_runtime_service.py`
- `planning/test_execution_loop.py`
```

Create the domain directories with `.gitkeep` files so the first reorganization commit is explicit and reviewable.

- [ ] **Step 2: Verify the new index file is discoverable and folders exist**

Run: `pytest --collect-only -q`

Expected:
- Collection still succeeds.
- No tests move yet, so the collected count should remain unchanged.

- [ ] **Step 3: Commit only if explicitly requested**

Do not create a commit unless the user asks for one.

### Task 2: Move the host and runtime tests into their domain folders

**Files:**
- Move: `mcp/tests/test_host_integration.py` -> `mcp/tests/host/test_host_integration.py`
- Move: `mcp/tests/test_claude_hook.py` -> `mcp/tests/host/test_claude_hook.py`
- Move: `mcp/tests/test_claude_host_contract.py` -> `mcp/tests/host/test_claude_host_contract.py`
- Move: `mcp/tests/test_host_state_contract.py` -> `mcp/tests/host/test_host_state_contract.py`
- Move: `mcp/tests/test_opencode_plugin_simplification.py` -> `mcp/tests/host/test_opencode_plugin_simplification.py`
- Move: `mcp/tests/test_opencode_external_skill_integration.py` -> `mcp/tests/host/test_opencode_external_skill_integration.py`
- Move: `mcp/tests/test_claude_external_skill_integration.py` -> `mcp/tests/host/test_claude_external_skill_integration.py`
- Move: `mcp/tests/test_mcp_server_cli.py` -> `mcp/tests/host/test_mcp_server_cli.py`
- Move: `mcp/tests/test_runtime_service.py` -> `mcp/tests/runtime/test_runtime_service.py`
- Move: `mcp/tests/test_managed_mode_service.py` -> `mcp/tests/runtime/test_managed_mode_service.py`
- Move: `mcp/tests/test_managed_file_service.py` -> `mcp/tests/runtime/test_managed_file_service.py`
- Move: `mcp/tests/test_session_store.py` -> `mcp/tests/runtime/test_session_store.py`
- Move: `mcp/tests/test_session_normalization_tools.py` -> `mcp/tests/runtime/test_session_normalization_tools.py`
- Move: `mcp/tests/test_runtime_session_goal_stability.py` -> `mcp/tests/runtime/test_runtime_session_goal_stability.py`
- Move: `mcp/tests/test_skill_store.py` -> `mcp/tests/runtime/test_skill_store.py`
- Move: `mcp/tests/test_skill_trigger_runtime.py` -> `mcp/tests/runtime/test_skill_trigger_runtime.py`
- Move: `mcp/tests/test_skill_override_runtime.py` -> `mcp/tests/runtime/test_skill_override_runtime.py`

- [ ] **Step 1: Move the files with git-aware renames**

Run:

```powershell
git mv "mcp/tests/test_host_integration.py" "mcp/tests/host/test_host_integration.py"
git mv "mcp/tests/test_claude_hook.py" "mcp/tests/host/test_claude_hook.py"
git mv "mcp/tests/test_claude_host_contract.py" "mcp/tests/host/test_claude_host_contract.py"
git mv "mcp/tests/test_host_state_contract.py" "mcp/tests/host/test_host_state_contract.py"
git mv "mcp/tests/test_opencode_plugin_simplification.py" "mcp/tests/host/test_opencode_plugin_simplification.py"
git mv "mcp/tests/test_opencode_external_skill_integration.py" "mcp/tests/host/test_opencode_external_skill_integration.py"
git mv "mcp/tests/test_claude_external_skill_integration.py" "mcp/tests/host/test_claude_external_skill_integration.py"
git mv "mcp/tests/test_mcp_server_cli.py" "mcp/tests/host/test_mcp_server_cli.py"
git mv "mcp/tests/test_runtime_service.py" "mcp/tests/runtime/test_runtime_service.py"
git mv "mcp/tests/test_managed_mode_service.py" "mcp/tests/runtime/test_managed_mode_service.py"
git mv "mcp/tests/test_managed_file_service.py" "mcp/tests/runtime/test_managed_file_service.py"
git mv "mcp/tests/test_session_store.py" "mcp/tests/runtime/test_session_store.py"
git mv "mcp/tests/test_session_normalization_tools.py" "mcp/tests/runtime/test_session_normalization_tools.py"
git mv "mcp/tests/test_runtime_session_goal_stability.py" "mcp/tests/runtime/test_runtime_session_goal_stability.py"
git mv "mcp/tests/test_skill_store.py" "mcp/tests/runtime/test_skill_store.py"
git mv "mcp/tests/test_skill_trigger_runtime.py" "mcp/tests/runtime/test_skill_trigger_runtime.py"
git mv "mcp/tests/test_skill_override_runtime.py" "mcp/tests/runtime/test_skill_override_runtime.py"
```

- [ ] **Step 2: Fix any import or path assumptions only if collection breaks**

If `mcp/tests/conftest.py` or any test helper uses hard-coded sibling paths, update them minimally.

```python
# Example pattern only if needed after moves
TESTS_ROOT = Path(__file__).resolve().parents[1]
```

- [ ] **Step 3: Verify host and runtime collection/runs**

Run:

```powershell
pytest mcp/tests/host -q
pytest mcp/tests/runtime -q
```

Expected:
- Both domain folders collect and run successfully.
- No import errors due to the new file paths.

### Task 3: Move the planning and security tests into their domain folders

**Files:**
- Move: `mcp/tests/test_plan_tools.py` -> `mcp/tests/planning/test_plan_tools.py`
- Move: `mcp/tests/test_plan_resolution.py` -> `mcp/tests/planning/test_plan_resolution.py`
- Move: `mcp/tests/test_plan_create_validate.py` -> `mcp/tests/planning/test_plan_create_validate.py`
- Move: `mcp/tests/test_plan_conductor_parse.py` -> `mcp/tests/planning/test_plan_conductor_parse.py`
- Move: `mcp/tests/test_plan_conductor_runtime.py` -> `mcp/tests/planning/test_plan_conductor_runtime.py`
- Move: `mcp/tests/test_plan_conductor_interaction.py` -> `mcp/tests/planning/test_plan_conductor_interaction.py`
- Move: `mcp/tests/test_execution_loop.py` -> `mcp/tests/planning/test_execution_loop.py`
- Move: `mcp/tests/test_execution_mode_select.py` -> `mcp/tests/planning/test_execution_mode_select.py`
- Move: `mcp/tests/test_verification_gate.py` -> `mcp/tests/planning/test_verification_gate.py`
- Move: `mcp/tests/test_subagent_packets.py` -> `mcp/tests/planning/test_subagent_packets.py`
- Move: `mcp/tests/test_conductor_hardening.py` -> `mcp/tests/planning/test_conductor_hardening.py`
- Move: `mcp/tests/test_file_ops.py` -> `mcp/tests/security/test_file_ops.py`
- Move: `mcp/tests/test_file_ops_create_and_read.py` -> `mcp/tests/security/test_file_ops_create_and_read.py`
- Move: `mcp/tests/test_query_gate_ux.py` -> `mcp/tests/security/test_query_gate_ux.py`
- Move: `mcp/tests/test_intent_guard.py` -> `mcp/tests/security/test_intent_guard.py`
- Move: `mcp/tests/test_scope_based_security.py` -> `mcp/tests/security/test_scope_based_security.py`
- Move: `mcp/tests/test_tiered_read_access.py` -> `mcp/tests/security/test_tiered_read_access.py`
- Move: `mcp/tests/test_tool_precision_paths.py` -> `mcp/tests/security/test_tool_precision_paths.py`

- [ ] **Step 1: Move the planning and security files**

Run:

```powershell
git mv "mcp/tests/test_plan_tools.py" "mcp/tests/planning/test_plan_tools.py"
git mv "mcp/tests/test_plan_resolution.py" "mcp/tests/planning/test_plan_resolution.py"
git mv "mcp/tests/test_plan_create_validate.py" "mcp/tests/planning/test_plan_create_validate.py"
git mv "mcp/tests/test_plan_conductor_parse.py" "mcp/tests/planning/test_plan_conductor_parse.py"
git mv "mcp/tests/test_plan_conductor_runtime.py" "mcp/tests/planning/test_plan_conductor_runtime.py"
git mv "mcp/tests/test_plan_conductor_interaction.py" "mcp/tests/planning/test_plan_conductor_interaction.py"
git mv "mcp/tests/test_execution_loop.py" "mcp/tests/planning/test_execution_loop.py"
git mv "mcp/tests/test_execution_mode_select.py" "mcp/tests/planning/test_execution_mode_select.py"
git mv "mcp/tests/test_verification_gate.py" "mcp/tests/planning/test_verification_gate.py"
git mv "mcp/tests/test_subagent_packets.py" "mcp/tests/planning/test_subagent_packets.py"
git mv "mcp/tests/test_conductor_hardening.py" "mcp/tests/planning/test_conductor_hardening.py"
git mv "mcp/tests/test_file_ops.py" "mcp/tests/security/test_file_ops.py"
git mv "mcp/tests/test_file_ops_create_and_read.py" "mcp/tests/security/test_file_ops_create_and_read.py"
git mv "mcp/tests/test_query_gate_ux.py" "mcp/tests/security/test_query_gate_ux.py"
git mv "mcp/tests/test_intent_guard.py" "mcp/tests/security/test_intent_guard.py"
git mv "mcp/tests/test_scope_based_security.py" "mcp/tests/security/test_scope_based_security.py"
git mv "mcp/tests/test_tiered_read_access.py" "mcp/tests/security/test_tiered_read_access.py"
git mv "mcp/tests/test_tool_precision_paths.py" "mcp/tests/security/test_tool_precision_paths.py"
```

- [ ] **Step 2: Reclassify or defer mixed tests deliberately**

Review these before moving, because they can plausibly belong to more than one domain:

```text
test_mcp_server_trace_depth.py
test_policy_service.py
test_tool_overview_payloads.py
test_tool_surface_taxonomy.py
```

If one is still mixed after review, leave it in `mcp/tests/` for this pass and add a note in `INDEX.md` rather than forcing a bad placement.

- [ ] **Step 3: Verify planning and security domains**

Run:

```powershell
pytest mcp/tests/planning -q
pytest mcp/tests/security -q
```

Expected:
- Both domains collect and run successfully.
- Any deferred mixed files remain explicitly documented rather than hidden.

### Task 4: Move indexing, config, and project tests and finalize the index

**Files:**
- Move obvious indexing files such as:
  - `test_code_sync.py`
  - `test_code_analysis.py`
  - `test_code_outlines.py`
  - `test_language_descriptors.py`
  - `test_code_modules.py`
  - `test_code_roles.py`
  - `test_index_freshness.py`
  - `test_index_store.py`
  - `test_schema_index_store.py`
- Move obvious config files such as:
  - `test_config_schema.py`
  - `test_config_resolution.py`
  - `test_config_migration.py`
  - `test_skill_provider_compatibility.py`
  - `test_skill_provider_registry.py`
  - `test_skill_override_policy.py`
  - `test_bundled_provider_completion.py`
- Move obvious project files such as:
  - `test_project_init_workflow_files.py`
  - `test_project_status_service.py`
  - `test_related_project_service.py`
  - `test_roadmap_feedback.py`
  - `test_ensure_mcp_config.py`
  - `test_cli.py`
  - `test_legacy_migration_service.py`
  - `test_updater_service.py`
- Modify: `mcp/tests/INDEX.md`

- [ ] **Step 1: Move the obvious indexing/config/project files**

Run representative moves like:

```powershell
git mv "mcp/tests/test_code_sync.py" "mcp/tests/indexing/test_code_sync.py"
git mv "mcp/tests/test_code_analysis.py" "mcp/tests/indexing/test_code_analysis.py"
git mv "mcp/tests/test_code_outlines.py" "mcp/tests/indexing/test_code_outlines.py"
git mv "mcp/tests/test_language_descriptors.py" "mcp/tests/indexing/test_language_descriptors.py"
git mv "mcp/tests/test_code_modules.py" "mcp/tests/indexing/test_code_modules.py"
git mv "mcp/tests/test_code_roles.py" "mcp/tests/indexing/test_code_roles.py"
git mv "mcp/tests/test_index_freshness.py" "mcp/tests/indexing/test_index_freshness.py"
git mv "mcp/tests/test_index_store.py" "mcp/tests/indexing/test_index_store.py"
git mv "mcp/tests/test_schema_index_store.py" "mcp/tests/indexing/test_schema_index_store.py"
git mv "mcp/tests/test_config_schema.py" "mcp/tests/config/test_config_schema.py"
git mv "mcp/tests/test_config_resolution.py" "mcp/tests/config/test_config_resolution.py"
git mv "mcp/tests/test_config_migration.py" "mcp/tests/config/test_config_migration.py"
git mv "mcp/tests/test_skill_provider_compatibility.py" "mcp/tests/config/test_skill_provider_compatibility.py"
git mv "mcp/tests/test_skill_provider_registry.py" "mcp/tests/config/test_skill_provider_registry.py"
git mv "mcp/tests/test_skill_override_policy.py" "mcp/tests/config/test_skill_override_policy.py"
git mv "mcp/tests/test_bundled_provider_completion.py" "mcp/tests/config/test_bundled_provider_completion.py"
git mv "mcp/tests/test_project_init_workflow_files.py" "mcp/tests/project/test_project_init_workflow_files.py"
git mv "mcp/tests/test_project_status_service.py" "mcp/tests/project/test_project_status_service.py"
git mv "mcp/tests/test_related_project_service.py" "mcp/tests/project/test_related_project_service.py"
git mv "mcp/tests/test_roadmap_feedback.py" "mcp/tests/project/test_roadmap_feedback.py"
git mv "mcp/tests/test_ensure_mcp_config.py" "mcp/tests/project/test_ensure_mcp_config.py"
git mv "mcp/tests/test_cli.py" "mcp/tests/project/test_cli.py"
git mv "mcp/tests/test_legacy_migration_service.py" "mcp/tests/project/test_legacy_migration_service.py"
git mv "mcp/tests/test_updater_service.py" "mcp/tests/project/test_updater_service.py"
```

- [ ] **Step 2: Update `mcp/tests/INDEX.md` to match the final placements**

Edit the broad test and non-obvious placement sections so they reflect any files left at the root and any cross-domain exceptions.

- [ ] **Step 3: Verify the moved domains and root leftovers collect cleanly**

Run:

```powershell
pytest mcp/tests/indexing -q
pytest mcp/tests/config -q
pytest mcp/tests/project -q
pytest --collect-only -q
```

Expected:
- Domain folders run successfully.
- Root-level leftovers are intentional and still collect.

### Task 5: Final cleanup, root leftovers review, and full-suite verification

**Files:**
- Modify: `mcp/tests/INDEX.md`
- Review: any remaining `mcp/tests/test_*.py` at the root

- [ ] **Step 1: Inspect remaining root-level test files**

Run:

```powershell
Get-ChildItem "mcp/tests" -File -Filter "test_*.py" | Select-Object -ExpandProperty Name
```

Expected:
- Only intentionally mixed or deferred files remain.

- [ ] **Step 2: Add explicit index notes for each remaining root-level test file**

For every remaining root-level file, add one line explaining why it stayed at the root for now.

```md
- `test_mcp_server_trace_depth.py`: temporarily left at root because it mixes indexing and security responsibilities; split before final placement.
```

- [ ] **Step 3: Run the full suite**

Run: `pytest`

Expected:
- The full MCP suite passes from the reorganized layout.

- [ ] **Step 4: Commit only if explicitly requested**

Do not create a commit unless the user asks for one.

## Self-Review

- Spec coverage: the plan creates domain folders, adds `mcp/tests/INDEX.md`, moves obvious files by primary contract, documents exceptions, and ends with full-suite verification.
- Placeholder scan: no TODO/TBD placeholders remain; deferred mixed files are handled explicitly with index notes instead of vague future work.
- Type consistency: all paths and folder names match the approved domain structure (`host`, `runtime`, `planning`, `security`, `indexing`, `config`, `project`).
