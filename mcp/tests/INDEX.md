# MCP Test Index

## Folder Map

- `host/`: Claude/OpenCode/Codex hooks, plugin integrations, host-facing runtime contracts
- `runtime/`: session lifecycle, managed mode, runtime services, orchestration state
- `planning/`: plans, conductor, execution loop, verification gate, subagent/task flow
- `security/`: query gate, file guardrails, intent guard, scope-based access behavior
- `indexing/`: code sync, analysis, outlines, descriptors, modules, freshness/index behavior
- `config/`: config schema, resolution, migration, provider compatibility
- `project/`: project bootstrap and project-surface workflows: init, readiness/status, roadmap, related-project surfaces, and operator entry contracts like the CLI when they primarily protect project-facing workflows; not a catch-all for host/runtime/planning internals

## Non-Obvious File Notes

- `host/test_host_state_contract.py`: stays under `host/` because it verifies host-consumed runtime payload contracts.
- `project/test_cli.py`: stays under `project/` because it verifies the CLI as an operator entry surface for init/status/sync/config project workflows, even when individual commands touch lower-level services.

## Intentional Root-Level Files After Task 5

- `test_action_surface_service.py`: stays at `mcp/tests/` because it exercises the cross-cutting action-surface assessment layer that combines session context, capability indexing, procedures, and execution history.
- `test_mcp_server_trace_depth.py`: stays at `mcp/tests/` because it mixes indexed-read gate coverage with trace-depth response shaping; split before choosing between `security/` and `indexing/`.
- `test_memory_store.py`: stays at `mcp/tests/` because it covers the shared `.MEMORY` persistence/routing store used across workflow, domain, and project memory paths rather than one test domain.
- `test_policy_service.py`: stays at `mcp/tests/` because it sits between runtime session policy enforcement and project/session selection behavior.
- `test_procedure_capability_link_store.py`: stays at `mcp/tests/` because it verifies the linkage layer between indexed procedures and indexed capabilities, spanning workflow parsing and capability metadata.
- `test_procedure_index_store.py`: stays at `mcp/tests/` because it verifies procedure indexing across workflow-rule compilation and persisted procedure records instead of a single runtime or indexing slice.
- `test_tool_overview_payloads.py`: stays at `mcp/tests/` because it spans project bootstrap, skill/runtime state, and plan overview payload contracts.
- `test_tool_surface_taxonomy.py`: stays at `mcp/tests/` because it checks the broad public MCP taxonomy rather than one domain folder's behavior.
- `test_workflow_action_service.py`: stays at `mcp/tests/` because it covers the workflow-action compiler and trigger mapping that feed both project workflow rules and runtime action handling.

## If You Changed X, Start Here

- plugin, host hook, host runtime payload changes -> `host/`
- runtime startup, session resume, managed mode changes -> `runtime/`
- plan, conductor, execution, verification changes -> `planning/`
- query gate, file ops, intent guard, scope changes -> `security/`
- code index, symbol search, outlines, descriptors, modules changes -> `indexing/`
- config schema, config loading, config migration changes -> `config/`
- project bootstrap, project status/readiness, roadmap, related-project surface changes -> `project/`

## Cross-Domain Placement Rule

- Do not create a separate catch-all bucket for broad integration coverage.
- Put a cross-domain test under the folder for the primary contract or operator-facing surface it protects.
- Choose the folder by the first question a maintainer would ask when the test fails: host contract -> `host/`, runtime/session lifecycle -> `runtime/`, plan/execution/verification flow -> `planning/`, security boundary -> `security/`, indexing contract -> `indexing/`, config resolution/schema -> `config/`, project bootstrap or project-surface workflow -> `project/`.
- If a test spans multiple areas, keep it with the dominant surface and add a short file-level note only when the placement would otherwise be surprising.
