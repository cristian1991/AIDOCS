@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_NAME=%~1"
if "%SCRIPT_NAME%"=="" exit /b 1
shift

set "BASH_EXE="
for %%P in ("%ProgramFiles%\Git\bin\bash.exe" "%ProgramFiles(x86)%\Git\bin\bash.exe" "%LocalAppData%\Programs\Git\bin\bash.exe") do (
  if not defined BASH_EXE if exist %%~P set "BASH_EXE=%%~P"
)
if not defined BASH_EXE for /f "delims=" %%P in ('where bash 2^>nul') do if not defined BASH_EXE set "BASH_EXE=%%P"
if not defined BASH_EXE exit /b 0

"%BASH_EXE%" "%SCRIPT_DIR%%SCRIPT_NAME%" %*
