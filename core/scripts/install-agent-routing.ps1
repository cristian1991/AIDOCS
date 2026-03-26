param(
  [string]$RootPath
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RootPath -or $RootPath.Trim() -eq "") {
  $RootPath = Split-Path -Parent $scriptDir
}

$root = (Resolve-Path $RootPath).Path
$projectRoot = $root
$sourceRoot = $root
$buildCandidate = Join-Path $root "build"
if (Test-Path (Join-Path $buildCandidate ".MEMORY\.aidocs\index.aidocs")) {
  $sourceRoot = (Resolve-Path $buildCandidate).Path
}
elseif ($root -like "*\build" -and (Test-Path (Join-Path (Split-Path -Parent $root) "mcp\server"))) {
  $projectRoot = (Resolve-Path (Split-Path -Parent $root)).Path
}

if (-not (Test-Path (Join-Path $projectRoot "mcp\server"))) {
  $candidateParent = Split-Path -Parent $root
  if ($candidateParent -and (Test-Path (Join-Path $candidateParent "mcp\server"))) {
    $projectRoot = (Resolve-Path $candidateParent).Path
  }
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
$opencodePluginsDir = Join-Path $opencodeDir "plugins"
$opencodeSettingsPath = Join-Path $opencodeDir "opencode.json"
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$claudeCommandsDir = Join-Path $claudeDir "commands"
$claudeSettingsPath = Join-Path $claudeDir "settings.json"

New-Item -ItemType Directory -Force -Path $opencodeDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodeCommandsDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodePluginsDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
New-Item -ItemType Directory -Force -Path $claudeCommandsDir | Out-Null

function Normalize-HookGroups {
  param([object]$Value)

  if ($null -eq $Value) {
    return @()
  }

  if ($Value -is [System.Array]) {
    return @($Value)
  }

  return @($Value)
}

function Remove-AidocsHookGroups {
  param([object[]]$Groups)

  $result = @()
  foreach ($group in (Normalize-HookGroups $Groups)) {
    $isAidocsGroup = $false
    foreach ($hook in (Normalize-HookGroups $group.hooks)) {
      if (($hook.command -and $hook.command -match "claude-hook\.ps1") -or ($hook.statusMessage -and $hook.statusMessage -like "AIDOCS *")) {
        $isAidocsGroup = $true
        break
      }
    }
    if (-not $isAidocsGroup) {
      $result += $group
    }
  }

  return ,$result
}

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
- After entering a project, read project `AGENTS.md`/`CLAUDE.md`, then `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/INDEX.md`, then inspect `/.MEMORY/sessions/*/SESSION.md` and read the selected session.
- Durable memory, plans, and task output belong only in project-local `/.MEMORY/**`.
- Spawned-agent plans/investigations belong in the active session under `/.MEMORY/sessions/<session-id>/agents/`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project `AGENTS.md` or `CLAUDE.md` if present
2) Follow the project router (`/.MEMORY/.aidocs/index.aidocs` -> `/.MEMORY/INDEX.md` -> selected `/.MEMORY/sessions/*/SESSION.md`)
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
- After entering a project, read project `AGENTS.md`/`CLAUDE.md`, then `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/INDEX.md`, then inspect `/.MEMORY/sessions/*/SESSION.md` and read the selected session.
- Durable memory, plans, and task output belong only in project-local `/.MEMORY/**`.
- Claude auto-memory `~/.claude/projects/<resolved>/memory/MEMORY.md` is bootstrap-only; never store memory, plans, or task output there.
- Spawned-agent plans/investigations belong in the active session under `/.MEMORY/sessions/<session-id>/agents/`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project `AGENTS.md` or `CLAUDE.md` if present
2) Follow the project router (`/.MEMORY/.aidocs/index.aidocs` -> `/.MEMORY/INDEX.md` -> selected `/.MEMORY/sessions/*/SESSION.md`)
3) If project setup is missing, fall back to $sourceRoot\.MEMORY\.aidocs\index.aidocs
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function New-LinkOrCopy {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Target
  )

  if (Test-Path $Target) {
    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  }

  try {
    New-Item -ItemType SymbolicLink -Path $Target -Target $Source -Force | Out-Null
    return "link"
  } catch {
    Copy-Item -LiteralPath $Source -Destination $Target -Force
    return "copy"
  }
}

[System.IO.File]::WriteAllText((Join-Path $opencodeDir "AGENTS.md"), $globalAgents, $utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $claudeDir "CLAUDE.md"), $globalClaude, $utf8NoBom)

$opencodePluginSource = Join-Path $sourceRoot "plugins\aidocs.js"
if (-not (Test-Path $opencodePluginSource)) {
  throw "Missing OpenCode plugin script: $opencodePluginSource"
}
$opencodePluginTarget = Join-Path $opencodePluginsDir "aidocs.js"
[System.IO.File]::WriteAllText($opencodePluginTarget, [System.IO.File]::ReadAllText($opencodePluginSource), $utf8NoBom)

# Copy plugin config JSON next to plugin
$pluginConfigSource = Join-Path $projectRoot "aidocs-plugin.json"
if (Test-Path $pluginConfigSource) {
  Copy-Item -Path $pluginConfigSource -Destination (Join-Path $opencodePluginsDir "aidocs-plugin.json") -Force
}

# Copy action_tokens next to the plugin so it can find them at runtime
$actionTokensRoot = Join-Path $projectRoot "action_tokens"
if (-not (Test-Path $actionTokensRoot)) {
  $actionTokensRoot = Join-Path $projectRoot "mcp\server\aidocs_mcp\action_tokens"
}
if (-not (Test-Path $actionTokensRoot)) {
  throw "Missing action_tokens directory: $actionTokensRoot"
}

# Copy action_tokens next to plugin (primary runtime path)
$pluginActionTokensDir = Join-Path $opencodePluginsDir "action_tokens"
New-Item -ItemType Directory -Force -Path $pluginActionTokensDir | Out-Null
Get-ChildItem -Path $pluginActionTokensDir -Filter "*.yaml" -File -ErrorAction SilentlyContinue | Remove-Item -Force

$opencodeActionTokenExports = @{}
Get-ChildItem -Path $actionTokensRoot -Filter "*.yaml" -File | ForEach-Object {
  $target = Join-Path $pluginActionTokensDir $_.Name
  Copy-Item -Path $_.FullName -Destination $target -Force
  $opencodeActionTokenExports[$target] = "copy"
}

# Also maintain legacy opencode/ subdir for backward compat
$opencodeActionTokensDir = Join-Path $actionTokensRoot "opencode"
New-Item -ItemType Directory -Force -Path $opencodeActionTokensDir | Out-Null
Get-ChildItem -Path $opencodeActionTokensDir -Filter "*.yaml" -File -ErrorAction SilentlyContinue | Remove-Item -Force

Get-ChildItem -Path $actionTokensRoot -Filter "*.yaml" -File | ForEach-Object {
  $target = Join-Path $opencodeActionTokensDir $_.Name
  $mode = New-LinkOrCopy -Source $_.FullName -Target $target
  $opencodeActionTokenExports[$target] = $mode
}

if (Test-Path $opencodeSettingsPath) {
  $opencodeSettingsRaw = [System.IO.File]::ReadAllText($opencodeSettingsPath)
  $opencodeSettings = if ($opencodeSettingsRaw.Trim()) { $opencodeSettingsRaw | ConvertFrom-Json } else { [pscustomobject]@{} }
} else {
  $opencodeSettings = [pscustomobject]@{}
}

if (-not $opencodeSettings.PSObject.Properties['$schema']) {
  $opencodeSettings | Add-Member -NotePropertyName '$schema' -NotePropertyValue 'https://opencode.ai/config.json'
}

if ((-not $opencodeSettings.PSObject.Properties['mcp']) -or $null -eq $opencodeSettings.mcp) {
  $opencodeSettings | Add-Member -NotePropertyName mcp -NotePropertyValue ([pscustomobject]@{})
}

$pythonExecutable = "python"
try {
  $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
} catch {
  try {
    $pythonExecutable = (Get-Command py -ErrorAction Stop).Source
  } catch {
    $pythonExecutable = "python"
  }
}

$aidocsMcpEntry = [pscustomobject]@{
  type = "local"
  enabled = $true
  timeout = 120000
  command = @($pythonExecutable, "-m", "aidocs_mcp.mcp_server")
  environment = [pscustomobject]@{
    PYTHONPATH = (Join-Path $projectRoot "mcp\server")
  }
}

$opencodeSettings.mcp | Add-Member -Force -NotePropertyName aidocs -NotePropertyValue $aidocsMcpEntry

$opencodeSettingsJson = $opencodeSettings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($opencodeSettingsPath, $opencodeSettingsJson, $utf8NoBom)

$claudeHookScript = Join-Path $sourceRoot "scripts\claude-hook.ps1"
if (-not (Test-Path $claudeHookScript)) {
  throw "Missing Claude hook script: $claudeHookScript"
}

if (Test-Path $claudeSettingsPath) {
  $claudeSettingsRaw = [System.IO.File]::ReadAllText($claudeSettingsPath)
  $claudeSettings = if ($claudeSettingsRaw.Trim()) { $claudeSettingsRaw | ConvertFrom-Json } else { [pscustomobject]@{} }
} else {
  $claudeSettings = [pscustomobject]@{}
}

if ((-not $claudeSettings.PSObject.Properties['hooks']) -or $null -eq $claudeSettings.hooks) {
  $claudeSettings | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
}

$claudeHookCommand = "& '$claudeHookScript'"
$userPromptHookGroup = [pscustomobject]@{
  hooks = @(
    [pscustomobject]@{
      type = "command"
      shell = "powershell"
      command = $claudeHookCommand
      timeout = 30
      statusMessage = "AIDOCS prompt routing"
    }
  )
}
$preToolHookGroup = [pscustomobject]@{
  matcher = "Read|Edit|Write|Glob|Grep|Bash"
  hooks = @(
    [pscustomobject]@{
      type = "command"
      shell = "powershell"
      command = $claudeHookCommand
      timeout = 30
      statusMessage = "AIDOCS tool guardrails"
    }
  )
}

$userPromptGroups = Remove-AidocsHookGroups (Normalize-HookGroups $claudeSettings.hooks.UserPromptSubmit)
$preToolGroups = Remove-AidocsHookGroups (Normalize-HookGroups $claudeSettings.hooks.PreToolUse)

$claudeSettings.hooks | Add-Member -Force -NotePropertyName UserPromptSubmit -NotePropertyValue @($userPromptGroups + $userPromptHookGroup)
$claudeSettings.hooks | Add-Member -Force -NotePropertyName PreToolUse -NotePropertyValue @($preToolGroups + $preToolHookGroup)

$claudeSettingsJson = $claudeSettings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($claudeSettingsPath, $claudeSettingsJson, $utf8NoBom)

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
Write-Host "-" $opencodeSettingsPath
Write-Host "-" $opencodePluginTarget
Write-Host "-" (Join-Path $claudeDir "CLAUDE.md")
Write-Host "-" $claudeSettingsPath
foreach ($k in $copied.Keys) {
  Write-Host "-" $k
}
foreach ($k in $claudeCopied.Keys) {
  Write-Host "-" $k
}
foreach ($k in $opencodeActionTokenExports.Keys) {
  Write-Host "-" $k "(" $opencodeActionTokenExports[$k] ")"
}
# Set AIDOCS_PATH as a persistent user environment variable
$currentAidocsPath = [System.Environment]::GetEnvironmentVariable("AIDOCS_PATH", "User")
if ($currentAidocsPath -ne $projectRoot) {
  [System.Environment]::SetEnvironmentVariable("AIDOCS_PATH", $projectRoot, "User")
  $env:AIDOCS_PATH = $projectRoot
  Write-Host "Set AIDOCS_PATH=$projectRoot (user env, persisted)"
} else {
  Write-Host "AIDOCS_PATH already set to $projectRoot"
}

Write-Host "AIDOCS source wired to:" $sourceRoot
Write-Host "Command pack version:" $commandPackVersion

$requiredCommandFiles = @(
  "aidocs.md",
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
