@echo off
rem Open the small run-state window (Edge/Chrome app mode against the local worker).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [sysmon] no venv found - run scripts\install.ps1 first
  exit /b 1
)
".venv\Scripts\python.exe" -m sysmon open %*
