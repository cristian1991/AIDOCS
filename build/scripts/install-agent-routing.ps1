param(
  [string]$RootPath
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RootPath -or $RootPath.Trim() -eq "") {
  $RootPath = Split-Path -Parent $scriptDir
}

$root = (Resolve-Path $RootPath).Path
$indexFile = Join-Path $root ".aidocs\index.aidocs"
if (-not (Test-Path $indexFile)) {
  throw ".aidocs/index.aidocs not found at: $root"
}

$opencodeDir = Join-Path $env:USERPROFILE ".config\opencode"
$opencodeCommandsDir = Join-Path $opencodeDir "commands"
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$claudeCommandsDir = Join-Path $claudeDir "commands"

New-Item -ItemType Directory -Force -Path $opencodeDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodeCommandsDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeCommandsDir | Out-Null

$header = "=================== 🛑 S T O P 🛑 ==================="

$globalAgents = @"
# Global AGENTS.md - Cross-Agent Bootstrap

AIDOCS source: $root

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print: $header
- Read only files relevant to the task (do not scan full repo by default).
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.

Routing order:
1) $root\.aidocs\index.aidocs
2) Project-local docs linked by the project router
"@

$globalClaude = @"
# Global CLAUDE.md - Cross-Agent Bootstrap

AIDOCS source: $root

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print: $header
- Read only files relevant to the task (do not scan full repo by default).
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.

Routing order:
1) $root\.aidocs\index.aidocs
2) Project-local docs linked by the project router
"@

Set-Content -Path (Join-Path $opencodeDir "AGENTS.md") -Value $globalAgents -Encoding UTF8
Set-Content -Path (Join-Path $claudeDir "CLAUDE.md") -Value $globalClaude -Encoding UTF8

$commandsDirs = @(
  (Join-Path $root ".opencode\commands"),
  (Join-Path $root ".opencode\command")
)

# Clean target command dirs before copying (removes stale/renamed commands)
Get-ChildItem -Path $opencodeCommandsDir -Filter "*.md" -File -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -Path $claudeCommandsDir -Filter "*.md" -File -ErrorAction SilentlyContinue | Remove-Item -Force

$copied = @{}
foreach ($srcDir in $commandsDirs) {
  if (Test-Path $srcDir) {
    Get-ChildItem -Path $srcDir -Filter "*.md" -File | ForEach-Object {
      $dst = Join-Path $opencodeCommandsDir $_.Name
      Copy-Item -Force $_.FullName $dst
      $copied[$dst] = $true
    }
  }
}

$claudeCommandSrcDir = Join-Path $root ".claude\commands"
$claudeCopied = @{}
if (Test-Path $claudeCommandSrcDir) {
  Get-ChildItem -Path $claudeCommandSrcDir -Filter "*.md" -File | ForEach-Object {
    $dst = Join-Path $claudeCommandsDir $_.Name
    Copy-Item -Force $_.FullName $dst
    $claudeCopied[$dst] = $true
  }
}

Write-Host "Installed global routing files:"
Write-Host "-" (Join-Path $opencodeDir "AGENTS.md")
Write-Host "-" (Join-Path $claudeDir "CLAUDE.md")
foreach ($k in $copied.Keys) {
  Write-Host "-" $k
}
foreach ($k in $claudeCopied.Keys) {
  Write-Host "-" $k
}
Write-Host "AIDOCS source wired to:" $root
