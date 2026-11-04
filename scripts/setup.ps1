# One-time local setup on Windows (PowerShell 5.1+). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
# Safe to re-run: it never overwrites .env or config\config.yaml, and never reads key contents.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    py -3.12 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements-dev.txt

if (-not (Test-Path .\config\config.yaml)) {
    Copy-Item .\config\config.example.yaml .\config\config.yaml
    Write-Host "created config\config.yaml"
}
if (-not (Test-Path .\.env)) {
    Copy-Item .\.env.example .\.env
    Write-Host "created .env - fill in KALSHI_KEY_ID yourself"
}

$key = Join-Path $HOME ".kalshi\demo_key.pem"
if (Test-Path $key) {
    # Owner-only, read-only. The bot refuses to start if the key is readable by anyone else.
    icacls $key /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
    Write-Host "locked $key to $($env:USERNAME) (read-only)"
} else {
    Write-Host "no key at $key yet - save your demo private key there, then re-run"
}

$w32 = Get-Service W32Time -ErrorAction SilentlyContinue
if ($null -eq $w32 -or $w32.Status -ne "Running") {
    Write-Host "WARNING: Windows Time service is not running; clock drift will grow."
    Write-Host "  Fix (admin PowerShell): Set-Service W32Time -StartupType Automatic; Start-Service W32Time; w32tm /resync"
}
