/**
 * Python bridge — spawn Python subprocesses, resolve runtime paths.
 */
const fsSync = require("node:fs")
const path = require("node:path")
const childProcess = require("node:child_process")

function resolvePythonBin() {
  return process.env.AIDOCS_PYTHON || process.env.PYTHON || "python"
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
  } catch { /* Python call failed */ }
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
  resolvePythonBin,
  mergePythonPath,
  readAidocsSourceRoot,
  resolveAidocsRuntimeSourceRoot,
  runPythonJson,
  runPythonGuardText,
}
