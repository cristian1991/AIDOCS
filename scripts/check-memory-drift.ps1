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

  while ($true) {
    Write-Host ""
    Write-Host "Select a drive:" 
    for ($i = 0; $i -lt $drives.Count; $i++) {
      Write-Host ("[{0}] {1}: {2}" -f ($i + 1), $drives[$i].Name, $drives[$i].Root)
    }

    $choice = (Read-Host "Enter number, drive letter, or q to cancel").Trim()
    if ($choice -match '^[Qq]$') {
      throw "Selection cancelled."
    }

    if ($choice -match '^\d+$') {
      $index = [int]$choice - 1
      if ($index -ge 0 -and $index -lt $drives.Count) {
        return $drives[$index].Root
      }
    }

    $driveMatch = $drives | Where-Object { $_.Name -ieq $choice.TrimEnd(':') } | Select-Object -First 1
    if ($driveMatch) {
      return $driveMatch.Root
    }

    Write-Host "Invalid selection. Try again."
  }
}

function Select-ScanRoot {
  $current = Select-DriveRoot

  while ($true) {
    $children = @()
    if (Test-Path -LiteralPath $current -PathType Container) {
      $children = Get-ChildItem -LiteralPath $current -Directory -Force -ErrorAction SilentlyContinue | Sort-Object Name
    }

    Write-Host ""
    Write-Host ("Current folder: {0}" -f $current)
    Write-Host "[Enter] Start scan here"
    Write-Host "[..]    Go up one level"
    Write-Host "[d]     Change drive"
    Write-Host "[p]     Paste a full folder path"
    Write-Host "[q]     Cancel"
    if ($children.Count -gt 0) {
      Write-Host ""
      for ($i = 0; $i -lt $children.Count; $i++) {
        Write-Host ("[{0}] {1}" -f ($i + 1), $children[$i].Name)
      }
    } else {
      Write-Host ""
      Write-Host "(No child directories)"
    }

    $choice = (Read-Host "Choose an option").Trim()
    if ($choice -eq "") {
      return $current
    }

    if ($choice -eq "..") {
      $parent = Split-Path -Parent $current
      if ($parent -and (Test-Path -LiteralPath $parent -PathType Container)) {
        $current = $parent
      }
      continue
    }

    switch -Regex ($choice) {
      '^[Qq]$' { throw "Selection cancelled." }
      '^[Dd]$' { $current = Select-DriveRoot; continue }
      '^[Pp]$' {
        $manualPath = (Read-Host "Enter folder path").Trim()
        if (Test-Path -LiteralPath $manualPath -PathType Container) {
          $current = (Resolve-Path -LiteralPath $manualPath).Path
        } else {
          Write-Host "Path not found."
        }
        continue
      }
      '^\d+$' {
        $index = [int]$choice - 1
        if ($index -ge 0 -and $index -lt $children.Count) {
          $current = $children[$index].FullName
        } else {
          Write-Host "Invalid selection."
        }
        continue
      }
      default {
        if (Test-Path -LiteralPath $choice -PathType Container) {
          $current = (Resolve-Path -LiteralPath $choice).Path
        } else {
          Write-Host "Invalid selection."
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
  if ($issues.Count -eq 0 -and -not ($indexPos -lt $nowPos -and $nowPos -lt $aidocsPos)) {
    $issues += 'router order is not INDEX -> NOW -> .aidocs/index'
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
