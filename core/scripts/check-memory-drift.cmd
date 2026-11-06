@echo off
setlocal

set SCRIPT_DIR=%~dp0
set MODE=check
set TARGET=

if /I "%~1"=="check" (
  set MODE=check
  set TARGET=%~2
) else if /I "%~1"=="fix" (
  set MODE=fix
  set TARGET=%~2
) else (
  set TARGET=%~1
)

REM Confirm powershell.exe resolves before delegating. Without this, a
REM missing interpreter leaves ERRORLEVEL=0 and the wrapper falsely reports
REM "no drift found".
where powershell >nul 2>&1
if errorlevel 1 (
  echo powershell.exe not found. Install Windows PowerShell or PowerShell 7.
  exit /b 2
)

if "%TARGET%"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%check-memory-drift.ps1" -Mode %MODE%
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%check-memory-drift.ps1" -Mode %MODE% -ScanRoot "%TARGET%"
)

set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo AIDOCS memory %MODE% completed. No drift found.
) else if "%EXIT_CODE%"=="1" (
  echo AIDOCS memory %MODE% completed. Drift remains.
) else (
  echo AIDOCS memory %MODE% did not complete cleanly.
)

exit /b %EXIT_CODE%
