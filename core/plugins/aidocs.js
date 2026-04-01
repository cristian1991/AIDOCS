const fs = require("node:fs/promises")
const fsSync = require("node:fs")
const path = require("node:path")
const childProcess = require("node:child_process")

const GUARDED_TOOLS = new Set(["read", "edit", "write", "glob", "grep", "bash", "task"])
const COMMANDS_DIR = path.join(__dirname, "..", ".commands")

let _cachedActionTokens = null
let _pluginConfig = null
let _runtimeHostStateCache = new Map()

const HOST_STATE_SUCCESS_TTL_MS = 3000
const HOST_STATE_FAILURE_TTL_MS = 15000

function aidocsMemoryConfigPath(projectRoot, fileName) {
  return path.join(projectRoot, ".MEMORY", "config", fileName)
}

function loadPluginConfig() {
  if (_pluginConfig) {
    return _pluginConfig
  }
  const defaults = {
    inject_message_directives: true,
    directive_style: "short",
    disregard_compaction: false,
    startup_context_once: true,
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
        return _pluginConfig
      }
    } catch {
      // ignore
    }
  }
  _pluginConfig = defaults
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

function resolvePythonBin() {
  return process.env.AIDOCS_PYTHON || process.env.PYTHON || "python"
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
    const result = childProcess.spawnSync(
      pythonBin,
      ["-c", script, projectRoot, sessionID || "", promptText || "", templatesRoot],
      {
        encoding: "utf8",
        env: {
          ...process.env,
          PYTHONPATH: mergePythonPath(process.env.PYTHONPATH, pythonPath),
        },
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

  const managedConfig = await readJsonIfExists(aidocsMemoryConfigPath(projectRoot, "aidocs-managed.json"))
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
    path.join(__dirname, "action_tokens"),
    // Dev: project root action_tokens/
    path.join(__dirname, "..", "..", "action_tokens"),
  ]

  // From AIDOCS_PATH env var (set by installer)
  const aidocsPath = process.env.AIDOCS_PATH
  if (aidocsPath) {
    candidates.push(path.join(aidocsPath, "action_tokens"))
  }

  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) {
    candidates.push(path.join(sourceRoot, "..", "action_tokens"))
    // Legacy: inside MCP package
    candidates.push(path.join(sourceRoot, "mcp", "server", "aidocs_mcp", "action_tokens"))
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
      if (!f.endsWith(".yaml")) return false
      if (enabledSet && !enabledSet.has(f.replace(".yaml", ""))) return false
      return true
    }).sort()
  } catch {
    _cachedActionTokens = []
    return _cachedActionTokens
  }
  for (const file of files) {
    const content = fsSync.readFileSync(path.join(actionTokensDir, file), "utf8")
    let currentKey = null
    for (const raw of content.split(/\r?\n/)) {
      const line = raw.trimEnd()
      if (!line || line.trimStart().startsWith("#")) {
        continue
      }
      const keyMatch = line.match(/^(\w[\w_]*):\s*$/)
      if (keyMatch) {
        currentKey = keyMatch[1]
        continue
      }
      const itemMatch = line.match(/^\s+-\s+(.+)$/)
      if (itemMatch && currentKey) {
        const token = itemMatch[1].trim()
        if (token) {
          if (!merged.has(currentKey)) {
            merged.set(currentKey, new Set())
          }
          merged.get(currentKey).add(token)
        }
      }
    }
  }
  _cachedActionTokens = Array.from(merged.entries()).map(([kind, tokens]) => [kind, Array.from(tokens)])
  return _cachedActionTokens
}

function classifyPromptAction(text) {
  const lower = text.trim().toLowerCase()
  if (/^(investigate|debug|diagnose|dig into)\b/.test(lower)) {
    return { action_kind: "investigate", why: "prefix:investigate" }
  }
  if (/^(inspect|examine|audit)\b/.test(lower)) {
    return { action_kind: "inspect", why: "prefix:inspect" }
  }
  const mapping = loadActionTokens()
  for (const [actionKind, tokens] of mapping) {
    if (tokens.some((token) => lower.includes(token))) {
      return { action_kind: actionKind, why: `matched:${actionKind}` }
    }
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
      latestPathMtime(aidocsMemoryConfigPath(projectRoot, "aidocs-managed.json")),
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
    const workflow = await readJsonIfExists(aidocsMemoryConfigPath(projectRoot, "workflow-actions.json"))
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
    provider_states: phase === "session" && sessionSnapshot && typeof sessionSnapshot.provider_states === "object"
      ? sessionSnapshot.provider_states
      : {},
    provider_state: phase === "session" && sessionSnapshot
      ? sessionSnapshot.provider_state || null
      : null,
    triggered: Array.isArray(sourceState.triggered) ? sourceState.triggered : [],
    mode_metadata: sourceState.mode_metadata || (promptState && promptState.mode_metadata) || null,
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
  if (isAidocsEntry) {
    const preamble = "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request or rule to store. Do NOT save this as a memory, preference, or workflow rule."
    if (!state.initialized) {
      return [
        preamble,
        "This project has no AIDOCS structure. Call the `project_init` MCP tool with the project root path",
        "to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates.",
        "After initialization, call `project_bootstrap_or_resume` to activate managed mode.",
      ].join(" ")
    }
    return [
      preamble,
      buildAidocsExecutionPrompt(),
    ].join(" ")
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
    return "AIDOCS startup detected stale indexes. Run `/aidocs` first to refresh bootstrap and index state before normal work."
  }

  if (!state.managed) {
    return [
      "This is an AIDOCS-ready project, but AIDOCS-managed mode is inactive.",
      "If the user is not invoking `/aidocs`, do not begin project work and do not use core repo tools.",
      "Tell the user to run `/aidocs` first.",
    ].join(" ")
  }

  const promptPayload = promptHostState && promptHostState.source === "runtime_host_state" && promptHostState.payload
    ? promptHostState.payload
    : (state && state.hostState && typeof state.hostState === "object" ? state.hostState : null)
  const promptState = promptPayload && promptPayload.prompt_state && typeof promptPayload.prompt_state === "object"
    ? promptPayload.prompt_state
    : null
  const classification = {
    action_kind: (promptState && promptState.action_kind) || classifyPromptAction(promptText).action_kind,
  }
  const workflowSummary = summarizeWorkflowActions(state.workflowActions)
  const parts = [
    "AIDOCS-managed mode is active for this project.",
    state.sessionID ? `Bound session: \`${state.sessionID}\`.` : "",
    `Action: \`${classification.action_kind}\`.`,
    "MANDATORY: Use AIDOCS MCP tools FIRST. Fall back to raw Read/Grep only if MCP returns empty.",
  ].filter(Boolean)
    const effectiveImportedSkillState = resolveImportedSkillStateForContext(state, promptHostState, arguments.length >= 5)
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
    if (workflowSummary) {
      parts.push(`Compiled workflow actions: ${workflowSummary}.`)
    }
  return parts.join(" ")
}

const ACTION_TOOL_DIRECTIVES = {
  edit: '`task_begin` → `aidocs_code_get_lines` (read) → `aidocs_code_edit_lines` or `aidocs_code_batch_edit` (write) → `task_complete`. Do NOT mix edit methods. Before editing: `aidocs_code_get_method_signature` / `aidocs_code_get_constructor_params` to confirm signatures. CSS: `aidocs_code_trace(class, mode="css_class")`. DB: `aidocs_schema_query(entity, mode="entity")`.',
  trace: '`aidocs_code_find(query, mode="references")` → `aidocs_code_trace(query, mode="field_flow"|"css_class"|"api_to_ui")`. DB: `aidocs_schema_query("Source→Target", mode="trace_path")`.',
  understand: '`aidocs_session_resume_bundle` (project/session/skills/plan overview) → `aidocs_action_surface_current_session_bundle` (likely next tools) → `aidocs_code_find(query, mode="symbols")` → `aidocs_code_get_symbol_snippet`. Precision: `aidocs_code_get_method_signature`, `aidocs_code_get_constructor_params`, `aidocs_code_get_enum_values`, `aidocs_code_get_service_api`. Broad: `aidocs_code_bundle(concept, mode="subsystem")`. DB: `aidocs_schema_query(name, mode="entity")`.',
  read_error: '`aidocs_code_find(symbol, mode="symbols")` → `aidocs_code_find(symbol, mode="references")` → `aidocs_code_get_symbol_snippet`. DB: `aidocs_schema_query(entity, mode="entity")`.',
  investigate: '`session_resume_bundle` (overview) → `action_surface_current_session_bundle` (common path) → `code_investigate(concept)` for guided navigation. Or: `code_bundle(concept, mode="subsystem")` → `code_find(concept, mode="mutations"|"validation"|"policy")`.',
  inspect: '`session_resume_bundle` (overview) → `action_surface_current_session_bundle` (common path) → `code_get_dependencies` / `code_find_dependents` → `code_get_modules`. Read only after narrowing.',
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
  const projectRoot = input.worktree || input.directory
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
        const promptText = extractPromptText(output.parts)
        const state = await resolveAidocsState(projectRoot, promptText)
        const activeCommand = activeCommandBySession.get(sessionID) || ""
        const activeCommandMeta = activeCommandMetaBySession.get(sessionID) || null

      // If /aidocs was sent as plain text (not caught by command.execute.before),
      // mark it as a command so system context and message transform hooks handle it.
      if (!activeCommandBySession.has(sessionID) && promptText.trim().replace(/^\//, "").toLowerCase().startsWith("aidocs")) {
        activeCommandBySession.set(sessionID, "aidocs")
        const metadata = await readCommandMetadata("aidocs")
        activeCommandMetaBySession.set(sessionID, metadata)
        // System context is the reliable path — chat.message output.parts mutation is ignored by OC.
        const directive = [
          "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request.",
          "Do NOT store this as a rule, preference, or memory. Do NOT interpret the command text as instructions to remember.",
          buildAidocsExecutionPrompt(),
        ].join(" ")
        sessionPromptContext.set(sessionID, directive)
        return
      }

        const promptHostState = await resolvePromptHostState(projectRoot, state, promptText)
        const context = buildPromptContext(state, promptText, activeCommand, activeCommandMeta, promptHostState)
      if (context) {
        let finalContext = context
        const config = loadPluginConfig()
          const shouldInjectStartup = config.startup_context_once && state.startupState === "ready" && state.sessionID && !startupInjectedSessions.has(sessionID)
        if (shouldInjectStartup) {
          const startupBlocks = await buildSessionStartContext(projectRoot, state)
          if (startupBlocks.length) {
            finalContext = [
              context,
              "Session start context for this conversation:\n" + startupBlocks.join("\n\n"),
            ].join("\n\n")
          }
          startupInjectedSessions.add(sessionID)
        }
        sessionPromptContext.set(sessionID, finalContext)
        // Store classification for message transform injection
        if (state.managed) {
          const cls = classifyPromptAction(promptText)
          sessionClassification.set(sessionID, cls.action_kind)
        }
      } else {
        sessionPromptContext.delete(sessionID)
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
      const config = loadPluginConfig()
      if (!config.disregard_compaction) {
        return
      }
      const state = await resolveAidocsState(projectRoot)
      const compactSessionID = state.sessionID || sessionID
      const blocks = await buildCompactionContext(projectRoot, compactSessionID)
      output.prompt = [
        "Ignore the default generic compaction style.",
        "Create a continuation summary that preserves AIDOCS structured state first.",
        "Read and preserve the important information from project memory, roadmap, session plan, handoff, and session journal.",
        "Prioritize current actionable work, blockers, what failed, and next steps over conversational filler.",
        "Do not duplicate long prose if the same information already exists in structured artifacts.",
        "Produce a concise but complete continuation summary for the next agent.",
        ...blocks,
      ].join("\n\n")
    },

    "experimental.chat.messages.transform": async ({ sessionID }, output) => {
      if (!Array.isArray(output.messages)) {
        return
      }
      const last = output.messages[output.messages.length - 1]
      if (!last || !Array.isArray(last.parts)) {
        return
      }

      // Rewrite the user message when /aidocs was sent as plain text.
      if (activeCommandBySession.get(sessionID) === "aidocs") {
        const text = extractPromptText(last.parts)
        if (text.trim().replace(/^\//, "").toLowerCase().startsWith("aidocs")) {
          last.parts = [{ type: "text", text: buildAidocsExecutionPrompt() }]
        }
        return
      }

      // Inject action-specific tool directive into the last user message.
      // Inject into user message (not system prompt) — models weight recent user-turn content higher.
      const config = loadPluginConfig()
      if (!config.inject_message_directives) {
        return
      }
      const actionKind = sessionClassification.get(sessionID)
      if (!actionKind) {
        return
      }
      const directive = getActionDirective(actionKind)
      if (!directive) {
        return
      }
      last.parts.push({
        type: "text",
        text: `\n<tool-directive action="${actionKind}">\n${directive}\n</tool-directive>`,
      })

      // Plan-aware message rewrite: when user confirms ("ok", "continue") and PLAN.md has unchecked steps,
      // inject the next step as context so the agent continues working instead of stopping.
      const promptText = extractPromptText(last.parts)
      const trimmed = promptText.replace(/<tool-directive[^>]*>[\s\S]*?<\/tool-directive>/g, "").trim()
      const isContinuation = trimmed.length < 40 && /^(ok|continue|next|go|yes|yep|yeah|sure|do it|keep going|proceed|all of them|perfect|great|nice|good|👍|✅|🚀)/i.test(trimmed)

      const state = isContinuation ? await resolveAidocsState(projectRoot, trimmed) : null
      if (isContinuation && state && state.managed && state.sessionID) {
        try {
          const planPath = path.join(projectRoot, ".MEMORY", "sessions", state.sessionID, "PLAN.md")
          if (fsSync.existsSync(planPath)) {
            const planText = fsSync.readFileSync(planPath, "utf8")
            // Find incomplete steps (lines starting with - [ ] )
            const incompleteSteps = planText.split(/\r?\n/)
              .filter((line) => /^\s*-\s*\[\s*\]/.test(line))
              .map((line) => line.replace(/^\s*-\s*\[\s*\]\s*/, "").trim())
              .filter(Boolean)

            if (incompleteSteps.length > 0) {
              const nextStep = incompleteSteps[0]
              const remaining = incompleteSteps.length
              last.parts.push({
                type: "text",
                text: `\n<plan-continuation>\nSession plan has ${remaining} incomplete step(s). Next: ${nextStep}\nContinue implementing. Do not stop to ask — the user confirmed.\n</plan-continuation>`,
              })
            }
          }
        } catch {
          // Plan read failed — skip continuation
        }
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

    "tool.execute.before": async ({ tool, sessionID }) => {
      const normalizedTool = String(tool || "").toLowerCase()
      if (!GUARDED_TOOLS.has(normalizedTool)) {
        return
      }
      const state = await resolveAidocsState(projectRoot)
      if (!state.initialized) {
        return
      }
      const activeCommandMeta = activeCommandMetaBySession.get(sessionID)
      if ((activeCommandMeta && activeCommandMeta.command_id === "aidocs") || activeCommandBySession.get(sessionID) === "aidocs") {
        return
      }
      if (!state.managed) {
        throw new Error("AIDOCS-managed mode is inactive for this initialized project. Run /aidocs first.")
      }
    },

    "tool.execute.after": async ({ tool, sessionID }) => {
      // After edit-type tools complete, remind about task lifecycle
      const normalizedTool = String(tool || "").toLowerCase()
      if (normalizedTool !== "edit" && normalizedTool !== "write") {
        return
      }
      const state = await resolveAidocsState(projectRoot)
      if (!state.managed) {
        return
      }
      // Inject task lifecycle reminder into session context
      const existing = sessionPromptContext.get(sessionID) || ""
      if (!existing.includes("task_complete")) {
        sessionPromptContext.set(sessionID, existing + " Remember to call `task_complete` when the edit task is done.")
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

module.exports = {
  AIDOCSPlugin,
  default: AIDOCSPlugin,
  _internal: {
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
    },
  }
