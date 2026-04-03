# OpenCode Read Gate Relative Path Design

## Purpose

Fix the OpenCode host plugin so the indexed-read gate applies consistently to in-project paths whether the caller passes them as project-relative paths or absolute in-project paths.

## Problem

`core/plugins/aidocs.js` currently enforces the indexed-read gate only when the requested path string already starts with the absolute project root. This matches absolute in-project paths but skips relative in-project paths like `src/secret.py`.

That behavior diverges from the MCP-side gate model, which stores and checks discovered exact paths as canonical project-relative paths.

## Scope

- Update only the OpenCode plugin path handling for raw read gating.
- Keep MCP query-gate storage and semantics unchanged.
- Add focused host integration test coverage for relative and absolute in-project reads.

## Non-Goals

- Do not change query-gate JSON format.
- Do not add a new secrets-specific read policy.
- Do not refactor plugin and MCP path handling into a cross-runtime shared abstraction in this change.

## Design

### Path classification

When the OpenCode plugin handles `tool.execute.before` for `read`:

1. Read the requested path from `output.args` or `input.args` as it does now.
2. Normalize separators and trim whitespace.
3. Resolve whether the requested path points inside the current project root.
4. If it resolves inside the project root, convert it to a canonical project-relative path.
5. Apply the existing query-gate check using that canonical relative path.
6. If it does not resolve inside the project root, leave it ungated.

### Canonical comparison model

The plugin should compare against `known_exact_paths` and `lane_exact_paths` using project-relative canonical paths, because that is already the MCP contract.

Examples:

- `src/secret.py` -> `src/secret.py`
- `C:/repo/project/src/secret.py` -> `src/secret.py`
- `D:/other/place/file.py` -> outside project, ungated

### Boundary behavior

- Relative in-project path: gated
- Absolute in-project path: gated
- Absolute path outside project: ungated
- Cross-drive path outside project: ungated
- Empty path: unchanged current behavior

## Testing

Use TDD for the fix.

Add or update host integration tests to cover:

1. Relative in-project read is blocked when query-gate access is denied.
2. Relative in-project read is allowed when that exact relative path is granted.
3. Absolute in-project read is blocked when query-gate access is denied.

Targeted verification:

- `pytest mcp/tests/test_host_integration.py -k "opencode_before_hook"`

## Risks

- Incorrect normalization could accidentally gate paths outside the project.
- Overly broad normalization could change existing behavior for non-project reads.

The implementation should therefore only canonicalize paths after confirming they resolve inside the current project root.

## Recommendation

Implement the smallest plugin-only fix. It aligns OpenCode with the existing MCP gate contract, closes the relative-path bypass, and avoids unnecessary broader refactoring.
