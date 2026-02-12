@echo off
setlocal

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%sync-build.ps1" %*

if errorlevel 1 (
  echo.
  echo Sync failed.
  exit /b 1
)

echo.
echo Sync completed.
exit /b 0
