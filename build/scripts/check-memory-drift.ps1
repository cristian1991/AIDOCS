param(
  [Alias("Path")]
  [string]$ScanRoot,
  [ValidateSet("check", "check-legacy", "fix")]
  [string]$Mode = "check",
  [switch]$Interactive
)

$ErrorActionPreference = "Stop"

$ManagedMarker = "<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->"

$skipFragments = @(
  '\.git\',
  '\node_modules\',
  '\bin\',
  '\obj\',
  '\.builder\workspaces\',
  '\coverage\',
  '\dist\'
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceRoot = Split-Path -Parent $scriptDir
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$isLegacyMode = $Mode -eq 'check-legacy'

function Test-SkipPath {
  param(
    [string]$Path
  )

  foreach ($fragment in $skipFragments) {
    if ($Path -like ("*{0}*" -f $fragment)) {
      return $true
    }
  }

  return $false
}

function Select-DriveRoot {
  $drives = Get-PSDrive -PSProvider FileSystem | Sort-Object Name
  if (-not $drives) {
    throw "No filesystem drives found."
  }

  $selectedIndex = 0
  while ($true) {
    Clear-Host
    Write-Host "AIDOCS Memory Update"
    Write-Host "Mode: $Mode"
    Write-Host "Controls: Up/Down = move  Right = enter selected drive  Enter = scan selected drive  Esc = cancel"
    Write-Host ""
    Write-Host "Select a drive:"
    for ($i = 0; $i -lt $drives.Count; $i++) {
      $prefix = if ($i -eq $selectedIndex) { ">" } else { " " }
      Write-Host ("{0} {1}: {2}" -f $prefix, $drives[$i].Name, $drives[$i].Root)
    }

    $key = [Console]::ReadKey($true).Key
    switch ($key) {
      'UpArrow' { if ($selectedIndex -gt 0) { $selectedIndex-- } }
      'DownArrow' { if ($selectedIndex -lt ($drives.Count - 1)) { $selectedIndex++ } }
      'RightArrow' { return $drives[$selectedIndex].Root }
      'Enter' { return $drives[$selectedIndex].Root }
      'Escape' { throw "Selection cancelled." }
    }
  }
}

function Select-ScanRoot {
  $current = Select-DriveRoot
  $selectedIndex = 0

  while ($true) {
    $children = @()
    if (Test-Path -LiteralPath $current -PathType Container) {
      $children = Get-ChildItem -LiteralPath $current -Directory -Force -ErrorAction SilentlyContinue | Sort-Object Name
    }

    if ($selectedIndex -ge $children.Count) {
      $selectedIndex = [Math]::Max(0, $children.Count - 1)
    }

    Clear-Host
    Write-Host "AIDOCS Memory Update"
    Write-Host "Mode: $Mode"
    Write-Host "Controls: Up/Down = move  Right = enter selected folder  Left = go up/back  Enter = scan current folder  Esc = cancel  P = paste full path"
    Write-Host ""
    Write-Host ("Current folder: {0}" -f $current)
    Write-Host "Press Enter to start in the current folder."
    if ($children.Count -gt 0) {
      Write-Host ""
      for ($i = 0; $i -lt $children.Count; $i++) {
        $prefix = if ($i -eq $selectedIndex) { ">" } else { " " }
        Write-Host ("{0} {1}" -f $prefix, $children[$i].Name)
      }
    } else {
      Write-Host ""
      Write-Host "(No child directories)"
    }

    $key = [Console]::ReadKey($true)
    switch ($key.Key) {
      'UpArrow' { if ($selectedIndex -gt 0) { $selectedIndex-- }; continue }
      'DownArrow' { if ($selectedIndex -lt ($children.Count - 1)) { $selectedIndex++ }; continue }
      'RightArrow' {
        if ($children.Count -gt 0) {
          $current = $children[$selectedIndex].FullName
          $selectedIndex = 0
        }
        continue
      }
      'LeftArrow' {
        $parent = Split-Path -Parent $current
        if (-not $parent) {
          $current = Select-DriveRoot
          $selectedIndex = 0
          continue
        }

        $resolvedCurrent = (Resolve-Path -LiteralPath $current).Path
        $isDriveRoot = ($resolvedCurrent.TrimEnd('\') + '\') -eq $resolvedCurrent
        if ($isDriveRoot) {
          $current = Select-DriveRoot
          $selectedIndex = 0
        } elseif (Test-Path -LiteralPath $parent -PathType Container) {
          $childName = Split-Path -Leaf $current
          $current = $parent
          $siblings = Get-ChildItem -LiteralPath $current -Directory -Force -ErrorAction SilentlyContinue | Sort-Object Name
          $matchIndex = 0
          for ($i = 0; $i -lt $siblings.Count; $i++) {
            if ($siblings[$i].Name -eq $childName) {
              $matchIndex = $i
              break
            }
          }
          $selectedIndex = $matchIndex
        }
        continue
      }
      'Enter' { return $current }
      'Escape' { throw "Selection cancelled." }
      default {
        if ($key.KeyChar -in @('p', 'P')) {
          $manualPath = (Read-Host "Enter folder path").Trim()
          if (Test-Path -LiteralPath $manualPath -PathType Container) {
            $current = (Resolve-Path -LiteralPath $manualPath).Path
            $selectedIndex = 0
          } else {
            Write-Host "Path not found."
            Start-Sleep -Milliseconds 700
          }
        }
      }
    }
  }
}

function Ensure-ParentDirectory {
  param(
    [string]$FilePath
  )

  $parent = Split-Path -Parent $FilePath
  if (-not [string]::IsNullOrWhiteSpace($parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
}

function Read-FileUtf8 {
  param(
    [string]$FilePath
  )

  return [System.IO.File]::ReadAllText($FilePath)
}

function Write-FileUtf8 {
  param(
    [string]$FilePath,
    [string]$Content
  )

  Ensure-ParentDirectory -FilePath $FilePath
  [System.IO.File]::WriteAllText($FilePath, $Content, $utf8NoBom)
}

function Split-ManagedContent {
  param(
    [string]$Content
  )

  if ([string]::IsNullOrEmpty($Content)) {
    return @('', '')
  }

  $markerIndex = $Content.IndexOf($ManagedMarker, [System.StringComparison]::Ordinal)
  if ($markerIndex -lt 0) {
    return $null
  }

  $markerLength = $ManagedMarker.Length
  $before = $Content.Substring(0, $markerIndex)
  $afterStart = $markerIndex + $markerLength
  $after = if ($afterStart -lt $Content.Length) { $Content.Substring($afterStart) } else { '' }
  return @($before, $after)
}

function Build-ManagedFileContent {
  param(
    [string]$ManagedContent,
    [string]$ExistingContent
  )

  $managedBody = $ManagedContent.TrimEnd("`r", "`n")
  $split = Split-ManagedContent -Content $ExistingContent
  if ($null -eq $split) {
    if ([string]::IsNullOrWhiteSpace($ExistingContent)) {
      return $managedBody + "`n`n" + $ManagedMarker + "`n"
    }

    $preserved = $ExistingContent.TrimStart("`r", "`n")
    return $managedBody + "`n`n" + $ManagedMarker + "`n`n" + $preserved.TrimEnd("`r", "`n") + "`n"
  }

  $userSection = $split[1].TrimStart("`r", "`n")
  if ([string]::IsNullOrWhiteSpace($userSection)) {
    return $managedBody + "`n`n" + $ManagedMarker + "`n"
  }

  return $managedBody + "`n`n" + $ManagedMarker + "`n`n" + $userSection.TrimEnd("`r", "`n") + "`n"
}

function Sync-ManagedFile {
  param(
    [string]$SourceFile,
    [string]$TargetFile
  )

  $managedContent = Read-FileUtf8 -FilePath $SourceFile
  $existingContent = if (Test-Path -LiteralPath $TargetFile -PathType Leaf) { Read-FileUtf8 -FilePath $TargetFile } else { '' }
  $newContent = Build-ManagedFileContent -ManagedContent $managedContent -ExistingContent $existingContent
  $changed = (-not (Test-Path -LiteralPath $TargetFile -PathType Leaf)) -or ($existingContent -ne $newContent)
  if ($changed) {
    Write-FileUtf8 -FilePath $TargetFile -Content $newContent
  }
  return $changed
}

function Sync-OptionalFileIfMissing {
  param(
    [string]$SourceFile,
    [string]$TargetFile
  )

  if (Test-Path -LiteralPath $TargetFile -PathType Leaf) {
    return $false
  }
  $content = Read-FileUtf8 -FilePath $SourceFile
  Write-FileUtf8 -FilePath $TargetFile -Content $content
  return $true
}

function Sync-CanonicalTree {
  param(
    [string]$SourceDir,
    [string]$TargetDir
  )

  $changes = @()
  Get-ChildItem -LiteralPath $SourceDir -Recurse -Force -ErrorAction Stop | ForEach-Object {
    $relative = $_.FullName.Substring($SourceDir.Length).TrimStart('\')
    $targetPath = Join-Path $TargetDir $relative
    if ($_.PSIsContainer) {
      if (-not (Test-Path -LiteralPath $targetPath -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
        $changes += $targetPath
      }
    } else {
      if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        Ensure-ParentDirectory -FilePath $targetPath
        Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
        $changes += $targetPath
      }
    }
  }
  return $changes
}

function New-MemoryIndexContent {
  param(
    [string]$ProjectRoot
  )

  $memoryRoot = Join-Path $ProjectRoot '.MEMORY'
  $lines = New-Object System.Collections.Generic.List[string]
  $null = $lines.Add('# Memory Index')
  $null = $lines.Add('')
  $null = $lines.Add('Durable-memory router only. Not the session-start entry point.')
  $null = $lines.Add('')
  $null = $lines.Add('Read order:')
  $null = $lines.Add('1. Read `/.MEMORY/.aidocs/index.aidocs` first.')
  $null = $lines.Add('2. Inspect active sessions under `sessions/*/SESSION.md`.')
  $null = $lines.Add('3. Read the selected session''s `SESSION.md`.')
  $null = $lines.Add('4. Only then use this file to open relevant durable-memory files.')
  $null = $lines.Add('')
  $null = $lines.Add('## Adjacent')
  $null = $lines.Add('- [/.MEMORY/.aidocs/index.aidocs](.aidocs/index.aidocs) — session-start router')
  $null = $lines.Add('- [CHANGELOG.md](CHANGELOG.md) — completed work history created by `/archive`')
  $null = $lines.Add('')
  $null = $lines.Add('## Sessions')
  $null = $lines.Add('- `sessions/` — active per-session runtime state')
  $null = $lines.Add('- `archive/sessions/` — completed session history')

  $sectionMap = @(
    @{ Header = 'Rules'; Path = 'rules'; Description = 'rule' },
    @{ Header = 'System'; Path = 'system'; Description = 'system file' },
    @{ Header = 'Config'; Path = 'config'; Description = 'config file' },
    @{ Header = 'Related Projects'; Path = 'related-projects'; Description = 'related-project file' },
    @{ Header = 'Domains'; Path = 'domains'; Description = 'domain file' },
    @{ Header = 'Daily'; Path = 'daily'; Description = 'session log' },
    @{ Header = 'Archive'; Path = 'archive'; Description = 'archived file'; Exclude = '^sessions/' }
  )

  foreach ($section in $sectionMap) {
    $null = $lines.Add('')
    $null = $lines.Add('## ' + $section.Header)
    $rootPath = Join-Path $memoryRoot $section.Path
    $items = @()
    if (Test-Path -LiteralPath $rootPath -PathType Container) {
      $items = Get-ChildItem -LiteralPath $rootPath -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.gitkeep' }
      if ($section.ContainsKey('Exclude')) {
        $pattern = $section.Exclude
        $items = @($items | Where-Object {
          $relative = $_.FullName.Substring($rootPath.Length + 1).Replace('\', '/')
          $relative -notmatch $pattern
        })
      }
      $items = @($items | Sort-Object FullName)
    }

    if ($items.Count -eq 0) {
      $null = $lines.Add('(none yet)')
      continue
    }

    foreach ($item in $items) {
      $relativeToMemory = $item.FullName.Substring($memoryRoot.Length + 1).Replace('\', '/')
      $null = $lines.Add(('- [{0}]({1}) — {2}' -f $item.Name, $relativeToMemory, $section.Description))
    }
  }

  $null = $lines.Add('')
  $null = $lines.Add($ManagedMarker)
  return (($lines -join "`n") + "`n")
}

function Test-RouterFile {
  param(
    [string]$FilePath
  )

  if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    return @{ Exists = $false; Ok = $false; Issues = @('missing file') }
  }

  [string]$content = Get-Content -LiteralPath $FilePath -Raw -Encoding UTF8
  $indexPos = $content.IndexOf('/.MEMORY/INDEX.md', [System.StringComparison]::Ordinal)
  $sessionsPos = $content.IndexOf('/.MEMORY/sessions/*/SESSION.md', [System.StringComparison]::Ordinal)
  $nowPos = $content.IndexOf('/.MEMORY/NOW.md', [System.StringComparison]::Ordinal)
  $aidocsPos = $content.IndexOf('/.MEMORY/.aidocs/index.aidocs', [System.StringComparison]::Ordinal)
  $issues = @()

  if ([string]::IsNullOrWhiteSpace($content)) { $issues += 'file is empty' }
  if ($indexPos -lt 0) { $issues += 'missing /.MEMORY/INDEX.md' }
  if ($aidocsPos -lt 0) { $issues += 'missing /.MEMORY/.aidocs/index.aidocs' }
  if (-not $isLegacyMode) {
    if ($sessionsPos -lt 0) { $issues += 'missing /.MEMORY/sessions/*/SESSION.md' }
    if ($issues.Count -eq 0 -and -not ($aidocsPos -lt $indexPos -and $indexPos -lt $sessionsPos)) {
      $issues += 'router order is not /.MEMORY/.aidocs/index -> INDEX -> sessions/*/SESSION'
    }
  } else {
    $hasNewOrder = ($sessionsPos -ge 0 -and $aidocsPos -lt $indexPos -and $indexPos -lt $sessionsPos)
    $hasLegacyOrder = ($nowPos -ge 0 -and $aidocsPos -lt $nowPos -and $nowPos -lt $indexPos)
    if (-not $hasNewOrder -and -not $hasLegacyOrder) {
      $issues += 'router order is neither legacy (.aidocs -> NOW -> INDEX) nor current (.aidocs -> INDEX -> sessions/*/SESSION)'
    }
  }

  return @{ Exists = $true; Ok = ($issues.Count -eq 0); Issues = $issues }
}

function Get-MarkdownLinks {
  param(
    [string]$Text
  )

  $targets = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($match in [regex]::Matches($Text, '\[[^\]]+\]\(([^)]+)\)')) {
    [string]$target = $match.Groups[1].Value
    if ([string]::IsNullOrWhiteSpace($target)) {
      continue
    }
    $target = $target.Trim().Replace('\', '/')
    if (-not $target.StartsWith('http', [System.StringComparison]::OrdinalIgnoreCase)) {
      $targets.Add($target) | Out-Null
    }
  }
  return $targets
}

function Test-IgnoredLegacyMemoryPath {
  param(
    [string]$RelativePath
  )

  if ([string]::IsNullOrWhiteSpace($RelativePath)) {
    return $false
  }

  $normalized = $RelativePath.Trim().Replace('\', '/')
  return $normalized -in @('TODO.md', 'DONE.md')
}

function Test-ManagedMarker {
  param(
    [string]$FilePath
  )

  if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    return $true
  }

  [string]$content = Get-Content -LiteralPath $FilePath -Raw -Encoding UTF8
  return $content.Contains($ManagedMarker)
}

function Test-MemoryIndex {
  param(
    [string]$ProjectRoot
  )

  $memoryRoot = Join-Path $ProjectRoot '.MEMORY'
  $indexPath = Join-Path $memoryRoot 'INDEX.md'
  if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
    return @{ Ok = $false; Issues = @('missing /.MEMORY/INDEX.md'); MissingLinks = @(); BrokenLinks = @() }
  }

  [string]$content = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8
  $issues = @()
  if ($content -notmatch '(?m)^# Memory Index\s*$') {
    $issues += 'INDEX.md header is not `# Memory Index`'
  }
  if (-not $isLegacyMode) {
    if ($content -notmatch [regex]::Escape('Durable-memory router only. Not the session-start entry point.')) {
      $issues += 'INDEX.md is missing the router-only declaration'
    }
    if ($content -notmatch [regex]::Escape('Read `/.MEMORY/.aidocs/index.aidocs` first.')) {
      $issues += 'INDEX.md is missing the session-entry guidance'
    }
    if ($content -notmatch [regex]::Escape('Inspect active sessions under `sessions/*/SESSION.md`.')) {
      $issues += 'INDEX.md is missing the session-inspection guidance'
    }
  } else {
    $hasLegacyGuidance = $content -match [regex]::Escape('Read `NOW.md` for active runtime state.')
    $hasCurrentGuidance = $content -match [regex]::Escape('Inspect active sessions under `sessions/*/SESSION.md`.')
    if (-not $hasLegacyGuidance -and -not $hasCurrentGuidance) {
      $issues += 'INDEX.md is missing both legacy and current runtime guidance'
    }
  }

  $links = Get-MarkdownLinks -Text $content
  $memoryFiles = @(
    Get-ChildItem -LiteralPath $memoryRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object {
        $_.Name -ne 'INDEX.md' -and
        $_.Name -ne '.gitkeep' -and
        $_.Name -ne 'NOW.md' -and
        $_.Name -ne 'TODO.md' -and
        $_.Name -ne 'DONE.md' -and
        $_.FullName -notmatch '\.MEMORY\\.aidocs\\' -and
        $_.FullName -notmatch '\.MEMORY\\agents\\' -and
        $_.FullName -notmatch '\.MEMORY\\plans\\' -and
        $_.FullName -notmatch '\.MEMORY\\sessions\\' -and
        $_.FullName -notmatch '\.MEMORY\\archive\\'
      } |
      ForEach-Object {
        [string]$fullName = $_.FullName
        if ([string]::IsNullOrWhiteSpace($fullName) -or $fullName.Length -le $memoryRoot.Length) {
          return
        }
        $fullName.Substring($memoryRoot.Length + 1).Replace('\', '/')
      }
  )

  $missingLinks = @($memoryFiles | Where-Object { -not $links.Contains($_) } | Sort-Object -Unique)
  $brokenLinks = @()
  foreach ($link in $links) {
    if (Test-IgnoredLegacyMemoryPath -RelativePath $link) {
      continue
    }
    $resolved = Join-Path $memoryRoot ($link -replace '/', '\')
    if (-not (Test-Path -LiteralPath $resolved)) {
      $brokenLinks += $link
    }
  }

  if ($missingLinks.Count -gt 0) {
    $issues += ("INDEX.md is missing {0} file link(s)" -f $missingLinks.Count)
  }
  if ($brokenLinks.Count -gt 0) {
    $issues += ("INDEX.md has {0} broken link(s)" -f $brokenLinks.Count)
  }

  return @{
    Ok = ($issues.Count -eq 0)
    Issues = $issues
    MissingLinks = $missingLinks
    BrokenLinks = ($brokenLinks | Sort-Object -Unique)
  }
}

function Test-MemoryProject {
  param(
    [string]$ProjectRoot
  )

  try {
    $agents = Test-RouterFile -FilePath (Join-Path $ProjectRoot 'AGENTS.md')
    $claude = Test-RouterFile -FilePath (Join-Path $ProjectRoot 'CLAUDE.md')
    $aidocsIndex = Join-Path $ProjectRoot '.MEMORY\.aidocs\index.aidocs'
    $aidocsGlobal = Join-Path $ProjectRoot '.MEMORY\.aidocs\global-instructions.aidocs'
    $aidocsCoding = Join-Path $ProjectRoot '.MEMORY\.aidocs\coding-standards.aidocs'
    $aidocsMemory = Join-Path $ProjectRoot '.MEMORY\.aidocs\memory-system.aidocs'
    $aidocsPersonality = Join-Path $ProjectRoot '.MEMORY\.aidocs\personality-system.aidocs'
    $aidocsResearch = Join-Path $ProjectRoot '.MEMORY\.aidocs\research-safety.aidocs'
    $memoryIndex = Join-Path $ProjectRoot '.MEMORY\INDEX.md'
    $sessionsRoot = Join-Path $ProjectRoot '.MEMORY\sessions'
    $archiveSessionsRoot = Join-Path $ProjectRoot '.MEMORY\archive\sessions'
    $legacyPlansRoot = Join-Path $ProjectRoot '.MEMORY\plans'
    $legacyAgentsRoot = Join-Path $ProjectRoot '.MEMORY\agents'
    $legacyNow = Join-Path $ProjectRoot '.MEMORY\NOW.md'
    $legacyTodo = Join-Path $ProjectRoot '.MEMORY\TODO.md'
    $legacyDone = Join-Path $ProjectRoot '.MEMORY\DONE.md'
    $index = Test-MemoryIndex -ProjectRoot $ProjectRoot
  }
  catch {
    return [pscustomobject]@{
      ProjectRoot = $ProjectRoot
      HasDrift = $true
      Issues = @('project scan error: ' + $_.Exception.Message)
      MissingLinks = @()
      BrokenLinks = @()
    }
  }

  $issues = @()
  if (-not $agents.Ok) {
    $issues += ('AGENTS.md: ' + ($agents.Issues -join '; '))
  }
  if (-not $claude.Ok) {
    $issues += ('CLAUDE.md: ' + ($claude.Issues -join '; '))
  }
  foreach ($requiredFile in @($aidocsIndex, $aidocsGlobal, $aidocsCoding, $aidocsMemory, $aidocsPersonality, $aidocsResearch, $memoryIndex)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      $issues += ('missing ' + $requiredFile.Substring($ProjectRoot.Length).Replace('\', '/'))
    }
  }
  foreach ($managedFile in @(
    (Join-Path $ProjectRoot 'AGENTS.md'),
    (Join-Path $ProjectRoot 'CLAUDE.md'),
    $aidocsIndex,
    $aidocsGlobal,
    $aidocsCoding,
    $aidocsMemory,
    $aidocsPersonality,
    $aidocsResearch,
    $memoryIndex
  )) {
    if (-not $isLegacyMode -and -not (Test-ManagedMarker -FilePath $managedFile)) {
      $relative = $managedFile.Substring($ProjectRoot.Length + 1).Replace('\', '/')
      $issues += ('missing managed marker: ' + $relative)
    }
  }
  if (-not (Test-Path -LiteralPath $sessionsRoot -PathType Container)) {
    if (-not $isLegacyMode) {
      $issues += 'missing /.MEMORY/sessions/'
    }
  }
  if (-not (Test-Path -LiteralPath $archiveSessionsRoot -PathType Container)) {
    if (-not $isLegacyMode) {
      $issues += 'missing /.MEMORY/archive/sessions/'
    }
  }
  if (-not $isLegacyMode) {
    if (Test-Path -LiteralPath $legacyPlansRoot -PathType Container) {
      $issues += 'legacy root plans workspace present: /.MEMORY/plans/'
    }
    if (Test-Path -LiteralPath $legacyAgentsRoot -PathType Container) {
      $issues += 'legacy root agents workspace present: /.MEMORY/agents/'
    }
    if (Test-Path -LiteralPath $legacyNow -PathType Leaf) {
      $issues += 'legacy runtime file present: /.MEMORY/NOW.md'
    }
    if (Test-Path -LiteralPath $legacyTodo -PathType Leaf) {
      $issues += 'legacy runtime file present: /.MEMORY/TODO.md'
    }
    if (Test-Path -LiteralPath $legacyDone -PathType Leaf) {
      $issues += 'legacy runtime file present: /.MEMORY/DONE.md'
    }
  }
  if (-not $index.Ok) {
    $issues += $index.Issues
  }

  return [pscustomobject]@{
    ProjectRoot = $ProjectRoot
    HasDrift = ($issues.Count -gt 0)
    Issues = $issues
    MissingLinks = $index.MissingLinks
    BrokenLinks = $index.BrokenLinks
  }
}

function Get-CandidateProjectRoots {
  param(
    [string]$RootPath
  )

  $roots = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)

  $files = Get-ChildItem -LiteralPath $RootPath -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object {
      -not (Test-SkipPath -Path $_.FullName) -and (
        $_.Name -eq 'AGENTS.md' -or
        $_.Name -eq 'CLAUDE.md' -or
        $_.FullName -match '\\.MEMORY\\INDEX\.md$' -or
        $_.FullName -match '\\.MEMORY\\\.aidocs\\index\.aidocs$'
      )
    }

  foreach ($file in $files) {
    $projectRoot = $null
    if ($file.FullName -match '\\.MEMORY\\INDEX\.md$') {
      $projectRoot = Split-Path -Parent (Split-Path -Parent $file.FullName)
    } elseif ($file.FullName -match '\\.MEMORY\\\.aidocs\\index\.aidocs$') {
      $projectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $file.FullName))
    } else {
      $projectRoot = Split-Path -Parent $file.FullName
    }

    if (-not [string]::IsNullOrWhiteSpace($projectRoot) -and (Test-Path -LiteralPath (Join-Path $projectRoot '.MEMORY') -PathType Container)) {
      $candidateIndex = Join-Path $projectRoot '.MEMORY\INDEX.md'
      if ($projectRoot -eq $sourceRoot -and -not (Test-Path -LiteralPath $candidateIndex -PathType Leaf)) {
        continue
      }
      $roots.Add($projectRoot) | Out-Null
    }
  }

  return @($roots | Sort-Object)
}

function Invoke-FixProject {
  param(
    [string]$ProjectRoot
  )

  $changes = New-Object System.Collections.Generic.List[string]
  $projectMemory = Join-Path $ProjectRoot '.MEMORY'
  $projectAidocs = Join-Path $projectMemory '.aidocs'

  foreach ($dir in @(
    $projectAidocs,
    (Join-Path $projectMemory 'sessions'),
    (Join-Path $projectMemory 'archive'),
    (Join-Path $projectMemory 'archive\sessions')
  )) {
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
      New-Item -ItemType Directory -Force -Path $dir | Out-Null
      $changes.Add($dir) | Out-Null
    }
  }

  foreach ($mapping in @(
    @{ Source = (Join-Path $sourceRoot 'AGENTS.md'); Target = (Join-Path $ProjectRoot 'AGENTS.md') },
    @{ Source = (Join-Path $sourceRoot 'CLAUDE.md'); Target = (Join-Path $ProjectRoot 'CLAUDE.md') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\index.aidocs'); Target = (Join-Path $projectAidocs 'index.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\global-instructions.aidocs'); Target = (Join-Path $projectAidocs 'global-instructions.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\coding-standards.aidocs'); Target = (Join-Path $projectAidocs 'coding-standards.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\memory-system.aidocs'); Target = (Join-Path $projectAidocs 'memory-system.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\research-safety.aidocs'); Target = (Join-Path $projectAidocs 'research-safety.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\personality-system.aidocs'); Target = (Join-Path $projectAidocs 'personality-system.aidocs') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\templates\memory\INDEX.md'); Target = (Join-Path $projectMemory 'INDEX.md') }
  )) {
    if (Sync-ManagedFile -SourceFile $mapping.Source -TargetFile $mapping.Target) {
      $changes.Add($mapping.Target) | Out-Null
    }
  }

  foreach ($treeMapping in @(
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\personalities'); Target = (Join-Path $projectAidocs 'personalities') },
    @{ Source = (Join-Path $sourceRoot '.MEMORY\.aidocs\templates'); Target = (Join-Path $projectAidocs 'templates') }
  )) {
    foreach ($change in (Sync-CanonicalTree -SourceDir $treeMapping.Source -TargetDir $treeMapping.Target)) {
      $changes.Add($change) | Out-Null
    }
  }

  $changeLogSource = Join-Path $sourceRoot '.MEMORY\.aidocs\templates\CHANGELOG.md'
  $changeLogTarget = Join-Path $projectMemory 'CHANGELOG.md'
  if (Sync-OptionalFileIfMissing -SourceFile $changeLogSource -TargetFile $changeLogTarget) {
    $changes.Add($changeLogTarget) | Out-Null
  }

  $newIndex = New-MemoryIndexContent -ProjectRoot $ProjectRoot
  $indexPath = Join-Path $projectMemory 'INDEX.md'
  $existingIndex = if (Test-Path -LiteralPath $indexPath -PathType Leaf) { Read-FileUtf8 -FilePath $indexPath } else { '' }
  $mergedIndex = Build-ManagedFileContent -ManagedContent $newIndex -ExistingContent $existingIndex
  if ($existingIndex -ne $mergedIndex) {
    Write-FileUtf8 -FilePath $indexPath -Content $mergedIndex
    $changes.Add($indexPath) | Out-Null
  }

  return [pscustomobject]@{
    ProjectRoot = $ProjectRoot
    Changes = @($changes | Sort-Object -Unique)
  }
}

try {
  if (-not $ScanRoot -or $ScanRoot.Trim() -eq '') {
    $ScanRoot = Select-ScanRoot
  }

  $resolvedRoot = (Resolve-Path -LiteralPath $ScanRoot).Path
  Write-Host ''
  $actionLabel = if ($Mode -eq 'fix') { 'Fixing' } else { 'Scanning' }
  Write-Host ($actionLabel + ': ' + $resolvedRoot)

  $projectRoots = @(Get-CandidateProjectRoots -RootPath $resolvedRoot)

  if (-not $projectRoots) {
    Write-Host 'No projects with /.MEMORY/ were found under the selected root.'
    exit 2
  }

  $fixResults = @()
  if ($Mode -eq 'fix') {
    $fixResults = @(
      foreach ($projectRoot in $projectRoots) {
        Invoke-FixProject -ProjectRoot $projectRoot
      }
    )
  }

  $results = @(
    foreach ($projectRoot in $projectRoots) {
      Test-MemoryProject -ProjectRoot $projectRoot
    }
  )

  $drifted = @($results | Where-Object { $_.HasDrift })
  Write-Host ''
  Write-Host ("Projects found: {0}" -f $results.Count)
  Write-Host ("Projects with drift: {0}" -f $drifted.Count)

  foreach ($result in $results) {
    $status = if ($result.HasDrift) { 'DRIFT' } else { 'OK' }
    Write-Host ''
    Write-Host ("[{0}] {1}" -f $status, $result.ProjectRoot)

    if ($Mode -eq 'fix') {
      $projectFix = $fixResults | Where-Object { $_.ProjectRoot -eq $result.ProjectRoot } | Select-Object -First 1
      if ($projectFix -and $projectFix.Changes.Count -gt 0) {
        foreach ($change in $projectFix.Changes) {
          $relative = $change.Substring($result.ProjectRoot.Length).TrimStart('\').Replace('\', '/')
          Write-Host ("  - fixed: {0}" -f $relative)
        }
      } else {
        Write-Host '  - no safe changes applied'
      }
    }

    if ($result.HasDrift) {
      foreach ($issue in $result.Issues) {
        Write-Host ("  - {0}" -f $issue)
      }
      foreach ($path in $result.MissingLinks) {
        Write-Host ("  - missing index link: {0}" -f $path)
      }
      foreach ($path in $result.BrokenLinks) {
        Write-Host ("  - broken index link: {0}" -f $path)
      }
    }
  }

  if ($drifted.Count -gt 0) {
    exit 1
  }

  exit 0
}
catch {
  Write-Host ''
  Write-Host ("Update failed: {0}" -f $_.Exception.Message)
  exit 2
}
