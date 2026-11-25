# hermes-win11-ops:sysmon - installer
#
# Creates the virtualenv, installs the package, seeds config.json, and registers
# a logon task that runs the worker ELEVATED in the user's own session (so the
# GUI can talk to it and run-state steps that need admin actually work).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -NoTask
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall

param(
    [switch]$NoTask,
    [switch]$Uninstall,
    [string]$Python = ""
)

# Native tools (uv, pip) write progress to stderr; with ErrorActionPreference=Stop
# PowerShell turns that into a terminating error. Use Continue and check results
# explicitly (venvPy existence + the smoke tests below).
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repo ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
$taskName = "hermes-sysmon"

function Write-Step($msg) { Write-Host "[install] $msg" }

if ($Uninstall) {
    Write-Step "removing scheduled task '$taskName' (if present)"
    schtasks /Delete /TN $taskName /F 2>$null | Out-Null
    Write-Step "task removed. The repo and .venv are left in place: $repo"
    exit 0
}

# ---------------------------------------------------------------- python
if (-not $Python) {
    foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "python")) {
        $parts = $cand.Split(" ")
        $exe = $parts[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) { $Python = $cand; break }
    }
}
if (-not $Python) {
    throw "No Python 3.11+ found. Install Python or pass -Python 'C:\path\to\python.exe'."
}
Write-Step "python: $Python"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Step "creating venv with uv"
    & uv venv --python 3.13 $venv 2>&1 | Out-Null
    & uv pip install --python $venvPy -e "$repo[dev]" 2>&1 | Out-Null
} else {
    Write-Step "creating venv with venv module"
    if ($Python -match "\s") {
        $pyParts = $Python.Split(" ")
        & $pyParts[0] $pyParts[1..($pyParts.Length - 1)] -m venv $venv
    } else {
        & $Python -m venv $venv
    }
    & $venvPy -m pip install --upgrade pip 2>&1 | Out-Null
    & $venvPy -m pip install -e "$repo" 2>&1 | Out-Null
}
if (-not (Test-Path $venvPy)) { throw "venv creation failed: $venvPy missing" }

# ---------------------------------------------------------------- config
$cfgDir = Join-Path $env:LOCALAPPDATA "hermes-sysmon"
$cfgPath = Join-Path $cfgDir "config.json"
if (-not (Test-Path $cfgPath)) {
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    if (Test-Path (Join-Path $repo "config.example.json")) {
        Copy-Item (Join-Path $repo "config.example.json") $cfgPath
        Write-Step "seeded config: $cfgPath"
    }
} else {
    Write-Step "config already present: $cfgPath"
}

# ------------------------------------------------------------ smoke test
Write-Step "importing the package"
& $venvPy -c "import sysmon, fastapi, psutil; print('sysmon', sysmon.__version__, 'ok')"
Write-Step "validating run-state manifests"
& $venvPy -c "from sysmon.config import load; from sysmon.runstates import Registry; c=load(); r=Registry(c.runstates_dir); r.reload(); print('runstates:', ', '.join(r.ids())); print('errors:', r.errors)"

# ----------------------------------------------------------------- task
if ($NoTask) {
    Write-Step "skipping scheduled task (-NoTask)"
} else {
    Write-Step "registering logon task '$taskName' (highest privileges)"
    $action = New-ScheduledTaskAction -Execute $venvPy -Argument "-m sysmon serve" -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Step "task registered. Start it now with: schtasks /Run /TN $taskName"
}

Write-Host ""
Write-Host "Dashboard : http://127.0.0.1:8765/"
Write-Host "Window    : $repo\sysmon-ui.cmd"
Write-Host "State     : $repo\sysmon-state.cmd"
Write-Host "Config    : $cfgPath"
Write-Host "Token     : $cfgDir\api_token.txt  (bearer token for remote callers)"
