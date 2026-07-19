# AIDOCS Windows Installer — bundles Python, zero external dependencies
# Usage: irm https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.ps1 | iex
# Or called by the Tauri dashboard on first run

$ErrorActionPreference = "Stop"

$AidocsHome = if ($env:AIDOCS_HOME) { $env:AIDOCS_HOME } else { Join-Path $env:LOCALAPPDATA "AIDOCS" }
# Pinned to the AIDOCS-blessed CPython (runtime_provisioner.PINNED / _PBS_*).
# The bootstrap archive is SHA-256 verified below against the win32-amd64 pin
# (keep in lockstep with mcp/server/aidocs_mcp/runtime_provisioner.py).
$PythonVersion = "3.12.13"
$PythonBuildTag = "20260510"
$Platform = "x86_64-pc-windows-msvc"
$PythonSha256 = "346dfbcb95171dd6d1275e6f8cb2e656cc15cb054c399ae54db57bfad4b1a60f"

$PythonUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/${PythonBuildTag}/cpython-${PythonVersion}+${PythonBuildTag}-${Platform}-install_only.tar.gz"

Write-Host ""
Write-Host "  AIDOCS Installer" -ForegroundColor Green
Write-Host "  ════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Platform: Windows x64"
Write-Host "  Install:  $AidocsHome"
Write-Host ""

# ── Check existing ──
if (Test-Path (Join-Path $AidocsHome "python")) {
    Write-Host "  Existing installation found." -ForegroundColor Yellow
    # Non-interactive paths (irm | iex, CI) lack a TTY — treat as "reinstall" so
    # the piped one-liner doesn't hang on Read-Host.
    $nonInteractive = $env:AIDOCS_ASSUME_YES -eq "1" -or (-not [Environment]::UserInteractive) -or [Console]::IsInputRedirected
    if ($nonInteractive) {
        Write-Host "  Non-interactive session — reinstalling."
        $reply = "y"
    } else {
        $reply = Read-Host "  Reinstall? [y/N]"
    }
    if ($reply -ne "y" -and $reply -ne "Y") {
        Write-Host "  Cancelled."
        exit 0
    }
    Remove-Item (Join-Path $AidocsHome "python") -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $AidocsHome | Out-Null

# ── Download Python ──
Write-Host "  [1/4] Downloading Python $PythonVersion..."
$tmpDir = Join-Path $env:TEMP "aidocs-install-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
$tarGz = Join-Path $tmpDir "python.tar.gz"
$tar = Join-Path $tmpDir "python.tar"

try {
    Invoke-WebRequest -Uri $PythonUrl -OutFile $tarGz -UseBasicParsing
} catch {
    Write-Host "  ERROR: Download failed: $_" -ForegroundColor Red
    exit 1
}

# ── SHA-256 verification (no unverified bootstrap interpreter) ──
$gotSha = (Get-FileHash -Algorithm SHA256 -Path $tarGz).Hash.ToLower()
if ($gotSha -ne $PythonSha256) {
    Write-Host "  ERROR: Python archive SHA-256 mismatch (possible tamper / wrong build)." -ForegroundColor Red
    Write-Host "         got:      $gotSha"
    Write-Host "         expected: $PythonSha256"
    exit 1
}

# ── Extract (tar.gz → tar → folder) ──
Write-Host "  [2/4] Extracting..."

# Use tar if available (Windows 10+), fall back to .NET
if (Get-Command tar -ErrorAction SilentlyContinue) {
    tar -xzf $tarGz -C $AidocsHome
} else {
    # Decompress gzip
    $gzStream = [System.IO.File]::OpenRead($tarGz)
    $decompressed = New-Object System.IO.Compression.GZipStream($gzStream, [System.IO.Compression.CompressionMode]::Decompress)
    $tarStream = [System.IO.File]::Create($tar)
    $decompressed.CopyTo($tarStream)
    $tarStream.Close()
    $decompressed.Close()
    $gzStream.Close()
    # Extract tar
    tar -xf $tar -C $AidocsHome
}

# python-build-standalone extracts to python/install/ or python/
$pythonDir = Join-Path $AidocsHome "python"
$installSubdir = Join-Path $pythonDir "install"
if (Test-Path $installSubdir) {
    $tmpPython = Join-Path $AidocsHome "python-tmp"
    Move-Item $installSubdir $tmpPython
    Remove-Item $pythonDir -Recurse -Force
    Rename-Item $tmpPython "python"
}

$pythonExe = Join-Path $AidocsHome "python\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "  ERROR: Python binary not found at $pythonExe" -ForegroundColor Red
    Get-ChildItem (Join-Path $AidocsHome "python") -ErrorAction SilentlyContinue
    exit 1
}

$pyVer = & $pythonExe --version 2>&1
Write-Host "  Python: $pyVer"

# ── Install aidocs-mcp ──
Write-Host "  [3/4] Installing aidocs-mcp..."
& $pythonExe -m pip install --upgrade pip -q 2>&1 | Out-Null
$pipOutput = & $pythonExe -m pip install aidocs-mcp -q 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Failed to install aidocs-mcp (pip exit $LASTEXITCODE)" -ForegroundColor Red
    $pipOutput | Select-Object -Last 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host "" -ForegroundColor Red
    Write-Host "  Common causes: no network, TLS certificate issue, or PyPI rate limit." -ForegroundColor Yellow
    Write-Host "  Retry: `"$pythonExe`" -m pip install aidocs-mcp" -ForegroundColor Yellow
    exit 1
}
$pipOutput | Select-Object -Last 3

# ── Create bin wrapper ──
$binDir = Join-Path $AidocsHome "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$wrapperContent = "@echo off`r`n`"$pythonExe`" -m aidocs_mcp.cli %*"
[System.IO.File]::WriteAllText((Join-Path $binDir "aidocs.cmd"), $wrapperContent)

# ── Add to PATH (user-level, no admin) ──
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [System.Environment]::SetEnvironmentVariable("Path", "$binDir;$userPath", "User")
    Write-Host "  Added to user PATH: $binDir"
}
$env:Path = "$binDir;$env:Path"

# ── Verify ──
Write-Host "  [4/4] Verifying..."
$aidocsVer = & (Join-Path $binDir "aidocs.cmd") version 2>&1
Write-Host "  aidocs: $aidocsVer"

# ── Cleanup ──
Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "  ════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Installation complete!" -ForegroundColor Green
Write-Host "  ════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Restart your terminal so PATH is refreshed."
Write-Host "    2. aidocs setup C:\path\to\your\project"
Write-Host "    3. aidocs doctor    (verify the install)"
Write-Host "    4. Open the project in your IDE and type /aidocs"
Write-Host ""
Write-Host "  If 'aidocs' is not found after restart, run the bin wrapper directly:"
Write-Host "    $binDir\aidocs.cmd"
Write-Host ""
