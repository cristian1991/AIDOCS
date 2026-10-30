/**
 * Python bridge — spawn Python subprocesses, resolve runtime paths.
 */
const fsSync = require("node:fs")
const path = require("node:path")
const childProcess = require("node:child_process")
// The interpreter AIDOCS owns, preferred over a bare `python` from PATH because
// PATH is not a contract (measured 2026-08-14: bare `python` resolved to a
// uv-managed CPython whose `_sqlite3` DLL would not load, every bridge call
// returned null, and the plugin silently lost ALL AIDOCS state).
//
// #1202: "the interpreter AIDOCS owns" is the CURRENT runtime GENERATION's,
// read from runtime/current.json at call time. This used to be the legacy
// ~/.aidocs/runtime venv, which kept serving this bridge a package dated
// 2026-09-28 for a month after generations replaced it. A missing or broken
// pointer is announced on stderr and NEVER falls back to that tree. The id
// shape is runtime_generations.CURRENT_GENERATION_ID_PATTERN, held in lockstep
// by tests/host/test_launchers_current_generation_1202.py.
const CURRENT_GENERATION_ID = /^gen-[0-9]{8}T[0-9]{6}Z-[0-9a-f]+$/
let _generationFaultAnnounced = false
function announceGenerationFault(why) {
  if (_generationFaultAnnounced) return
  _generationFaultAnnounced = true
  try {
    console.error(
      `[aidocs] no current AIDOCS runtime generation (${why}); the legacy venv is never used. ` +
      "Run `aidocs runtime --fix`, or set AIDOCS_PYTHON.",
    )
  } catch { /* never let diagnostics break the host */ }
}

function resolveCurrentGenerationPython() {
  const home = process.env.USERPROFILE || process.env.HOME || ""
  const runtimeRoot = process.env.AIDOCS_RUNTIME_ROOT || (home ? path.join(home, ".aidocs", "runtime") : "")
  if (!runtimeRoot) return ""
  const pointer = path.join(runtimeRoot, "current.json")
  let raw
  try {
    raw = fsSync.readFileSync(pointer, "utf8")
  } catch {
    announceGenerationFault(`no activation pointer at ${pointer}`)
    return ""
  }
  let gid
  try {
    gid = JSON.parse(raw).generation_id
  } catch {
    gid = undefined
  }
  if (typeof gid !== "string" || !CURRENT_GENERATION_ID.test(gid)) {
    announceGenerationFault(`activation pointer ${pointer} is malformed or names an invalid generation id`)
    return ""
  }
  const gen = path.join(runtimeRoot, "generations", gid)
  try {
    if (!fsSync.existsSync(path.join(gen, "generation.complete.json"))) {
      announceGenerationFault(`generation ${gid} is not on disk or was never sealed`)
      return ""
    }
  } catch {
    return ""
  }
  const venv = path.join(gen, "venv")
  const candidates = process.platform === "win32"
    ? [path.join(venv, "Scripts", "python.exe")]
    : [path.join(venv, "bin", "python3"), path.join(venv, "bin", "python")]
  for (const candidate of candidates) {
    try {
      if (fsSync.existsSync(candidate)) return candidate
    } catch { /* unreadable candidate is simply not a candidate */ }
  }
  announceGenerationFault(`generation ${gid} has no python`)
  return ""
}

function resolvePythonBin() {
  // AIDOCS_PYTHON is the explicit operator override and always wins. Otherwise
  // the CURRENT sealed runtime generation (#1202), or NOTHING: "" makes every
  // caller's spawn fail into its own unreachable path (a deny on fail-closed
  // events), and resolveCurrentGenerationPython has already announced why.
  // There is deliberately no PYTHON-env or bare-PATH fallback: an ambient
  // python can import a stale aidocs_mcp, which is the #1202 defect itself.
  return process.env.AIDOCS_PYTHON || resolveCurrentGenerationPython() || ""
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
  resolveCurrentGenerationPython,
  announceBridgeFault,
  resolvePythonBin,
  mergePythonPath,
  readAidocsSourceRoot,
  resolveAidocsRuntimeSourceRoot,
  runPythonJson,
  runPythonGuardText,
}
