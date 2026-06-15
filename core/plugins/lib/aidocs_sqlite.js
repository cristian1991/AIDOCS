/**
 * Direct read access to the project's big-boss aidocs.sqlite3 from the
 * plugin boot path. Bypasses the MCP server for questions that must
 * resolve before the server is even known to be available — the
 * "is managed mode active?" question that every prompt starts with.
 *
 * Dual-runtime by design:
 *   - Production (OpenCode CLI) runs under Bun -> bun:sqlite (stdlib).
 *   - Tests + dev tooling run under Node 22.5+ -> node:sqlite (stdlib).
 * Either path is zero-install stdlib, so the plugin ships with no
 * npm dependencies and no native-compile step.
 *
 * Read handles are closed in a finally block because Windows pins the
 * WAL file while the handle is open; unclosed handles block pytest
 * teardown and operator `rm -rf` alike.
 */
const path = require("node:path")
const fsSync = require("node:fs")

let _dbFactory = null

function _loadSqlite() {
  if (_dbFactory) return _dbFactory
  if (typeof Bun !== "undefined") {
    // Bun path: require("bun:sqlite").Database is the synchronous API.
    const { Database } = require("bun:sqlite")
    _dbFactory = (file) => new Database(file, { readonly: true })
    return _dbFactory
  }
  try {
    // Node path: node:sqlite ships DatabaseSync for synchronous access.
    // readOnly keeps the plugin strictly non-mutating; plugin reads
    // must never create a new DB if the file disappears between the
    // exists-check and the open.
    const { DatabaseSync } = require("node:sqlite")
    _dbFactory = (file) => new DatabaseSync(file, { readOnly: true })
    return _dbFactory
  } catch (err) {
    // node:sqlite became stable in Node 22.5 (July 2024). Operators on
    // older Node see a clear remediation instead of a cryptic module
    // error; production runs under Bun so this branch is dev-only.
    const e = new Error(
      "AIDOCS plugin requires Bun (production) or Node >= 22.5 (dev). " +
      "node:sqlite is missing on this runtime. Original error: " +
      err.message
    )
    e.code = "AIDOCS_SQLITE_UNAVAILABLE"
    throw e
  }
}

function bigBossDbPath(projectRoot) {
  return path.join(projectRoot, ".MEMORY", ".index", "aidocs.sqlite3")
}

function _withReadDb(projectRoot, callback) {
  const dbPath = bigBossDbPath(projectRoot)
  if (!fsSync.existsSync(dbPath)) return null
  const makeDb = _loadSqlite()
  const db = makeDb(dbPath)
  try {
    return callback(db)
  } finally {
    db.close()
  }
}

/**
 * Read the aidocs_managed row for this project.
 * Returns null when the DB doesn't exist OR the row isn't present —
 * both cases mean "not actively managed" from the plugin's POV.
 */
function readManagedMode(projectRoot) {
  try {
    return _withReadDb(projectRoot, (db) => {
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
    })
  } catch (err) {
    // Table missing (store not yet initialized on this project)
    // surfaces as "no such table". Treat identically to "no row" —
    // managed mode is off until the Python side's init_db runs.
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

/**
 * Read the session_query_gate row for this (project, sessionId).
 * Returns null when the DB doesn't exist OR the row isn't present —
 * both cases mean "no gate state yet" from the plugin's POV.
 * The list/dict fields are stored as JSON-encoded TEXT; we decode
 * them here so callers get native JS shapes.
 */
function readQueryGate(projectRoot, sessionId) {
  if (!sessionId) return null
  try {
    return _withReadDb(projectRoot, (db) => {
      const row = db.prepare(
        "SELECT last_tool, known_exact_paths, current_lane_id, " +
        "lane_exact_paths, lane_allowed_tools, lane_extra_tools, " +
        "lane_raw_tools_granted, user_intent_tools, " +
        "user_intent_bash_subcommands, turn_edited_files, updated_at " +
        "FROM session_query_gate WHERE session_id = ?"
      ).get(sessionId)
      if (!row) return null
      // Mirror the legacy JSON's key names so plugin code that checks
      // gate.known_exact_paths / gate.lane_exact_paths keeps working
      // without any field renames.
      const parseJsonList = (s) => {
        try { return JSON.parse(s || "[]") } catch { return [] }
      }
      const parseJsonObj = (s) => {
        try { return JSON.parse(s || "{}") } catch { return {} }
      }
      return {
        last_tool: row.last_tool,
        known_exact_paths: parseJsonList(row.known_exact_paths),
        current_lane_id: row.current_lane_id,
        lane_exact_paths: parseJsonList(row.lane_exact_paths),
        lane_allowed_tools: parseJsonList(row.lane_allowed_tools),
        lane_extra_tools: parseJsonList(row.lane_extra_tools),
        lane_raw_tools_granted: parseJsonObj(row.lane_raw_tools_granted),
        user_intent_tools: parseJsonList(row.user_intent_tools),
        user_intent_bash_subcommands: parseJsonList(row.user_intent_bash_subcommands),
        turn_edited_files: parseJsonList(row.turn_edited_files),
        updated_at: row.updated_at,
      }
    })
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

/**
 * Read the compiled workflow_actions payload for this project.
 * Returns null when the DB doesn't exist OR the row isn't present —
 * both cases mean "compile_project_rules hasn't run yet" and the
 * plugin should fall through to its no-actions default.
 */
function readWorkflowActions(projectRoot) {
  try {
    return _withReadDb(projectRoot, (db) => {
      const row = db.prepare(
        "SELECT payload FROM workflow_actions WHERE id = 1"
      ).get()
      if (!row) return null
      try {
        return JSON.parse(row.payload || "{}")
      } catch {
        return null
      }
    })
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

/**
 * Read the persisted resolved-config snapshot for this project.
 * Returns { resolved, layers, active_layers, last_updated } or null
 * when the DB / table / row isn't there yet (pre-bootstrap).
 *
 * Replaces the legacy `.MEMORY/config/resolved-config.json` sidecar
 * (migrated 2026-04-20 into resolved_config table). The plugin no
 * longer opens any JSON file to learn effective config.
 */
function readResolvedConfig(projectRoot) {
  try {
    return _withReadDb(projectRoot, (db) => {
      const row = db.prepare(
        "SELECT resolved_json, layers_json, active_layers_json, last_updated " +
        "FROM resolved_config WHERE id = 1"
      ).get()
      if (!row) return null
      const safeParse = (s, fallback) => {
        try { return JSON.parse(s || "") } catch { return fallback }
      }
      return {
        resolved: safeParse(row.resolved_json, {}),
        layers: safeParse(row.layers_json, {}),
        active_layers: safeParse(row.active_layers_json, []),
        last_updated: row.last_updated || null,
      }
    })
  } catch (err) {
    if (err && typeof err.message === "string" && err.message.includes("no such table")) {
      return null
    }
    throw err
  }
}

/**
 * Empire DB path — operator-scoped sqlite at ~/.aidocs/empire.sqlite3
 * (overridable via AIDOCS_EMPIRE_DB env, same shape as Python's
 * intent_tokens_store.empire_db_path).
 */
function empireDbPath() {
  const override = (process.env.AIDOCS_EMPIRE_DB || "").trim()
  if (override) return override
  const home = process.env.USERPROFILE || process.env.HOME || ""
  return path.join(home, ".aidocs", "empire.sqlite3")
}

function _withEmpireReadDb(callback) {
  const dbPath = empireDbPath()
  if (!fsSync.existsSync(dbPath)) return null
  const makeDb = _loadSqlite()
  const db = makeDb(dbPath)
  try {
    return callback(db)
  } finally {
    db.close()
  }
}

/**
 * Read action_token rows from the empire intent_lemma_sets table.
 * Returns Map<action_kind, Set<token>> for the given lang. Used by
 * classify.js to replace the YAML/TOML file walk with a direct
 * sqlite read. Returns null when the empire DB doesn't exist.
 *
 * Phase 6 (2026-05-14): opencode plugin now reads from the same
 * empire sqlite as the Python NLP service, per king-directive
 * "opencode plugin should also use the same sql stuff."
 */
function readActionTokensFromEmpire(lang) {
  return _withEmpireReadDb((db) => {
    try {
      const rows = db.prepare(
        "SELECT parent_key, token FROM intent_lemma_sets " +
        "WHERE lang = ? AND kind = 'action_token'"
      ).all(lang)
      const merged = new Map()
      for (const row of rows) {
        const key = row.parent_key
        if (!merged.has(key)) merged.set(key, new Set())
        merged.get(key).add(row.token)
      }
      return merged
    } catch (err) {
      if (err && typeof err.message === "string" && err.message.includes("no such table")) {
        return null
      }
      throw err
    }
  })
}

/**
 * Enumerate distinct langs present in the empire intent_lemma_sets
 * table. Used by classify.js to know which langs to query when
 * "all" is configured.
 */
function readIntentTokensLangs() {
  return _withEmpireReadDb((db) => {
    try {
      const rows = db.prepare(
        "SELECT DISTINCT lang FROM intent_lemma_sets ORDER BY lang"
      ).all()
      return rows.map((r) => r.lang)
    } catch (err) {
      if (err && typeof err.message === "string" && err.message.includes("no such table")) {
        return []
      }
      throw err
    }
  }) || []
}

module.exports = {
  bigBossDbPath,
  readManagedMode,
  readQueryGate,
  readWorkflowActions,
  readResolvedConfig,
  empireDbPath,
  readActionTokensFromEmpire,
  readIntentTokensLangs,
}

