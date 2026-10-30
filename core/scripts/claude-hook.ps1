param()

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Split-Path -Parent $scriptDir
$repoRoot = Split-Path -Parent $runtimeRoot
$serverRoot = Join-Path $repoRoot "mcp\server"

if (-not (Test-Path $serverRoot)) {
  throw "AIDOCS MCP server package not found: $serverRoot"
}

if ($env:PYTHONPATH -and $env:PYTHONPATH.Trim() -ne "") {
  $env:PYTHONPATH = "$serverRoot;$($env:PYTHONPATH)"
} else {
  $env:PYTHONPATH = $serverRoot
}

# Claude Code hooks run with a minimal PATH; a bare `python` that silently
# resolves to a Windows Store alias (or fails to resolve at all) makes the
# hook a no-op. Probe for a usable interpreter and emit a loud hook message
# to stderr so the operator sees the root cause instead of "nothing
# happened".
$pythonCmd = $null
foreach ($candidate in @("py", "python", "python3")) {
  $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
  if ($resolved) {
    $pythonCmd = $resolved.Source
    break
  }
}

if (-not $pythonCmd) {
  [Console]::Error.WriteLine("AIDOCS hook: no python interpreter found on PATH. Re-run 'aidocs setup' or install Python 3.11+.")
  exit 1
}

$inputPayload = [Console]::In.ReadToEnd()
if ($inputPayload -and $inputPayload.Trim() -ne "") {
  $inputPayload | & $pythonCmd -m aidocs_mcp.claude_hook
} else {
  & $pythonCmd -m aidocs_mcp.claude_hook
}

exit $LASTEXITCODE
