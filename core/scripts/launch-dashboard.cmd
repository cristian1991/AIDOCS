@echo off
setlocal

set AIDOCS_PATH=%~dp0..\..
set DASHBOARD_EXE=%AIDOCS_PATH%\apps\aidocs-dashboard\src-tauri\target\release\aidocs-dashboard.exe
set AIDOCS_HOME=%USERPROFILE%\.aidocs
set INSTALLED_EXE=%AIDOCS_HOME%\aidocs-dashboard.exe

if exist "%INSTALLED_EXE%" (
  start "" "%INSTALLED_EXE%"
  exit /b 0
)

if exist "%DASHBOARD_EXE%" (
  start "" "%DASHBOARD_EXE%"
  exit /b 0
)

echo AIDOCS Dashboard not found.
echo.
echo Expected locations:
echo   %INSTALLED_EXE%
echo   %DASHBOARD_EXE%
echo.
echo Build from source:
echo   cd %AIDOCS_PATH%\apps\aidocs-dashboard ^&^& npm install ^&^& npm run tauri build
echo.
echo Or download the latest release from:
echo   https://github.com/cristian1991/AIDOCS/releases
pause
exit /b 1
