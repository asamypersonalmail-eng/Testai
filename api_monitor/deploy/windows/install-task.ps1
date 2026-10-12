<#
.SYNOPSIS
  Registers a Windows Task Scheduler job that runs one monitoring cycle every 5 minutes.
  Each service is still checked only when its own interval (from config.json) is due.
  Run from an elevated PowerShell prompt.
#>
param(
    [string]$AppHome = 'C:\api-monitor',
    [string]$TaskName = 'API Synthetic Monitor',
    [string]$RunAsUser = 'NT AUTHORITY\SYSTEM'   # better: a dedicated low-privilege service account
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $AppHome 'deploy\windows\run-monitor.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Mode once -AppHome `"$AppHome`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $RunAsUser -RunLevel Limited -Force | Out-Null
Write-Host "Registered task '$TaskName'. Check it with: Get-ScheduledTaskInfo -TaskName '$TaskName'"
