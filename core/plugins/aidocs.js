/**
 * AIDOCS OpenCode Plugin — orchestrates AIDOCS managed mode for OpenCode hosts.
 *
 * Architecture: thin orchestrator that delegates to focused modules in ./lib/:
 *   - session.js  — filesystem helpers, session listing, index status
 *   - config.js   — plugin configuration loading
 *   - python.js   — Python subprocess bridge
 *   - state.js    — AIDOCS state resolution (managed mode, sessions, host state)
 *   - classify.js — action token loading, prompt classification
 *   - gate.js     — read gate, path normalization, native tool recording
 *   - context.js  — prompt context building, message directives, compaction
 */
const fsSync = require("node:fs")
const path = require("node:path")

// Phoenix 2026-05-12: module-eval probe removed after #168 root cause
// found and fixed. The fix: register the plugin in `opencode.jsonc`'s
// `plugin` array so opencode actually evaluates the module (vs
// auto-discovery from ~/.config/opencode/plugins/*.js, which logs
// "loading plugin" but doesn't actually load).

const { extractPromptText, readCommandMetadata, normalizeCommandName } = require("./lib/session")
const { loadPluginConfig } = require("./lib/config")
const py = require("./lib/python")
const { runPythonJson, runPythonGuardText } = py
const { resolveAidocsState, resolvePromptHostState } = require("./lib/state")
const { recordNativeToolUse, maybeStampWorkerHostSessionId } = require("./lib/gate")
const { MESSAGE_DIRECTIVE_ACTIONS, getActionDirective, buildAidocsExecutionPrompt, buildPromptContext, buildCompactionContext, buildSessionStartContext, renderHelperSkillGuidance } = require("./lib/context")

// ── THE OPENCODE GATE'S EARLY RETURN (#674) ──────────────────────────────────
//
// A tool absent from this set is NOT GATED AT ALL — `tool.execute.before`
// returns before any verdict, refusal or trace. Silent ungoverned execution.
//
// This is the OpenCode answer to the same question the Claude Code PreToolUse
// matcher answers: "which host tool names must reach the AIDOCS gate". That
// side converged on ONE declaration in #644 —
// `claude_hooks_install.PRETOOLUSE_MATCHER_TOKENS`. JS cannot import a Python
// tuple, so parity here is a CONTRACT TEST that NAMES that symbol rather than
// restating its value:
//     mcp/tests/security/test_opencode_guarded_tools_drift_674.py
// It also pins THIS file against its second copy in
// `mcp/server/aidocs_mcp/data/opencode_plugin.js` — the two are hand-kept
// identical and have drifted before (CHANGELOG.md:2746, "CRITICAL —
// apply_patch missing from OC plugin GUARDED_TOOLS").
//
// #674: the set carried `task` and NOT `agent` — the identical 2026-07-09
// "agents cheat" bypass #644 closed for Claude Code, still open here. The gate
// logic was always ready (tool_gate_service.agent_dispatch_brief fires for
// tool_name in {task, agent}); the event simply never arrived.
//
// ADDING a token here WIDENS governance. REMOVING one disables enforcement for
// that tool, for every OpenCode user, silently.
const GUARDED_TOOLS = new Set([
  "read", "edit", "write", "glob", "grep", "bash", "task",
  // `agent`: this host surfaces the subagent-spawn tool under BOTH names
  // depending on build. Without it the dispatch escapes gating, auditing and
  // parent-linkage. (#674, mirroring the 2026-07-09 CC fix.)
  "agent",
  "multiedit", "patch", "apply_patch", "search", "listdir",
  // `skill`: the 2026-07-30 skill-surface redirect reached the CC matcher only.
  // Read-path; opens no write door.
  "skill",
  "notebookedit", "webfetch",
  // `schedulewakeup`: the force-wakeup guard's own stamp path never fires if
  // this tool bypasses the gate — a catch-22 where every tool is then refused.
  "schedulewakeup",
  // Shell-equivalent host tools (#68). `bash` alone is not the shell surface;
  // pre-fix on the CC side, PowerShell calls bypassed the gate cascade entirely
  // and a verified `Remove-Item -Force` deleted a file with zero gate fire.
  "powershell", "pwsh", "cmd", "wsl", "monitor",
])

// `mcp__.*` is a REGEX on the Claude Code side and `Set.has()` is exact-match,
// so AIDOCS's own MCP tool surface is carried across as a PREFIX rule — a
// different mechanism for the same coverage. Named as such in the contract
// test's REGEX_TOKENS_CARRIED_AS_PREFIXES so it cannot be quietly dropped.
const GUARDED_TOOL_PREFIXES = ["mcp__"]

function isGuardedTool(normalizedTool) {
  if (GUARDED_TOOLS.has(normalizedTool)) return true
  return GUARDED_TOOL_PREFIXES.some((p) => normalizedTool.startsWith(p))
}

const COMMANDS_DIR = path.join(__dirname, "..", ".commands")

// Trivial-prompt detector REMOVED 2026-04-30 (mirrors the Python-side
// claude_hook.py removal). Short replies like "ok"/"yes"/"thanks"
// previously short-circuited the chat.message hook before any
// AIDOCS pipeline ran — a security gap: a malicious user could
// fragment instructions across turns and use a "trivial" reply as
// an unaudited cover turn. Every prompt now flows through the full
// pipeline (grant detect, revoke detect, helper-guidance seed, etc.).

const _TOOL_NAME_ALIASES = new Map([
  ["applypatch", "apply_patch"],
  ["apply-patch", "apply_patch"],
  ["multi_edit", "multiedit"],
  ["multi-edit", "multiedit"],
  ["list_dir", "listdir"],
  ["list-dir", "listdir"],
])

function canonicalToolName(tool) {
  const raw = String(tool || "").trim().toLowerCase()
  if (!raw) return ""
  return _TOOL_NAME_ALIASES.get(raw) || raw
}

// Oversize cap on guarded native output (#296, parity with #235's host-side
// output handling): a huge read result is truncated BEFORE the secret scan,
// so what reaches the host is always a bounded, guarded copy — never the
// whole raw output.
const NATIVE_OUTPUT_GUARD_MAX_CHARS = 262144

function capNativeOutputText(text) {
  if (typeof text !== "string" || text.length <= NATIVE_OUTPUT_GUARD_MAX_CHARS) return text
  const omitted = text.length - NATIVE_OUTPUT_GUARD_MAX_CHARS
  return text.slice(0, NATIVE_OUTPUT_GUARD_MAX_CHARS)
    + `\n[aidocs output guard: truncated ${omitted} chars over the ${NATIVE_OUTPUT_GUARD_MAX_CHARS}-char cap]`
}

function guardNativeToolOutput(value, projectRoot) {
  if (typeof value === "string") {
    const capped = capNativeOutputText(value)
    const guard = runPythonGuardText(projectRoot, capped)
    return guard && guard.redacted_text ? guard.redacted_text : capped
  }
  if (Array.isArray(value)) return value.map((item) => guardNativeToolOutput(item, projectRoot))
  if (!value || typeof value !== "object") return value

  if (typeof value.text === "string") {
    const capped = capNativeOutputText(value.text)
    const guard = runPythonGuardText(projectRoot, capped)
    const guardedText = guard && guard.redacted_text ? guard.redacted_text : capped
    return guardedText === value.text ? value : { ...value, text: guardedText }
  }

  const out = { ...value }
  for (const key of Object.keys(out)) out[key] = guardNativeToolOutput(out[key], projectRoot)
  return out
}

// OpenCode's hook contract is MUTATE-the-output-object-IN-PLACE (exactly like
// the "shell.env" hook below: `output.env.X = ...`). Reassigning the hook's
// local `_output` parameter discards the guarded copy and the host keeps the
// RAW unredacted output (#296 — the #235-class silent bypass). This copies
// the guarded result back onto the SAME object reference the host handed to
// the hook, preserving shape. Returns the value local consumers (native tool
// recording) should use — for a top-level primitive, that is the guarded
// value itself, since a primitive parameter cannot be mutated in place.
function applyGuardedOutputInPlace(hostOutput, guarded) {
  if (guarded === hostOutput) return hostOutput
  if (!hostOutput || typeof hostOutput !== "object") return guarded
  if (Array.isArray(hostOutput) && Array.isArray(guarded)) {
    hostOutput.length = 0
    for (const item of guarded) hostOutput.push(item)
    return hostOutput
  }
  if (!Array.isArray(hostOutput) && guarded && typeof guarded === "object" && !Array.isArray(guarded)) {
    for (const key of Object.keys(hostOutput)) {
      if (!Object.prototype.hasOwnProperty.call(guarded, key)) delete hostOutput[key]
    }
    for (const key of Object.keys(guarded)) hostOutput[key] = guarded[key]
    return hostOutput
  }
  // Shape mismatch (should not happen — the guard is shape-preserving):
  // fail closed by returning the guarded copy for local consumers.
  return guarded
}

async function AIDOCSPlugin(input) {
  const projectRoot = input.worktree || input.directory
  // Gate (Phoenix 2026-05-12, Empire bug report): activate ONLY when the
  // opencode session is rooted in an AIDOCS-managed project, OR when
  // launched as an AIDOCS-spawned Expert (env-driven). Without this
  // gate, opencode in a non-AIDOCS project inherits AIDOCS_PROJECT_ROOT
  // from the operator's shell, the plugin spawns Python host_state
  // which falls back to the env var, and the unrelated project's
  // session ends up routed to AIDOCS's phoenix state.
  const _fsSync = require("node:fs")
  const _path = require("node:path")
  // Marker->sqlite migration: cheap PRE-GATE on the index db file existing.
  // The Python side this plugin spawns is AUTHORITATIVE (it requires the
  // deliberate commission STAMP inside the db) — a rare incidental-db
  // false-positive here just costs one probe that reports unmanaged.
  const _aidocsMarker = _path.join(projectRoot || ".", ".MEMORY", ".index", "aidocs.sqlite3")
  const _isAidocsProject = !!projectRoot && _fsSync.existsSync(_aidocsMarker)
  const _isAidocsExpert = !!(process.env.AIDOCS_EXPERT_LANE_ID || "").trim()
  if (!_isAidocsProject && !_isAidocsExpert) {
    return {}
  }
  // Phoenix 2026-05-09 plugin-load probe: write a witness file on
  // factory invocation. Absolute path under the user's home dir to
  // remove cwd/projectRoot as a variable — confirms factory IS
  // invoked (vs silent path-resolution failure).
  // Remove after #152 investigation closes.
  try {
    const witnessPath = require("path").join(
      require("os").homedir(),
      "aidocs-plugin-witness.log",
    )
    require("fs").appendFileSync(
      witnessPath,
      `${new Date().toISOString()} factory-invoked projectRoot=${projectRoot || "<none>"} env-worker=${process.env.AIDOCS_EXPERT_LANE_ID || "<none>"} cwd=${process.cwd()}\n`,
    )
  } catch (e) {
    // Last-resort: write to /tmp or stderr
    try { console.error("AIDOCS plugin witness failed: " + e.message) } catch {}
  }
  const sessionPromptContext = new Map()
  const sessionClassification = new Map()
  const activeCommandBySession = new Map()
  const activeCommandMetaBySession = new Map()
  const startupInjectedSessions = new Set()

  return {
    // ── Event handling ──

    event: async ({ event }) => {
      try {
        const witnessRoot = projectRoot || process.cwd()
        require("fs").appendFileSync(
          require("path").join(witnessRoot, ".MEMORY", ".runtime", "plugin-load-witness.log"),
          `${new Date().toISOString()} event type=${(event && event.type) || "<none>"}\n`,
        )
      } catch {}
      if (event && event.type === "command.executed" && event.properties && event.properties.sessionID) {
        activeCommandBySession.delete(event.properties.sessionID)
        activeCommandMetaBySession.delete(event.properties.sessionID)
      }
    },

    // ── Chat message (prompt received) ──

    // ── Chat message (prompt received) ──

"chat.message": async ({ sessionID }, output) => {
          // Phoenix 2026-05-09 §VIII: stamp worker's host_session_id
          // (opencode's `ses_...` uuid) into session_lane_agents on
          // first chat.message of this worker process. JS-side flag
          // gates further python spawns. No-op for conductor-side
          // opencode (AIDOCS_EXPERT_LANE_ID env unset).
          maybeStampWorkerHostSessionId(projectRoot, sessionID)

          const promptText = extractPromptText(output.parts)

          const state = await resolveAidocsState(projectRoot, promptText)
        const promptHostState = await resolvePromptHostState(projectRoot, state, promptText)
        const enforcement = state?.hostState?.host_actions?.enforcement
          || promptHostState?.payload?.host_actions?.enforcement
        if (enforcement?.decision === "block" && enforcement?.stage === "prompt") {
          sessionPromptContext.delete(sessionID)
          sessionClassification.delete(sessionID)
          return {
            output: {
              parts: [{ text: String(enforcement.user_message || enforcement.reason || "Read blocked. Use ai_get_lines instead.") }],
            },
          }
        }
        const legacyCredentialBlock = !enforcement && state && state.hostState
          && state.hostState.host_actions
          && state.hostState.host_actions.prompt
          && state.hostState.host_actions.prompt.block
          && typeof state.hostState.host_actions.prompt.block === "object"
          ? state.hostState.host_actions.prompt.block
          : null
        if (legacyCredentialBlock && legacyCredentialBlock.decision === "block") {
          output.parts = [{ type: "text", text: String(legacyCredentialBlock.reason || "Prompt blocked. Run /aidocs first.") }]
          sessionPromptContext.delete(sessionID)
          sessionClassification.delete(sessionID)
          return
        }
        const activeCommand = activeCommandBySession.get(sessionID) || ""
        const activeCommandMeta = activeCommandMetaBySession.get(sessionID) || null

      // Handle /aidocs sent as plain text
      if (!activeCommandBySession.has(sessionID) && promptText.trim().replace(/^\//, "").toLowerCase().startsWith("aidocs")) {
        activeCommandBySession.set(sessionID, "aidocs")
        activeCommandMetaBySession.set(sessionID, await readCommandMetadata(COMMANDS_DIR, "aidocs"))
        sessionPromptContext.set(sessionID, [
          "CRITICAL: The user typed `/aidocs`. This is a SYSTEM COMMAND, not a memory request.",
          "Do NOT store this as a rule, preference, or memory. Do NOT interpret the command text as instructions to remember.",
          buildAidocsExecutionPrompt(),
        ].join(" "))
        return
      }
        const context = buildPromptContext(state, promptText, activeCommand, activeCommandMeta, promptHostState, {
        suppressSessionHelperGuidance: startupInjectedSessions.has(sessionID),
      })

if (context) {
        let finalContext = context
        const firstPromptForSession = !startupInjectedSessions.has(sessionID)
        const config = loadPluginConfig(projectRoot)

        if (config.startup_context_once && state.startupState === "ready" && state.sessionID && firstPromptForSession) {
          const startupBlocks = await buildSessionStartContext(projectRoot, state)
          if (startupBlocks.length) {
            finalContext = [context, "Session start context for this conversation:\n" + startupBlocks.join("\n\n")].join("\n\n")
          }
        }
if (firstPromptForSession) {
          const startupSkillBlocks = renderHelperSkillGuidance(state.importedSkillState)
          if (startupSkillBlocks.length && !finalContext.includes("Active AIDOCS helper skill guidance:")) {
            finalContext = [
              finalContext,
              "Learned AIDOCS helper skills for this conversation:\n" + startupSkillBlocks.join("\n"),
            ].join("\n\n")
          }
          startupInjectedSessions.add(sessionID)
        }
        sessionPromptContext.set(sessionID, finalContext)
        const promptAction = promptHostState && promptHostState.payload
          && promptHostState.payload.host_actions
          && promptHostState.payload.host_actions.prompt
          && typeof promptHostState.payload.host_actions.prompt.action_kind === "string"
          ? promptHostState.payload.host_actions.prompt.action_kind
          : null
        if (state.managed && promptAction) {
          sessionClassification.set(sessionID, promptAction)
        } else {
          sessionClassification.delete(sessionID)
        }
      } else {
        sessionPromptContext.delete(sessionID)
        sessionClassification.delete(sessionID)
      }
    },

    // ── System prompt injection ──

    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const context = sessionPromptContext.get(sessionID)
      if (context) output.system.push(context)
    },

    // ── Compaction ──

    "experimental.session.compacting": async ({ sessionID }, output) => {
      // Rotate agent_memory_epoch via bump_compaction_count so once-
      // per-epoch dedup (helper skills, DNT banners) re-fires on the
      // first post-compaction prompt. 2026-05-03: prior version had
      // a comment claiming this happened "on the server side" but no
      // production code called it. Now actually called below via the
      // python bridge. Also clear the closure-local startup dedup
      // Set so the JS-side once-per-session marker resets too.
      try {
        runPythonJson(projectRoot, ["-c",
          `from pathlib import Path; from aidocs_mcp.agent_memory_epoch import bump_compaction_count; bump_compaction_count(Path(${JSON.stringify(String(projectRoot))}), host_kind="opencode", host_session_id=${JSON.stringify(String(sessionID))})`,
        ])
      } catch (e) { /* best-effort: don't block compaction on bump failure */ }
      startupInjectedSessions.delete(sessionID)
      const config = loadPluginConfig(projectRoot)
      if (!config.disregard_compaction) return
      const state = await resolveAidocsState(projectRoot)
      const blocks = await buildCompactionContext(projectRoot, state.sessionID || sessionID)
      output.prompt = [
        "Ignore the default generic compaction style.",
        "Create a continuation summary that preserves AIDOCS structured state first.",
        "Read and preserve the important information from project memory, roadmap, session plan, handoff, and session journal.",
        "Prioritize current actionable work, blockers, what failed, and next steps over conversational filler.",
        "Do not duplicate long prose if the same information already exists in structured artifacts.",
        "Produce a concise but complete continuation summary for the next agent.",
        ...blocks,
      ].join("\n\n")
      // Reset token counters on compaction.
      // #885: this used to call clear_token_usage, which DELETED from the
      // append-only hash-chained execution_events ledger on every compaction,
      // from a plugin subprocess with no operator and no audit-of-audit. The
      // reset is now an APPEND (a token_usage_reset watermark the token
      // queries floor on): the counter still reads zero, the evidence stays.
      if (state.sessionID) {
        runPythonJson(projectRoot, ["-c",
          `from pathlib import Path; from aidocs_mcp.execution_index_store import ExecutionIndexStore; ExecutionIndexStore().reset_token_usage_counter(Path(${JSON.stringify(String(projectRoot))}), session_id=${JSON.stringify(String(state.sessionID))}, reason="opencode compaction", actor="opencode_plugin")`,
        ])
      }
    },

    // ── Message transform (tool directives + plan continuation) ──

    "experimental.chat.messages.transform": async ({ sessionID }, output) => {
      if (!Array.isArray(output.messages)) return
      const last = output.messages[output.messages.length - 1]
      if (!last || !Array.isArray(last.parts)) return

      // Rewrite /aidocs sent as plain text
      if (activeCommandBySession.get(sessionID) === "aidocs") {
        const text = extractPromptText(last.parts)
        if (text.trim().replace(/^\//, "").toLowerCase().startsWith("aidocs")) {
          last.parts = [{ type: "text", text: buildAidocsExecutionPrompt() }]
        }
        return
      }

      const config = loadPluginConfig(projectRoot)
      if (!config.inject_message_directives) return

      // Plan-aware continuation
      const promptText = extractPromptText(last.parts)
      const trimmed = promptText.replace(/<tool-directive[^>]*>[\s\S]*?<\/tool-directive>/g, "").trim()
      const isContinuation = trimmed.length < 40 && /^(ok|continue|next|go|yes|yep|yeah|sure|do it|keep going|proceed|all of them|perfect|great|nice|good|👍|✅|🚀)/i.test(trimmed)

      let runtimePromptActions = null
      if (isContinuation) {
        const state = await resolveAidocsState(projectRoot, trimmed)
        const promptHostState = await resolvePromptHostState(projectRoot, state, trimmed)
        runtimePromptActions = promptHostState && promptHostState.payload
          && promptHostState.payload.host_actions
          && promptHostState.payload.host_actions.prompt
          && typeof promptHostState.payload.host_actions.prompt === "object"
          ? promptHostState.payload.host_actions.prompt
          : null
        const continuationHint = runtimePromptActions && typeof runtimePromptActions.continuation_hint === "string"
          ? runtimePromptActions.continuation_hint.trim()
          : ""
        if (continuationHint) {
          last.parts.push({ type: "text", text: `\n<plan-continuation>\n${continuationHint}\nContinue implementing. Do not stop to ask — the user confirmed.\n</plan-continuation>` })
        }
      }

      // Inject tool directive
      const actionKind = sessionClassification.get(sessionID)
      if (!actionKind || !MESSAGE_DIRECTIVE_ACTIONS.has(actionKind)) return
      const directive = getActionDirective(actionKind)
      if (directive) {
        last.parts.push({ type: "text", text: `\n<tool-directive action="${actionKind}">\n${directive}\n</tool-directive>` })
      }
    },

    // ── Command interception ──

    "command.execute.before": async ({ command, sessionID }, output) => {
      const normalized = normalizeCommandName(command)
      activeCommandBySession.set(sessionID, normalized)
      activeCommandMetaBySession.set(sessionID, await readCommandMetadata(COMMANDS_DIR, normalized))
      if (normalized === "aidocs") {
        output.parts = [{ type: "text", text: buildAidocsExecutionPrompt() }]
      }
    },

    // ── Tool gate (PreToolUse equivalent) ──

    "tool.execute.before": async (input, output) => {
      const { tool, sessionID } = input || {}
      const normalizedTool = canonicalToolName(tool)
      if (!isGuardedTool(normalizedTool)) return

      const state = await resolveAidocsState(projectRoot)
      if (!state.initialized) return

      // Allow tools during /aidocs command
      const activeCommandMeta = activeCommandMetaBySession.get(sessionID)
      if ((activeCommandMeta && activeCommandMeta.command_id === "aidocs") || activeCommandBySession.get(sessionID) === "aidocs") return

      if (!state.managed) throw new Error("AIDOCS-managed mode is inactive for this initialized project. Run /aidocs first.")

      if (normalizedTool === "read") {
        const hostActions = state && state.hostState && typeof state.hostState.host_actions === "object"
          ? state.hostState.host_actions
          : null
          if (hostActions?.pre_tool?.read_gate_mode === "runtime") {
            const readDecision = runPythonJson(projectRoot, ["-c",
              [
                "import json, sys",
                "from pathlib import Path",
                "from aidocs_mcp.service_hub import AidocsServiceHub",
                "from aidocs_mcp.mcp_server import _require_indexed_read_gate",
                `project_root = Path(${JSON.stringify(String(projectRoot))})`,
                "templates_root = project_root / 'core' / '.MEMORY' / '.aidocs' / 'templates'",
                "hub = AidocsServiceHub(templates_root=templates_root)",
                `tool_input = json.loads(${JSON.stringify(JSON.stringify((output && output.args) || (input && input.args) || {}))})`,
                "exact_path = tool_input.get('filePath') or tool_input.get('path') or ''",
                "result = _require_indexed_read_gate(hub, project_root, exact_path=exact_path, known_exact_path=False)",
                "print(json.dumps(result or {}))",
              ].join(";"),
            ])
            const enforcement = readDecision && typeof readDecision.enforcement === "object"
              ? readDecision.enforcement
              : null
            if (enforcement && enforcement.decision === "block") {
              throw new Error(String(enforcement.user_message || enforcement.reason || "Read blocked. Use ai_get_lines instead."))
            }
            if (!enforcement && readDecision && readDecision.error) {
              throw new Error(String(readDecision.error))
            }
            return
          }
      }

      // Delegate all other tools to AgentOrchestrator.check_tool() — full cascade:
      // raw tool blocking, bash allowlist/denylist, heuristic judge, infrastructure protection
      const toolArgs = (output && output.args) || (input && input.args) || {}
      const decision = runPythonJson(projectRoot, ["-c",
        [
          "import json, sys",
          "from pathlib import Path",
          "from aidocs_mcp.runtime_service import RuntimeService",
          "from aidocs_mcp.service_hub import AidocsServiceHub",
          "from aidocs_mcp.agent_orchestrator import AgentOrchestrator",
          `project_root = Path(${JSON.stringify(String(projectRoot))})`,
          "templates_root = project_root / 'core' / '.MEMORY' / '.aidocs' / 'templates'",
          "runtime = RuntimeService(AidocsServiceHub(templates_root=templates_root))",
          "orch = AgentOrchestrator(runtime)",
          `tool_name = ${JSON.stringify(normalizedTool)}`,
          `tool_input = json.loads(${JSON.stringify(JSON.stringify(toolArgs))})`,
          "d = orch.check_tool(project_root, tool_name, tool_input)",
          'print(json.dumps({"allowed": d.allowed, "reason": d.reason, "advisory": d.advisory}))',
        ].join(";"),
      ])

      if (decision && !decision.allowed) {
        throw new Error(decision.reason || `Tool '${normalizedTool}' blocked. Use AIDOCS tool instead.`)
      }

      // Advisory nudges — inject into session context
      if (decision && decision.advisory) {
        const existing = sessionPromptContext.get(sessionID) || ""
        if (!existing.includes(decision.advisory)) {
          sessionPromptContext.set(sessionID, decision.advisory + (existing ? "\n" + existing : ""))
        }
      }

      // Conductor comms: lane state + message injection
      if (state.sessionID) {
        try {
          const gate = await getQueryGateState(projectRoot, state.sessionID)
          const laneID = (gate && gate.current_lane_id) || "default"
          const commsResult = runPythonJson(projectRoot, ["-c",
            `import json; from pathlib import Path; from aidocs_mcp.conductor_comms import check_lane_and_messages; print(json.dumps(check_lane_and_messages(Path(${JSON.stringify(String(projectRoot))}), ${JSON.stringify(String(laneID))})))`,
          ])
          if (commsResult) {
            if (commsResult.lane_state === "paused") throw new Error(`Lane '${laneID}' is paused: ${commsResult.lane_reason || "paused by conductor"}. Wait for conductor to resume.`)
            if (commsResult.lane_state === "canceled") throw new Error(`Lane '${laneID}' has been canceled. Stop working.`)
            const messages = commsResult.pending_messages || []
            if (messages.length > 0) {
              const msgTexts = messages.map((m) => `>>> CONDUCTOR MESSAGE: ${m.content}`).join("\n")
              const existing = sessionPromptContext.get(sessionID) || ""
              sessionPromptContext.set(sessionID, msgTexts + (existing ? "\n" + existing : ""))
            }
          }
        } catch (err) {
          if (String(err).includes("Lane '")) throw err
        }
      }
    },

    // ── Post-tool recording ──

    "tool.execute.after": async (input, _output) => {
      const { tool, sessionID, args } = input || {}
      const normalizedTool = canonicalToolName(tool)
      const state = await resolveAidocsState(projectRoot)
      if (!state.managed) return
      if (normalizedTool === "read") {
        _output = applyGuardedOutputInPlace(_output, guardNativeToolOutput(_output, projectRoot))
      }
      if (state.sessionID) recordNativeToolUse(projectRoot, state.sessionID, tool, args, _output)
      if (normalizedTool === "edit" || normalizedTool === "write") {
        const existing = sessionPromptContext.get(sessionID) || ""
        if (!existing.includes("task_complete")) {
          sessionPromptContext.set(sessionID, existing + " Remember to call `task_complete` when the edit task is done.")
        }
      }
      // TodoWrite → task_* auto-bridge: invoke python claude_hook with a
      // PostToolUse payload. Python owns the diff + dispatch logic so
      // Claude and OpenCode emit identical lifecycle events. Non-blocking.
      if (normalizedTool === "todowrite" && state.sessionID) {
        try {
          const { spawn } = require("child_process")
          const payload = JSON.stringify({
            hook_event_name: "PostToolUse",
            cwd: projectRoot,
            tool_name: "TodoWrite",
            tool_input: args || {},
          })
          const proc = spawn(
            process.env.AIDOCS_PYTHON || "python",
            ["-m", "aidocs_mcp.claude_hook"],
            { stdio: ["pipe", "ignore", "ignore"], detached: true },
          )
          proc.stdin.end(payload)
          proc.unref()
        } catch (_e) {
          // Fire-and-forget — auto-bridge must never block tool flow.
        }
      }
    },

    // ── Shell environment ──

    "shell.env": async (_input, output) => {
      const state = await resolveAidocsState(projectRoot)
      if (!state.initialized) return
      output.env.AIDOCS_INITIALIZED = "1"
      output.env.AIDOCS_MANAGED_MODE = state.managed ? "1" : "0"
      if (state.sessionID) output.env.AIDOCS_SESSION_ID = state.sessionID
    },
  }
}

// OpenCode v1 plugin format
module.exports = { server: AIDOCSPlugin, id: "aidocs" }

// Test-only access — use a getter to avoid polluting module.exports keys
// (Bun's ESM bridge hoists all own-properties of module.exports as named exports,
// which breaks getLegacyPlugins if non-function values appear)
Object.defineProperty(module.exports, "_test", {
  get() {
    return {
      ...require("./lib/session"),
      ...require("./lib/config"),
      ...require("./lib/python"),
      ...require("./lib/state"),
      ...require("./lib/classify"),
      ...require("./lib/gate"),
      ...require("./lib/context"),
      AIDOCSPlugin,
      canonicalToolName,
      guardNativeToolOutput,
      applyGuardedOutputInPlace,
      NATIVE_OUTPUT_GUARD_MAX_CHARS,
      GUARDED_TOOLS,
      GUARDED_TOOL_PREFIXES,
      isGuardedTool,
    }
  },
  enumerable: false,
  configurable: false,
})
