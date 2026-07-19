/**
 * Action classification — load action tokens, classify user prompts.
 */
const fsSync = require("node:fs")
const path = require("node:path")

const { readAidocsSourceRoot } = require("./python")
const { runPythonJson } = require("./python")
const { loadPluginConfig } = require("./config")
const {
  readActionTokensFromEmpire,
  readIntentTokensLangs,
} = require("./aidocs_sqlite")

let _cachedActionTokens = null

function resolveActionTokensDir() {
  // Phase 6 (2026-05-14): TOMLs moved to package seed dir. Empire
  // sqlite is the primary source; this path is the fallback for
  // installs that haven't migrated yet (no empire DB present).
  const candidates = [
    path.join(__dirname, "..", "intent_tokens"),
    path.join(__dirname, "..", "..", "..", "intent_tokens"),
  ]
  if (process.env.AIDOCS_PATH) candidates.push(path.join(process.env.AIDOCS_PATH, "intent_tokens"))
  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) {
    candidates.push(path.join(sourceRoot, "..", "intent_tokens"))
    candidates.push(path.join(sourceRoot, "mcp", "server", "aidocs_mcp", "seed", "intent_tokens"))
    candidates.push(path.join(sourceRoot, "mcp", "server", "aidocs_mcp", "intent_tokens"))
  }
  for (const candidate of candidates) {
    try { if (fsSync.existsSync(candidate)) return candidate } catch { /* ignore */ }
  }
  return ""
}

function loadActionTokens() {
  if (_cachedActionTokens) return _cachedActionTokens

  const config = loadPluginConfig()
  const langEnabled = (config.languages_enabled || "all").toLowerCase().trim()
  const enabledSet = langEnabled === "all" ? null : new Set(langEnabled.split(",").map((s) => s.trim()).filter(Boolean))
  const merged = new Map()

  // Primary source: empire intent_lemma_sets sqlite. Per Empire-directive
  // 2026-05-14: opencode plugin uses the same sqlite store as the
  // Python NLP service. File-based fallback below kicks in only when
  // the empire DB hasn't been seeded yet (fresh install).
  try {
    const langs = readIntentTokensLangs()
    if (langs && langs.length > 0) {
      const langsToRead = enabledSet
        ? langs.filter((l) => enabledSet.has(l))
        : langs
      for (const lang of langsToRead) {
        const langMap = readActionTokensFromEmpire(lang)
        if (!langMap) continue
        for (const [key, tokens] of langMap.entries()) {
          if (!merged.has(key)) merged.set(key, new Set())
          for (const t of tokens) merged.get(key).add(t)
        }
      }
      if (merged.size > 0) {
        _cachedActionTokens = Array.from(merged.entries()).map(([kind, tokens]) => [kind, Array.from(tokens)])
        return _cachedActionTokens
      }
    }
  } catch { /* fall through to file-based read */ }

  // File-based fallback (legacy / fresh-install pre-seed).
  const dir = resolveActionTokensDir()
  if (!dir) { _cachedActionTokens = []; return _cachedActionTokens }

  let files
  try {
    files = fsSync.readdirSync(dir).filter((f) => {
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
    const content = fsSync.readFileSync(path.join(dir, file), "utf8")
    if (file.endsWith(".toml")) {
      _parseTomlTokens(content, merged)
    } else {
      _parseYamlTokens(content, merged)
    }
  }

  _cachedActionTokens = Array.from(merged.entries()).map(([kind, tokens]) => [kind, Array.from(tokens)])
  return _cachedActionTokens
}


function _parseTomlTokens(content, merged) {
  for (const raw of content.split(/\r?\n/)) {
    const line = raw.trimEnd()
    if (!line || line.trimStart().startsWith("#")) continue
    const match = line.match(/^(\w[\w_]*)\s*=\s*\[(.+)\]\s*$/)
    if (!match) continue
    const key = match[1]
    if (key.startsWith("__")) continue
    const tokens = match[2].match(/"([^"]+)"/g)
    if (!tokens) continue
    if (!merged.has(key)) merged.set(key, new Set())
    for (const t of tokens) merged.get(key).add(t.replace(/"/g, ""))
  }
}

function _parseYamlTokens(content, merged) {
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

function classifyPromptAction(text, projectRoot) {
  const lower = text.trim().toLowerCase()
  if (/^(investigate|debug|diagnose|dig into)\b/.test(lower)) return { action_kind: "investigate", why: "prefix:investigate" }
  if (/^(inspect|examine|audit)\b/.test(lower)) return { action_kind: "inspect", why: "prefix:inspect" }

  const mapping = loadActionTokens()
  if (mapping.length > 0) {
    for (const [actionKind, tokens] of mapping) {
      if (tokens.some((token) => lower.includes(token))) return { action_kind: actionKind, why: `matched:${actionKind}` }
    }
    return { action_kind: "understand", why: "default:understand" }
  }

  // Fallback: delegate to Python
  const result = runPythonJson(projectRoot || process.cwd(), [
    "-c",
    `import json; from aidocs_mcp.intent_guard import classify_action; print(json.dumps(classify_action(${JSON.stringify(text)})))`,
  ])
  if (result && result.action_kind) return { action_kind: result.action_kind, why: `mcp:${result.action_kind}` }
  return { action_kind: "understand", why: "default:understand" }
}

module.exports = {
  resolveActionTokensDir,
  loadActionTokens,
  classifyPromptAction,
}
