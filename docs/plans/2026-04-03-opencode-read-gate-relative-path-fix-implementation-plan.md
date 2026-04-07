# OpenCode Read Gate Relative Path Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the OpenCode host plugin enforce the indexed-read gate for both relative and absolute in-project read paths while leaving outside-project paths unchanged.

**Architecture:** Keep the existing MCP query-gate contract unchanged and fix only the OpenCode plugin. Add test coverage first, then introduce a small plugin helper that resolves in-project paths to canonical project-relative form before comparing them against query-gate grants.

**Tech Stack:** Python pytest, Node.js plugin hooks, JavaScript path utilities

---

## File Structure

- Modify: `mcp/tests/test_host_integration.py`
  - Extend host integration coverage for relative denied reads, relative granted reads, and absolute denied in-project reads.
- Modify: `core/plugins/aidocs.js`
  - Add a small helper that converts an incoming read path into a canonical project-relative path only when the target resolves inside `projectRoot`.
  - Reuse existing gate comparison logic with the canonical relative path.

### Task 1: Expand the failing host integration coverage

**Files:**
- Modify: `mcp/tests/test_host_integration.py:151-223`
- Test: `mcp/tests/test_host_integration.py`

- [ ] **Step 1: Write the failing tests**

Add one more denied-path case for an absolute in-project path and one granted-path case that keeps using a relative grant.

```python
def test_opencode_before_hook_blocks_read_without_query_gate_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-04-01-a", status="active")
    _write_managed_mode(project_root, "2026-04-01-a")
    _write_query_gate(
        project_root,
        "2026-04-01-a",
        {
            "allow_read": False,
            "known_exact_paths": [],
            "lane_exact_paths": [],
        },
    )
    absolute_secret = (project_root / "src" / "secret.py").as_posix()
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const hooks = await plugin.AIDOCSPlugin({{ directory: {json.dumps(str(project_root))} }});
  const results = [];
  for (const filePath of ["src/secret.py", {json.dumps(absolute_secret)}]) {{
    try {{
      await hooks["tool.execute.before"](
        {{ tool: "read", sessionID: "s1", args: {{ filePath }} }},
        {{ args: {{ filePath }} }}
      );
      results.push({{ filePath, blocked: false }});
    }} catch (err) {{
      results.push({{ filePath, blocked: true, message: String(err.message || err) }});
    }}
  }}
  console.log(JSON.stringify(results));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    results = _run_node_json(script)

    assert results[0]["blocked"] is True
    assert results[1]["blocked"] is True
    assert "src/secret.py" in results[0]["message"]
    assert "src/secret.py" in results[1]["message"]


def test_opencode_before_hook_allows_read_with_query_gate_grant(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_bootstrapped_project(project_root)
    _write_session(project_root, "2026-04-01-b", status="active")
    _write_managed_mode(project_root, "2026-04-01-b")
    _write_query_gate(
        project_root,
        "2026-04-01-b",
        {
            "allow_read": False,
            "known_exact_paths": ["src/app.py"],
            "lane_exact_paths": [],
        },
    )
    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const hooks = await plugin.AIDOCSPlugin({{ directory: {json.dumps(str(project_root))} }});
  try {{
    await hooks["tool.execute.before"](
      {{ tool: "read", sessionID: "s1", args: {{ filePath: "src/app.py" }} }},
      {{ args: {{ filePath: "src/app.py" }} }}
    );
    console.log(JSON.stringify({{ blocked: false }}));
  }} catch (err) {{
    console.log(JSON.stringify({{ blocked: true, message: String(err.message || err) }}));
  }}
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["blocked"] is False
```

- [ ] **Step 2: Run the tests to verify the new case fails**

Run: `pytest mcp/tests/test_host_integration.py -k "opencode_before_hook"`

Expected:
- The relative denied-read assertion still fails on `src/secret.py`.
- The new absolute denied-read assertion passes or gets close enough to show the split behavior.
- Overall test selection remains red until the plugin fix is applied.

- [ ] **Step 3: Commit the red test state only if explicitly requested**

Do not commit unless the user explicitly asks for it.

### Task 2: Canonicalize in-project read paths in the OpenCode plugin

**Files:**
- Modify: `core/plugins/aidocs.js:463-493`
- Modify: `core/plugins/aidocs.js:1295-1322`
- Test: `mcp/tests/test_host_integration.py`

- [ ] **Step 1: Write the minimal implementation**

Add a helper near `normalizeGatePath()` that resolves whether a candidate path belongs to the project and, if so, returns a canonical project-relative path.

```javascript
function toProjectRelativeGatePath(projectRoot, filePath) {
  const normalized = normalizeGatePath(filePath)
  if (!normalized || !projectRoot) {
    return null
  }

  const rootResolved = path.resolve(projectRoot)
  const candidateResolved = path.isAbsolute(normalized)
    ? path.resolve(normalized)
    : path.resolve(rootResolved, normalized)

  let relativeToRoot
  try {
    relativeToRoot = path.relative(rootResolved, candidateResolved)
  } catch {
    return null
  }

  if (!relativeToRoot) {
    return null
  }

  const relativeNormalized = relativeToRoot.replace(/\\/g, "/").trim()
  if (
    !relativeNormalized ||
    relativeNormalized.startsWith("../") ||
    relativeNormalized === ".." ||
    path.isAbsolute(relativeNormalized)
  ) {
    return null
  }

  return relativeNormalized
}
```

Then update the read gate to use the canonical relative path instead of the raw incoming path.

```javascript
const candidatePath = normalizeGatePath(
  output && output.args && (output.args.filePath || output.args.path)
    ? (output.args.filePath || output.args.path)
    : input && input.args && (input.args.filePath || input.args.path)
      ? (input.args.filePath || input.args.path)
      : ""
)
if (!candidatePath) {
  return
}

const gatePath = toProjectRelativeGatePath(projectRoot, candidatePath)
if (!gatePath) {
  return
}

const gate = await getQueryGateState(projectRoot, state.sessionID)
if (!gate) {
  return
}
if (hasGrantedReadAccess(gate, gatePath)) {
  return
}

throw new Error(`AIDOCS indexed-read gate: "${gatePath}" has not been discovered via code_investigate, code_find, code_trace, or code_bundle. Use AIDOCS indexed tools first before raw Read.`)
```

- [ ] **Step 2: Run the targeted tests to verify green**

Run: `pytest mcp/tests/test_host_integration.py -k "opencode_before_hook"`

Expected:
- Relative denied read passes.
- Relative granted read passes.
- Absolute denied in-project read passes.
- No unrelated host integration tests in that selection fail.

- [ ] **Step 3: Refactor only if needed to keep helper naming and boundary checks clear**

Allowed cleanup:

```javascript
// Keep the helper local to read-gate behavior so the plugin stays small and the MCP contract remains unchanged.
```

Do not broaden the fix into a cross-runtime shared abstraction.

### Task 3: Verify and document the outcome in session state

**Files:**
- Modify: `/.MEMORY/sessions/2026-04-03-archive-consolidation/SESSION.md` via task update tools
- Test: `mcp/tests/test_host_integration.py`

- [ ] **Step 1: Run the exact verification command again**

Run: `pytest mcp/tests/test_host_integration.py -k "opencode_before_hook"`

Expected: all selected tests pass.

- [ ] **Step 2: Update session state with the fix result**

Record:
- plugin now canonicalizes in-project read paths before gate checks
- relative and absolute in-project reads both honor query-gate state
- outside-project reads remain ungated

- [ ] **Step 3: Commit only if explicitly requested**

Do not create a commit unless the user asks for one.

## Self-Review

- Spec coverage: the plan covers relative denied reads, relative granted reads, absolute in-project denied reads, and preserves outside-project behavior.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: the plan uses existing plugin/test names (`normalizeGatePath`, `hasGrantedReadAccess`, `tool.execute.before`) and keeps query-gate comparisons project-relative.
