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

$inputPayload = [Console]::In.ReadToEnd()
if ($inputPayload -and $inputPayload.Trim() -ne "") {
  $inputPayload | & python -m aidocs_mcp.claude_hook
} else {
  & python -m aidocs_mcp.claude_hook
}

exit $LASTEXITCODE
