# AIDOCS Workflow Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a static HTML debug-map of the actual current AIDOCS workflow, from setup through runtime behavior, with explicit decision paths and clear OpenCode vs Claude divergence.

**Architecture:** Build the audit from actual code-path investigation instead of docs or tests. First capture a structured flow inventory from install scripts, runtime entrypoints, and host adapters; then normalize that inventory into a decision-graph source format; finally render a static HTML page that presents the workflow as a traceable debug map with shared, Claude, and OpenCode lanes.

**Tech Stack:** Markdown, JSON or JS data file, static HTML/CSS/JS, Python and JavaScript source tracing, shell verification commands

---

## File Structure

- Create: `docs/inspection/index.html`
  - Main static audit page for the workflow debug map.
- Create: `docs/inspection/flows.json`
  - Structured source-of-truth inventory for traced steps, branches, and lane assignments.
- Create: `docs/inspection/README.md`
  - Short note explaining how the inspection artifacts are organized and regenerated.
- Create: `docs/inspection/sources.md`
  - Evidence ledger listing investigated files, entrypoints, and state artifacts.
- Create: `docs/inspection/gaps.md`
  - Short list of confusing, contradictory, or host-divergent behaviors found during the audit.
- Modify if needed: `docs/inspection/index.html`
  - Iteratively fill and refine the rendered page.

### Task 1: Capture the system evidence inventory and flow extraction template

**Files:**
- Create: `docs/inspection/sources.md`
- Create: `docs/inspection/flows.json`

- [ ] **Step 1: Create the evidence ledger**

Write `docs/inspection/sources.md` with explicit investigation buckets.

```md
# Workflow Inspection Sources

## Install / Setup Sources
- `core/scripts/install-agent-routing.cmd`
- `core/scripts/install-agent-routing.sh`
- `README_INSTALL.md`

## Project Setup / MCP Sources
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/server/aidocs_mcp/managed_mode_service.py`
- `mcp/server/aidocs_mcp/session_store.py`

## Claude Host Sources
- `mcp/server/aidocs_mcp/claude_hook.py`
- `mcp/HOST_INTEGRATION.md`

## OpenCode Host Sources
- `core/plugins/aidocs.js`
- `mcp/HOST_INTEGRATION.md`

## State Files To Trace
- `/.MEMORY/config/aidocs-managed.json`
- `/.MEMORY/.aidocs/index.aidocs`
- `/.MEMORY/INDEX.md`
- `/.MEMORY/sessions/<session-id>/SESSION.md`
```

- [ ] **Step 2: Create the structured flow inventory skeleton**

Write `docs/inspection/flows.json` with top-level sections and an explicit step schema.

```json
{
  "sections": [
    {
      "id": "setup",
      "title": "0. AIDOCS setup",
      "flows": []
    },
    {
      "id": "project-mcp-setup",
      "title": "1. AIDOCS MCP setup in project",
      "flows": []
    },
    {
      "id": "runtime-start",
      "title": "2. AIDOCS runtime start in project",
      "flows": []
    },
    {
      "id": "what-happens-when",
      "title": "3. What happens when",
      "flows": []
    }
  ],
  "step_schema": {
    "id": "string",
    "lane": "shared|claude|opencode",
    "trigger": "string",
    "layer": "installer|host|plugin_hook|mcp_runtime|filesystem_state",
    "entrypoint": "string",
    "inputs": [],
    "reads": [],
    "decision": "string",
    "branches": [],
    "writes": [],
    "result": "string",
    "evidence": []
  }
}
```

- [ ] **Step 3: Verify the source artifacts exist and are readable**

Run:

```powershell
Test-Path "docs/inspection/sources.md"
Test-Path "docs/inspection/flows.json"
```

Expected:
- Both commands return `True`.

### Task 2: Trace and record section 0 and section 1 flows from current code paths

**Files:**
- Modify: `docs/inspection/sources.md`
- Modify: `docs/inspection/flows.json`

- [ ] **Step 1: Trace `0. AIDOCS setup` from install entrypoints**

Investigate and record the actual setup path from:

```text
core/scripts/install-agent-routing.cmd
core/scripts/install-agent-routing.sh
README_INSTALL.md
```

Capture concrete steps such as:
- installer entry command
- global files written
- OpenCode plugin install destination
- Claude hook wiring destination
- command pack installation
- optional MCP install branch

- [ ] **Step 2: Trace `1. AIDOCS MCP setup in project` from runtime/project entrypoints**

Investigate and record the actual project-local setup path from:

```text
mcp/server/aidocs_mcp/runtime_service.py
mcp/server/aidocs_mcp/mcp_server.py
mcp/server/aidocs_mcp/managed_mode_service.py
mcp/server/aidocs_mcp/session_store.py
```

Capture concrete steps such as:
- project init/repair path
- `.mcp.json` setup path
- `/.MEMORY/` creation/repair path
- managed-mode marker creation path
- index/bootstrap side effects

- [ ] **Step 3: Encode sections 0 and 1 into `flows.json`**

Add actual flow nodes and branch conditions using the plan’s step schema.

Example shape:

```json
{
  "id": "setup.install.windows",
  "lane": "shared",
  "trigger": "operator runs Windows installer",
  "layer": "installer",
  "entrypoint": "core/scripts/install-agent-routing.cmd",
  "inputs": ["repo root"],
  "reads": ["installer template files"],
  "decision": "host artifacts present or need refresh",
  "branches": ["refresh global routing", "refresh OpenCode plugin", "refresh Claude hooks"],
  "writes": ["global routing files", "~/.config/opencode/plugins/aidocs.js", "~/.claude/settings.json"],
  "result": "global host wiring refreshed",
  "evidence": ["core/scripts/install-agent-routing.cmd", "README_INSTALL.md"]
}
```

- [ ] **Step 4: Verify the inventory contains sections 0 and 1 content**

Run:

```powershell
python -c "import json, pathlib; data=json.loads(pathlib.Path('docs/inspection/flows.json').read_text(encoding='utf-8')); print([s['title']+':'+str(len(s['flows'])) for s in data['sections'][:2]])"
```

Expected:
- The first two sections report non-zero flow counts.

### Task 3: Trace and record section 2 runtime-start flow

**Files:**
- Modify: `docs/inspection/flows.json`
- Modify: `docs/inspection/sources.md`

- [ ] **Step 1: Trace the `/aidocs` runtime-start path**

Investigate the actual runtime-start path from current entrypoints such as:

```text
mcp/server/aidocs_mcp/runtime_service.py
mcp/server/aidocs_mcp/mcp_server.py
core/.commands/aidocs.md
```

Capture concrete decisions such as:
- bootstrap vs resume
- uninitialized vs initialized project
- no sessions vs one session vs multiple sessions
- managed-mode activation success path
- startup context/report generation

- [ ] **Step 2: Encode section 2 into `flows.json`**

Represent the runtime-start path as a step graph with explicit branch nodes.

- [ ] **Step 3: Verify section 2 is populated**

Run:

```powershell
python -c "import json, pathlib; data=json.loads(pathlib.Path('docs/inspection/flows.json').read_text(encoding='utf-8')); print(data['sections'][2]['title'], len(data['sections'][2]['flows']))"
```

Expected:
- Section 2 reports a non-zero flow count.

### Task 4: Trace and record section 3 trigger flows, including Claude/OpenCode divergence

**Files:**
- Modify: `docs/inspection/flows.json`
- Modify: `docs/inspection/sources.md`
- Create or modify: `docs/inspection/gaps.md`

- [ ] **Step 1: Trace Claude live trigger paths**

Investigate current Claude behavior from:

```text
mcp/server/aidocs_mcp/claude_hook.py
mcp/HOST_INTEGRATION.md
```

Capture flows for:
- normal prompt
- unmanaged prompt
- managed prompt
- explicit target path
- PreToolUse guardrails
- edit/task reminder path

- [ ] **Step 2: Trace OpenCode live trigger paths**

Investigate current OpenCode behavior from:

```text
core/plugins/aidocs.js
mcp/HOST_INTEGRATION.md
```

Capture flows for:
- `chat.message`
- prompt context shaping
- unmanaged gate
- managed-mode prompt context
- `tool.execute.before`
- raw read gate
- `tool.execute.after`
- task-complete reminder path

- [ ] **Step 3: Add host-shared and host-divergent flows to `flows.json`**

Use `lane = shared`, `claude`, or `opencode` explicitly for every step.

- [ ] **Step 4: Record only high-value confusion points in `gaps.md`**

Write brief entries such as:

```md
# Workflow Gaps

- OpenCode normal prompt flow does not currently call runtime prompt classification/routing at hook time; it relies on plugin-local state and prompt context shaping.
- Claude has stronger prompt-time enforcement than OpenCode for managed-mode routing.
```

- [ ] **Step 5: Verify section 3 contains all major trigger families**

Run:

```powershell
python -c "import json, pathlib; data=json.loads(pathlib.Path('docs/inspection/flows.json').read_text(encoding='utf-8')); print(data['sections'][3]['title'], len(data['sections'][3]['flows']))"
```

Expected:
- Section 3 reports a substantial non-zero flow count covering both hosts.

### Task 5: Render the static HTML debug map and perform final inspection verification

**Files:**
- Create: `docs/inspection/index.html`
- Create or modify: `docs/inspection/README.md`

- [ ] **Step 1: Build the HTML inspection page**

Render `docs/inspection/index.html` so it can load or embed the traced flow data and present:

- the four top-level sections
- branch-heavy flow groups
- shared / Claude / OpenCode lanes
- per-step debug fields
- clear decision points
- links or references back to evidence files/entrypoints

Minimal HTML structure example:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>AIDOCS Workflow Inspection</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head>
  <body>
    <main>
      <h1>AIDOCS Workflow Inspection</h1>
      <p>Actual current flow, traced from code paths and runtime state.</p>
      <section id="setup"></section>
      <section id="project-mcp-setup"></section>
      <section id="runtime-start"></section>
      <section id="what-happens-when"></section>
    </main>
  </body>
</html>
```

- [ ] **Step 2: Add a short artifact README**

Create `docs/inspection/README.md` with a short explanation of:
- what `index.html` is
- what `flows.json` is
- what `sources.md` and `gaps.md` are

- [ ] **Step 3: Verify the artifact set is complete**

Run:

```powershell
Test-Path "docs/inspection/index.html"
Test-Path "docs/inspection/flows.json"
Test-Path "docs/inspection/sources.md"
Test-Path "docs/inspection/gaps.md"
Test-Path "docs/inspection/README.md"
```

Expected:
- All commands return `True`.

- [ ] **Step 4: Run a final sanity check over the rendered inventory**

Run:

```powershell
python -c "import json, pathlib; data=json.loads(pathlib.Path('docs/inspection/flows.json').read_text(encoding='utf-8')); print(sum(len(s['flows']) for s in data['sections']))"
```

Expected:
- A positive total flow count, with all four sections populated.

- [ ] **Step 5: Commit only if explicitly requested**

Do not create a commit unless the user asks for one.

## Self-Review

- Spec coverage: the plan captures the four requested sections, the debug-step schema, the decision-graph requirement, the static HTML artifact, and the shared vs Claude vs OpenCode split.
- Placeholder scan: there are no TODO/TBD placeholders; every task names specific files, commands, and expected outputs.
- Type consistency: all artifact names and section ids match the spec (`setup`, `project-mcp-setup`, `runtime-start`, `what-happens-when`; `index.html`, `flows.json`, `sources.md`, `gaps.md`).
