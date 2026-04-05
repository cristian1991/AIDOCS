@echo off
setlocal

set AIDOCS_PATH=%~dp0..\..
if defined AIDOCS_PATH (
  set DASHBOARD_DIR=%AIDOCS_PATH%\apps\aidocs-dashboard
) else (
  echo AIDOCS_PATH not set. Run setup.cmd first.
  pause
  exit /b 1
)

if not exist "%DASHBOARD_DIR%\src-tauri\target\release\AIDOCS Dashboard.exe" (
  echo Dashboard not built yet. Building...
  cd /d "%DASHBOARD_DIR%"
  call npm run tauri build
)

if exist "%DASHBOARD_DIR%\src-tauri\target\release\AIDOCS Dashboard.exe" (
  start "" "%DASHBOARD_DIR%\src-tauri\target\release\AIDOCS Dashboard.exe"
) else (
  echo Starting in dev mode...
  cd /d "%DASHBOARD_DIR%"
  call npm run tauri dev
)
