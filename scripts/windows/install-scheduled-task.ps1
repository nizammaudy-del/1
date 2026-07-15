<#
.SYNOPSIS
    Registers a non-elevated logon-triggered Scheduled Task that keeps the
    Headroom proxy running persistently on Windows.

.DESCRIPTION
    Headroom's built-in `headroom install apply --preset persistent-service`
    and `--preset persistent-task` both fail with "Access is denied" on a
    standard (non-administrator) Windows account:

      - persistent-service calls `sc.exe create ...`, which requires an
        elevated SCM handle (OpenSCManager fails with error 5).
      - persistent-task registers the task with `/SC ONSTART`, which also
        requires administrator rights to create.

    This script is a workaround: it registers the same long-running
    `headroom proxy` process as a Scheduled Task using an `AtLogOn` trigger
    instead of `OnStart`. Per-user AtLogOn task registration does not require
    elevation, so it works on a standard account. See ../../wiki/windows-deployment.md
    for the full writeup and reproduction steps.

.PARAMETER PythonExe
    Path to the venv's python.exe that has headroom-ai installed.

.PARAMETER Port
    Proxy port. Defaults to 8787.

.PARAMETER TaskName
    Scheduled Task name. Defaults to HeadroomProxy.

.EXAMPLE
    .\install-scheduled-task.ps1 -PythonExe "C:\Users\me\headroom-ai\.venv\Scripts\python.exe"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [int]$Port = 8787,

    [string]$Mode = 'token',

    [string]$Backend = 'anthropic',

    [string]$TaskName = 'HeadroomProxy'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $PythonExe)) {
    throw "PythonExe not found: $PythonExe"
}

$proxyArgs = "-m headroom.cli proxy --host 127.0.0.1 --port $Port --mode $Mode --backend $Backend --telemetry"

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $proxyArgs -WorkingDirectory $HOME
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Headroom local proxy (non-admin persistent workaround)" -Force | Out-Null

Write-Host "Registered Scheduled Task '$TaskName' (AtLogOn, RunLevel=Limited, no elevation required)."
Write-Host "Start it now with: Start-ScheduledTask -TaskName '$TaskName'"
