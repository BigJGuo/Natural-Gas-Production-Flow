# Intraday slow-scraper pull — invoked every 15 min by Windows Task Scheduler.
#
# Runs the 2 Playwright scrapers (tceconnects = Cameron/Plaquemines via TC eConnects,
# iroquois = Waddington) for TODAY's gas day at the latest-posted cycle. These launch
# headless Chromium and take ~2-3 min combined, and their data only changes per cycle,
# so they run less often than the fast HTTP group. Idempotent upserts (no duplicates).
#
# Registered by tools/register_intraday_tasks.ps1 as NG-Feedgas-Intraday-Slow.
# Run manually to test:  powershell -File tools/intraday_slow.ps1

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\Intern\AppData\Local\Programs\Python\Python312\python.exe"
$LogDir = Join-Path $ProjectRoot "data\logs"
$LogFile = Join-Path $LogDir "intraday_slow.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $stamp  intraday SLOW START ====="

Set-Location $ProjectRoot
$env:PYTHONPATH = "src"

$output = & $Python -m ng_feedgas pull --pipeline slow --date today --cycle auto 2>&1
Add-Content -Path $LogFile -Value $output

$endStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $endStamp  intraday SLOW END (exit=$LASTEXITCODE) ====="
Add-Content -Path $LogFile -Value ""
