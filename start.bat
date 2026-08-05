@echo off
setlocal
cd /d "%~dp0"

where powershell.exe >nul 2>&1 || (
  echo PowerShell is required to run this application.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1"
set "exitCode=%ERRORLEVEL%"
endlocal & exit /b %exitCode%
