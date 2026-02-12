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

$buildDir = Join-Path $root "build"

# Clear build directory (preserve .git if present)
if (Test-Path $buildDir) {
  Get-ChildItem -Path $buildDir -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null

# Manifest: directories to copy
$dirCopies = @(
  @{ Src = ".aidocs"; Dst = ".aidocs" }
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
  "opencode.jsonc",
  "AIDOCS.md",
  "README.md",
  "README_INSTALL.md"
)

# Manifest: scripts directory
$scriptCopies = @(
  "scripts\install-agent-routing.ps1",
  "scripts\install-agent-routing.cmd"
)

$synced = @()

# Copy full directories
foreach ($dir in $dirCopies) {
  $srcPath = Join-Path $root $dir.Src
  $dstPath = Join-Path $buildDir $dir.Dst
  if (Test-Path $srcPath) {
    Copy-Item -Recurse -Force $srcPath $dstPath
    $synced += $dir.Dst + "/"
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

# Copy command directories (only .md files)
foreach ($cmd in $commandCopies) {
  $srcPath = Join-Path $root $cmd.Src
  $dstPath = Join-Path $buildDir $cmd.Dst
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
  $srcPath = Join-Path $root $file
  $dstPath = Join-Path $buildDir $file
  if (Test-Path $srcPath) {
    Copy-Item -Force $srcPath $dstPath
    $synced += $file
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

# Copy scripts
$scriptsDstDir = Join-Path $buildDir "scripts"
New-Item -ItemType Directory -Force -Path $scriptsDstDir | Out-Null
foreach ($script in $scriptCopies) {
  $srcPath = Join-Path $root $script
  $dstPath = Join-Path $buildDir $script
  if (Test-Path $srcPath) {
    Copy-Item -Force $srcPath $dstPath
    $synced += $script
  } else {
    Write-Warning "Source not found: $srcPath"
  }
}

Write-Host "Synced $($synced.Count) items to build/:"
foreach ($item in $synced) {
  Write-Host "  $item"
}
Write-Host "Build directory: $buildDir"
