# Hourly AIS collection + daily inference — invoked by Windows Task Scheduler.
#
# Listens on the aisstream.io feed for 10 minutes around the LNG-terminal
# bounding boxes, then rolls today's observations into per-terminal estimates.
# Run frequently (hourly) so berth occupancy builds up — LNG carriers moor for
# 12-24h, so a single short listen catches little, but 24 windows/day does.
#
# Register/refresh with:  tools/register_ais_task.ps1  (or see chat)
# Run manually to test:   powershell -File tools/ais_track.ps1

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\Intern\AppData\Local\Programs\Python\Python312\python.exe"
$LogDir = Join-Path $ProjectRoot "data\logs"
$LogFile = Join-Path $LogDir "ais_track.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $stamp  AIS track START ====="

Set-Location $ProjectRoot
$env:PYTHONPATH = "src"

# 1) Collect for 10 minutes
$collect = & $Python -m ng_feedgas ais-collect --duration 600 2>&1
Add-Content -Path $LogFile -Value $collect

# 2) Roll today's observations into per-terminal inference
$infer = & $Python -m ng_feedgas ais-infer --date today 2>&1
Add-Content -Path $LogFile -Value $infer

$endStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "===== $endStamp  AIS track END (exit=$LASTEXITCODE) ====="
Add-Content -Path $LogFile -Value ""
