<#
.SYNOPSIS
    Removes the Scheduled Task created by install-scheduled-task.ps1 and stops
    any running proxy process it launched.

.PARAMETER TaskName
    Scheduled Task name. Defaults to HeadroomProxy.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'HeadroomProxy'
)

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No Scheduled Task named '$TaskName' found; nothing to do."
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Removed Scheduled Task '$TaskName'."
Write-Host "Note: this does not kill an already-running proxy process; stop it manually if needed."
