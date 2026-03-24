const fs = require("node:fs/promises")
const fsSync = require("node:fs")
const path = require("node:path")

const GUARDED_TOOLS = new Set(["read", "edit", "write", "glob", "grep", "bash", "task"])
const COMMANDS_DIR = path.join(__dirname, "..", ".commands")

let _cachedActionTokens = null

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
    path.join(__dirname, "..", "..", "mcp", "server", "aidocs_mcp", "action_tokens"),
  ]

  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) {
    candidates.push(path.join(sourceRoot, "mcp", "server", "aidocs_mcp", "action_tokens"))
    candidates.push(path.join(sourceRoot, "server", "aidocs_mcp", "action_tokens"))
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
  let files
  try {
    files = fsSync.readdirSync(actionTokensDir).filter((f) => f.endsWith(".yaml")).sort()
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
  const isAidocsEntry = (activeCommandMeta && activeCommandMeta.command_id === "aidocs") || activeCommand === "aidocs" || promptText.startsWith("/aidocs")
  if (isAidocsEntry) {
    if (!state.initialized) {
      return [
        "AIDOCS entry command detected on a project without AIDOCS structure.",
        "This project needs initialization. Call the `project_init` MCP tool with the project root path",
        "to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates.",
        "After initialization, call `project_bootstrap_or_resume` to activate managed mode.",
      ].join(" ")
    }
    return [
      "AIDOCS entry command detected.",
      "Use the MCP bootstrap/orchestrator flow for this project.",
      "For default user-facing output, prefer the top-level `report` field from MCP/runtime responses.",
      "Use `readiness_summary` for compact structured state and deeper payloads only when needed.",
      "Report selected session and managed-mode state after the command completes.",
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
    `AIDOCS suggests action kind: \`${classification.action_kind}\` (advisory — use your judgment if the classification seems wrong).`,
    "Prefer session-guided memory and MCP-first workflows when they materially improve retrieval, routing, or task lifecycle handling.",
    "When MCP/runtime responses include a top-level `report`, prefer it for default user-facing summaries.",
    "Use `readiness_summary` only when compact structured state helps, and use deeper payloads only for advanced inspection.",
    "For edit tasks, maintain `task_begin` and `task_complete` discipline.",
  ].filter(Boolean)
  if (workflowSummary) {
    parts.push(`Compiled workflow actions: ${workflowSummary}.`)
  }
  return parts.join(" ")
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
      const context = buildPromptContext(state, promptText, activeCommand, activeCommandMeta)
      if (context) {
        sessionPromptContext.set(sessionID, context)
      } else {
        sessionPromptContext.delete(sessionID)
      }
    },

    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const context = sessionPromptContext.get(sessionID)
      if (!context) {
        return
      }
      output.system.push(context)
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
  },
}
