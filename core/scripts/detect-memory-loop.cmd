@echo off
setlocal

set SCRIPT_DIR=%~dp0
set TARGET=%~1

if "%TARGET%"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%detect-memory-loop.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%detect-memory-loop.ps1" -ProjectRoot "%TARGET%"
)

exit /b %ERRORLEVEL%
