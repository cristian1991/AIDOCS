param(
  [Alias('Path')]
  [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'

function Get-MarkdownLinks {
  param(
    [string]$Text
  )

  $targets = New-Object System.Collections.Generic.List[string]
  foreach ($match in [regex]::Matches($Text, '\[[^\]]+\]\(([^)]+)\)')) {
    $target = $match.Groups[1].Value.Trim()
    if ([string]::IsNullOrWhiteSpace($target)) {
      continue
    }
    if ($target -match '^(https?:)?//') {
      continue
    }
    $targets.Add($target.Replace('\', '/')) | Out-Null
  }
  return $targets
}

function Resolve-MemoryLink {
  param(
    [string]$FromFile,
    [string]$Link,
    [string]$MemoryRoot
  )

  $normalized = $Link.Trim().Replace('\', '/')
  if ([string]::IsNullOrWhiteSpace($normalized)) {
    return $null
  }

  if ($normalized.StartsWith('/.MEMORY/')) {
    $relative = $normalized.Substring('/.MEMORY/'.Length)
    $candidate = Join-Path $MemoryRoot ($relative -replace '/', '\')
  } else {
    $fromDir = Split-Path -Parent $FromFile
    $candidate = Join-Path $fromDir ($normalized -replace '/', '\')
  }

  try {
    $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
  } catch {
    return $null
  }

  if ($resolved -notlike "$MemoryRoot*") {
    return $null
  }

  return $resolved
}

function Get-MemoryFiles {
  param(
    [string]$MemoryRoot
  )

  return @(Get-ChildItem -LiteralPath $MemoryRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @('.md', '.aidocs') } |
    Sort-Object FullName)
}

function Find-Cycles {
  param(
    [hashtable]$Graph
  )

  $visited = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
  $cycles = New-Object System.Collections.Generic.List[string]

  foreach ($start in ($Graph.Keys | Sort-Object)) {
    $stack = New-Object System.Collections.Generic.List[string]
    $onStack = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)

    $visit = {
      param($node, $self)
      if ($onStack.Contains($node)) {
        $index = $stack.IndexOf($node)
        if ($index -ge 0) {
          $cycle = @($stack[$index..($stack.Count - 1)] + $node)
          $cycles.Add(($cycle -join ' -> ')) | Out-Null
        }
        return
      }
      if ($visited.Contains($node)) {
        return
      }

      $visited.Add($node) | Out-Null
      $stack.Add($node) | Out-Null
      $onStack.Add($node) | Out-Null

      foreach ($next in $Graph[$node]) {
        & $self $next $self
      }

      [void]$stack.RemoveAt($stack.Count - 1)
      $onStack.Remove($node) | Out-Null
    }

    & $visit $start $visit
  }

  return @($cycles | Sort-Object -Unique)
}

try {
  if (-not $ProjectRoot -or [string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Get-Location).Path
  }

  $resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
  $memoryRoot = Join-Path $resolvedRoot '.MEMORY'

  if (-not (Test-Path -LiteralPath $memoryRoot -PathType Container)) {
    throw "Missing /.MEMORY/ under: $resolvedRoot"
  }

  $graph = @{}
  $broken = New-Object System.Collections.Generic.List[string]
  $files = Get-MemoryFiles -MemoryRoot $memoryRoot

  foreach ($file in $files) {
    $filePath = $file.FullName
    $relativeFromMemory = $filePath.Substring($memoryRoot.Length + 1).Replace('\', '/')
    $graph[$relativeFromMemory] = New-Object System.Collections.Generic.List[string]
    $content = Get-Content -LiteralPath $filePath -Raw -Encoding UTF8
    foreach ($link in (Get-MarkdownLinks -Text $content)) {
      $resolved = Resolve-MemoryLink -FromFile $filePath -Link $link -MemoryRoot $memoryRoot
      if ($null -eq $resolved) {
        continue
      }
      $targetRelative = $resolved.Substring($memoryRoot.Length + 1).Replace('\', '/')
      $graph[$relativeFromMemory].Add($targetRelative) | Out-Null
    }
  }

  $cycles = Find-Cycles -Graph $graph

  Write-Host ''
  Write-Host ('Memory graph scanned: ' + $resolvedRoot)
  Write-Host ('Memory files inspected: ' + $files.Count)

  if ($cycles.Count -eq 0) {
    Write-Host 'No memory loops found.'
    exit 0
  }

  Write-Host ('Loops found: ' + $cycles.Count)
  foreach ($cycle in $cycles) {
    Write-Host ('- ' + $cycle)
  }

  exit 1
}
catch {
  Write-Host ''
  Write-Host ('Loop detection failed: ' + $_.Exception.Message)
  exit 2
}
