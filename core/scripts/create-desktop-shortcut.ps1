# Creates an AIDOCS Dashboard shortcut on the Windows desktop
# WScript.Shell is a Windows-only COM object; pwsh on Linux/macOS will throw
# an opaque exception if we don't guard early, so detect the platform first
# and give a portable message.
if (-not $IsWindows -and -not $env:OS) {
  Write-Host "This script creates a Windows desktop shortcut and only runs on Windows."
  exit 1
}

$projectRoot = (Get-Item "$PSScriptRoot\..\.." -ErrorAction Stop).FullName
$dashboardExe = Join-Path $projectRoot "apps\aidocs-dashboard\src-tauri\target\release\aidocs-dashboard.exe"
$iconSource = Join-Path $projectRoot "apps\aidocs-dashboard\src-tauri\icons\icon.ico"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "AIDOCS Dashboard.lnk"

if (-not (Test-Path $dashboardExe)) {
  Write-Host "Dashboard executable not found. Build it first:"
  Write-Host "  cd apps\aidocs-dashboard"
  Write-Host "  npm install"
  Write-Host "  npm run tauri build"
  exit 1
}

try {
  $shell = New-Object -ComObject WScript.Shell
} catch {
  Write-Host "Failed to create WScript.Shell COM object: $($_.Exception.Message)"
  Write-Host "This script requires Windows Script Host, which is standard on Windows desktop SKUs."
  exit 1
}
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $dashboardExe
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "AIDOCS Operator Dashboard"
if (Test-Path $iconSource) {
  $shortcut.IconLocation = $iconSource
}
$shortcut.Save()

Write-Host "Desktop shortcut created: $shortcutPath"
