@echo off
rem Start the sysmon worker in this console (Ctrl-C stops it).
rem For a permanent, elevated, logon-started worker use scripts\install.ps1.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [sysmon] no venv found - run: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  exit /b 1
)
".venv\Scripts\python.exe" -m sysmon serve %*
