/**
 * AIDOCS state resolution — determines project initialization, managed mode, session binding.
 */
const path = require("node:path")
const childProcess = require("node:child_process")

const { fileExists, readJsonIfExists, computeIndexStatus } = require("./session")
const { aidocsMemoryConfigPath } = require("./config")
const { resolvePythonBin, mergePythonPath, resolveAidocsRuntimeSourceRoot, runPythonJson, announceBridgeFault } = require("./python")
const { extractImportedSkillState } = require("./context")
const { readWorkflowActions } = require("./aidocs_sqlite")

// Host state cache
const _cache = new Map()
const HOST_STATE_SUCCESS_TTL_MS = 3000
const HOST_STATE_FAILURE_TTL_MS = 15000

// The HOST axis (the OpenCode window) is part of the key: two windows on one
// project are bound to different sessions, so one window's answer is never
// another's (identity recut #1121).
function _cacheKey(projectRoot, sessionID, promptText, hostSessionID) {
  return JSON.stringify([projectRoot, sessionID || "", promptText || "", hostSessionID || ""])
}

function _failureKey(projectRoot) {
  return JSON.stringify([projectRoot, "__runtime_failure__"])
}

function _readCache(key) {
  const cached = _cache.get(key)
  if (!cached) return null
  if (cached.expiresAt <= Date.now()) { _cache.delete(key); return null }
  return cached.value
}

function _writeCache(key, value, ttlMs) {
  _cache.set(key, { value, expiresAt: Date.now() + ttlMs })
}

// `sessionID` is the AIDOCS session (an optional explicit target);
// `hostSessionID` is the OpenCode window asking. The Python side stamps the
// window as the REQUEST identity, so host_state resolves THIS window's own
// binding -- or none -- and never the project singleton. An absent window is
// stamped as the honest scoped empty, not left to a process fallback.
function runAidocsHostState(projectRoot, sessionID, promptText, hostSessionID = "") {
  const exactKey = _cacheKey(projectRoot, sessionID, promptText, hostSessionID)
  const cached = _readCache(exactKey)
  if (cached !== null) return cached
  if (_readCache(_failureKey(projectRoot)) === false) return null

  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  if (!sourceRoot) return null

  const pythonBin = resolvePythonBin()
  const pythonPath = path.join(sourceRoot, "mcp", "server")
  const templatesRoot = path.join(sourceRoot, "core", ".MEMORY", ".aidocs", "templates")
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "from aidocs_mcp.runtime_service import RuntimeService",
    "from aidocs_mcp.service_hub import AidocsServiceHub",
    "project_root = Path(sys.argv[1])",
    "session_id = sys.argv[2] or None",
    "prompt = sys.argv[3] or None",
    "templates_root = Path(sys.argv[4])",
    "host_session_id = sys.argv[5]",
    "from aidocs_mcp.mcp_server_runtime_helpers import set_request_host_identity",
    "set_request_host_identity(host_session_id, host_kind='opencode')",
    "runtime = RuntimeService(AidocsServiceHub(templates_root=templates_root))",
    "host_state = runtime.host_state(project_root, session_id=session_id, prompt_text=prompt, host_session_id=host_session_id)",
    "print(json.dumps(host_state))",
  ].join(";")

  try {
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, sessionID || "", promptText || "", templatesRoot, hostSessionID || ""],
      {
        encoding: "utf8",
        env: (() => { const e = { ...process.env }; delete e.AIDOCS_PROJECT_ROOT; e.PYTHONPATH = mergePythonPath(process.env.PYTHONPATH, pythonPath); return e })(),
        timeout: 10000,
      },
    )
    if (result.status !== 0 || !result.stdout.trim()) {
      // Never silent: a null here strips managed mode, skills and prompt
      // context from the session, which is indistinguishable from "not an
      // AIDOCS project" unless we say so (2026-08-14).
      announceBridgeFault(pythonBin, result.stderr || result.error || `status=${result.status}`)
      _writeCache(_failureKey(projectRoot), false, HOST_STATE_FAILURE_TTL_MS)
      return null
    }
    const hostState = { source: "runtime_host_state", payload: JSON.parse(result.stdout) }
    _writeCache(exactKey, hostState, HOST_STATE_SUCCESS_TTL_MS)
    _cache.delete(_failureKey(projectRoot))
    return hostState
  } catch (err) {
    announceBridgeFault(pythonBin, err && err.message)
    _writeCache(_failureKey(projectRoot), false, HOST_STATE_FAILURE_TTL_MS)
    return null
  }
}

async function resolveFilesystemAidocsState(projectRoot) {
  const memoryRoot = path.join(projectRoot, ".MEMORY")
  const aidocsRoot = path.join(memoryRoot, ".aidocs")

  if (!(await fileExists(aidocsRoot))) {
    return { initialized: false, bootstrapped: false, managed: false, sessionID: null, startupState: "not_initialized", indexStatus: "missing" }
  }
  // No-scroll seal (2026-06): .MEMORY/INDEX.md is RETIRED. "Bootstrapped" is now
  // signalled by the canonical sqlite index, which bootstrap creates + seeds
  // (durable memory lives there, not in markdown). The .aidocs/ dir above marks
  // "initialized"; the sqlite index marks "bootstrapped".
  if (!(await fileExists(path.join(memoryRoot, ".index", "aidocs.sqlite3")))) {
    return { initialized: true, bootstrapped: false, managed: false, sessionID: null, startupState: "not_bootstrapped", indexStatus: "missing" }
  }

  // PROJECT-LEVEL FACTS ONLY (identity recut #1121). Which session a window
  // is bound to is a per-window answer only the Python runtime can give; this
  // fallback used to read the project SINGLETON (`aidocs_managed WHERE id = 1`)
  // and guess the lone active session, which handed one window another's
  // binding. A window the runtime could not answer for is unbound here.
  const indexStatus = await computeIndexStatus(projectRoot, memoryRoot)
  return { initialized: true, bootstrapped: true, managed: false, sessionID: null, startupState: "unbound", indexStatus }
}

async function resolveAidocsState(projectRoot, promptText = "", hostSessionID = "") {
  // Post-Beat-3 the compiled workflow lives in aidocs.sqlite3; reading
  // the JSON file is a no-op (it's been ingested + deleted by the
  // Python store's init path).
  const workflow = readWorkflowActions(projectRoot)
  const runtimeHostState = runAidocsHostState(projectRoot, null, promptText, hostSessionID)
  const payload = runtimeHostState && runtimeHostState.payload && typeof runtimeHostState.payload === "object"
    ? runtimeHostState.payload : null
  const sessionState = payload && payload.session_state && typeof payload.session_state === "object"
    ? payload.session_state : null

  const needsFallback = !payload || !sessionState
    || typeof sessionState.state !== "string"
    || !(typeof sessionState.session_id === "string" && sessionState.session_id.trim())
    || typeof sessionState.index_status !== "string"
  const fallback = needsFallback ? await resolveFilesystemAidocsState(projectRoot) : null

  const startupState = sessionState && typeof sessionState.state === "string"
    ? sessionState.state : fallback.startupState
  const sessionID = sessionState && typeof sessionState.session_id === "string" && sessionState.session_id.trim()
    ? sessionState.session_id.trim() : fallback.sessionID

  return {
    initialized: payload ? startupState !== "not_initialized" : fallback.initialized,
    bootstrapped: payload ? (startupState !== "not_initialized" && startupState !== "not_bootstrapped") : fallback.bootstrapped,
    managed: payload ? Boolean(sessionState && sessionState.managed) : fallback.managed,
    sessionID,
    sessionSummaries: [],
    startupState,
    indexStatus: sessionState && typeof sessionState.index_status === "string"
      ? sessionState.index_status : fallback.indexStatus,
    workflowActions: Array.isArray(workflow && workflow.actions) ? workflow.actions : [],
    hostState: payload,
    importedSkillState: extractImportedSkillState(payload, "session"),
  }
}

async function resolvePromptHostState(projectRoot, state, promptText, hostSessionID = "") {
  const statePrompt = state && state.hostState && state.hostState.prompt_state && typeof state.hostState.prompt_state.prompt_text === "string"
    ? state.hostState.prompt_state.prompt_text : null
  if (state && state.hostState && statePrompt === promptText) {
    return { source: "runtime_host_state", payload: state.hostState }
  }
  if (!state || !state.managed || !state.sessionID || !promptText.trim()) return null
  const runtimeHostState = runAidocsHostState(projectRoot, state.sessionID, promptText, hostSessionID)
  const payload = runtimeHostState && runtimeHostState.payload && typeof runtimeHostState.payload === "object"
    ? runtimeHostState.payload : null
  const resolvedSessionID = payload && payload.session_state && typeof payload.session_state.session_id === "string"
    ? payload.session_state.session_id : null
  if (resolvedSessionID && resolvedSessionID !== state.sessionID) return null
  return runtimeHostState
}

async function resolvePromptImportedSkillState(projectRoot, state, promptText, hostSessionID = "") {
  const promptHostState = await resolvePromptHostState(projectRoot, state, promptText, hostSessionID)
  return promptHostState && promptHostState.payload
    ? extractImportedSkillState(promptHostState.payload, "prompt")
    : null
}

module.exports = {
  runAidocsHostState,
  resolveFilesystemAidocsState,
  resolveAidocsState,
  resolvePromptHostState,
  resolvePromptImportedSkillState,
}
