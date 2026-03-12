param(
  [Alias("Path")]
  [string]$ScanRoot,
  [switch]$Interactive
)

$ErrorActionPreference = "Stop"

$skipFragments = @(
  '\.git\',
  '\node_modules\',
  '\bin\',
  '\obj\',
  '\.builder\workspaces\',
  '\coverage\',
  '\dist\'
)

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
    Write-Host "Memory Drift Check"
    Write-Host "Controls: Up/Down = move  Right = enter selected drive  Enter = scan selected drive  Esc = cancel"
    Write-Host ""
    Write-Host "Select a drive:"
    for ($i = 0; $i -lt $drives.Count; $i++) {
      $prefix = if ($i -eq $selectedIndex) { ">" } else { " " }
      Write-Host ("{0} {1}: {2}" -f $prefix, $drives[$i].Name, $drives[$i].Root)
    }

    $key = [Console]::ReadKey($true).Key
    switch ($key) {
      'UpArrow' {
        if ($selectedIndex -gt 0) {
          $selectedIndex--
        }
      }
      'DownArrow' {
        if ($selectedIndex -lt ($drives.Count - 1)) {
          $selectedIndex++
        }
      }
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
    Write-Host "Memory Drift Check"
    Write-Host "Controls: Up/Down = move  Right = enter selected folder  Left = go up/back  Enter = scan current folder  Esc = cancel  P = paste full path"
    Write-Host ""
    Write-Host ("Current folder: {0}" -f $current)
    Write-Host "Press Enter to start the scan in the current folder."
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
      'UpArrow' {
        if ($selectedIndex -gt 0) {
          $selectedIndex--
        }
        continue
      }
      'DownArrow' {
        if ($selectedIndex -lt ($children.Count - 1)) {
          $selectedIndex++
        }
        continue
      }
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

function Test-RouterFile {
  param(
    [string]$FilePath
  )

  if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    return @{ Exists = $false; Ok = $false; Issues = @("missing file") }
  }

  [string]$content = Get-Content -LiteralPath $FilePath -Raw -Encoding UTF8
  $indexPos = $content.IndexOf('/.MEMORY/INDEX.md', [System.StringComparison]::Ordinal)
  $nowPos = $content.IndexOf('/.MEMORY/NOW.md', [System.StringComparison]::Ordinal)
  $aidocsPos = $content.IndexOf('.aidocs/index.aidocs', [System.StringComparison]::Ordinal)
  $issues = @()

  if ([string]::IsNullOrWhiteSpace($content)) { $issues += 'file is empty' }
  if ($indexPos -lt 0) { $issues += 'missing /.MEMORY/INDEX.md' }
  if ($nowPos -lt 0) { $issues += 'missing /.MEMORY/NOW.md' }
  if ($aidocsPos -lt 0) { $issues += 'missing .aidocs/index.aidocs' }
  if ($issues.Count -eq 0 -and -not ($aidocsPos -lt $nowPos -and $nowPos -lt $indexPos)) {
    $issues += 'router order is not .aidocs/index -> NOW -> INDEX'
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
  if ($content -notmatch [regex]::Escape('Main project memory entry point. Router only.')) {
    $issues += 'INDEX.md is missing the router-only declaration'
  }
  if ($content -notmatch [regex]::Escape('Read `NOW.md` for active runtime state.')) {
    $issues += 'INDEX.md is missing the runtime-use guidance'
  }

  $links = Get-MarkdownLinks -Text $content
  $memoryFiles = @(
    Get-ChildItem -LiteralPath $memoryRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -ne 'INDEX.md' -and $_.Name -ne '.gitkeep' } |
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
    $aidocsIndex = Join-Path $ProjectRoot '.aidocs\index.aidocs'
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
  if (-not (Test-Path -LiteralPath $aidocsIndex -PathType Leaf)) {
    $issues += 'missing .aidocs/index.aidocs'
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

try {
  if (-not $ScanRoot -or $ScanRoot.Trim() -eq '') {
    $ScanRoot = Select-ScanRoot
  }

  $resolvedRoot = (Resolve-Path -LiteralPath $ScanRoot).Path
  Write-Host ""
  Write-Host ("Scanning: {0}" -f $resolvedRoot)

  $indexFiles = Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File -Filter 'INDEX.md' -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\.MEMORY\\INDEX\.md$' -and -not (Test-SkipPath -Path $_.FullName) } |
    Sort-Object FullName

  if (-not $indexFiles) {
    Write-Host 'No projects with /.MEMORY/INDEX.md were found under the selected root.'
    exit 2
  }

  $results = @(
    foreach ($indexFile in $indexFiles) {
    $projectRoot = Split-Path -Parent (Split-Path -Parent $indexFile.FullName)
    Test-MemoryProject -ProjectRoot $projectRoot
    }
  )

  $drifted = @($results | Where-Object { $_.HasDrift })
  Write-Host ""
  Write-Host ("Projects found: {0}" -f $results.Count)
  Write-Host ("Projects with drift: {0}" -f $drifted.Count)

  foreach ($result in $results) {
    $status = if ($result.HasDrift) { 'DRIFT' } else { 'OK' }
    Write-Host ""
    Write-Host ("[{0}] {1}" -f $status, $result.ProjectRoot)
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
  Write-Host ""
  Write-Host ("Check failed: {0}" -f $_.Exception.Message)
  exit 2
}
