# Single-command entry point for Windows PowerShell.
#   .\run.ps1            -> run tests (default)
#   .\run.ps1 test       -> run tests
#   .\run.ps1 validate   -> Phase 1 reconciliation
#   .\run.ps1 sync       -> fetch league state (Phase 2+)
#   .\run.ps1 run        -> launch web UI (Phase 2+)
#
# Bootstraps a local .venv if missing.

param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Args)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "No .venv found; creating one..." -ForegroundColor Yellow
    $base = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $base)) { $base = "python" }
    & $base -m venv .venv
    & $venvPy -m pip install --quiet --upgrade pip
    if (Test-Path "requirements.txt") {
        & $venvPy -m pip install --quiet -r requirements.txt
    } else {
        & $venvPy -m pip install --quiet pytest
    }
}

if (-not $Args -or $Args.Count -eq 0) { $Args = @("test") }

& $venvPy "manage.py" @Args
exit $LASTEXITCODE
