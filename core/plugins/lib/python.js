/**
 * Python bridge — spawn Python subprocesses, resolve runtime paths.
 */
const fsSync = require("node:fs")
const path = require("node:path")
const childProcess = require("node:child_process")

// The interpreter AIDOCS installed for itself. Preferred over a bare `python`
// from PATH because PATH is not a contract: whatever answers to `python` in the
// spawning process's environment may be a different interpreter entirely, and
// this bridge needs one that can import aidocs_mcp AND its extension modules.
//
// Measured 2026-08-14: under the test venv's PATH, bare `python` resolved to a
// uv-managed CPython whose `_sqlite3` DLL would not load
// ("ImportError: DLL load failed while importing _sqlite3"). Every bridge call
// then returned null, the plugin lost ALL AIDOCS state — managed mode, skills,
// prompt context — and reported nothing, because both spawn helpers swallow
// failure. 21 host tests failed on it and read as plugin logic bugs.
function resolveRuntimeVenvPython() {
  const home = process.env.USERPROFILE || process.env.HOME || ""
  if (!home) return ""
  const base = path.join(home, ".aidocs", "runtime", "venv")
  const candidates = process.platform === "win32"
    ? [path.join(base, "Scripts", "python.exe")]
    : [path.join(base, "bin", "python3"), path.join(base, "bin", "python")]
  for (const candidate of candidates) {
    try {
      if (fsSync.existsSync(candidate)) return candidate
    } catch { /* unreadable candidate is simply not a candidate */ }
  }
  return ""
}

function resolvePythonBin() {
  // AIDOCS_PYTHON is the explicit operator override and always wins. The AIDOCS
  // runtime venv comes next: it is the interpreter this product provisioned and
  // depends on, so it is a stronger guarantee than the generic PYTHON var or a
  // bare PATH lookup, both of which remain as fallbacks.
  return (
    process.env.AIDOCS_PYTHON ||
    resolveRuntimeVenvPython() ||
    process.env.PYTHON ||
    "python"
  )
}

// A bridge that fails silently is indistinguishable from a project with no
// AIDOCS state, which is exactly how the interpreter fault above stayed
// invisible. Announce the FIRST failure per process on stderr — opencode shows
// it, tests ignore it (they parse stdout), and it never repeats or throws.
let _bridgeFaultAnnounced = false
function announceBridgeFault(pythonBin, detail) {
  if (_bridgeFaultAnnounced) return
  _bridgeFaultAnnounced = true
  try {
    console.error(
      `[aidocs] python bridge unavailable via ${pythonBin || "python"} — ` +
      `AIDOCS state is degraded for this session. ` +
      `Set AIDOCS_PYTHON to a working interpreter. Detail: ${String(detail || "").slice(0, 400)}`,
    )
  } catch { /* never let diagnostics break the host */ }
}

function mergePythonPath(existing, extraPath) {
  if (!extraPath) return existing || ""
  if (!existing) return extraPath
  return `${extraPath}${path.delimiter}${existing}`
}

function readAidocsSourceRoot() {
  const agentsPath = path.join(__dirname, "..", "..", "AGENTS.md")
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
  if (process.env.AIDOCS_PATH) candidates.push(process.env.AIDOCS_PATH)
  const sourceRoot = readAidocsSourceRoot()
  if (sourceRoot) candidates.push(sourceRoot)
  candidates.push(path.resolve(__dirname, "..", "..", ".."))

  for (const candidate of candidates) {
    try {
      const runtimePath = path.join(candidate, "mcp", "server", "aidocs_mcp", "runtime_service.py")
      if (fsSync.existsSync(runtimePath)) return candidate
    } catch { /* ignore */ }
  }
  return ""
}

function runPythonJson(projectRoot, args) {
  const pythonBin = resolvePythonBin()
  const sourceRoot = resolveAidocsRuntimeSourceRoot()
  const pythonPath = process.env.AIDOCS_MCP_PATH || (sourceRoot ? path.join(sourceRoot, "mcp", "server") : "")
  // Defense-in-depth (Phoenix 2026-05-12): strip AIDOCS_PROJECT_ROOT
  // from the spawn env so Python's discover_project_root resolves
  // against the actual cwd, not whatever the operator has exported
  // globally. Without this, opencode in a non-AIDOCS project leaks
  // into AIDOCS state.
  const childEnv = { ...process.env }
  delete childEnv.AIDOCS_PROJECT_ROOT
  childEnv.PYTHONPATH = mergePythonPath(process.env.PYTHONPATH, pythonPath)
  try {
    const result = childProcess.spawnSync(pythonBin, args, {
      encoding: "utf8",
      cwd: String(projectRoot),
      env: childEnv,
      timeout: 5000,
    })
    if (result.status === 0 && result.stdout) return JSON.parse(result.stdout.trim())
    announceBridgeFault(pythonBin, result.stderr || result.error || `status=${result.status}`)
  } catch (err) { announceBridgeFault(pythonBin, err && err.message) }
  return null
}

function runPythonGuardText(projectRoot, text) {
  if (typeof text !== "string" || !text) return null
  return runPythonJson(projectRoot, ["-c", [
    "import json",
    "from aidocs_mcp.output_guard import scan_text",
    "text = json.loads(" + JSON.stringify(JSON.stringify(String(text))) + ")",
    "result = scan_text(text, redact=True)",
    'print(json.dumps({"clean": result.clean, "redacted_text": result.redacted_text, "redaction_count": result.redaction_count}))',
  ].join(";")])
}

module.exports = {
  resolveRuntimeVenvPython,
  announceBridgeFault,
  resolvePythonBin,
  mergePythonPath,
  readAidocsSourceRoot,
  resolveAidocsRuntimeSourceRoot,
  runPythonJson,
  runPythonGuardText,
}
