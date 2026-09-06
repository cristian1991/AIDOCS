/**
 * Filesystem and session helpers — reading files, listing sessions, computing index status.
 */
const fs = require("node:fs/promises")
const fsSync = require("node:fs")
const path = require("node:path")

async function fileExists(target) {
  try {
    await fs.access(target)
    return true
  } catch {
    return false
  }
}

async function readJsonIfExists(target) {
  if (!(await fileExists(target))) return null
  return JSON.parse(await fs.readFile(target, "utf8"))
}

async function readTextIfExists(target) {
  if (!(await fileExists(target))) return null
  return fs.readFile(target, "utf8")
}

function extractSectionBullet(text, heading) {
  if (typeof text !== "string" || !text.trim()) return ""
  const lines = text.split(/\r?\n/)
  let inSection = false
  for (const raw of lines) {
    const line = String(raw || "")
    if (/^##\s+/.test(line)) {
      if (inSection) break
      inSection = line.trim() === `## ${heading}`
      continue
    }
    if (inSection && /^\s*-\s+/.test(line)) {
      return line.replace(/^\s*-\s+/, "").trim()
    }
  }
  return ""
}

function extractPromptText(parts) {
  const textParts = []
  for (const part of parts || []) {
    if (!part) continue
    if (typeof part === "string") { textParts.push(part); continue }
    if (typeof part.text === "string") { textParts.push(part.text); continue }
    if (typeof part.value === "string") { textParts.push(part.value) }
  }
  return textParts.join("\n").trim()
}

function latestPathMtime(target) {
  if (!target || !fsSync.existsSync(target)) return 0
  const stat = fsSync.statSync(target)
  if (stat.isFile()) return stat.mtimeMs
  let latest = stat.mtimeMs
  for (const entry of fsSync.readdirSync(target, { withFileTypes: true })) {
    latest = Math.max(latest, latestPathMtime(path.join(target, entry.name)))
  }
  return latest
}

async function listSessionSummaries(projectRoot) {
  const sessionRoot = path.join(projectRoot, ".MEMORY", "sessions")
  if (!(await fileExists(sessionRoot))) return []
  const entries = await fs.readdir(sessionRoot, { withFileTypes: true })
  const sessions = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const sessionFile = path.join(sessionRoot, entry.name, "SESSION.md")
    const text = await readTextIfExists(sessionFile)
    if (!text) continue
    sessions.push({
      session_id: entry.name,
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
  if (!(await fileExists(indexDb))) return "missing"
  const indexMtime = fsSync.statSync(indexDb).mtimeMs
  const latestMemory = Math.max(
    latestPathMtime(path.join(memoryRoot, "INDEX.md")),
    latestPathMtime(path.join(memoryRoot, ".aidocs")),
    latestPathMtime(path.join(memoryRoot, "sessions")),
    latestPathMtime(path.join(memoryRoot, "rules")),
    // Managed-mode state is sqlite-canonical now; the legacy
    // aidocs-managed.json is inert and no longer a freshness input.
  )
  let latestProject = 0
  const skipDirs = new Set([".git", ".MEMORY", ".pytest_cache", ".venv", "node_modules", "dist", "build", "__pycache__"])
  for (const entry of fsSync.readdirSync(projectRoot, { withFileTypes: true })) {
    if (skipDirs.has(entry.name)) continue
    latestProject = Math.max(latestProject, latestPathMtime(path.join(projectRoot, entry.name)))
  }
  return (latestMemory > indexMtime || latestProject > indexMtime) ? "stale" : "ready"
}

function parseSimpleFrontmatter(text) {
  if (typeof text !== "string" || !text.startsWith("---\n")) return {}
  const end = text.indexOf("\n---\n", 4)
  if (end === -1) return {}
  const result = {}
  for (const rawLine of text.slice(4, end).split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith("#")) continue
    const sep = line.indexOf(":")
    if (sep === -1) continue
    const key = line.slice(0, sep).trim()
    let value = line.slice(sep + 1).trim()
    if (value === "true") value = true
    else if (value === "false") value = false
    result[key] = value
  }
  return result
}

function normalizeCommandName(command) {
  return String(command || "").trim().replace(/^\//, "").toLowerCase()
}

async function readCommandMetadata(commandsDir, commandName) {
  const normalized = normalizeCommandName(commandName)
  if (!normalized) return null
  const text = await readTextIfExists(path.join(commandsDir, `${normalized}.md`))
  if (!text) return null
  return { command_id: normalized, ...parseSimpleFrontmatter(text) }
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

module.exports = {
  fileExists,
  readJsonIfExists,
  readTextIfExists,
  extractSectionBullet,
  extractPromptText,
  latestPathMtime,
  listSessionSummaries,
  computeIndexStatus,
  parseSimpleFrontmatter,
  normalizeCommandName,
  readCommandMetadata,
  takeFirstNonEmpty,
}
