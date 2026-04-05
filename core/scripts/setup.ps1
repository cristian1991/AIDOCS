param(
  [string]$RootPath
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RootPath -or $RootPath.Trim() -eq "") {
  $RootPath = Split-Path -Parent (Split-Path -Parent $scriptDir)
}

function Resolve-RepoAndCoreRoots {
  param([string]$CandidateRoot)

  $resolved = (Resolve-Path $CandidateRoot).Path
  $parent = Split-Path -Parent $resolved
  $candidates = @($resolved)
  if ($parent) {
    $candidates += $parent
  }

  foreach ($candidate in $candidates) {
    $coreCandidate = Join-Path $candidate "core"
    if ((Test-Path (Join-Path $candidate "mcp\server")) -and (Test-Path (Join-Path $coreCandidate "plugins\aidocs.js"))) {
      return [pscustomobject]@{
        ProjectRoot = (Resolve-Path $candidate).Path
        CoreRoot = (Resolve-Path $coreCandidate).Path
      }
    }
  }

  if ((Test-Path (Join-Path $resolved "plugins\aidocs.js")) -and $parent -and (Test-Path (Join-Path $parent "mcp\server"))) {
    return [pscustomobject]@{
      ProjectRoot = (Resolve-Path $parent).Path
      CoreRoot = (Resolve-Path $resolved).Path
    }
  }

  throw "Could not resolve repo root and core root from: $CandidateRoot"
}

$roots = Resolve-RepoAndCoreRoots -CandidateRoot $RootPath
$projectRoot = $roots.ProjectRoot
$coreRoot = $roots.CoreRoot

$indexFile = Join-Path $projectRoot ".MEMORY\.aidocs\index.aidocs"
if (-not (Test-Path $indexFile)) {
  throw ".MEMORY/.aidocs/index.aidocs not found at repo root: $projectRoot"
}

$versionFile = Join-Path $projectRoot ".MEMORY\.aidocs\command-pack.version"
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
$opencodeActionHooksDir = Join-Path $opencodeDir "action_hooks"
$opencodeSettingsPath = if (Test-Path (Join-Path $opencodeDir "opencode.jsonc")) {
  Join-Path $opencodeDir "opencode.jsonc"
} else {
  Join-Path $opencodeDir "opencode.json"
}
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$claudeCommandsDir = Join-Path $claudeDir "commands"
$claudeSettingsPath = Join-Path $claudeDir "settings.json"

New-Item -ItemType Directory -Force -Path $opencodeDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodeCommandsDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodePluginsDir | Out-Null
New-Item -ItemType Directory -Force -Path $opencodeActionHooksDir | Out-Null
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

function Write-AgentFileWithBackup {
  param(
    [string]$TargetPath,
    [string]$TemplatePath,
    [string]$AidocsPath,
    [string]$StopHeader
  )

  # Read and substitute template
  $templateContent = [System.IO.File]::ReadAllText($TemplatePath, $utf8NoBom)
  $managed = $templateContent.Replace("{{AIDOCS_PATH}}", $AidocsPath).Replace("{{STOP_HEADER}}", $StopHeader)

  if (Test-Path $TargetPath) {
    $existing = [System.IO.File]::ReadAllText($TargetPath, $utf8NoBom)

    # Extract user section (everything after AIDOCS:END)
    $endTag = "<!-- AIDOCS:END -->"
    $endIdx = $existing.IndexOf($endTag)
    if ($endIdx -ge 0) {
      $userSection = $existing.Substring($endIdx + $endTag.Length).TrimStart("`r", "`n")
    } else {
      # No tags — entire file is user content, preserve it all
      $userSection = $existing
    }

    # Backup before overwrite
    $backupPath = $TargetPath + ".backup"
    [System.IO.File]::WriteAllText($backupPath, $existing, $utf8NoBom)

    # Merge: AIDOCS managed section + user section
    if ($userSection.Trim()) {
      $finalContent = $managed + "`n" + $userSection
    } else {
      $finalContent = $managed
    }
  } else {
    $finalContent = $managed
  }

  [System.IO.File]::WriteAllText($TargetPath, $finalContent, $utf8NoBom)
}

$agentsTemplate = Join-Path $coreRoot "templates\global-agents.md.tmpl"
$claudeTemplate = Join-Path $coreRoot "templates\global-claude.md.tmpl"

Write-AgentFileWithBackup -TargetPath (Join-Path $opencodeDir "AGENTS.md") -TemplatePath $agentsTemplate -AidocsPath $projectRoot -StopHeader $header
Write-AgentFileWithBackup -TargetPath (Join-Path $claudeDir "CLAUDE.md") -TemplatePath $claudeTemplate -AidocsPath $projectRoot -StopHeader $header

# ── Smart file installation via manifest-aware Python installer ──
$installScript = Join-Path $scriptDir "install_files.py"

Write-Host "`nInstalling plugin files..."
& python $installScript $projectRoot $coreRoot $opencodePluginsDir "plugin"

Write-Host "`nInstalling action tokens..."
$actionTokensRoot = Join-Path $projectRoot "action_tokens"
if (-not (Test-Path $actionTokensRoot)) {
  $actionTokensRoot = Join-Path $projectRoot "mcp\server\aidocs_mcp\action_tokens"
}
$pluginActionTokensDir = Join-Path $opencodePluginsDir "action_tokens"
& python $installScript $projectRoot $coreRoot $pluginActionTokensDir "action_tokens"

Write-Host "`nInstalling action hooks..."
& python $installScript $projectRoot $coreRoot $opencodeActionHooksDir "action_hooks"

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

  # Use venv Python if available, otherwise system Python
  $venvPythonPath = if ($IsLinux -or $IsMacOS) { Join-Path $venvDir "bin/python" } else { Join-Path $venvDir "Scripts\python.exe" }
  if (Test-Path $venvPythonPath) {
    $pythonExecutable = $venvPythonPath
  } else {
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
  }

  $mcpServerPath = Join-Path $projectRoot "mcp\server"
  $aidocsMcpEntry = [pscustomobject]@{
    type = "local"
    enabled = $true
    timeout = 120000
    command = @($pythonExecutable, "-m", "aidocs_mcp.mcp_server")
    environment = [pscustomobject]@{
      PYTHONPATH = $mcpServerPath
    }
  }

$opencodeSettings.mcp | Add-Member -Force -NotePropertyName aidocs -NotePropertyValue $aidocsMcpEntry

$opencodeSettingsJson = $opencodeSettings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($opencodeSettingsPath, $opencodeSettingsJson, $utf8NoBom)

$claudeHookScript = Join-Path $coreRoot "scripts\claude-hook.ps1"
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
$sessionStartHookGroup = [pscustomobject]@{
  hooks = @(
    [pscustomobject]@{
      type = "command"
      shell = "powershell"
      command = $claudeHookCommand
      timeout = 30
      statusMessage = "AIDOCS startup routing"
    }
  )
}
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

$sessionStartGroups = Remove-AidocsHookGroups (Normalize-HookGroups $claudeSettings.hooks.SessionStart)
$userPromptGroups = Remove-AidocsHookGroups (Normalize-HookGroups $claudeSettings.hooks.UserPromptSubmit)
$preToolGroups = Remove-AidocsHookGroups (Normalize-HookGroups $claudeSettings.hooks.PreToolUse)

$claudeSettings.hooks | Add-Member -Force -NotePropertyName SessionStart -NotePropertyValue @($sessionStartGroups + $sessionStartHookGroup)
$claudeSettings.hooks | Add-Member -Force -NotePropertyName UserPromptSubmit -NotePropertyValue @($userPromptGroups + $userPromptHookGroup)
$claudeSettings.hooks | Add-Member -Force -NotePropertyName PreToolUse -NotePropertyValue @($preToolGroups + $preToolHookGroup)

$claudeSettingsJson = $claudeSettings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($claudeSettingsPath, $claudeSettingsJson, $utf8NoBom)

# ── Disable CC auto-memory for AIDOCS project — memory_capture is the only path ──
$projectClaudeDir = Join-Path $projectRoot ".claude"
New-Item -ItemType Directory -Force -Path $projectClaudeDir | Out-Null
$localSettingsPath = Join-Path $projectClaudeDir "settings.local.json"
if (Test-Path $localSettingsPath) {
  $localSettings = [System.IO.File]::ReadAllText($localSettingsPath) | ConvertFrom-Json
} else {
  $localSettings = [pscustomobject]@{}
}
if ($localSettings.autoMemoryEnabled -ne $false) {
  $localSettings | Add-Member -Force -NotePropertyName autoMemoryEnabled -NotePropertyValue $false
  $localSettingsJson = $localSettings | ConvertTo-Json -Depth 10
  [System.IO.File]::WriteAllText($localSettingsPath, $localSettingsJson, $utf8NoBom)
  Write-Host "Disabled CC auto-memory in $localSettingsPath"
}

$skipGlobalCommands = @("doctor.md")

$sharedCommandsDir = Join-Path $coreRoot ".commands"

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
if (Test-Path $actionHooksRoot) {
  Get-ChildItem -Path $opencodeActionHooksDir -Filter "*.toml" -File | ForEach-Object {
    Write-Host "-" $_.FullName
  }
}
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

# ── Auto-install MCP runtime into ~/.aidocs/venv ──
$aidocsHome = Join-Path $env:USERPROFILE ".aidocs"
$venvDir = Join-Path $aidocsHome "venv"
$mcpPackageDir = Join-Path $projectRoot "mcp"

if (Test-Path (Join-Path $mcpPackageDir "pyproject.toml")) {
  if (-not (Test-Path $venvDir)) {
    Write-Host "Creating AIDOCS MCP venv at $venvDir..."
    & python -m venv $venvDir
  }

  $venvPython = if ($IsLinux -or $IsMacOS) { Join-Path $venvDir "bin/python" } else { Join-Path $venvDir "Scripts\python.exe" }
  $venvPip = if ($IsLinux -or $IsMacOS) { Join-Path $venvDir "bin/pip" } else { Join-Path $venvDir "Scripts\pip.exe" }

  if (Test-Path $venvPython) {
    Write-Host "Installing AIDOCS MCP runtime..."
    & $venvPip install -e $mcpPackageDir --quiet 2>$null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "MCP runtime installed successfully."
    } else {
      Write-Host "WARNING: MCP runtime install failed (exit $LASTEXITCODE). You may need to install manually: cd mcp && pip install -e ."
    }
  }
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

Write-Host "AIDOCS source wired to:" $projectRoot
Write-Host "AIDOCS core assets wired to:" $coreRoot
Write-Host "Command pack version:" $commandPackVersion

# ── Create desktop shortcut ──
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "AIDOCS.lnk"
$launchScript = Join-Path $coreRoot "scripts\launch-dashboard.cmd"
$iconSource = Join-Path $projectRoot "apps\aidocs-dashboard\src-tauri\icons\icon.ico"
$logoSvg = Join-Path $projectRoot "docs\assets\cn-logo.svg"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launchScript
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "AIDOCS Dashboard"
if (Test-Path $iconSource) {
  $shortcut.IconLocation = $iconSource
}
$shortcut.Save()
Write-Host "Desktop shortcut created: $shortcutPath"

# ── Create aidocs-dashboard shell command ──
$aidocsBinDir = Join-Path $env:USERPROFILE ".aidocs\bin"
New-Item -ItemType Directory -Force -Path $aidocsBinDir | Out-Null
$dashCmd = Join-Path $aidocsBinDir "aidocs-dashboard.cmd"
$dashCmdContent = "@echo off`r`ncall `"$launchScript`" %*"
Set-Content -Path $dashCmd -Value $dashCmdContent -Encoding UTF8
# Add to PATH if not already there
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$aidocsBinDir*") {
  [Environment]::SetEnvironmentVariable("PATH", "$userPath;$aidocsBinDir", "User")
  $env:PATH = "$env:PATH;$aidocsBinDir"
  Write-Host "Added $aidocsBinDir to PATH"
}
Write-Host "Shell command: aidocs-dashboard"

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
