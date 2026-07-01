/**
 * Prompt context building — system prompt injection, message directives, compaction.
 */
const fsSync = require("node:fs")
const path = require("node:path")

const { readTextIfExists, takeFirstNonEmpty } = require("./session")
const { classifyPromptAction } = require("./classify")
const MESSAGE_DIRECTIVE_ACTIONS = new Set(["edit", "write_memory", "task_begin", "task_update", "task_complete"])

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

function summarizeWorkflowActions(actions) {
  if (!actions.length) return ""
  const rendered = actions.slice(0, 3).map((a) => `\`${a.trigger || "?"} -> ${a.kind || "?"}\``)
  if (actions.length > rendered.length) rendered.push(`and ${actions.length - rendered.length} more`)
  return rendered.join(", ")
}

// ── Imported skill state ──

function extractImportedSkillState(hostState, phase) {
  if (!hostState || typeof hostState !== "object") return null
  const skillState = hostState.skill_state && typeof hostState.skill_state === "object" ? hostState.skill_state : null
  const sessionSnapshot = skillState && skillState.session_snapshot && typeof skillState.session_snapshot === "object" ? skillState.session_snapshot : null
  const promptActivation = skillState && skillState.prompt_activation && typeof skillState.prompt_activation === "object" ? skillState.prompt_activation : null
  const promptState = hostState.prompt_state && typeof hostState.prompt_state === "object" ? hostState.prompt_state : null
  const sourceState = phase === "prompt" ? promptActivation : sessionSnapshot
  if (!sourceState) return null
  return {
    source: sourceState.source || null,
    session_id: sourceState.session_id || (hostState.session_state && hostState.session_state.session_id) || null,
    intent: phase === "prompt" ? promptState && promptState.intent : null,
    workflow_state: phase === "prompt" ? promptState && promptState.action_kind : null,
    selected_skills: Array.isArray(sessionSnapshot && sessionSnapshot.selected_skills) ? sessionSnapshot.selected_skills.filter(Boolean) : [],
    active_skills: Array.isArray(sourceState.active_skills) ? sourceState.active_skills.filter(Boolean) : [],
    runtime_owned_capabilities: Array.isArray(sourceState.runtime_owned_capabilities) ? sourceState.runtime_owned_capabilities.filter((i) => i && typeof i === "object") : [],
    provider_states: phase === "session" && sessionSnapshot && typeof sessionSnapshot.provider_states === "object" ? sessionSnapshot.provider_states : {},
    provider_state: phase === "session" && sessionSnapshot ? sessionSnapshot.provider_state || null : null,
    triggered: Array.isArray(sourceState.triggered) ? sourceState.triggered : [],
    mode_metadata: sourceState.mode_metadata || (promptState && promptState.mode_metadata) || null,
    helper_skill_guidance: Array.isArray(sourceState.helper_skill_guidance) ? sourceState.helper_skill_guidance : [],
    override_modes: phase === "prompt" && promptState && typeof promptState.override_modes === "object" ? promptState.override_modes : {},
    activation_succeeded: phase === "prompt" ? Boolean(sourceState.activation_succeeded ?? (promptState && promptState.activation_succeeded)) : undefined,
  }
}

function resolveEffectiveImportedSkillState(state, promptHostState) {
  const promptPayload = promptHostState && promptHostState.source === "runtime_host_state" && promptHostState.payload ? promptHostState.payload : null
  const promptRuntimeState = extractImportedSkillState(promptPayload, "prompt")
  if (promptRuntimeState) return promptRuntimeState
  const startupRuntimeState = extractImportedSkillState(state && state.hostState, "session")
  if (startupRuntimeState) return startupRuntimeState
  return state && state.importedSkillState ? state.importedSkillState : null
}

function resolveImportedSkillStateForContext(state, promptHostState, promptStateWasProvided) {
  if (promptStateWasProvided) {
    if (!promptHostState) return null
    if (promptHostState.source && promptHostState.source !== "runtime_host_state") return promptHostState
    return resolveEffectiveImportedSkillState(state, promptHostState)
  }
  return resolveEffectiveImportedSkillState(state, promptHostState)
}

function renderHelperSkillGuidance(importedSkillState) {
  const guidance = importedSkillState && Array.isArray(importedSkillState.helper_skill_guidance)
    ? importedSkillState.helper_skill_guidance.filter((i) => i && typeof i === "object" && typeof i.content === "string" && i.content.trim()) : []
  if (!guidance.length) return []
  return guidance.slice(0, 2).map((item) => {
    const name = String(item.name || item.skill_id || "skill").trim()
    return `<aidocs-skill name="${name}">\n${String(item.content || "").trim()}\n</aidocs-skill>`
  })
}

function renderRuntimeOwnedCapabilities(importedSkillState) {
  const caps = importedSkillState && Array.isArray(importedSkillState.runtime_owned_capabilities)
    ? importedSkillState.runtime_owned_capabilities.filter((i) => i && typeof i === "object" && typeof i.capability_id === "string" && i.capability_id.trim()) : []
  if (!caps.length) return ""
  return `Runtime-owned workflow capabilities: ${caps.map((i) => `\`${String(i.capability_id).trim()}\``).join(", ")}.`
}

// ── Prompt context ──

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

function buildPromptContext(state, promptText, activeCommand, activeCommandMeta, promptHostState, options = {}) {
  const isAidocsEntry = (activeCommandMeta && activeCommandMeta.command_id === "aidocs")
    || activeCommand === "aidocs"
    || promptText.startsWith("/aidocs")
    || promptText.toLowerCase().trim().replace(/^\//, "").startsWith("aidocs")

  const explicitPromptStateProvided = arguments.length >= 5
  const promptPayload = promptHostState && promptHostState.source === "runtime_host_state" && promptHostState.payload
    ? promptHostState.payload
    : (!explicitPromptStateProvided && state && state.hostState && typeof state.hostState === "object" ? state.hostState : null)

  const interactionText = promptPayload && promptPayload.interaction_text && typeof promptPayload.interaction_text === "object"
    ? promptPayload.interaction_text : null

  if (isAidocsEntry) {
    const preamble = "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request or rule to store. Do NOT save this as a memory, preference, or workflow rule."
    if (!state.initialized) {
      return [preamble, interactionText && typeof interactionText.missing_structure === "string"
        ? interactionText.missing_structure
        : "This project has no AIDOCS structure. Call the `project_init` MCP tool with the project root path to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates. After initialization, call `project_bootstrap_or_resume` to activate managed mode."
      ].join(" ")
    }
    return [preamble, interactionText && typeof interactionText.execution_prompt === "string"
      ? interactionText.execution_prompt : buildAidocsExecutionPrompt()
    ].join(" ")
  }

  // Prefer interaction text from runtime host state
  if (interactionText && typeof interactionText.startup_message === "string" && interactionText.startup_message.trim()) return interactionText.startup_message
  if (interactionText && typeof interactionText.unmanaged_message === "string" && interactionText.unmanaged_message.trim()) return interactionText.unmanaged_message
  if (interactionText && typeof interactionText.prompt_context === "string" && interactionText.prompt_context.trim()) {
    const parts = [interactionText.prompt_context]
    if (typeof interactionText.action_directive === "string" && interactionText.action_directive.trim()) parts.push(interactionText.action_directive)
    return parts.join(" ")
  }

  // Fallback: build from state
  if (state.startupState === "not_initialized") return "This project is not initialized for AIDOCS. Run `/aidocs` first to initialize and bootstrap it before normal work."
  if (state.startupState === "not_bootstrapped") return "AIDOCS project structure is incomplete or not fully bootstrapped. Run `/aidocs` first to repair bootstrap state before normal work."
  if (state.startupState === "no_session") return "AIDOCS is initialized, but no session exists yet. Run `/aidocs` first so the session can be created before normal work."
  if (state.startupState === "multiple_sessions") return "Multiple plausible AIDOCS sessions exist. Ask the user which session to connect to before normal work, then use `/aidocs` to bind managed mode."
  if (!state.managed) return "This is an AIDOCS-ready project, but AIDOCS-managed mode is inactive. If the user is not invoking `/aidocs`, do not begin project work and do not use core repo tools. Tell the user to run `/aidocs` first."

  const lifecycleState = promptPayload && promptPayload.lifecycle_state && typeof promptPayload.lifecycle_state === "object" ? promptPayload.lifecycle_state : null
  const promptState = promptPayload && promptPayload.prompt_state && typeof promptPayload.prompt_state === "object" ? promptPayload.prompt_state : null
  const classification = { action_kind: (promptState && promptState.action_kind) || classifyPromptAction(promptText).action_kind }
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

  // Imported skills
  const effectiveSkillState = resolveImportedSkillStateForContext(state, promptHostState, explicitPromptStateProvided)
  const importedSkills = effectiveSkillState && Array.isArray(effectiveSkillState.active_skills) ? effectiveSkillState.active_skills.filter(Boolean) : []
  if (importedSkills.length) parts.push(`Imported skills: ${importedSkills.map((i) => `\`${i}\``).join(", ")}.`)

  const importedSkillModes = effectiveSkillState && effectiveSkillState.mode_metadata && effectiveSkillState.mode_metadata.active_skill_modes
    ? Object.entries(effectiveSkillState.mode_metadata.active_skill_modes) : []
  if (importedSkillModes.length) parts.push(`Imported skill modes: ${importedSkillModes.map(([id, mode]) => `\`${id}=${mode}\``).join(", ")}.`)

  const runtimeCaps = renderRuntimeOwnedCapabilities(effectiveSkillState)
  if (runtimeCaps) parts.push(runtimeCaps)

  const helperBlocks = renderHelperSkillGuidance(effectiveSkillState)
  const suppressSessionHelperGuidance = Boolean(options && options.suppressSessionHelperGuidance)
  const canRenderHelperBlocks = helperBlocks.length && (!suppressSessionHelperGuidance || effectiveSkillState && effectiveSkillState.source === "live_prompt")
  if (canRenderHelperBlocks) { parts.push("Active AIDOCS helper skill guidance:"); parts.push(...helperBlocks) }

  if (workflowSummary) parts.push(`Compiled workflow actions: ${workflowSummary}.`)
  if (lifecycleState && lifecycleState.needs_task_complete) parts.push("Lifecycle follow-through: meaningful edit work happened since the last lifecycle tool call; use `ai_task(mode='complete')` if the task is done.")
  else if (lifecycleState && lifecycleState.needs_task_update) parts.push("Lifecycle follow-through: meaningful work has accumulated since the last lifecycle tool call; use `ai_task(mode='update')` to record progress.")

  return parts.join(" ")
}

// ── Compaction + startup context ──

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
  for (const c of roadmapCandidates) { roadmapText = await readTextIfExists(c); if (roadmapText) break }

  if (memoryIndex) blocks.push("Memory index:\n" + takeFirstNonEmpty(memoryIndex.split(/\r?\n/), 10).map((l) => `- ${l}`).join("\n"))
  if (roadmapText) blocks.push("Roadmap:\n" + takeFirstNonEmpty(roadmapText.split(/\r?\n/), 10).map((l) => `- ${l}`).join("\n"))
  if (sessionFile) blocks.push("Session:\n" + takeFirstNonEmpty(sessionFile.split(/\r?\n/), 12).map((l) => `- ${l}`).join("\n"))
  if (planFile) blocks.push("Plan:\n" + takeFirstNonEmpty(planFile.split(/\r?\n/), 12).map((l) => `- ${l}`).join("\n"))
  if (handoffFile) blocks.push("Handoff:\n" + takeFirstNonEmpty(handoffFile.split(/\r?\n/), 12).map((l) => `- ${l}`).join("\n"))
  if (journalFile) {
    const recent = journalFile.split(/\r?\n/).filter((l) => l.trim().startsWith("- `")).slice(-8)
    if (recent.length) blocks.push("Recent journal:\n" + recent.map((l) => `- ${l.trim()}`).join("\n"))
  }
  return blocks
}

async function buildSessionStartContext(projectRoot, state) {
  const blocks = []
  const sid = state && state.sessionID ? state.sessionID : null
  const sessionFile = sid ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sid, "SESSION.md")) : null
  const planFile = sid ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sid, "plans", "PLAN.md")) : null
  const handoffFile = sid ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sid, `${sid}.handoff.md`)) : null
  const journalFile = sid ? await readTextIfExists(path.join(projectRoot, ".MEMORY", "sessions", sid, "journal.md")) : null

  if (sessionFile) blocks.push("Session:\n" + takeFirstNonEmpty(sessionFile.split(/\r?\n/), 10).map((l) => `- ${l}`).join("\n"))
  if (planFile) blocks.push("Plan:\n" + takeFirstNonEmpty(planFile.split(/\r?\n/), 10).map((l) => `- ${l}`).join("\n"))
  if (handoffFile) blocks.push("Handoff:\n" + takeFirstNonEmpty(handoffFile.split(/\r?\n/), 12).map((l) => `- ${l}`).join("\n"))
  if (journalFile) {
    const recent = journalFile.split(/\r?\n/).filter((l) => l.trim().startsWith("- `")).slice(-5)
    if (recent.length) blocks.push("Recent journal:\n" + recent.map((l) => `- ${l.trim()}`).join("\n"))
  }
  return blocks
}

module.exports = {
  MESSAGE_DIRECTIVE_ACTIONS,
  ACTION_TOOL_DIRECTIVES,
  getActionDirective,
  summarizeWorkflowActions,
  extractImportedSkillState,
  resolveEffectiveImportedSkillState,
  resolveImportedSkillStateForContext,
  renderHelperSkillGuidance,
  renderRuntimeOwnedCapabilities,
  buildAidocsExecutionPrompt,
  buildPromptContext,
  buildCompactionContext,
  buildSessionStartContext,
}
