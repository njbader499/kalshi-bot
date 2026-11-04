# Lint, type-check and unit-test. Add -Integration to also run the live DEMO tests.
#   powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1 [-Integration]
param([switch]$Integration)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m mypy kalshi_maker
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Integration) {
    $env:KALSHI_INTEGRATION = "1"
    try {
        & $py -m pytest -m integration -v
        $rc = $LASTEXITCODE
    } finally {
        Remove-Item Env:KALSHI_INTEGRATION -ErrorAction SilentlyContinue
    }
    exit $rc
}
