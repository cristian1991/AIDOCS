/**
 * Read gate — path normalization, project-relative resolution, grant checking.
 */
const path = require("node:path")
const childProcess = require("node:child_process")

const { resolvePythonBin, mergePythonPath, resolveAidocsRuntimeSourceRoot } = require("./python")
const { readQueryGate } = require("./aidocs_sqlite")

function normalizeGatePath(filePath) {
  if (!filePath || typeof filePath !== "string") return null
  const normalized = filePath.replace(/\\/g, "/").trim()
  return normalized || null
}

function toProjectRelativeGatePath(projectRoot, filePath) {
  const normalized = normalizeGatePath(filePath)
  if (!normalized || !projectRoot) return null

  const rootResolved = path.resolve(projectRoot)
  const candidateResolved = path.isAbsolute(normalized)
    ? path.resolve(normalized)
    : path.resolve(rootResolved, normalized)
  const relativeNormalized = normalizeGatePath(path.relative(rootResolved, candidateResolved))

  if (!relativeNormalized || relativeNormalized === "." || relativeNormalized === ".."
    || relativeNormalized.startsWith("../") || /^[A-Za-z]:\//.test(relativeNormalized)) {
    return null
  }
  return relativeNormalized
}

async function getQueryGateState(projectRoot, sessionID) {
  // Post-Beat-3 gate state lives in aidocs.sqlite3. The helper returns
  // the same shape the legacy JSON had (known_exact_paths, lane_*,
  // user_intent_*, etc.) so downstream grant checks don't change.
  if (!sessionID) return null
  return readQueryGate(projectRoot, sessionID)
}

function hasGrantedReadAccess(gate, filePath) {
  const normalized = normalizeGatePath(filePath)
  if (!normalized) return true
  const known = new Set(Array.isArray(gate && gate.known_exact_paths)
    ? gate.known_exact_paths.map(normalizeGatePath).filter(Boolean) : [])
  const lane = new Set(Array.isArray(gate && gate.lane_exact_paths)
    ? gate.lane_exact_paths.map(normalizeGatePath).filter(Boolean) : [])
  return known.has(normalized) || lane.has(normalized)
}

function summarizeExecutionValue(value, maxLength = 500) {
  if (value === undefined || value === null) return null
  let text = typeof value === "string" ? value : (() => { try { return JSON.stringify(value) } catch { return String(value) } })()
  if (!text) return null
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

function recordNativeToolUse(projectRoot, sessionID, toolName, args, outputValue) {
  if (!projectRoot || !sessionID || !toolName) return false
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  if (!sourceRoot) return false

  const pythonBin = resolvePythonBin()
  const pythonPath = path.join(sourceRoot, "mcp", "server")
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "from aidocs_mcp.execution_index_store import ExecutionIndexStore",
    "project_root = Path(sys.argv[1])",
    "session_id = sys.argv[2]",
    "tool_name = sys.argv[3]",
    "target_entity = sys.argv[4] or None",
    "payload = json.loads(sys.argv[5])",
    "store = ExecutionIndexStore()",
    "store.record_event(project_root, 'native_tool_use', 'opencode_plugin', session_id=session_id, capability_name=tool_name, action_kind='native_tool', target_entity=target_entity, status='success', payload=payload)",
  ].join(";")

  const payload = {
    args: summarizeExecutionValue(args),
    output: summarizeExecutionValue(outputValue),
  }

  try {
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, sessionID, String(toolName), summarizeExecutionValue(args, 200) || "", JSON.stringify(payload)],
      {
        encoding: "utf8",
        env: (() => { const e = { ...process.env }; delete e.AIDOCS_PROJECT_ROOT; e.PYTHONPATH = mergePythonPath(process.env.PYTHONPATH, pythonPath); return e })(),
        timeout: 10000,
      },
    )
    return result.status === 0
  } catch {
    return false
  }
}

// Phoenix 2026-05-09 §VIII deny-path: per-worker host_session_id stamp.
// When this opencode process is a lane worker (AIDOCS_EXPERT_LANE_ID +
// AIDOCS_EXPERT_ID env set), opencode's `ses_...` uuid (passed to
// chat.message hook as sessionID) is the worker's host_session_id —
// the value `lane_resume_dispatcher.resume_worker_on_deny` needs to
// `opencode -s <id>` the right session. The opencode plugin only
// surfaces this id locally; it must be persisted into AIDOCS sqlite
// for the deny-path dispatcher to find. Once per worker process —
// the JS-side flag avoids spawning python on every chat.message.
let _aidocsWorkerHostSessionStamped = false
function stampWorkerHostSessionId(projectRoot, workerId, hostSessionId) {
  if (!projectRoot || !workerId || !hostSessionId) return false
  if (_aidocsWorkerHostSessionStamped) return true
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  if (!sourceRoot) return false
  const pythonBin = resolvePythonBin()
  const pythonPath = path.join(sourceRoot, "mcp", "server")
  const script = [
    "import sys",
    "from pathlib import Path",
    "from aidocs_mcp.session_lane_agents_store import SessionLaneAgentsStore",
    "project_root = Path(sys.argv[1])",
    "worker_id = sys.argv[2]",
    "host_session_id = sys.argv[3]",
    "store = SessionLaneAgentsStore()",
    "store.set_host_session_id(project_root, worker_id, host_session_id)",
    // Stamp the worker's OS pid too. session_lane_agents.pid was never written
    // by ANY path (0/84 rows in the live index), which made reap_crashed's
    // "provably dead pid = crashed now" limb unreachable — every dead worker
    // lingered as 'running' until the 300s heartbeat cutoff. THIS is the only
    // seam that knows the real pid: the spawn paths register the row before
    // launching the CLI and then use blocking subprocess.run, which never
    // exposes a child pid, whereas this code runs INSIDE the worker process.
    // Rides the existing spawnSync — no additional process.
    "store.set_pid(project_root, worker_id, int(sys.argv[4]))",
  ].join(";")
  try {
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, workerId, hostSessionId, String(process.pid)],
      {
        encoding: "utf8",
        env: (() => { const e = { ...process.env }; delete e.AIDOCS_PROJECT_ROOT; e.PYTHONPATH = mergePythonPath(process.env.PYTHONPATH, pythonPath); return e })(),
        timeout: 10000,
      },
    )
    if (result.status === 0) {
      _aidocsWorkerHostSessionStamped = true
      return true
    }
    return false
  } catch {
    return false
  }
}

function maybeStampWorkerHostSessionId(projectRoot, hostSessionId) {
  if (_aidocsWorkerHostSessionStamped) return
  const workerLaneId = (process.env.AIDOCS_EXPERT_LANE_ID || "").trim()
  const workerId = (process.env.AIDOCS_EXPERT_ID || "").trim()
  if (!workerLaneId || !workerId || !hostSessionId) return
  stampWorkerHostSessionId(projectRoot, workerId, hostSessionId)
}

module.exports = {
  normalizeGatePath,
  toProjectRelativeGatePath,
  getQueryGateState,
  hasGrantedReadAccess,
  summarizeExecutionValue,
  recordNativeToolUse,
  maybeStampWorkerHostSessionId,
}
