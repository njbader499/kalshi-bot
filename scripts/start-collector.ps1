# Start the shadow collector detached from this console (it keeps running after the
# terminal or Claude Code closes). It runs from a pinned git worktree so editing this repo
# can never make a restart load half-written code.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\start-collector.ps1 [-Commit <sha>]
#
# Stop it: create a KILL file in the repo root (the bot exits with code 10 and the supervisor
# does not restart it), then delete KILL before starting again.
param([string]$Commit = "HEAD")
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$wt = Join-Path (Split-Path $repo -Parent) "kalshi-bot-collector"
$sha = (git -C $repo rev-parse $Commit).Trim()
if (-not $sha) { throw "unknown commit $Commit" }

if (Test-Path (Join-Path $repo "KILL")) { throw "KILL file present in $repo; delete it first" }

# git writes progress to stderr; under "Stop", PowerShell 5.1 would treat that as fatal.
$ErrorActionPreference = "Continue"
if (-not (Test-Path $wt)) {
    git -C $repo worktree add -q --detach $wt $sha
} else {
    git -C $wt checkout -q --detach $sha
}
if ($LASTEXITCODE -ne 0) { throw "git could not prepare $wt at $sha" }
$ErrorActionPreference = "Stop"

# Collector config: the local config.yaml with ABSOLUTE paths into the main repo, so the
# ledger, logs and KILL file are the ones you already use.
$cfgSrc = Join-Path $repo "config\config.yaml"
$cfgDst = Join-Path $repo "config\collector.yaml"
$abs = $repo.Replace('\', '/')
$text = [IO.File]::ReadAllText($cfgSrc, [Text.Encoding]::UTF8) `
    -replace 'data_dir:\s*"\./data"', "data_dir: `"$abs/data`"" `
    -replace 'log_dir:\s*"\./logs"', "log_dir: `"$abs/logs`"" `
    -replace 'kill_file:\s*"\./KILL"', "kill_file: `"$abs/KILL`""
if ($text -notmatch [regex]::Escape("kill_file: `"$abs/KILL`"")) { throw "collector config rewrite failed" }
# UTF-8 without BOM (Set-Content -Encoding utf8 in PowerShell 5.1 adds one).
[IO.File]::WriteAllText($cfgDst, $text, (New-Object Text.UTF8Encoding($false)))

$py = Join-Path $repo ".venv\Scripts\python.exe"
$sup = Join-Path $wt "scripts\collector.ps1"
$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$sup`" " +
       "-Root `"$wt`" -Config `"$cfgDst`" -EnvFile `"$(Join-Path $repo '.env')`" " +
       "-LogDir `"$(Join-Path $repo 'logs')`" -Python `"$py`""
# Win32_Process.Create starts the process outside this console's job object, so it
# survives this window closing.
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $wt
}
if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($r.ReturnValue)" }
Write-Host "collector supervisor started: pid $($r.ProcessId), code at $sha ($wt)"
Write-Host "logs: $(Join-Path $repo 'logs')\collector-*.log ; stop: New-Item $(Join-Path $repo 'KILL')"
