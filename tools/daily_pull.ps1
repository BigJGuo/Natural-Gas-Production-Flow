# Daily NG feedgas pull — invoked by Windows Task Scheduler.
#
# Scrapes all pipeline EBBs for yesterday's evening cycle and writes to
# data/feedgas.db. Appends a timestamped log to data/logs/daily_pull.log.
#
# Register/refresh the scheduled task with:  tools/register_daily_task.ps1
# Run manually to test:                      powershell -File tools/daily_pull.ps1

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\Intern\AppData\Local\Programs\Python\Python312\python.exe"
$LogDir = Join-Path $ProjectRoot "data\logs"
$LogFile = Join-Path $LogDir "daily_pull.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $stamp  daily pull START ====="

Set-Location $ProjectRoot
$env:PYTHONPATH = "src"

# Pull all scrapers for yesterday's evening cycle. stderr is merged so any
# per-scraper failures land in the log without aborting the whole run.
$output = & $Python -m ng_feedgas pull --pipeline all --date yesterday --cycle evening 2>&1
Add-Content -Path $LogFile -Value $output

$endStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $endStamp  daily pull END (exit=$LASTEXITCODE) ====="
Add-Content -Path $LogFile -Value ""
