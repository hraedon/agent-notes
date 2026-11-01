<#
.SYNOPSIS
    Run agent-notes-embed-server on Windows, or register it as a hidden logon task.

.DESCRIPTION
    Without -Register, runs the embedding service in a loop: if it exits with
    an error it is restarted after a short, growing delay. Output goes to
    %LOCALAPPDATA%\agent-notes\embed.log.

    With -Register, creates (or replaces) the scheduled task
    "agent-notes embed" for the current user: at logon, hidden window
    (conhost --headless), restart 3 times at 1-minute intervals, no time limit.
    This is the same pattern as the "agent-notes dispatcher" task.

    The service binds 127.0.0.1:8766. Point the CLI at it by adding
    AGENT_NOTES_EMBED_URL=http://127.0.0.1:8766 to the per-user suite.env
    (%LOCALAPPDATA%\agent-suite\agent-suite\suite.env).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File agent-notes-embed.ps1 -Register
#>
[CmdletBinding()]
param(
    [switch]$Register,
    [string]$TaskName = "agent-notes embed",
    # agent-notes-embed-server on PATH by default; otherwise a python.exe that has agent-notes.
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
        -Settings $settings -Description "agent-notes warm embedding service" `
        -Force | Out-Null
    Write-Output "Registered scheduled task '$TaskName'. Start it with: Start-ScheduledTask -TaskName '$TaskName'"
    return
}

$logDir = Join-Path $env:LOCALAPPDATA "agent-notes"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:PYTHONUTF8 = "1"
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

if ($Python) {
    $exe = $Python
    $exeArgs = @("-m", "agent_notes.embed_server")
} else {
    $found = Get-Command agent-notes-embed-server -ErrorAction SilentlyContinue
    if (-not $found) { throw "agent-notes-embed-server is not on PATH; reinstall agent-notes or pass -Python" }
    $exe = $found.Source
    $exeArgs = @()
}

# Windows PowerShell 5.1 turns redirected native stderr into errors; don't stop on them.
$ErrorActionPreference = "Continue"
$delay = 5
while ($true) {
    $started = Get-Date
    & $exe @exeArgs 2>> (Join-Path $logDir "embed.log")
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }        # clean stop
    if (((Get-Date) - $started).TotalMinutes -gt 10) { $delay = 5 }
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 300)
}
