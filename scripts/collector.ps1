# Shadow collector supervisor: keeps `python -m kalshi_maker run` alive.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\collector.ps1 -Root <checkout> `
#       -Config <config.yaml> -EnvFile <.env> -LogDir <dir>
#
# Normally started detached by scripts\start-collector.ps1 (see HANDOFF.md).
# Exit codes from the bot:
#   0  clean stop (e.g. --max-seconds)      -> stop supervising
#   10 KILL file present                     -> stop supervising (manual restart only)
#   11 another instance holds the lock       -> stop (never run two bots)
#   anything else (crash, 30 loop errors)    -> restart with backoff 10 s .. 5 min
# The bot places NO orders while trading_enabled is false (config).
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$LogDir,
    [Parameter(Mandatory = $true)][string]$Python
)
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $Root
$failures = 0

function Write-Sup([string]$msg) {
    # Another process (e.g. a log tailer) can hold the file open on Windows; retry, then
    # fall back to a second file so a supervisor decision is never silently unlogged.
    $line = "{0}  [supervisor] {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ"), $msg
    foreach ($i in 1..10) {
        try {
            Add-Content -Path (Join-Path $LogDir "collector-supervisor.log") -Value $line -ErrorAction Stop
            return
        } catch { Start-Sleep -Milliseconds 300 }
    }
    try { Add-Content -Path (Join-Path $LogDir "collector-supervisor-fallback.log") -Value $line -ErrorAction Stop } catch { }
}

Write-Sup "starting (pid $PID) root=$Root config=$Config"
while ($true) {
    $day = (Get-Date).ToUniversalTime().ToString("yyyyMMdd")
    $out = Join-Path $LogDir "collector-$day.log"
    $started = Get-Date
    # stdout = operator lines, stderr = JSON logs + any crash traceback; both appended to
    # the day's file via cmd.exe redirection (PowerShell 5.1 would mangle native stderr).
    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $inner = "`"$Python`" -m kalshi_maker --config `"$Config`" --env-file `"$EnvFile`" run >> `"$out`" 2>&1"
    & cmd.exe /d /s /c "`"$inner`""
    $rc = $LASTEXITCODE
    $ran = ((Get-Date) - $started).TotalSeconds
    Write-Sup ("bot exited rc={0} after {1:N0}s" -f $rc, $ran)
    if ($rc -eq 0 -or $rc -eq 10 -or $rc -eq 11) {
        Write-Sup "not restarting (rc=$rc)"
        break
    }
    if ($ran -gt 600) { $failures = 0 } else { $failures++ }
    $delay = [Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Min($failures, 5)))
    Write-Sup ("restarting in {0}s (failure streak {1})" -f $delay, $failures)
    Start-Sleep -Seconds $delay
}
