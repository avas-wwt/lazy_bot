<#
.SYNOPSIS
    One-time setup for lazy_bot: Python, packages, your .env and CSV, and the weekly scheduled task.

.DESCRIPTION
    Safe to run again at any time: it only installs or creates what is missing, and re-registers the
    scheduled task with the RUN_DAY / RUN_TIME from your .env.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -SkipTask
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -RemoveTask
#>
param(
    [switch]$SkipTask,    # set everything up except the scheduled task
    [switch]$RemoveTask   # delete the scheduled task and stop
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$TaskName = 'lazy_bot timesheets'

if ($RemoveTask) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' scheduled task."
    } else {
        Write-Host "There is no '$TaskName' scheduled task."
    }
    return
}

function Find-Python {
    foreach ($path in "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe", "$env:ProgramFiles\Python313\python.exe") {
        if (Test-Path $path) { return $path }
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $exe = & $launcher.Source -3.13 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    return $null
}

# 1. Python 3.13 (per user, no admin)
$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Python 3.13 is missing and winget is not available. Install Python 3.13 from https://www.python.org/downloads/ and run setup.ps1 again.'
    }
    Write-Host 'Installing Python 3.13 for your user (python.org installer through winget)...'
    winget install --id Python.Python.3.13 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
    $python = Find-Python
    if (-not $python) { throw 'Python 3.13 did not install. Install it from https://www.python.org/downloads/ and run setup.ps1 again.' }
}
Write-Host "Python:   $python"

# 2. Virtual environment and packages (Playwright uses your installed Edge: nothing else to download)
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host 'Creating .venv...'
    & $python -m venv .venv
    if ($LASTEXITCODE) { throw 'Could not create the .venv folder.' }
}
Write-Host 'Packages: installing/updating from requirements.txt...'
& $venvPython -m pip install --disable-pip-version-check --quiet -r requirements.txt
if ($LASTEXITCODE) { throw 'pip install failed; see the messages above.' }

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { Write-Warning 'Microsoft Edge was not found; lazy_bot needs it for the browser platforms.' }

# 3. Your settings and your daily log (both stay out of git)
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host 'Created .env: open it and fill in your values.'
}
$csv = (& $venvPython -c "from lazy_bot.config import load_settings; print(load_settings().csv_path)").Trim()
if ($LASTEXITCODE) { throw '.env could not be read; fix the message above and run setup.ps1 again.' }
if (-not (Test-Path $csv)) {
    New-Item -ItemType Directory -Force (Split-Path $csv) | Out-Null
    # UTF-8 with a BOM, so Excel shows accented characters correctly.
    [IO.File]::WriteAllText($csv, "date,hours,description`r`n", (New-Object Text.UTF8Encoding($true)))
    Write-Host "Created $csv"
}
Write-Host "Log file: $csv"

# 4. The scheduled task: weekly at RUN_DAY/RUN_TIME, and at logon to catch up on a missed run
if ($SkipTask) {
    Write-Host 'Skipped the scheduled task (-SkipTask).'
    return
}
$when = (& $venvPython -c "from lazy_bot.config import DAY_NAMES, load_settings; s = load_settings(); print(DAY_NAMES[s.run_day], s.run_time.strftime('%H:%M'))").Trim()
$day, $time = $when -split ' '
$dayNames = @{ MON = 'Monday'; TUE = 'Tuesday'; WED = 'Wednesday'; THU = 'Thursday'; FRI = 'Friday'; SAT = 'Saturday'; SUN = 'Sunday' }
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $venvPython -Argument '-m lazy_bot run --pause' -WorkingDirectory $PSScriptRoot
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dayNames[$day] -At $time
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $me
$atLogon.Delay = 'PT3M'  # give the network/VPN a moment after signing in to Windows
# Interactive: runs only while you're signed in, in a window you can see (for MFA and the confirm prompt).
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$options = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $weekly, $atLogon -Principal $principal `
    -Settings $options -Description 'Fills your weekly timesheets from timesheet.csv (lazy_bot).' -Force | Out-Null
Write-Host "Task:     '$TaskName' runs every $($dayNames[$day]) at $time, and 3 minutes after you sign in to Windows."
Write-Host '          (When nothing is due it closes straight away.)'
