@echo off
setlocal

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1" %*

if errorlevel 1 (
  echo.
  echo Setup failed.
  pause
  exit /b 1
)

echo.
echo Setup completed.
pause
exit /b 0
