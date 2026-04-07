# Test Suite Reorganization Design

## Purpose

Reorganize the `mcp/tests/` suite so humans and agents can identify the right tests to inspect and run without repeatedly falling back to the full suite.

## Problem

The current `mcp/tests/` layout is mostly flat. The suite has grown broad enough that:

- test ownership is hard to infer from file paths alone
- agents have trouble selecting the smallest relevant test set
- developers rerun broad slices repeatedly to discover where behavior actually lives
- cross-domain files are not clearly distinguished from domain-focused files

The main pain is not the absence of a faster subset. The problem is structural discoverability.

## Goals

- Split the test suite into stable domain folders with maintainable boundaries.
- Make file paths communicate test ownership.
- Add a concise test index that helps humans and agents choose likely relevant tests.
- Preserve trust in the full suite while improving selective test execution.

## Non-Goals

- Do not introduce a new `fast` or partial confidence lane.
- Do not redesign CI around multiple mandatory test phases in this change.
- Do not mechanically rename every file if its ownership is unclear.
- Do not force mixed test files into a single domain without first splitting them if needed.

## Proposed Folder Structure

Reorganize `mcp/tests/` around technical boundaries:

- `mcp/tests/host/`
  - host integrations, hooks, plugin contracts, host-facing runtime state
- `mcp/tests/runtime/`
  - runtime services, session lifecycle, managed mode, orchestration
- `mcp/tests/planning/`
  - plans, conductor, execution loop, verification gate, subagent/task flow
- `mcp/tests/security/`
  - file access guardrails, query gate, intent guard, scope-based access behavior
- `mcp/tests/indexing/`
  - code sync, analysis, outlines, descriptors, modules, freshness/index behavior
- `mcp/tests/config/`
  - config schema, config resolution, config migration, provider compatibility
- `mcp/tests/project/`
  - project initialization, status, related project services, roadmap/project wiring

## Placement Rules

The destination for each file should be based on its primary contract, not every subsystem it touches.

Examples:

- `test_host_integration.py` -> `host/`
- `test_claude_hook.py` -> `host/`
- `test_runtime_service.py` -> `runtime/`
- `test_plan_tools.py` -> `planning/`
- `test_execution_loop.py` -> `planning/`
- `test_file_ops.py` -> `security/`
- `test_query_gate_ux.py` -> `security/`
- `test_code_analysis.py` -> `indexing/`
- `test_code_sync.py` -> `indexing/`
- `test_config_schema.py` -> `config/`
- `test_project_status_service.py` -> `project/`

If a file is too mixed to place confidently, split it into smaller files before moving it.

## Test Index

Add `mcp/tests/INDEX.md` as a short operational guide.

It should include only the following:

1. Folder map
   - one short description per domain folder
2. Non-obvious file notes
   - exceptions where a filename or behavior could be misread
3. Change-to-test guidance
   - short mapping such as:
     - plugin/hook changes -> `host/`
     - runtime startup/session changes -> `runtime/`
     - plan/conductor/execution changes -> `planning/`
     - query gate/file guardrail changes -> `security/`
     - code index/analysis changes -> `indexing/`
4. Broad cross-domain tests
   - explicitly identify tests that intentionally cross boundaries

The index should stay concise and operational. It is not a prose testing guide.

## Migration Strategy

Reorganize incrementally instead of moving everything in one blind pass.

Suggested order:

1. Create destination folders and the index.
2. Move the most obvious files first.
3. Update imports, fixtures, and any path-sensitive assumptions.
4. Run affected test slices after each move cluster.
5. Split mixed files only when their ownership is unclear.
6. Run the full suite after the reorganization is complete.

## Verification

The reorganization is successful when:

- all moved tests still collect normally under pytest
- targeted domain runs work from the new paths
- the full suite still passes
- a human or agent can infer likely test location from the changed subsystem and the index

## Risks

- large rename batches can obscure real changes in review
- mixed test files may be shoved into the wrong folder to satisfy the structure too quickly
- fragile imports or path assumptions may break after moves

These risks are reduced by moving obvious files first, splitting mixed files when needed, and validating collection and execution throughout the migration.

## Recommendation

Adopt domain folders centered on `host`, `runtime`, `planning`, and `security`, with supporting domains for `indexing`, `config`, and `project`. Pair the new structure with a compact `mcp/tests/INDEX.md` so path layout and test discovery guidance reinforce each other.
