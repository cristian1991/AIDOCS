param(
  [string]$RootPath
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RootPath -or $RootPath.Trim() -eq "") {
  $RootPath = Split-Path -Parent $scriptDir
}

$root = (Resolve-Path $RootPath).Path
$buildDir = Join-Path $root "build"
$indexFile = Join-Path $buildDir ".MEMORY\.aidocs\index.aidocs"
if (-not (Test-Path $indexFile)) {
  throw "build/.MEMORY/.aidocs/index.aidocs not found at: $buildDir"
}

$sourceRoot = $buildDir
$targetRoot = $root

# Manifest: directories to mirror outward from build/
$dirCopies = @(
  @{ Src = ".MEMORY\.aidocs"; Dst = ".MEMORY\.aidocs" }
)

# Manifest: specific subdirectories (only .md files)
$commandCopies = @(
  @{ Src = ".claude\commands"; Dst = ".claude\commands"; Filter = "*.md" }
  @{ Src = ".opencode\command"; Dst = ".opencode\command"; Filter = "*.md" }
)

# Manifest: individual files
$fileCopies = @(
  "AGENTS.md",
  "CLAUDE.md",
  "opencode.jsonc"
)

# Manifest: scripts directory
$scriptCopies = @(
  "scripts\install-agent-routing.ps1",
  "scripts\install-agent-routing.cmd",
  "scripts\check-memory-drift.ps1",
  "scripts\check-memory-drift.cmd"
)

$synced = @()

# Copy full directories from build/ to root
foreach ($dir in $dirCopies) {
  $srcPath = Join-Path $sourceRoot $dir.Src
  $dstPath = Join-Path $targetRoot $dir.Dst
  if (Test-Path $srcPath) {
    $dstParent = Split-Path -Parent $dstPath
    if (-not [string]::IsNullOrWhiteSpace($dstParent)) {
      New-Item -ItemType Directory -Force -Path $dstParent | Out-Null
    }
    Copy-Item -Recurse -Force $srcPath $dstPath
    $synced += $dir.Dst + "/"
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

# Copy command directories (only .md files) from build/ to root
foreach ($cmd in $commandCopies) {
  $srcPath = Join-Path $sourceRoot $cmd.Src
  $dstPath = Join-Path $targetRoot $cmd.Dst
  if (Test-Path $srcPath) {
    New-Item -ItemType Directory -Force -Path $dstPath | Out-Null
    Get-ChildItem -Path $srcPath -Filter $cmd.Filter -File | ForEach-Object {
      Copy-Item -Force $_.FullName (Join-Path $dstPath $_.Name)
      $synced += "$($cmd.Dst)\$($_.Name)"
    }
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

# Copy individual files
foreach ($file in $fileCopies) {
  $srcPath = Join-Path $sourceRoot $file
  $dstPath = Join-Path $targetRoot $file
  if (Test-Path $srcPath) {
    Copy-Item -Force $srcPath $dstPath
    $synced += $file
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

# Copy scripts from build/ to root
$scriptsDstDir = Join-Path $targetRoot "scripts"
New-Item -ItemType Directory -Force -Path $scriptsDstDir | Out-Null
foreach ($script in $scriptCopies) {
  $srcPath = Join-Path $sourceRoot $script
  $dstPath = Join-Path $targetRoot $script
  if (Test-Path $srcPath) {
    Copy-Item -Force $srcPath $dstPath
    $synced += $script
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

Write-Host "Mirrored $($synced.Count) items from build/ to root:"
foreach ($item in $synced) {
  Write-Host "  $item"
}
Write-Host "Source build: $buildDir"
Write-Host "Mirror target: $targetRoot"
