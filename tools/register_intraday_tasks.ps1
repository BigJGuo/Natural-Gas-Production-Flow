# Registers the two continuous intraday scraping tasks (idempotent).
#
#   NG-Feedgas-Intraday-Fast  — 7 HTTP scrapers, every 5 minutes
#   NG-Feedgas-Intraday-Slow  — 2 Playwright scrapers, every 15 minutes
#
# Both fire at logon + startup and then repeat on their interval indefinitely,
# run only while the user is logged on (no stored password; Playwright-safe),
# and skip a tick if the previous run is still going (MultipleInstances IgnoreNew).
#
# Run:  powershell -File tools/register_intraday_tasks.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

function Register-IntradayTask {
    param(
        [string]$TaskName,
        [string]$ScriptName,
        [string]$Interval,        # ISO 8601 duration, e.g. PT5M
        [int]$TimeLimitMinutes,
        [string]$Description
    )
    $script = Join-Path $ProjectRoot "tools\$ScriptName"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NonInteractive -WindowStyle Hidden -File `"$script`""

    # A single -Once trigger with RepetitionInterval (no duration => indefinite),
    # starting at the next top of the minute. This is the same pattern the working
    # AIS task uses; it does NOT require admin (an AtStartup trigger would). With
    # StartWhenAvailable it resumes after a reboot once the user logs back on.
    $minutes = [int]([System.Xml.XmlConvert]::ToTimeSpan($Interval)).TotalMinutes
    $startAt = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes(((Get-Date).Minute) + 1)
    $trigger = New-ScheduledTaskTrigger -Once -At $startAt `
        -RepetitionInterval (New-TimeSpan -Minutes $minutes)

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes) `
        -MultipleInstances IgnoreNew `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings `
        -Description $Description | Out-Null
    Write-Output "Registered $TaskName (every $Interval)"
}

Register-IntradayTask -TaskName "NG-Feedgas-Intraday-Fast" -ScriptName "intraday_fast.ps1" `
    -Interval "PT5M" -TimeLimitMinutes 10 `
    -Description "Fast HTTP pipeline scrapers every 5 min (today, auto cycle). Logs to data/logs/intraday_fast.log."

Register-IntradayTask -TaskName "NG-Feedgas-Intraday-Slow" -ScriptName "intraday_slow.ps1" `
    -Interval "PT15M" -TimeLimitMinutes 15 `
    -Description "Playwright pipeline scrapers every 15 min (today, auto cycle). Logs to data/logs/intraday_slow.log."

Write-Output ""
Write-Output "Current state:"
Get-ScheduledTask -TaskName "NG-Feedgas-Intraday-*" |
    Select-Object TaskName, State | Format-Table -AutoSize | Out-String
