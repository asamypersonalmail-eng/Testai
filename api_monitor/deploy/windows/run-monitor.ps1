<#
.SYNOPSIS
  Runs the API synthetic monitor on Windows, loading secrets from a protected file.

.EXAMPLE
  .\run-monitor.ps1 -Mode once          # single cycle (Task Scheduler)
  .\run-monitor.ps1 -Mode run           # continuous (console / NSSM service)
  .\run-monitor.ps1 -Mode validate -ExtraArgs '--check-secrets'
#>
param(
    [ValidateSet('run', 'once', 'check', 'validate', 'status', 'report', 'test-email')]
    [string]$Mode = 'once',
    [string]$AppHome = 'C:\api-monitor',
    [string]$Config = 'C:\ProgramData\api-monitor\config.json',
    [string]$Secrets = 'C:\ProgramData\api-monitor\secrets.env',
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

# Load KEY=VALUE lines into this process only (not into the machine environment).
if (Test-Path $Secrets) {
    foreach ($line in Get-Content -LiteralPath $Secrets) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $name = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        $hash = $value.IndexOf(' #')
        if ($hash -ge 0) { $value = $value.Substring(0, $hash).Trim() }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$python = Join-Path $AppHome 'venv\Scripts\python.exe'
& $python (Join-Path $AppHome 'monitor.py') -c $Config $Mode @ExtraArgs
exit $LASTEXITCODE
