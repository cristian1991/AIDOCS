param(
  [string]$RootPath
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RootPath -or $RootPath.Trim() -eq "") {
  $RootPath = Split-Path -Parent $scriptDir
}

$root = (Resolve-Path $RootPath).Path
$sourceRoot = $root
$buildCandidate = Join-Path $root "build"
if (Test-Path (Join-Path $buildCandidate ".MEMORY\.aidocs\index.aidocs")) {
  $sourceRoot = (Resolve-Path $buildCandidate).Path
}

$indexFile = Join-Path $sourceRoot ".MEMORY\.aidocs\index.aidocs"
if (-not (Test-Path $indexFile)) {
  throw ".MEMORY/.aidocs/index.aidocs not found at runtime root: $sourceRoot"
}

$versionFile = Join-Path $sourceRoot ".MEMORY\.aidocs\command-pack.version"
$commandPackVersion = "unknown"
if (Test-Path $versionFile) {
  $rawVersion = (Get-Content -Path $versionFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($rawVersion) {
    $commandPackVersion = $rawVersion.Trim()
  }
}

$opencodeDir = Join-Path $env:USERPROFILE ".config\opencode"
$opencodeCommandsDir = Join-Path $opencodeDir "commands"
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$claudeCommandsDir = Join-Path $claudeDir "commands"

New-Item -ItemType Directory -Force -Path $opencodeDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodeCommandsDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeCommandsDir | Out-Null

$header = [System.Char]::ConvertFromUtf32(0x1F6D1) + " STOP"

$globalAgents = @"
# Global AGENTS.md - Cross-Agent Bootstrap

AIDOCS source: $sourceRoot

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print a blank line, then: $header
- Read only files relevant to the task (do not scan full repo by default).
- After entering a project, read project `AGENTS.md`/`CLAUDE.md`, then `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`.
- Durable memory, plans, and task output belong only in project-local `/.MEMORY/**`.
- Spawned-agent plans/investigations belong in `/.MEMORY/agents/YYYY-MM-DD-<topic>-plan.md` or `/.MEMORY/agents/YYYY-MM-DD-<topic>-investigation.md`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project `AGENTS.md` or `CLAUDE.md` if present
2) Follow the project router (`/.MEMORY/.aidocs/index.aidocs` -> `/.MEMORY/NOW.md` -> `/.MEMORY/INDEX.md`)
3) If project setup is missing, fall back to $sourceRoot\.MEMORY\.aidocs\index.aidocs
"@

$globalClaude = @"
# Global CLAUDE.md - Cross-Agent Bootstrap

AIDOCS source: $sourceRoot

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print a blank line, then: $header
- Read only files relevant to the task (do not scan full repo by default).
- After entering a project, read project `AGENTS.md`/`CLAUDE.md`, then `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`.
- Durable memory, plans, and task output belong only in project-local `/.MEMORY/**`.
- Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md` is bootstrap-only; never store memory, plans, or task output there.
- Spawned-agent plans/investigations belong in `/.MEMORY/agents/YYYY-MM-DD-<topic>-plan.md` or `/.MEMORY/agents/YYYY-MM-DD-<topic>-investigation.md`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project `AGENTS.md` or `CLAUDE.md` if present
2) Follow the project router (`/.MEMORY/.aidocs/index.aidocs` -> `/.MEMORY/NOW.md` -> `/.MEMORY/INDEX.md`)
3) If project setup is missing, fall back to $sourceRoot\.MEMORY\.aidocs\index.aidocs
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $opencodeDir "AGENTS.md"), $globalAgents, $utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $claudeDir "CLAUDE.md"), $globalClaude, $utf8NoBom)

$skipGlobalCommands = @("doctor.md")

$sharedCommandsDir = Join-Path $sourceRoot ".commands"

# Clean target command dirs before copying (removes stale/renamed commands)
Get-ChildItem -Path $opencodeCommandsDir -Filter "*.md" -File -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -Path $claudeCommandsDir -Filter "*.md" -File -ErrorAction SilentlyContinue | Remove-Item -Force

$copied = @{}
$claudeCopied = @{}
if (-not (Test-Path $sharedCommandsDir)) {
  throw "Missing shared command source folder: $sharedCommandsDir"
}

Get-ChildItem -Path $sharedCommandsDir -Filter "*.md" -File | ForEach-Object {
  if (-not ($skipGlobalCommands -contains $_.Name)) {
    $raw = [System.IO.File]::ReadAllText($_.FullName)

    $claudeDst = Join-Path $claudeCommandsDir $_.Name
    [System.IO.File]::WriteAllText($claudeDst, $raw, $utf8NoBom)
    $claudeCopied[$claudeDst] = $true

    $opencodeRaw = $raw
    if ($opencodeRaw -match "(?s)^---\r?\n(.*?)\r?\n---") {
      $frontmatter = $matches[1]
      if ($frontmatter -notmatch "(?m)^agent:\s*") {
        $replacement = "---`r`n$frontmatter`r`nagent: build`r`n---"
        $opencodeRaw = [System.Text.RegularExpressions.Regex]::Replace($opencodeRaw, "(?s)^---\r?\n(.*?)\r?\n---", [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement }, 1)
      }
    }

    $opencodeDst = Join-Path $opencodeCommandsDir $_.Name
    [System.IO.File]::WriteAllText($opencodeDst, $opencodeRaw, $utf8NoBom)
    $copied[$opencodeDst] = $true
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
Write-Host "AIDOCS source wired to:" $sourceRoot
Write-Host "Command pack version:" $commandPackVersion

$requiredCommandFiles = @(
  "memstart.md",
  "project-init.md",
  "project-update.md",
  "reingest.md",
  "archive.md",
  "personality.md",
  "clean.md"
)

foreach ($commandName in $requiredCommandFiles) {
  $openCodeTarget = Join-Path $opencodeCommandsDir $commandName
  if (-not (Test-Path $openCodeTarget)) {
    throw "Missing installed OpenCode command: $openCodeTarget"
  }

  $claudeTarget = Join-Path $claudeCommandsDir $commandName
  if (-not (Test-Path $claudeTarget)) {
    throw "Missing installed Claude command: $claudeTarget"
  }
}
