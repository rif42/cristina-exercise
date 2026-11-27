#Requires -Version 5.1
<#
.SYNOPSIS
    Runs fetch_place.py every 5 minutes via Windows Task Scheduler.

.DESCRIPTION
    No parameters  -> run the fetch once (this is what the scheduled task invokes).
    -Register      -> create or refresh the scheduled task (repeats indefinitely).
    -Unregister    -> delete the scheduled task.

    Every run appends to logs\sync.log (rotated to sync.log.old past 1 MB).
    The schedule has no end date: it keeps firing every IntervalMinutes until the
    task is unregistered.
    The task runs as the current user and only while that user is logged on;
    overlapping runs are skipped, and a missed run starts as soon as possible.

.EXAMPLE
    .\sync_places.ps1 -Register
.EXAMPLE
    .\sync_places.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Unregister,
    [string]$TaskName = 'countries.dev places sync',
    [ValidateRange(1, 1440)][int]$IntervalMinutes = 5,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $scriptDir 'sync_places.ps1'
$fetchScript = Join-Path $scriptDir 'fetch_place.py'
$logDir = Join-Path $scriptDir 'logs'
$logPath = Join-Path $logDir 'sync.log'

function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "python executable not found: $Python"
        }
        return (Resolve-Path -LiteralPath $Python).Path
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw 'python not found on PATH; pass -Python <path to python.exe>'
    }
    return $cmd.Source
}

function Invoke-Fetch {
    $pythonExe = Resolve-Python
    if (-not (Test-Path -LiteralPath $fetchScript)) {
        throw "fetch script not found: $fetchScript"
    }

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.old" -Force
    }

    $started = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    Add-Content -LiteralPath $logPath -Value "[$started] start (pid $PID)"

    # Native stderr redirected through PowerShell becomes a terminating
    # NativeCommandError under $ErrorActionPreference = 'Stop', so run the fetch
    # with .NET process redirection and append the captured output ourselves.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pythonExe
    $psi.Arguments = '"{0}"' -f $fetchScript
    $psi.WorkingDirectory = $scriptDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    try {
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()
        $proc.WaitForExit()
        $code = $proc.ExitCode
        $output = $stdoutTask.Result
        $errors = $stderrTask.Result
    }
    finally {
        $proc.Dispose()
    }

    $entry = @()
    foreach ($text in @($output, $errors)) {
        if ($text) { $entry += ($text -split "`r?`n" | Where-Object { $_.Trim() -ne '' }) }
    }
    if ($entry.Count -gt 0) { Add-Content -LiteralPath $logPath -Value $entry }
    $finished = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    Add-Content -LiteralPath $logPath -Value "[$finished] exit $code"
    return $code
}

function Register-Sync {
    $pythonExe = Resolve-Python

    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $scriptPath, $pythonExe
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $scriptDir

    # No -RepetitionDuration: omitting it leaves <Duration> out of the task XML,
    # which Task Scheduler treats as "repeat indefinitely". Passing MaxValue (or
    # any huge value) is rejected at registration, so the trigger must stay open-ended.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Fetch countries.dev city data into places.db every $IntervalMinutes minutes." `
        -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $TaskName
    $nextRun = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
    Write-Host "registered '$TaskName': every $IntervalMinutes min, indefinitely"
    Write-Host "  action : powershell.exe $arguments"
    Write-Host "  runs as: $($principal.UserId) (interactive, logged-on only)"
    Write-Host "  next   : $nextRun"
    Write-Host "  log    : $logPath"
    return 0
}

function Unregister-Sync {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "task '$TaskName' is not registered"
        return 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task '$TaskName'"
    return 0
}

if ($Register -and $Unregister) {
    throw 'use either -Register or -Unregister'
}

$exitCode = if ($Register) { Register-Sync } elseif ($Unregister) { Unregister-Sync } else { Invoke-Fetch }
exit $exitCode
