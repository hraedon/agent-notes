<#
.SYNOPSIS
    Run agent-notes-dispatch on Windows, or register it as a hidden logon task.

.DESCRIPTION
    Without -Register, runs the dispatcher in a loop: if it exits with an error
    it is restarted after a short, growing delay. Output goes to
    %LOCALAPPDATA%\agent-notes\dispatch.log (rotated by the dispatcher itself).

    With -Register, creates (or replaces) the scheduled task
    "agent-notes dispatcher" for the current user: at logon, hidden window
    (conhost --headless), restart 3 times at 1-minute intervals, no time limit.
    This is the same pattern as the "Paseo daemon" task.

    On Windows the dispatcher does L3 wakes only; review jobs need Linux and
    are answered with REVIEW_PLATFORM_UNSUPPORTED. Configuration is in
    docs/dispatcher.md. This script never prints environment values.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File agent-notes-dispatch.ps1 -Register
#>
[CmdletBinding()]
param(
    [switch]$Register,
    [string]$TaskName = "agent-notes dispatcher",
    # agent-notes-dispatch on PATH by default; otherwise a python.exe that has agent-notes.
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if ($Register) {
    $script = $MyInvocation.MyCommand.Path
    $arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
    if ($Python) { $arguments += " -Python `"$Python`"" }
    $action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "agent-notes Plan 025 dispatcher (L3 Paseo wake)" `
        -Force | Out-Null
    Write-Output "Registered scheduled task '$TaskName'. Start it with: Start-ScheduledTask -TaskName '$TaskName'"
    return
}

$logDir = Join-Path $env:LOCALAPPDATA "agent-notes"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:AGENT_NOTES_DISPATCH_LOG = Join-Path $logDir "dispatch.log"
$env:PYTHONUTF8 = "1"
# paseo.cmd lives in the npm global directory; make sure PATH finds it.
$npm = Join-Path $env:APPDATA "npm"
if (Test-Path $npm) { $env:PATH = "$npm;$env:USERPROFILE\.local\bin;$env:PATH" }

if ($Python) {
    $exe = $Python
    $exeArgs = @("-m", "agent_notes.dispatcher")
} else {
    $found = Get-Command agent-notes-dispatch -ErrorAction SilentlyContinue
    if (-not $found) { throw "agent-notes-dispatch is not on PATH; reinstall agent-notes or pass -Python" }
    $exe = $found.Source
    $exeArgs = @()
}

# Windows PowerShell 5.1 turns redirected native stderr into errors; don't stop on them.
$ErrorActionPreference = "Continue"
$delay = 5
while ($true) {
    $started = Get-Date
    & $exe @exeArgs 2>> (Join-Path $logDir "dispatch.stderr.log")
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }        # clean stop
    if ($code -eq 3) { exit 3 }       # no usable VM ID: set AGENT_NOTES_VM
    if (((Get-Date) - $started).TotalMinutes -gt 10) { $delay = 5 }
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 300)
}
