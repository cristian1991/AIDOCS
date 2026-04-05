@echo off
setlocal

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1" %*

if errorlevel 1 (
  echo.
  echo Install failed.
  exit /b 1
)

echo.
echo Install completed.
exit /b 0
