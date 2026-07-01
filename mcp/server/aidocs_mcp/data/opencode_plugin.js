const fs = require("node:fs/promises")
const fsSync = require("node:fs")
const path = require("node:path")
const childProcess = require("node:child_process")

const GUARDED_TOOLS = new Set(["read", "edit", "write", "glob", "grep", "bash", "task", "multiedit", "patch", "apply_patch", "search", "listdir", "notebookedit", "webfetch"])
const COMMANDS_DIR = path.join(__dirname, "..", ".commands")

let _cachedActionTokens = null
let _pluginConfig = null
let _pluginConfigHasProject = false
let _runtimeHostStateCache = new Map()

const HOST_STATE_SUCCESS_TTL_MS = 3000
const HOST_STATE_FAILURE_TTL_MS = 15000
const MESSAGE_DIRECTIVE_ACTIONS = new Set(["edit", "write_memory", "task_begin", "task_update", "task_complete"])

// Big-boss sqlite reader (Beat 3). Dual-runtime: bun:sqlite in
// production (OpenCode runs under Bun), node:sqlite in dev/tests.
// Both are stdlib, zero install, zero npm deps.
let _aidocsDbFactory = null
function _aidocsLoadSqlite() {
  if (_aidocsDbFactory) return _aidocsDbFactory
  if (typeof Bun !== "undefined") {
    const { Database } = require("bun:sqlite")
    _aidocsDbFactory = (file) => new Database(file, { readonly: true })
    return _aidocsDbFactory
  }
  try {
    const { DatabaseSync } = require("node:sqlite")
    _aidocsDbFactory = (file) => new DatabaseSync(file, { readOnly: true })
    return _aidocsDbFactory
  } catch (err) {
    // Runtime too old — surface a clear remediation; prod runs Bun so
    // dev is the only place this branch fires.
    const e = new Error(
      "AIDOCS plugin requires Bun (production) or Node >= 22.5 (dev). " +
      "node:sqlite is missing on this runtime. Original error: " + err.message
    )
    e.code = "AIDOCS_SQLITE_UNAVAILABLE"
    throw e
  }
}

function _aidocsReadWorkflowActions(projectRoot) {
  const dbPath = path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")
  if (!fsSync.existsSync(dbPath)) return null
  try {
    const makeDb = _aidocsLoadSqlite()
    const db = makeDb(dbPath)
    try {
      const row = db.prepare(
        "SELECT payload FROM workflow_actions WHERE id = 1"
      ).get()
      if (!row) return null
      try { return JSON.parse(row.payload || "{}") } catch { return null }
    } finally {
      db.close()
    }
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

function _aidocsReadQueryGate(projectRoot, sessionId) {
  if (!sessionId) return null
  const dbPath = path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")
  if (!fsSync.existsSync(dbPath)) return null
  try {
    const makeDb = _aidocsLoadSqlite()
    const db = makeDb(dbPath)
    try {
      const row = db.prepare(
        "SELECT last_tool, known_exact_paths, current_lane_id, " +
        "lane_exact_paths, lane_allowed_tools, lane_extra_tools, " +
        "lane_raw_tools_granted, user_intent_tools, " +
        "user_intent_bash_subcommands, turn_edited_files, updated_at " +
        "FROM session_query_gate WHERE session_id = ?"
      ).get(sessionId)
      if (!row) return null
      // Decode JSON-encoded TEXT columns into native JS so downstream
      // grant checks see the same shape the legacy JSON used to give.
      const parseList = (s) => { try { return JSON.parse(s || "[]") } catch { return [] } }
      const parseObj = (s) => { try { return JSON.parse(s || "{}") } catch { return {} } }
      return {
        last_tool: row.last_tool,
        known_exact_paths: parseList(row.known_exact_paths),
        current_lane_id: row.current_lane_id,
        lane_exact_paths: parseList(row.lane_exact_paths),
        lane_allowed_tools: parseList(row.lane_allowed_tools),
        lane_extra_tools: parseList(row.lane_extra_tools),
        lane_raw_tools_granted: parseObj(row.lane_raw_tools_granted),
        user_intent_tools: parseList(row.user_intent_tools),
        user_intent_bash_subcommands: parseList(row.user_intent_bash_subcommands),
        turn_edited_files: parseList(row.turn_edited_files),
        updated_at: row.updated_at,
      }
    } finally {
      db.close()
    }
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

function _aidocsReadManagedMode(projectRoot) {
  const dbPath = path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")
  if (!fsSync.existsSync(dbPath)) return null
  try {
    const makeDb = _aidocsLoadSqlite()
    const db = makeDb(dbPath)
    try {
      const row = db.prepare(
        "SELECT active, session_id, activated_at, last_updated, source " +
        "FROM aidocs_managed WHERE id = 1"
      ).get()
      if (!row) return null
      return {
        active: row.active === 1,
        session_id: row.session_id || null,
        activated_at: row.activated_at || null,
        last_updated: row.last_updated || null,
        source: row.source || null,
      }
    } finally {
      db.close()
    }
  } catch (err) {
    // Table missing (store not yet initialized) means managed mode is
    // off — same outcome as "no row." Don't surface sqlite errors to
    // callers that only care about the simple yes/no question.
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

function _aidocsReadResolvedConfig(projectRoot) {
  const dbPath = path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")
  if (!fsSync.existsSync(dbPath)) return null
  try {
    const makeDb = _aidocsLoadSqlite()
    const db = makeDb(dbPath)
    try {
      const row = db.prepare(
        "SELECT resolved_json, layers_json, active_layers_json, last_updated " +
        "FROM resolved_config WHERE id = 1"
      ).get()
      if (!row) return null
      const safeParse = (s, fb) => { try { return JSON.parse(s || "") } catch { return fb } }
      return {
        resolved: safeParse(row.resolved_json, {}),
        layers: safeParse(row.layers_json, {}),
        active_layers: safeParse(row.active_layers_json, []),
        last_updated: row.last_updated || null,
      }
    } finally {
      db.close()
    }
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

function pluginDebugLog(projectRoot, message, extra = null) {
  try {
    const root = projectRoot || process.env.AIDOCS_PATH || process.cwd()
    const runtimeDir = path.join(root, ".MEMORY", ".runtime")
    fsSync.mkdirSync(runtimeDir, { recursive: true })
    const logPath = path.join(runtimeDir, "opencode-plugin-debug.log")
    const payload = extra ? ` ${JSON.stringify(extra)}` : ""
    fsSync.appendFileSync(logPath, `${new Date().toISOString()} ${message}${payload}\n`)
  } catch {
    // ignore logging failures
  }
}

function aidocsMemoryConfigPath(projectRoot, fileName) {
  return path.join(projectRoot, ".MEMORY", "config", fileName)
}

function loadResolvedConfig(projectRoot) {
  if (!projectRoot) return null
  // Migrated 2026-04-20 from .MEMORY/config/resolved-config.json —
  // the merged config snapshot now lives in aidocs.sqlite3.resolved_config.
  // Sqlite is already open for managed-mode + query-gate reads, so one
  // file per event covers everything and dashboard/TOML edits surface
  // without a JSON cache-rewrite dance.
  try {
    const snapshot = _aidocsReadResolvedConfig(projectRoot)
    if (snapshot && snapshot.resolved) {
      return snapshot.resolved
    }
  } catch {
    // sqlite not yet initialized or locked — fall through to null.
  }
  return null
}

function loadPluginConfig(projectRoot) {
  // Re-resolve when a projectRoot becomes available after an earlier no-project call,
  // because resolved-config.json lives under the project and wasn't reachable before.
  if (_pluginConfig && (!projectRoot || _pluginConfigHasProject)) {
    return _pluginConfig
  }
  const defaults = {
    inject_message_directives: true,
    directive_style: "short",
    disregard_compaction: false,
    startup_context_once: true,
  }

  const resolved = loadResolvedConfig(projectRoot)
  if (resolved) {
    const agent = resolved.agent || {}
    _pluginConfig = {
      ...defaults,
      inject_message_directives: agent.inject_message_directives ?? defaults.inject_message_directives,
      directive_style: agent.directive_style ?? defaults.directive_style,
    }
    _pluginConfigHasProject = !!projectRoot
    return _pluginConfig
  }

  const candidates = [
    path.join(__dirname, "aidocs-plugin.json"),
    path.join(__dirname, "..", "..", "aidocs-plugin.json"),
  ]
  const aidocsPath = process.env.AIDOCS_PATH
  if (aidocsPath) {
    candidates.push(path.join(aidocsPath, "aidocs-plugin.json"))
  }
  for (const candidate of candidates) {
    try {
      if (fsSync.existsSync(candidate)) {
        const raw = JSON.parse(fsSync.readFileSync(candidate, "utf8"))
        _pluginConfig = { ...defaults, ...raw }
        _pluginConfigHasProject = !!projectRoot
        return _pluginConfig
      }
    } catch {
      // ignore
    }
  }
  _pluginConfig = defaults
  _pluginConfigHasProject = !!projectRoot
  return _pluginConfig
}

function readAidocsSourceRoot() {
  const agentsPath = path.join(__dirname, "..", "AGENTS.md")
  try {
    const text = fsSync.readFileSync(agentsPath, "utf8")
    const match = text.match(/^AIDOCS source:\s*(.+)$/m)
    return match ? match[1].trim() : ""
  } catch {
    return ""
  }
}

function resolveAidocsRuntimeSourceRoot() {
  const candidates = []
  const aidocsPath = process.env.AIDOCS_PATH
  if (aidocsPath) {
    candidates.push(aidocsPath)
  }
  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) {
    candidates.push(sourceRoot)
  }
  candidates.push(path.resolve(__dirname, "..", ".."))
  for (const candidate of candidates) {
    try {
      const runtimePath = path.join(candidate, "mcp", "server", "aidocs_mcp", "runtime_service.py")
      if (fsSync.existsSync(runtimePath)) {
        return candidate
      }
    } catch {
      // ignore
    }
  }
  return ""
}

function runPythonJson(projectRoot, args) {
  const pythonBin = resolvePythonBin()
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  const pythonPath = process.env.AIDOCS_MCP_PATH || (sourceRoot ? path.join(sourceRoot, "mcp", "server") : "")
  try {
    const result = childProcess.spawnSync(pythonBin, args, {
      encoding: "utf8",
      cwd: String(projectRoot),
      env: {
        ...process.env,
        PYTHONPATH: mergePythonPath(process.env.PYTHONPATH, pythonPath),
      },
      timeout: 5000,
    })
    if (result.status === 0 && result.stdout) {
      return JSON.parse(result.stdout.trim())
    }
  } catch {
    // Python call failed — return null
  }
  return null
}

function resolvePythonBin() {
  return process.env.AIDOCS_PYTHON || process.env.PYTHON || "python"
}

// Host-agnostic services bridge. The Python CLI in
// aidocs_mcp.host_adapter_cli accepts JSON on stdin and returns
// JSON on stdout. This single helper replaces every inline gate
// re-implementation in this plugin — pretool, posttool, prompt,
// session_start, compact each route through one Python service call.
// Cross-host parity contract: Claude Code, OpenCode, OpenAI Agents,
// future Codex adapters all consume the SAME service surface.
//
// Failure policy: security-relevant events (pretool) fail CLOSED —
// if the CLI is unreachable, errors out, or returns no parseable
// stdout, the helper returns verdict="deny". Non-security events
// fail open with verdict="continue". This mirrors the Python-side
// FAIL_CLOSED_EVENTS doctrine and matches /goal §"fail closed where
// security decisions are undecided."

// Typed event-kind constants. Adapters MUST reference HostEvent.*
// rather than pass raw strings, so a typo can't silently downgrade
// a pretool gate to a continue-on-unknown response on the CLI side.
// Mirror of EVENT_* constants in aidocs_mcp.host_adapter_cli.
const HostEvent = Object.freeze({
  PRETOOL: "pretool",
  POSTTOOL: "posttool",
  PROMPT_MUTATE: "prompt_mutate",
  SESSION_START: "session_start",
  COMPACT: "compact",
  OC_CHAT_MESSAGE: "oc_chat_message",
  OC_MESSAGE_TRANSFORM: "oc_message_transform",
})

const FAIL_CLOSED_EVENTS = new Set([HostEvent.PRETOOL])

function callHostAdapterService(projectRoot, eventKind, payload) {
  const failClosed = FAIL_CLOSED_EVENTS.has(String(eventKind))
  const fallbackVerdict = failClosed ? "deny" : "continue"
  const unreachableEnvelope = {
    verdict: fallbackVerdict,
    reason: "host_adapter_cli_unreachable",
    error: "host_adapter_cli_unreachable",
  }
  const pythonBin = resolvePythonBin()
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  const pythonPath = process.env.AIDOCS_MCP_PATH || (sourceRoot ? path.join(sourceRoot, "mcp", "server") : "")
  try {
    const result = childProcess.spawnSync(
      pythonBin,
      ["-m", "aidocs_mcp.host_adapter_cli", String(eventKind)],
      {
        encoding: "utf8",
        cwd: String(projectRoot),
        input: JSON.stringify(payload || {}),
        env: {
          ...process.env,
          PYTHONPATH: mergePythonPath(process.env.PYTHONPATH, pythonPath),
        },
        timeout: 10000,
      },
    )
    // Try to parse stdout regardless of status code — the CLI emits
    // JSON on every code path including errors. Status 0 with parseable
    // JSON is the happy path; status non-zero with parseable JSON gives
    // us the CLI's own fail-closed/open verdict.
    if (result.stdout && result.stdout.trim()) {
      try {
        return JSON.parse(result.stdout.trim())
      } catch {
        // stdout was not JSON — fall through to the unreachable envelope
      }
    }
  } catch {
    // Python call failed entirely (timeout, spawn error, etc.)
  }
  return unreachableEnvelope
}

function mergePythonPath(existing, extraPath) {
  if (!extraPath) {
    return existing || ""
  }
  if (!existing) {
    return extraPath
  }
  return `${extraPath}${path.delimiter}${existing}`
}

function summarizeExecutionValue(value, maxLength = 500) {
  if (value === undefined || value === null) {
    return null
  }
  let text = null
  if (typeof value === "string") {
    text = value
  } else {
    try {
      text = JSON.stringify(value)
    } catch {
      text = String(value)
    }
  }
  if (!text) {
    return null
  }
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

function recordNativeToolUse(projectRoot, sessionID, toolName, args, outputValue) {
  if (!projectRoot || !sessionID || !toolName) {
    return false
  }
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  if (!sourceRoot) {
    return false
  }
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
        env: {
          ...process.env,
          PYTHONPATH: mergePythonPath(process.env.PYTHONPATH, pythonPath),
        },
        timeout: 10000,
      }
    )
    return result.status === 0
  } catch {
    return false
  }
}

// Phoenix 2026-05-09 §VIII deny-path: per-worker host_session_id stamp.
// When this opencode process is a lane worker (AIDOCS_EXPERT_LANE_ID +
// AIDOCS_EXPERT_ID env set), its real host session uuid (the `ses_...`
// value opencode generates and surfaces via chat.message hooks) needs
// to be written into AIDOCS sqlite so the deny-path dispatcher can
// `opencode -s <id>` the correct session. Once per worker process —
// the JS-side flag avoids spawning python on every chat.message fire.
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
    "SessionLaneAgentsStore().set_host_session_id(project_root, worker_id, host_session_id)",
  ].join(";")
  try {
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, workerId, hostSessionId],
      {
        encoding: "utf8",
        env: {
          ...process.env,
          PYTHONPATH: mergePythonPath(process.env.PYTHONPATH, pythonPath),
        },
        timeout: 10000,
      }
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

function hostStateCacheKey(projectRoot, sessionID, promptText) {
  return JSON.stringify([projectRoot, sessionID || "", promptText || ""])
}

function hostStateFailureKey(projectRoot) {
  return JSON.stringify([projectRoot, "__runtime_failure__"])
}

function readFreshHostStateCache(key) {
  const cached = _runtimeHostStateCache.get(key)
  if (!cached) {
    return null
  }
  if (cached.expiresAt <= Date.now()) {
    _runtimeHostStateCache.delete(key)
    return null
  }
  return cached.value
}

function writeHostStateCache(key, value, ttlMs) {
  _runtimeHostStateCache.set(key, {
    value,
    expiresAt: Date.now() + ttlMs,
  })
}

function runAidocsHostState(projectRoot, sessionID, promptText) {
  const exactCacheKey = hostStateCacheKey(projectRoot, sessionID, promptText)
  const cached = readFreshHostStateCache(exactCacheKey)
  if (cached !== null) {
    return cached
  }
  if (readFreshHostStateCache(hostStateFailureKey(projectRoot)) === false) {
    return null
  }
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  if (!sourceRoot) {
    return null
  }
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
    "runtime = RuntimeService(AidocsServiceHub(templates_root=templates_root))",
    "host_state = runtime.host_state(project_root, session_id=session_id, prompt_text=prompt)",
    "print(json.dumps(host_state))",
  ].join(";")
  try {
    // Defense-in-depth (Phoenix 2026-05-12): strip AIDOCS_PROJECT_ROOT
    // from spawn env. Operator's shell may export it pointing at an
    // AIDOCS install; without this strip, Python's discover_project_root
    // prefers the env var over the explicit projectRoot arg and routes
    // the wrong project to AIDOCS state.
    const childEnv = { ...process.env }
    delete childEnv.AIDOCS_PROJECT_ROOT
    childEnv.PYTHONPATH = mergePythonPath(process.env.PYTHONPATH, pythonPath)
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, sessionID || "", promptText || "", templatesRoot],
      {
        encoding: "utf8",
        env: childEnv,
        timeout: 10000,
      },
      )
      if (result.status !== 0 || !result.stdout.trim()) {
        writeHostStateCache(hostStateFailureKey(projectRoot), false, HOST_STATE_FAILURE_TTL_MS)
        return null
      }
      const hostState = {
        source: "runtime_host_state",
        payload: JSON.parse(result.stdout),
      }
      writeHostStateCache(exactCacheKey, hostState, HOST_STATE_SUCCESS_TTL_MS)
      _runtimeHostStateCache.delete(hostStateFailureKey(projectRoot))
      return hostState
    } catch {
      writeHostStateCache(hostStateFailureKey(projectRoot), false, HOST_STATE_FAILURE_TTL_MS)
      return null
    }
  }

async function resolveFilesystemAidocsState(projectRoot) {
  const memoryRoot = path.join(projectRoot, ".MEMORY")
  const aidocsRoot = path.join(memoryRoot, ".aidocs")
  if (!(await fileExists(aidocsRoot))) {
    return {
      initialized: false,
      bootstrapped: false,
      managed: false,
      sessionID: null,
      startupState: "not_initialized",
      indexStatus: "missing",
    }
  }

  if (!(await fileExists(path.join(memoryRoot, "INDEX.md")))) {
    return {
      initialized: true,
      bootstrapped: false,
      managed: false,
      sessionID: null,
      startupState: "not_bootstrapped",
      indexStatus: "missing",
    }
  }

  // Managed-mode state lives in aidocs.sqlite3 post-Beat-3. The legacy
  // JSON path is ingested + deleted by the Python store's init step,
  // so the plugin reads from sqlite directly — fastest possible path
  // for a question every prompt asks.
  const managedConfig = _aidocsReadManagedMode(projectRoot)
  const managedSessionID = managedConfig && managedConfig.active && typeof managedConfig.session_id === "string" && managedConfig.session_id.trim()
    ? managedConfig.session_id.trim()
    : null
  const sessionSummaries = await listSessionSummaries(projectRoot)
  const activeSessions = sessionSummaries.filter((session) => String(session.status || "").toLowerCase() === "active")
  const plausibleSessions = activeSessions.length ? activeSessions : sessionSummaries
  const indexStatus = await computeIndexStatus(projectRoot, memoryRoot)
  let startupState = "ready"
  if (!managedSessionID && !plausibleSessions.length) {
    startupState = "no_session"
  } else if (!managedSessionID && plausibleSessions.length > 1) {
    startupState = "multiple_sessions"
  } else if (indexStatus === "stale") {
    startupState = "stale_indexes"
  }

  const sessionID = managedSessionID || (plausibleSessions.length === 1 ? plausibleSessions[0].session_id : null)
  return {
    initialized: true,
    bootstrapped: true,
    managed: Boolean(managedSessionID),
    sessionID,
    startupState,
    indexStatus,
  }
}

function resolveActionTokensDir() {
  const candidates = [
    // Colocated with plugin (installed by installer next to aidocs.js)
    path.join(__dirname, "intent_tokens"),
    // Dev: project root intent_tokens/
    path.join(__dirname, "..", "..", "intent_tokens"),
  ]

  // From AIDOCS_PATH env var (set by installer)
  const aidocsPath = process.env.AIDOCS_PATH
  if (aidocsPath) {
    candidates.push(path.join(aidocsPath, "intent_tokens"))
  }

  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) {
    candidates.push(path.join(sourceRoot, "..", "intent_tokens"))
    // Legacy: inside MCP package
    candidates.push(path.join(sourceRoot, "mcp", "server", "aidocs_mcp", "intent_tokens"))
  }

  for (const candidate of candidates) {
    try {
      if (fsSync.existsSync(candidate)) {
        return candidate
      }
    } catch {
      // ignore
    }
  }

  return ""
}

function loadActionTokens() {
  if (_cachedActionTokens) {
    return _cachedActionTokens
  }
  const actionTokensDir = resolveActionTokensDir()
  if (!actionTokensDir) {
    _cachedActionTokens = []
    return _cachedActionTokens
  }
  const merged = new Map()
  // Filter by configured languages
  const config = loadPluginConfig()
  const langEnabled = (config.languages_enabled || "all").toLowerCase().trim()
  const enabledSet = langEnabled === "all" ? null : new Set(langEnabled.split(",").map((s) => s.trim()).filter(Boolean))

  let files
  try {
    files = fsSync.readdirSync(actionTokensDir).filter((f) => {
      if (!f.endsWith(".toml") && !f.endsWith(".yaml")) return false
      const stem = f.replace(/\.(toml|yaml)$/, "")
      if (enabledSet && !enabledSet.has(stem)) return false
      if (stem.startsWith("__") || stem === "opencode") return false
      return true
    }).sort()
  } catch {
    _cachedActionTokens = []
    return _cachedActionTokens
  }
  for (const file of files) {
    const content = fsSync.readFileSync(path.join(actionTokensDir, file), "utf8")
    if (file.endsWith(".toml")) {
      for (const raw of content.split(/\r?\n/)) {
        const line = raw.trimEnd()
        if (!line || line.trimStart().startsWith("#")) continue
        const inlineMatch = line.match(/^(\w[\w_]*)\s*=\s*\[(.+)\]\s*$/)
        if (inlineMatch) {
          const key = inlineMatch[1]
          if (key.startsWith("__")) continue
          const tokens = inlineMatch[2].match(/"([^"]+)"/g)
          if (tokens) {
            if (!merged.has(key)) merged.set(key, new Set())
            for (const t of tokens) merged.get(key).add(t.replace(/"/g, ""))
          }
        }
      }
    } else {
      let currentKey = null
      for (const raw of content.split(/\r?\n/)) {
        const line = raw.trimEnd()
        if (!line || line.trimStart().startsWith("#")) continue
        const keyMatch = line.match(/^(\w[\w_]*):\s*$/)
        if (keyMatch) { currentKey = keyMatch[1]; continue }
        const itemMatch = line.match(/^\s+-\s+(.+)$/)
        if (itemMatch && currentKey) {
          const token = itemMatch[1].trim()
          if (token) {
            if (!merged.has(currentKey)) merged.set(currentKey, new Set())
            merged.get(currentKey).add(token)
          }
        }
      }
    }
  }
  _cachedActionTokens = Array.from(merged.entries()).map(([kind, tokens]) => [kind, Array.from(tokens)])
  return _cachedActionTokens
}

function classifyPromptAction(text, projectRoot) {
  const lower = text.trim().toLowerCase()
  // Fast prefix checks (no token loading needed)
  if (/^(investigate|debug|diagnose|dig into)\b/.test(lower)) {
    return { action_kind: "investigate", why: "prefix:investigate" }
  }
  if (/^(inspect|examine|audit)\b/.test(lower)) {
    return { action_kind: "inspect", why: "prefix:inspect" }
  }
  // Try local tokens first (fast, works when intent_tokens/ is accessible)
  const mapping = loadActionTokens()
  if (mapping.length > 0) {
    for (const [actionKind, tokens] of mapping) {
      if (tokens.some((token) => lower.includes(token))) {
        return { action_kind: actionKind, why: `matched:${actionKind}` }
      }
    }
    return { action_kind: "understand", why: "default:understand" }
  }
  // Fallback: delegate to Python MCP runtime (works with pip-installed package)
  const result = runPythonJson(projectRoot || process.cwd(), [
    "-c",
    `import json; from aidocs_mcp.intent_guard import classify_action; print(json.dumps(classify_action(${JSON.stringify(text)})))`,
  ])
  if (result && result.action_kind) {
    return { action_kind: result.action_kind, why: `mcp:${result.action_kind}` }
  }
  return { action_kind: "understand", why: "default:understand" }
}

async function fileExists(target) {
  try {
    await fs.access(target)
    return true
  } catch {
    return false
  }
}

async function readJsonIfExists(target) {
  if (!(await fileExists(target))) {
    return null
  }
  return JSON.parse(await fs.readFile(target, "utf8"))
}

function normalizeGatePath(filePath) {
  if (!filePath || typeof filePath !== "string") {
    return null
  }
  const normalized = filePath.replace(/\\/g, "/").trim()
  return normalized || null
}

function toProjectRelativeGatePath(projectRoot, filePath) {
  const normalized = normalizeGatePath(filePath)
  if (!normalized || !projectRoot) {
    return null
  }

  const rootResolved = path.resolve(projectRoot)
  const candidateResolved = path.isAbsolute(normalized)
    ? path.resolve(normalized)
    : path.resolve(rootResolved, normalized)
  const relativeToRoot = path.relative(rootResolved, candidateResolved)
  const relativeNormalized = normalizeGatePath(relativeToRoot)

  if (
    !relativeNormalized ||
    relativeNormalized === "." ||
    relativeNormalized === ".." ||
    relativeNormalized.startsWith("../") ||
    /^[A-Za-z]:\//.test(relativeNormalized)
  ) {
    return null
  }

  return relativeNormalized
}

async function getQueryGateState(projectRoot, sessionID) {
  if (!sessionID) {
    return null
  }
  // Post-Beat-3 gate state lives in aidocs.sqlite3. Shape matches the
  // legacy JSON so hasGrantedReadAccess / other grant checks below
  // keep reading gate.known_exact_paths, gate.lane_exact_paths, etc.
  return _aidocsReadQueryGate(projectRoot, sessionID)
}

function hasGrantedReadAccess(gate, filePath) {
  const normalized = normalizeGatePath(filePath)
  if (!normalized) {
    return true
  }
    // allow_read removed — per-file discovery only via known_exact_paths
  const known = new Set(Array.isArray(gate && gate.known_exact_paths)
    ? gate.known_exact_paths.map(normalizeGatePath).filter(Boolean)
    : [])
  const lane = new Set(Array.isArray(gate && gate.lane_exact_paths)
    ? gate.lane_exact_paths.map(normalizeGatePath).filter(Boolean)
    : [])
  return known.has(normalized) || lane.has(normalized)
}

async function readTextIfExists(target) {
  if (!(await fileExists(target))) {
    return null
  }
  return fs.readFile(target, "utf8")
}

function extractSectionBullet(text, heading) {
  if (typeof text !== "string" || !text.trim()) {
    return ""
  }
  const lines = text.split(/\r?\n/)
  let inSection = false
  for (const raw of lines) {
    const line = String(raw || "")
    if (/^##\s+/.test(line)) {
      if (inSection) {
        break
      }
      inSection = line.trim() === `## ${heading}`
      continue
    }
    if (inSection && /^\s*-\s+/.test(line)) {
      return line.replace(/^\s*-\s+/, "").trim()
    }
  }
  return ""
}

function latestPathMtime(target) {
  if (!target || !fsSync.existsSync(target)) {
    return 0
  }
  const stat = fsSync.statSync(target)
  if (stat.isFile()) {
    return stat.mtimeMs
  }
  let latest = stat.mtimeMs
  for (const entry of fsSync.readdirSync(target, { withFileTypes: true })) {
    latest = Math.max(latest, latestPathMtime(path.join(target, entry.name)))
  }
  return latest
}

async function listSessionSummaries(projectRoot) {
  const sessionRoot = path.join(projectRoot, ".MEMORY", "sessions")
  if (!(await fileExists(sessionRoot))) {
    return []
  }
  const entries = await fs.readdir(sessionRoot, { withFileTypes: true })
  const sessions = []
  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue
    }
    const sessionID = entry.name
    const sessionFile = path.join(sessionRoot, sessionID, "SESSION.md")
    const text = await readTextIfExists(sessionFile)
    if (!text) {
      continue
    }
    sessions.push({
      session_id: sessionID,
      title: extractSectionBullet(text, "Title"),
      status: extractSectionBullet(text, "Status") || "unknown",
      owner: extractSectionBullet(text, "Owner"),
      goal: extractSectionBullet(text, "Goal"),
      last_updated: extractSectionBullet(text, "Last Updated"),
    })
  }
  return sessions.sort((a, b) => String(a.session_id).localeCompare(String(b.session_id)))
}

async function computeIndexStatus(projectRoot, memoryRoot) {
  const indexDb = path.join(memoryRoot, ".index", "aidocs.sqlite3")
  if (!(await fileExists(indexDb))) {
    return "missing"
  }
  const indexMtime = fsSync.statSync(indexDb).mtimeMs
  const latestMemory = Math.max(
    latestPathMtime(path.join(memoryRoot, "INDEX.md")),
    latestPathMtime(path.join(memoryRoot, ".aidocs")),
    latestPathMtime(path.join(memoryRoot, "sessions")),
    latestPathMtime(path.join(memoryRoot, "rules")),
      // Post-Beat-3 managed-mode state lives in aidocs.sqlite3; watch
      // the big-boss DB's mtime for the cache-invalidation signal the
      // legacy JSON used to provide.
      latestPathMtime(path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")),
  )
  let latestProject = 0
  for (const entry of fsSync.readdirSync(projectRoot, { withFileTypes: true })) {
    if ([".git", ".MEMORY", ".pytest_cache", ".venv", "node_modules", "dist", "build", "__pycache__"].includes(entry.name)) {
      continue
    }
    latestProject = Math.max(latestProject, latestPathMtime(path.join(projectRoot, entry.name)))
  }
  if (latestMemory > indexMtime || latestProject > indexMtime) {
    return "stale"
  }
  return "ready"
}

function parseSimpleFrontmatter(text) {
  if (typeof text !== "string" || !text.startsWith("---\n")) {
    return {}
  }
  const end = text.indexOf("\n---\n", 4)
  if (end === -1) {
    return {}
  }
  const frontmatter = text.slice(4, end)
  const result = {}
  for (const rawLine of frontmatter.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith("#")) {
      continue
    }
    const separator = line.indexOf(":")
    if (separator === -1) {
      continue
    }
    const key = line.slice(0, separator).trim()
    let value = line.slice(separator + 1).trim()
    if (value === "true") {
      value = true
    } else if (value === "false") {
      value = false
    }
    result[key] = value
  }
  return result
}

async function readCommandMetadata(commandName) {
  const normalized = normalizeCommandName(commandName)
  if (!normalized) {
    return null
  }
  const commandPath = path.join(COMMANDS_DIR, `${normalized}.md`)
  const text = await readTextIfExists(commandPath)
  if (!text) {
    return null
  }
  const metadata = parseSimpleFrontmatter(text)
  return {
    command_id: normalized,
    ...metadata,
  }
}

async function resolveAidocsState(projectRoot, promptText = "") {
    // Post-Beat-3 the compiled workflow lives in aidocs.sqlite3; reading
    // the JSON file is a no-op (Python store has ingested + deleted it).
    const workflow = _aidocsReadWorkflowActions(projectRoot)
  const runtimeHostState = runAidocsHostState(projectRoot, null, promptText)
  const payload = runtimeHostState && runtimeHostState.payload && typeof runtimeHostState.payload === "object"
    ? runtimeHostState.payload
    : null
  const sessionState = payload && payload.session_state && typeof payload.session_state === "object"
    ? payload.session_state
    : null
  const needsFallbackState = !payload
    || !sessionState
    || typeof sessionState.state !== "string"
    || !(typeof sessionState.session_id === "string" && sessionState.session_id.trim())
    || typeof sessionState.index_status !== "string"
  const fallbackState = needsFallbackState ? await resolveFilesystemAidocsState(projectRoot) : null
  const startupState = sessionState && typeof sessionState.state === "string"
    ? sessionState.state
    : fallbackState.startupState
  const sessionID = sessionState && typeof sessionState.session_id === "string" && sessionState.session_id.trim()
    ? sessionState.session_id.trim()
    : fallbackState.sessionID

  return {
    initialized: payload ? startupState !== "not_initialized" : fallbackState.initialized,
    bootstrapped: payload ? (startupState !== "not_initialized" && startupState !== "not_bootstrapped") : fallbackState.bootstrapped,
    managed: payload ? Boolean(sessionState && sessionState.managed) : fallbackState.managed,
    sessionID,
    sessionSummaries: [],
    startupState,
    indexStatus: sessionState && typeof sessionState.index_status === "string"
      ? sessionState.index_status
      : fallbackState.indexStatus,
    workflowActions: Array.isArray(workflow && workflow.actions) ? workflow.actions : [],
    hostState: payload,
    importedSkillState: extractImportedSkillStateFromHostState(payload, "session"),
  }
}

function extractPromptText(parts) {
  const textParts = []
  for (const part of parts || []) {
    if (!part) {
      continue
    }
    if (typeof part === "string") {
      textParts.push(part)
      continue
    }
    if (typeof part.text === "string") {
      textParts.push(part.text)
      continue
    }
    if (typeof part.value === "string") {
      textParts.push(part.value)
    }
  }
  return textParts.join("\n").trim()
}

function summarizeWorkflowActions(actions) {
  if (!actions.length) {
    return ""
  }
  const rendered = actions.slice(0, 3).map((action) => `\`${action.trigger || "?"} -> ${action.kind || "?"}\``)
  if (actions.length > rendered.length) {
    rendered.push(`and ${actions.length - rendered.length} more`)
  }
  return rendered.join(", ")
}

function extractImportedSkillStateFromHostState(hostState, phase) {
  if (!hostState || typeof hostState !== "object") {
    return null
  }
  const skillState = hostState.skill_state && typeof hostState.skill_state === "object"
    ? hostState.skill_state
    : null
  const sessionSnapshot = skillState && skillState.session_snapshot && typeof skillState.session_snapshot === "object"
    ? skillState.session_snapshot
    : null
  const promptActivation = skillState && skillState.prompt_activation && typeof skillState.prompt_activation === "object"
    ? skillState.prompt_activation
    : null
  const promptState = hostState.prompt_state && typeof hostState.prompt_state === "object"
    ? hostState.prompt_state
    : null
  const sourceState = phase === "prompt" ? promptActivation : sessionSnapshot
  if (!sourceState) {
    return null
  }
  return {
    source: sourceState.source || null,
    session_id: sourceState.session_id || (hostState.session_state && hostState.session_state.session_id) || null,
    intent: phase === "prompt" ? promptState && promptState.intent : null,
    workflow_state: phase === "prompt" ? promptState && promptState.action_kind : null,
    selected_skills: Array.isArray(sessionSnapshot && sessionSnapshot.selected_skills)
      ? sessionSnapshot.selected_skills.filter(Boolean)
      : [],
    active_skills: Array.isArray(sourceState.active_skills)
      ? sourceState.active_skills.filter(Boolean)
      : [],
    runtime_owned_capabilities: Array.isArray(sourceState.runtime_owned_capabilities)
      ? sourceState.runtime_owned_capabilities.filter((item) => item && typeof item === "object")
      : [],
    provider_states: phase === "session" && sessionSnapshot && typeof sessionSnapshot.provider_states === "object"
      ? sessionSnapshot.provider_states
      : {},
    provider_state: phase === "session" && sessionSnapshot
      ? sessionSnapshot.provider_state || null
      : null,
    triggered: Array.isArray(sourceState.triggered) ? sourceState.triggered : [],
    mode_metadata: sourceState.mode_metadata || (promptState && promptState.mode_metadata) || null,
    helper_skill_guidance: Array.isArray(sourceState.helper_skill_guidance)
      ? sourceState.helper_skill_guidance
      : [],
    override_modes: phase === "prompt" && promptState && typeof promptState.override_modes === "object"
      ? promptState.override_modes
      : {},
    activation_succeeded: phase === "prompt"
      ? Boolean(sourceState.activation_succeeded ?? (promptState && promptState.activation_succeeded))
      : undefined,
  }
}

function resolveEffectiveImportedSkillState(state, promptHostState) {
  const promptPayload = promptHostState && promptHostState.source === "runtime_host_state" && promptHostState.payload
    ? promptHostState.payload
    : null
  const promptRuntimeState = extractImportedSkillStateFromHostState(promptPayload, "prompt")
  if (promptRuntimeState) {
    return promptRuntimeState
  }
  const startupRuntimeState = extractImportedSkillStateFromHostState(state && state.hostState, "session")
  if (startupRuntimeState) {
    return startupRuntimeState
  }
  return state && state.importedSkillState ? state.importedSkillState : null
}

function renderHelperSkillGuidance(importedSkillState) {
  const guidance = importedSkillState && Array.isArray(importedSkillState.helper_skill_guidance)
    ? importedSkillState.helper_skill_guidance.filter((item) => item && typeof item === "object" && typeof item.content === "string" && item.content.trim())
    : []
  if (!guidance.length) {
    return []
  }
  return guidance.slice(0, 2).map((item) => {
    const name = String(item.name || item.skill_id || "skill").trim()
    const content = String(item.content || "").trim()
    return `<aidocs-skill name="${name}">\n${content}\n</aidocs-skill>`
  })
}

function renderRuntimeOwnedCapabilities(importedSkillState) {
  const capabilities = importedSkillState && Array.isArray(importedSkillState.runtime_owned_capabilities)
    ? importedSkillState.runtime_owned_capabilities.filter((item) => item && typeof item === "object" && typeof item.capability_id === "string" && item.capability_id.trim())
    : []
  if (!capabilities.length) {
    return ""
  }
  return `Runtime-owned workflow capabilities: ${capabilities.map((item) => `\`${String(item.capability_id).trim()}\``).join(", ")}.`
}

function resolveImportedSkillStateForContext(state, promptHostState, promptStateWasProvided) {
  if (promptStateWasProvided) {
    if (!promptHostState) {
      return null
    }
    if (promptHostState.source && promptHostState.source !== "runtime_host_state") {
      return promptHostState
    }
    return resolveEffectiveImportedSkillState(state, promptHostState)
  }
  return resolveEffectiveImportedSkillState(state, promptHostState)
}

async function resolvePromptImportedSkillState(projectRoot, state, promptText) {
  const promptHostState = await resolvePromptHostState(projectRoot, state, promptText)
  return promptHostState && promptHostState.payload
    ? extractImportedSkillStateFromHostState(promptHostState.payload, "prompt")
    : null
}

async function resolvePromptHostState(projectRoot, state, promptText) {
  const statePrompt = state && state.hostState && state.hostState.prompt_state && typeof state.hostState.prompt_state.prompt_text === "string"
    ? state.hostState.prompt_state.prompt_text
    : null
  if (state && state.hostState && statePrompt === promptText) {
    return {
      source: "runtime_host_state",
      payload: state.hostState,
    }
  }
  if (!state || !state.managed || !state.sessionID || !promptText.trim()) {
    return null
  }
  const runtimeHostState = runAidocsHostState(projectRoot, state.sessionID, promptText)
  const payload = runtimeHostState && runtimeHostState.payload && typeof runtimeHostState.payload === "object"
    ? runtimeHostState.payload
    : null
  const resolvedSessionID = payload && payload.session_state && typeof payload.session_state.session_id === "string"
    ? payload.session_state.session_id
    : null
  if (resolvedSessionID && resolvedSessionID !== state.sessionID) {
    return null
  }
  return runtimeHostState
}

function buildPromptContext(state, promptText, activeCommand, activeCommandMeta, promptHostState) {
  const isAidocsEntry = (activeCommandMeta && activeCommandMeta.command_id === "aidocs") || activeCommand === "aidocs" || promptText.startsWith("/aidocs") || promptText.toLowerCase().trim().replace(/^\//, "").startsWith("aidocs")
  const explicitPromptStateProvided = arguments.length >= 5
  const promptPayload = promptHostState && promptHostState.source === "runtime_host_state" && promptHostState.payload
    ? promptHostState.payload
    : (!explicitPromptStateProvided && state && state.hostState && typeof state.hostState === "object" ? state.hostState : null)
  const interactionText = promptPayload && promptPayload.interaction_text && typeof promptPayload.interaction_text === "object"
    ? promptPayload.interaction_text
    : null
  if (isAidocsEntry) {
    const preamble = "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request or rule to store. Do NOT save this as a memory, preference, or workflow rule."
    if (!state.initialized) {
      return [
        preamble,
        interactionText && typeof interactionText.missing_structure === "string"
          ? interactionText.missing_structure
          : [
              "This project has no AIDOCS structure. Call the `project_init` MCP tool with the project root path",
              "to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates.",
              "After initialization, call `project_bootstrap_or_resume` to activate managed mode.",
            ].join(" "),
      ].join(" ")
    }
    return [
      preamble,
      interactionText && typeof interactionText.execution_prompt === "string"
        ? interactionText.execution_prompt
        : buildAidocsExecutionPrompt(),
    ].join(" ")
  }

  if (interactionText && typeof interactionText.startup_message === "string" && interactionText.startup_message.trim()) {
    return interactionText.startup_message
  }

  if (interactionText && typeof interactionText.unmanaged_message === "string" && interactionText.unmanaged_message.trim()) {
    return interactionText.unmanaged_message
  }

  if (interactionText && typeof interactionText.prompt_context === "string" && interactionText.prompt_context.trim()) {
    const parts = [interactionText.prompt_context]
    if (typeof interactionText.action_directive === "string" && interactionText.action_directive.trim()) {
      parts.push(interactionText.action_directive)
    }
    return parts.join(" ")
  }

  if (state.startupState === "not_initialized") {
    return "This project is not initialized for AIDOCS. Run `/aidocs` first to initialize and bootstrap it before normal work."
  }

  if (state.startupState === "not_bootstrapped") {
    return "AIDOCS project structure is incomplete or not fully bootstrapped. Run `/aidocs` first to repair bootstrap state before normal work."
  }

  if (state.startupState === "no_session") {
    return "AIDOCS is initialized, but no session exists yet. Run `/aidocs` first so the session can be created before normal work."
  }

  if (state.startupState === "multiple_sessions") {
    return "Multiple plausible AIDOCS sessions exist. Ask the user which session to connect to before normal work, then use `/aidocs` to bind managed mode."
  }

  if (state.startupState === "stale_indexes") {
    // Stale indexes are a warning, not a blocker — the agent can still work.
    // Append the warning to normal context instead of replacing it.
  }

  if (!state.managed) {
    return [
      "This is an AIDOCS-ready project, but AIDOCS-managed mode is inactive.",
      "If the user is not invoking `/aidocs`, do not begin project work and do not use core repo tools.",
      "Tell the user to run `/aidocs` first.",
    ].join(" ")
  }
  const lifecycleState = promptPayload && promptPayload.lifecycle_state && typeof promptPayload.lifecycle_state === "object"
    ? promptPayload.lifecycle_state
    : null
  const promptState = promptPayload && promptPayload.prompt_state && typeof promptPayload.prompt_state === "object"
    ? promptPayload.prompt_state
    : null
  const classification = {
    action_kind: (promptState && promptState.action_kind) || classifyPromptAction(promptText).action_kind,
  }
  const prefersStrictMcpPath = MESSAGE_DIRECTIVE_ACTIONS.has(classification.action_kind)
  const workflowSummary = summarizeWorkflowActions(state.workflowActions)
  const parts = [
    "AIDOCS-managed mode is active for this project.",
    state.sessionID ? `Bound session: \`${state.sessionID}\`.` : "",
    state.sessionID ? "Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup." : "",
    `Action: \`${classification.action_kind}\`.`,
    prefersStrictMcpPath
      ? "Prefer AIDOCS MCP tools first for this task. Use raw Read/Grep when the user already gave an explicit target or direct inspection is faster."
      : "Use AIDOCS MCP tools when they materially help, but avoid read/search churn; if the user already gave a direct file, error, or exact target, inspect it directly.",
    state.startupState === "stale_indexes" ? "Note: indexes are stale. Run `/aidocs` when convenient to refresh, but you can continue working." : "",
  ].filter(Boolean)
    const effectiveImportedSkillState = resolveImportedSkillStateForContext(state, promptHostState, explicitPromptStateProvided)
  const importedSkills = effectiveImportedSkillState && Array.isArray(effectiveImportedSkillState.active_skills)
    ? effectiveImportedSkillState.active_skills.filter(Boolean)
    : []
    if (importedSkills.length) {
      parts.push(`Imported skills: ${importedSkills.map((item) => `\`${item}\``).join(", ")}.`)
    }
    const importedSkillModes = effectiveImportedSkillState && effectiveImportedSkillState.mode_metadata && effectiveImportedSkillState.mode_metadata.active_skill_modes
      ? Object.entries(effectiveImportedSkillState.mode_metadata.active_skill_modes)
      : []
    if (importedSkillModes.length) {
      parts.push(`Imported skill modes: ${importedSkillModes.map(([skillID, mode]) => `\`${skillID}=${mode}\``).join(", ")}.`)
    }
    const runtimeOwnedCapabilities = renderRuntimeOwnedCapabilities(effectiveImportedSkillState)
    if (runtimeOwnedCapabilities) {
      parts.push(runtimeOwnedCapabilities)
    }
    const helperSkillBlocks = renderHelperSkillGuidance(effectiveImportedSkillState)
    if (helperSkillBlocks.length) {
      parts.push("Active AIDOCS helper skill guidance:")
      parts.push(...helperSkillBlocks)
    }
    if (workflowSummary) {
      parts.push(`Compiled workflow actions: ${workflowSummary}.`)
    }
    if (lifecycleState && lifecycleState.needs_task_complete) {
      parts.push("Lifecycle follow-through: meaningful edit work happened since the last lifecycle tool call; use `ai_task(mode='complete')` if the task is done.")
    } else if (lifecycleState && lifecycleState.needs_task_update) {
      parts.push("Lifecycle follow-through: meaningful work has accumulated since the last lifecycle tool call; use `ai_task(mode='update')` to record progress.")
    }
  return parts.join(" ")
}

// TEST/BACKCOMPAT-ONLY: this table is no longer consulted by any
// live hook. The canonical source is host_adapter_cli
// ._ACTION_TOOL_DIRECTIVES in Python; messages.transform reads
// the Python table via HostEvent.OC_MESSAGE_TRANSFORM and only
// renders the returned append_parts. The JS copy stays here
// solely so `plugin._test.ACTION_TOOL_DIRECTIVES` (exported below
// for existing tests) keeps resolving. New directives belong in
// the Python table; do NOT add to this copy without also updating
// the canonical Python mirror, or the data sources will diverge.
const ACTION_TOOL_DIRECTIVES = {
  edit: '`task_begin` → `ai_get_lines` (read) → `ai_edit_lines` or `ai_batch_edit` (write) → `task_complete`. Do NOT mix edit methods. Before editing: `ai_get_symbol_info(kind="signature")` or `kind="constructor"` to confirm signatures. CSS: `ai_trace(class, mode="css_class")`. DB: `schema_query(entity, mode="entity")`.',
  trace: '`ai_find(query, mode="references")` → `ai_trace(query, mode="field_flow"|"css_class"|"api_to_ui")`. DB: `schema_query("Source→Target", mode="trace_path")`.',
  understand: '`session_resume_bundle` (project/session/skills/plan overview) → `action_surface_current_session_bundle` (likely next tools) → `ai_find(query, mode="symbols")` → `ai_get_symbol_snippet`. Precision: `ai_get_symbol_info(kind="signature"|"constructor"|"enum"|"api"|"properties")`. Broad: `ai_bundle(concept, mode="subsystem")`. DB: `schema_query(name, mode="entity")`.',
  read_error: '`ai_find(symbol, mode="symbols")` → `ai_find(symbol, mode="references")` → `ai_get_symbol_snippet`. DB: `schema_query(entity, mode="entity")`.',
  investigate: '`session_resume_bundle` (overview) → `action_surface_current_session_bundle` (common path) → `ai_investigate(concept)` for guided navigation. Or: `ai_bundle(concept, mode="subsystem")` → `ai_find(concept, mode="mutations"|"validation"|"policy")`.',
  inspect: '`session_resume_bundle` (overview) → `action_surface_current_session_bundle` (common path) → `ai_get_dependencies` / `ai_find(mode="references")` → `ai_get_modules`. Read only after narrowing.',
}

function getActionDirective(actionKind) {
  return ACTION_TOOL_DIRECTIVES[actionKind] || ""
}

function buildAidocsExecutionPrompt() {
  return [
    "Execute the `/aidocs` entry flow now for the current project.",
    "Use the `aidocs` MCP server first when available.",
    "Call the MCP orchestrator entry flow for this project.",
    "If setup is required, initialize AIDOCS first and then continue bootstrap.",
    "If multiple plausible sessions exist, STOP and ask the user which session to bind.",
    "Keep the default user-facing output terse and prefer the top-level `report` field when present.",
    "Report: setup required or not, index sync status, selected session or session-selection requirement, managed-mode state and bound session, and the first retrieval bundle only when useful.",
    "Do not broad-read the repo before AIDOCS bootstrap and session selection complete unless the user explicitly points at a file or error first.",
  ].join(" ")
}

function takeFirstNonEmpty(lines, limit) {
  const picked = []
  for (const raw of lines) {
    const line = String(raw || "").trim()
    if (!line) continue
    picked.push(line)
    if (picked.length >= limit) break
  }
  return picked
}

async function buildCompactionContext(projectRoot, sessionID) {
  const blocks = []
  const memoryIndex = await readTextIfExists(path.join(projectRoot, ".MEMORY", "INDEX.md"))
  const sessionFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "SESSION.md")) : null
  const planFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "plans", "PLAN.md")) : null
  const handoffFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, `${sessionID}.handoff.md`)) : null
  const journalFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "journal.md")) : null

  const roadmapCandidates = [
    path.join(projectRoot, "ROADMAP_2_0_0.md"),
    path.join(projectRoot, "ROADMAP.md"),
    path.join(projectRoot, "mcp", "ROADMAP.md"),
  ]
  let roadmapText = null
  for (const candidate of roadmapCandidates) {
    roadmapText = await readTextIfExists(candidate)
    if (roadmapText) break
  }

  if (memoryIndex) {
    blocks.push("Memory index:\n" + takeFirstNonEmpty(memoryIndex.split(/\r?\n/), 10).map((line) => `- ${line}`).join("\n"))
  }
  if (roadmapText) {
    blocks.push("Roadmap:\n" + takeFirstNonEmpty(roadmapText.split(/\r?\n/), 10).map((line) => `- ${line}`).join("\n"))
  }
  if (sessionFile) {
    blocks.push("Session:\n" + takeFirstNonEmpty(sessionFile.split(/\r?\n/), 12).map((line) => `- ${line}`).join("\n"))
  }
  if (planFile) {
    blocks.push("Plan:\n" + takeFirstNonEmpty(planFile.split(/\r?\n/), 12).map((line) => `- ${line}`).join("\n"))
  }
  if (handoffFile) {
    blocks.push("Handoff:\n" + takeFirstNonEmpty(handoffFile.split(/\r?\n/), 12).map((line) => `- ${line}`).join("\n"))
  }
  if (journalFile) {
    const journalLines = journalFile.split(/\r?\n/).filter((line) => line.trim().startsWith("- `")).slice(-8)
    if (journalLines.length) {
      blocks.push("Recent journal:\n" + journalLines.map((line) => `- ${line.trim()}`).join("\n"))
    }
  }
  return blocks
}

async function buildSessionStartContext(projectRoot, state) {
  const blocks = []
  const sessionID = state && state.sessionID ? state.sessionID : null
  const sessionFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "SESSION.md")) : null
  const planFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "plans", "PLAN.md")) : null
  const handoffFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, `${sessionID}.handoff.md`)) : null
  const journalFile = sessionID ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sessionID, "journal.md")) : null

  if (sessionFile) {
    blocks.push("Session:\n" + takeFirstNonEmpty(sessionFile.split(/\r?\n/), 10).map((line) => `- ${line}`).join("\n"))
  }
  if (planFile) {
    blocks.push("Plan:\n" + takeFirstNonEmpty(planFile.split(/\r?\n/), 10).map((line) => `- ${line}`).join("\n"))
  }
  if (handoffFile) {
    blocks.push("Handoff:\n" + takeFirstNonEmpty(handoffFile.split(/\r?\n/), 12).map((line) => `- ${line}`).join("\n"))
  }
  if (journalFile) {
    const recent = journalFile.split(/\r?\n/).filter((line) => line.trim().startsWith("- `")).slice(-5)
    if (recent.length) {
      blocks.push("Recent journal:\n" + recent.map((line) => `- ${line.trim()}`).join("\n"))
    }
  }
  return blocks
}

function normalizeCommandName(command) {
  return String(command || "").trim().replace(/^\//, "").toLowerCase()
}


async function AIDOCSPlugin(input) {
  require("fs").writeFileSync(require("path").join(__dirname, "aidocs-plugin-init.log"), 
    new Date().toISOString() + " plugin init called\n", 
    { flag: "a" });
  const projectRoot = input.worktree || input.directory
  // Gate (Phoenix 2026-05-12, king bug report): activate ONLY when the
  // opencode session is rooted in an AIDOCS-managed project, OR when
  // launched as an AIDOCS-spawned Expert (env-driven). Without this
  // gate, opencode in a non-AIDOCS project would inherit
  // AIDOCS_PROJECT_ROOT from the operator's shell, the plugin would
  // spawn Python host_state which falls back to the env var, and the
  // dental project's session ends up routed to AIDOCS's phoenix state.
  // Marker->sqlite migration: cheap PRE-GATE on the index db file existing.
  // The Python side this plugin spawns is AUTHORITATIVE (it requires the
  // deliberate commission STAMP inside the db) — a rare incidental-db
  // false-positive here just costs one probe that reports unmanaged.
  const aidocsMarker = path.join(projectRoot || ".", ".MEMORY", ".index", "aidocs.sqlite3")
  const isAidocsProject = !!projectRoot && fsSync.existsSync(aidocsMarker)
  const isAidocsExpert = !!(process.env.AIDOCS_EXPERT_LANE_ID || "").trim()
  if (!isAidocsProject && !isAidocsExpert) {
    return {}
  }
  const sessionPromptContext = new Map()
  const sessionClassification = new Map()
  const activeCommandBySession = new Map()
  const activeCommandMetaBySession = new Map()
  const startupInjectedSessions = new Set()

  return {
    event: async ({ event }) => {
      if (event && event.type === "command.executed" && event.properties && event.properties.sessionID) {
        activeCommandBySession.delete(event.properties.sessionID)
        activeCommandMetaBySession.delete(event.properties.sessionID)
      }
    },

      "chat.message": async ({ sessionID }, output) => {
        // Phase-3 thin-adapter migration (2026-05-20). All chat.message
        // authoritative law (state resolution, prompt-context composition,
        // startup-injection decision, action classification) now lives
        // in host_adapter_cli._handle_oc_chat_message → PromptMutator
        // + LifecycleService + intent_guard. JS only:
        //   1. Phoenix §VIII host_session_id stamp (env-side plumbing).
        //   2. Extract prompt text from the host event.
        //   3. Forward to HostEvent.OC_CHAT_MESSAGE.
        //   4. Render the returned envelope (session prompt context,
        //      classification, optional /aidocs command marker).
        maybeStampWorkerHostSessionId(projectRoot, sessionID)
        const promptText = extractPromptText(output.parts)
        const activeCommand = activeCommandBySession.get(sessionID) || ""

        const result = callHostAdapterService(projectRoot, HostEvent.OC_CHAT_MESSAGE, {
          prompt: promptText,
          host_session_id: String(sessionID || ""),
          active_command: String(activeCommand || ""),
          startup_already_injected: startupInjectedSessions.has(sessionID),
          payload: {},
        })
        pluginDebugLog(projectRoot, "chat.message.host_event", {
          is_aidocs_command: !!(result && result.is_aidocs_command),
          should_inject_startup: !!(result && result.should_inject_startup),
          has_context: !!(result && result.session_prompt_context),
          classification: result && result.session_classification,
        })

        // /aidocs command handling — host-specific rendering. The
        // decision came from Python (is_aidocs_command); the actual
        // prompt text composition is local because it pulls from
        // .aidocs/templates that the host already has on disk.
        if (result && result.is_aidocs_command && !activeCommandBySession.has(sessionID)) {
          activeCommandBySession.set(sessionID, "aidocs")
          const metadata = await readCommandMetadata("aidocs")
          activeCommandMetaBySession.set(sessionID, metadata)
          const directive = [
            "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request.",
            "Do NOT store this as a rule, preference, or memory. Do NOT interpret the command text as instructions to remember.",
            buildAidocsExecutionPrompt(),
          ].join(" ")
          sessionPromptContext.set(sessionID, directive)
          return
        }

        // Render the Python-composed session prompt context.
        const ctx = (result && result.session_prompt_context) || ""
        if (ctx) {
          sessionPromptContext.set(sessionID, ctx)
        } else {
          sessionPromptContext.delete(sessionID)
        }
        if (result && result.should_inject_startup) {
          startupInjectedSessions.add(sessionID)
        }
        const cls = result && result.session_classification
        if (cls) {
          sessionClassification.set(sessionID, cls)
        } else {
          sessionClassification.delete(sessionID)
        }
      },

    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const context = sessionPromptContext.get(sessionID)
      if (!context) {
        return
      }
      output.system.push(context)
    },

    "experimental.session.compacting": async ({ sessionID }, output) => {
      // Phase-2 thin-adapter migration (2026-05-20). Compaction
      // logic (epoch rotation + token-counter reset + structured
      // continuation prompt assembly) lives in
      // LifecycleService.on_post_compact, invoked via
      // host_adapter_cli HostEvent.COMPACT. JS only:
      //   * clears the local startup-dedup Set so first
      //     post-compaction prompt re-injects helper skills.
      //   * forwards the host event and renders the returned
      //     `prompt` envelope into output.prompt.
      startupInjectedSessions.delete(sessionID)
      const config = loadPluginConfig(projectRoot)
      if (!config.disregard_compaction) {
        return
      }
      const state = await resolveAidocsState(projectRoot)
      const compactResult = callHostAdapterService(projectRoot, HostEvent.COMPACT, {
        host_kind: "opencode",
        host_session_id: String(state.sessionID || sessionID || ""),
        payload: {},
      })
      pluginDebugLog(projectRoot, "session.compacting.host_compact", {
        has_prompt: !!(compactResult && compactResult.prompt),
        why: compactResult && compactResult.why,
      })
      if (compactResult && typeof compactResult.prompt === "string" && compactResult.prompt.length > 0) {
        output.prompt = compactResult.prompt
      }
    },

    "experimental.chat.messages.transform": async ({ sessionID }, output) => {
      // Phase-4 thin-adapter migration (2026-05-20). All
      // messages.transform authoritative law (plan-continuation
      // detection by reading PLAN.md, action-directive injection
      // based on classification, /aidocs command rewrite) lives in
      // host_adapter_cli._handle_oc_message_transform. JS only:
      //   1. Validate the host envelope.
      //   2. Forward to HostEvent.OC_MESSAGE_TRANSFORM.
      //   3. Render: replace_parts swaps last.parts entirely;
      //      append_parts get pushed onto last.parts.
      if (!Array.isArray(output.messages)) {
        return
      }
      const last = output.messages[output.messages.length - 1]
      if (!last || !Array.isArray(last.parts)) {
        return
      }

      const config = loadPluginConfig(projectRoot)
      const promptText = extractPromptText(last.parts)
      const activeCommand = activeCommandBySession.get(sessionID) || ""
      const cls = sessionClassification.get(sessionID) || ""

      const result = callHostAdapterService(projectRoot, HostEvent.OC_MESSAGE_TRANSFORM, {
        prompt: promptText,
        host_session_id: String(sessionID || ""),
        active_command: String(activeCommand || ""),
        session_classification: String(cls || ""),
        inject_directives: !!config.inject_message_directives,
        payload: {},
      })
      pluginDebugLog(projectRoot, "messages.transform.host_event", {
        replace: result && Array.isArray(result.replace_parts) && result.replace_parts.length > 0,
        append: result && Array.isArray(result.append_parts) ? result.append_parts.length : 0,
      })

      // Render: replace_parts with marker='aidocs_command' means the
      // Python side decided the message is the /aidocs entry. The
      // actual prompt text composition lives in JS (it reads
      // .aidocs/templates on disk via buildAidocsExecutionPrompt) —
      // pure rendering, not law.
      const replaceParts = (result && Array.isArray(result.replace_parts)) ? result.replace_parts : []
      if (replaceParts.length > 0 && replaceParts[0].marker === "aidocs_command") {
        last.parts = [{ type: "text", text: buildAidocsExecutionPrompt() }]
        return
      }

      const appendParts = (result && Array.isArray(result.append_parts)) ? result.append_parts : []
      for (const part of appendParts) {
        last.parts.push(part)
      }
    },

    "command.execute.before": async ({ command, sessionID }, output) => {
      const normalized = normalizeCommandName(command)
      activeCommandBySession.set(sessionID, normalized)
      const metadata = await readCommandMetadata(normalized)
      activeCommandMetaBySession.set(sessionID, metadata)

      if ((metadata && metadata.command_id === "aidocs") || normalized === "aidocs") {
        output.parts = [
          {
            type: "text",
            text: buildAidocsExecutionPrompt(),
          },
        ]
      }
    },

      "tool.execute.before": async (input, output) => {
        const { tool, sessionID } = input || {}
        pluginDebugLog(projectRoot, "tool.execute.before.enter", {
          tool: String(tool || ""),
          sessionID: String(sessionID || ""),
        })
        const normalizedTool = String(tool || "").toLowerCase()
        if (!GUARDED_TOOLS.has(normalizedTool)) {
          pluginDebugLog(projectRoot, "tool.execute.before.skip_unguarded", { tool: normalizedTool })
          return
        }
        const state = await resolveAidocsState(projectRoot)
        if (!state.initialized) {
          pluginDebugLog(projectRoot, "tool.execute.before.allow_uninitialized", { tool: normalizedTool })
          return
        }
        // OpenCode-specific: the /aidocs command itself runs through
        // the guarded tools. Allow them through while that command is
        // active — host-specific UI affordance, not gate logic.
        const activeCommandMeta = activeCommandMetaBySession.get(sessionID)
        if ((activeCommandMeta && activeCommandMeta.command_id === "aidocs") || activeCommandBySession.get(sessionID) === "aidocs") {
          pluginDebugLog(projectRoot, "tool.execute.before.allow_aidocs_command", { tool: normalizedTool })
          return
        }

        // Host-agnostic pretool gate — delegated to ToolGate via
        // the host_adapter_cli bridge. Replaces the inline managed-
        // mode-required + conductor_comms + orchestrator_check logic.
        // CC, OpenCode, OpenAI Agents now all consume the SAME gate
        // composition for the same (tool, input, session).
        const gateInput = input?.args || input?.input || {}
        const pretoolResult = callHostAdapterService(projectRoot, HostEvent.PRETOOL, {
          tool_name: String(tool || ""),
          tool_input: gateInput,
          host_session_id: String(sessionID || ""),
          project_root: String(projectRoot),
          payload: {},
        })
        pluginDebugLog(projectRoot, "tool.execute.before.host_gate", {
          tool: normalizedTool,
          verdict: pretoolResult && pretoolResult.verdict,
          why: pretoolResult && pretoolResult.why,
        })
        if (pretoolResult && pretoolResult.verdict === "deny") {
          throw new Error(
            String(pretoolResult.reason || "AIDOCS gate denied this tool"),
          )
        }
        if (pretoolResult && pretoolResult.verdict === "ask") {
          // OpenCode has no native ask surface; surface as deny with
          // the ask reason so the operator sees the freeze message.
          throw new Error(
            String(pretoolResult.reason || "AIDOCS requires confirmation"),
          )
        }
        // Conductor messages (additional_context_blocks) → fold into
        // session prompt context for the next message.
        const blocks = (pretoolResult && pretoolResult.additional_context_blocks) || []
        if (blocks.length > 0) {
          const msgTexts = blocks.join("\n")
          const existing = sessionPromptContext.get(sessionID) || ""
          sessionPromptContext.set(sessionID, msgTexts + (existing ? "\n" + existing : ""))
        }

      // Edit-syntax validation + indexed-read gate are now canonical
      // sub-gates inside ToolGate.evaluate_tool (called above via
      // callHostAdapterService → HostEvent.PRETOOL). The pretoolResult
      // verdict already reflects them — no extra JS-side enforcement
      // is needed. Keeping this surface free of policy is the v1
      // contract: opencode_plugin.js carries zero authoritative law.
      pluginDebugLog(projectRoot, "tool.execute.before.canonical_gate_only", {
        tool: normalizedTool,
      })
    },

    "tool.execute.after": async (input, output) => {
      // Phase-1 thin-adapter migration (2026-05-20). The posttool
      // pipeline composition (native_tool_use audit, output-guard
      // scan, task-lifecycle nudges, etc.) lives in
      // LifecycleService.on_post_tool_use_audit + on_tool_end_output_guard
      // — invoked via host_adapter_cli HostEvent.POSTTOOL. The JS side
      // only collects the host event and forwards it; any envelope
      // returned (e.g. additional_context_blocks) is folded into the
      // session prompt context for the next turn.
      const { tool, sessionID, args } = input || {}
      const state = await resolveAidocsState(projectRoot)
      if (!state.managed) {
        return
      }
      const posttoolResult = callHostAdapterService(projectRoot, HostEvent.POSTTOOL, {
        tool_name: String(tool || ""),
        tool_input: args || {},
        tool_response: output,
        host_session_id: String(sessionID || ""),
        agent_id: "",
        lane_id: null,
        payload: {},
      })
      pluginDebugLog(projectRoot, "tool.execute.after.host_posttool", {
        tool: String(tool || ""),
        audit_count: posttoolResult && Array.isArray(posttoolResult.audit_events)
          ? posttoolResult.audit_events.length : 0,
        findings: posttoolResult && Array.isArray(posttoolResult.output_guard_findings)
          ? posttoolResult.output_guard_findings.length : 0,
      })
      // Render: any additional_context_blocks returned by the service
      // (task_complete nudges, output-guard advisories) get folded
      // into sessionPromptContext for the next chat turn.
      const blocks = (posttoolResult && posttoolResult.additional_context_blocks) || []
      if (blocks.length > 0) {
        const append = blocks.join("\n")
        const existing = sessionPromptContext.get(sessionID) || ""
        sessionPromptContext.set(sessionID, existing + (existing ? "\n" : "") + append)
      }
    },

    "shell.env": async (_input, output) => {
      const state = await resolveAidocsState(projectRoot)
      if (!state.initialized) {
        return
      }
      output.env.AIDOCS_INITIALIZED = "1"
      output.env.AIDOCS_MANAGED_MODE = state.managed ? "1" : "0"
      if (state.sessionID) {
        output.env.AIDOCS_SESSION_ID = state.sessionID
      }
    },
  }
}

// OpenCode v1 plugin format — Bun ESM bridge exposes function.length/.name
// as enumerable values which crashes getLegacyPlugins. Using the v1 format
// with { server: fn } makes readV1Plugin succeed and skip getLegacyPlugins.
module.exports = { server: AIDOCSPlugin, id: "aidocs" }

// Test-only access — attach to the default export object so it doesn't
// appear as a top-level export value (which would break getLegacyPlugins)
if (typeof process !== "undefined" && process.env.NODE_ENV === "test") {
  module.exports._test = {
  resolveAidocsState,
  readTextIfExists,
  parseSimpleFrontmatter,
  readCommandMetadata,
  extractPromptText,
  buildPromptContext,
  buildAidocsExecutionPrompt,
  summarizeWorkflowActions,
  listSessionSummaries,
  computeIndexStatus,
  extractSectionBullet,
  normalizeCommandName,
  readAidocsSourceRoot,
  normalizeGatePath,
  getQueryGateState,
  hasGrantedReadAccess,
  summarizeExecutionValue,
  recordNativeToolUse,
  resolveActionTokensDir,
  loadActionTokens,
  classifyPromptAction,
  getActionDirective,
  ACTION_TOOL_DIRECTIVES,
  loadPluginConfig,
  resolveAidocsRuntimeSourceRoot,
  runAidocsHostState,
  resolvePromptHostState,
  resolvePromptImportedSkillState,
  }
}
