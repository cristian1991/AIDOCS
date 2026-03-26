const fs = require("node:fs/promises")
const fsSync = require("node:fs")
const path = require("node:path")

const GUARDED_TOOLS = new Set(["read", "edit", "write", "glob", "grep", "bash", "task"])
const COMMANDS_DIR = path.join(__dirname, "..", ".commands")

let _cachedActionTokens = null
let _pluginConfig = null

function loadPluginConfig() {
  if (_pluginConfig) {
    return _pluginConfig
  }
  const defaults = {
    inject_message_directives: true,
    directive_style: "short",
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

async function resolveAidocsState(projectRoot) {
  const memoryRoot = path.join(projectRoot, ".MEMORY")
  const routerPath = path.join(memoryRoot, ".aidocs", "index.aidocs")
  const initialized = (await fileExists(routerPath)) && ((await fileExists(path.join(projectRoot, "AGENTS.md"))) || (await fileExists(path.join(projectRoot, "CLAUDE.md"))))
  if (!initialized) {
    return {
      initialized: false,
      managed: false,
      sessionID: null,
      workflowActions: [],
    }
  }

  const managed = await readJsonIfExists(path.join(memoryRoot, "config", "aidocs-managed.json"))
  const workflow = await readJsonIfExists(path.join(memoryRoot, "config", "workflow-actions.json"))
  return {
    initialized: true,
    managed: Boolean(managed && managed.active),
    sessionID: managed && typeof managed.session_id === "string" ? managed.session_id : null,
    workflowActions: Array.isArray(workflow && workflow.actions) ? workflow.actions : [],
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

function buildPromptContext(state, promptText, activeCommand, activeCommandMeta) {
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

  if (!state.initialized) {
    return ""
  }

  if (!state.managed) {
    return [
      "This is an AIDOCS-initialized project, but AIDOCS-managed mode is inactive.",
      "If the user is not invoking `/aidocs`, do not begin project work and do not use core repo tools.",
      "Tell the user to run `/aidocs` first.",
    ].join(" ")
  }

  const classification = classifyPromptAction(promptText)
  const workflowSummary = summarizeWorkflowActions(state.workflowActions)
  const parts = [
    "AIDOCS-managed mode is active for this project.",
    state.sessionID ? `Bound session: \`${state.sessionID}\`.` : "",
    `Action: \`${classification.action_kind}\`.`,
    "MANDATORY: Use AIDOCS MCP tools FIRST. Fall back to raw Read/Grep only if MCP returns empty.",
  ].filter(Boolean)
  if (workflowSummary) {
    parts.push(`Compiled workflow actions: ${workflowSummary}.`)
  }
  return parts.join(" ")
}

const ACTION_TOOL_DIRECTIVES = {
  edit: "`task_begin` → `code_get_outline` → `code_search_symbols` → Edit → `task_complete`. CSS: add `code_trace_css_class`. API: add `code_trace_api_to_ui`. DB: add `schema_get_entity`.",
  trace: "`code_find_references` (usages) → `code_trace_field_flow` (cross-layer) → `code_trace_css_class` (CSS+HTML). DB: `schema_trace_relationship_path`. API→UI: `code_trace_api_to_ui`.",
  understand: "`code_get_outline` (structure) → `code_search_symbols` (find) → `code_get_symbol_snippet` (read). Broad: `code_get_subsystem_bundle`. DB: `schema_get_entity`.",
  read_error: "`code_search_symbols` (find failing symbol) → `code_find_references` (trace) → `code_get_symbol_snippet` (read method). DB: add `schema_get_entity`.",
  investigate: "`code_get_subsystem_bundle` (broad) → narrow with `code_find_mutation_points` / `code_find_validation_surfaces` / `code_find_policy_surfaces`. Or: `code_investigate` for guided plan.",
  inspect: "`code_get_outline` → `code_get_dependencies` / `code_find_dependents` → `code_get_modules`. Read only after narrowing.",
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

function normalizeCommandName(command) {
  return String(command || "").trim().replace(/^\//, "").toLowerCase()
}

async function AIDOCSPlugin(input) {
  const projectRoot = input.worktree || input.directory
  const sessionPromptContext = new Map()
  const sessionClassification = new Map()
  const activeCommandBySession = new Map()
  const activeCommandMetaBySession = new Map()

  return {
    event: async ({ event }) => {
      if (event && event.type === "command.executed" && event.properties && event.properties.sessionID) {
        activeCommandBySession.delete(event.properties.sessionID)
        activeCommandMetaBySession.delete(event.properties.sessionID)
      }
    },

    "chat.message": async ({ sessionID }, output) => {
      const state = await resolveAidocsState(projectRoot)
      const promptText = extractPromptText(output.parts)
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

      const context = buildPromptContext(state, promptText, activeCommand, activeCommandMeta)
      if (context) {
        sessionPromptContext.set(sessionID, context)
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
      // This is more effective than system prompt alone — closer to model attention.
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
    normalizeCommandName,
    readAidocsSourceRoot,
    resolveActionTokensDir,
    loadActionTokens,
    classifyPromptAction,
    getActionDirective,
    ACTION_TOOL_DIRECTIVES,
    loadPluginConfig,
  },
}
