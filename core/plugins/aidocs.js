const fs = require("node:fs/promises")
const path = require("node:path")

const GUARDED_TOOLS = new Set(["read", "edit", "write", "glob", "grep", "bash", "task"])

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

function buildPromptContext(state, promptText) {
  if (!state.initialized) {
    return ""
  }

  if (promptText.startsWith("/aidocs")) {
    return [
      "AIDOCS entry command detected.",
      "Use the MCP bootstrap/orchestrator flow for this project.",
      "Report selected session and managed-mode state after the command completes.",
    ].join(" ")
  }

  if (!state.managed) {
    return [
      "This is an AIDOCS-initialized project, but AIDOCS-managed mode is inactive.",
      "If the user is not invoking `/aidocs`, do not begin project work and do not use core repo tools.",
      "Tell the user to run `/aidocs` first.",
    ].join(" ")
  }

  const workflowSummary = summarizeWorkflowActions(state.workflowActions)
  const parts = [
    "AIDOCS-managed mode is active for this project.",
    state.sessionID ? `Bound session: \`${state.sessionID}\`.` : "",
    "Use model judgment for prompt interpretation; deterministic routing helpers are fallback only.",
    "Prefer session-guided memory and MCP-first workflows when they materially improve retrieval, routing, or task lifecycle handling.",
    "For edit tasks, maintain `task_begin` and `task_complete` discipline.",
  ].filter(Boolean)
  if (workflowSummary) {
    parts.push(`Compiled workflow actions: ${workflowSummary}.`)
  }
  return parts.join(" ")
}

function normalizeCommandName(command) {
  return String(command || "").trim().replace(/^\//, "").toLowerCase()
}

async function AIDOCSPlugin(input) {
  const projectRoot = input.worktree || input.directory
  const sessionPromptContext = new Map()
  const aidocsCommandSessions = new Set()

  return {
    event: async ({ event }) => {
      if (event && event.type === "command.executed" && normalizeCommandName(event.properties && event.properties.name) === "aidocs") {
        aidocsCommandSessions.delete(event.properties.sessionID)
      }
    },

    "chat.message": async ({ sessionID }, output) => {
      const state = await resolveAidocsState(projectRoot)
      const promptText = extractPromptText(output.parts)
      const context = buildPromptContext(state, promptText)
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

    "command.execute.before": async ({ command, sessionID }) => {
      if (normalizeCommandName(command) === "aidocs") {
        aidocsCommandSessions.add(sessionID)
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
      if (aidocsCommandSessions.has(sessionID)) {
        return
      }
      if (!state.managed) {
        throw new Error("AIDOCS-managed mode is inactive for this initialized project. Run /aidocs first.")
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
    extractPromptText,
    buildPromptContext,
    summarizeWorkflowActions,
    normalizeCommandName,
  },
}
