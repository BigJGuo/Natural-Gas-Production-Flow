# Intraday fast-scraper pull — invoked every 5 min by Windows Task Scheduler.
#
# Runs the 7 plain-HTTP scrapers for TODAY's gas day at the latest-posted cycle
# (--cycle auto picks by US/Central clock). Idempotent: upsert_flows refreshes
# rows in place, so repeated 5-min runs never create duplicates.
#
# Registered by tools/register_intraday_tasks.ps1 as NG-Feedgas-Intraday-Fast.
# Run manually to test:  powershell -File tools/intraday_fast.ps1

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\Intern\AppData\Local\Programs\Python\Python312\python.exe"
$LogDir = Join-Path $ProjectRoot "data\logs"
$LogFile = Join-Path $LogDir "intraday_fast.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $stamp  intraday FAST START ====="

Set-Location $ProjectRoot
$env:PYTHONPATH = "src"

$output = & $Python -m ng_feedgas pull --pipeline fast --date today --cycle auto 2>&1
Add-Content -Path $LogFile -Value $output

$endStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $endStamp  intraday FAST END (exit=$LASTEXITCODE) ====="
Add-Content -Path $LogFile -Value ""
