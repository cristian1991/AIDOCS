@echo off
setlocal

set SCRIPT_DIR=%~dp0

if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%check-memory-drift.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%check-memory-drift.ps1" -ScanRoot "%~1"
)

set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo Memory drift check completed. No drift found.
) else if "%EXIT_CODE%"=="1" (
  echo Memory drift check completed. Drift was found.
) else (
  echo Memory drift check did not complete cleanly.
)

exit /b %EXIT_CODE%
