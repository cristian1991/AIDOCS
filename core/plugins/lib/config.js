/**
 * Plugin configuration — reads the resolved config from
 * aidocs.sqlite3.resolved_config (single source of truth). Falls back
 * to aidocs-plugin.json for pre-bootstrap projects (no sqlite yet) and
 * to DEFAULTS as a last resort.
 *
 * Migrated 2026-04-20 from .MEMORY/config/resolved-config.json — the
 * JSON sidecar created drift between TOML edits and what the plugin
 * saw. Sqlite is already open for managed-mode + query-gate reads, so
 * one file opened once per event covers everything.
 */
const fsSync = require("node:fs")
const path = require("node:path")
const { readResolvedConfig } = require("./aidocs_sqlite")

let _pluginConfig = null
let _pluginConfigHasProject = false

const DEFAULTS = {
  inject_message_directives: true,
  directive_style: "short",
  disregard_compaction: false,
  startup_context_once: true,
}

function aidocsMemoryConfigPath(projectRoot, fileName) {
  return path.join(projectRoot, ".MEMORY", "config", fileName)
}

function loadResolvedConfig(projectRoot) {
  if (!projectRoot) return null
  try {
    const snapshot = readResolvedConfig(projectRoot)
    if (snapshot && snapshot.resolved) return snapshot.resolved
  } catch {
    // sqlite not yet initialized or locked — fall through to next source.
  }
  return null
}

function loadPluginConfig(projectRoot) {
  if (_pluginConfig && (!projectRoot || _pluginConfigHasProject)) return _pluginConfig

  const resolved = loadResolvedConfig(projectRoot)
  if (resolved) {
    const agent = resolved.agent || {}
    _pluginConfig = {
      ...DEFAULTS,
      inject_message_directives: agent.inject_message_directives ?? DEFAULTS.inject_message_directives,
      directive_style: agent.directive_style ?? DEFAULTS.directive_style,
    }
    _pluginConfigHasProject = !!projectRoot
    return _pluginConfig
  }

  const candidates = [
    path.join(__dirname, "..", "aidocs-plugin.json"),
    path.join(__dirname, "..", "..", "..", "aidocs-plugin.json"),
  ]
  const aidocsPath = process.env.AIDOCS_PATH
  if (aidocsPath) candidates.push(path.join(aidocsPath, "aidocs-plugin.json"))

  for (const candidate of candidates) {
    try {
      if (fsSync.existsSync(candidate)) {
        const raw = JSON.parse(fsSync.readFileSync(candidate, "utf8"))
        _pluginConfig = { ...DEFAULTS, ...raw }
        _pluginConfigHasProject = !!projectRoot
        return _pluginConfig
      }
    } catch { /* ignore */ }
  }

  _pluginConfig = DEFAULTS
  _pluginConfigHasProject = !!projectRoot
  return _pluginConfig
}

module.exports = {
  aidocsMemoryConfigPath,
  loadResolvedConfig,
  loadPluginConfig,
}
