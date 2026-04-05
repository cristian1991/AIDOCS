# Creates an AIDOCS Dashboard shortcut on the Windows desktop
$projectRoot = (Get-Item "$PSScriptRoot\..\.." -ErrorAction Stop).FullName
$dashboardExe = Join-Path $projectRoot "apps\aidocs-dashboard\src-tauri\target\release\aidocs-dashboard.exe"
$iconSource = Join-Path $projectRoot "apps\aidocs-dashboard\src-tauri\icons\icon.ico"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "AIDOCS Dashboard.lnk"

if (-not (Test-Path $dashboardExe)) {
  Write-Host "Dashboard executable not found. Build it first:"
  Write-Host "  cd apps/aidocs-dashboard && npm run tauri build"
  exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $dashboardExe
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "AIDOCS Operator Dashboard"
if (Test-Path $iconSource) {
  $shortcut.IconLocation = $iconSource
}
$shortcut.Save()

Write-Host "Desktop shortcut created: $shortcutPath"
