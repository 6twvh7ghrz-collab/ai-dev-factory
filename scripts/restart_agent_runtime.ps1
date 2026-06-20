param(
    [string]$Config = "",
    [string]$Mode = "mock",
    [int]$AllowedTaskId = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'stop_agent_runtime.ps1') | Out-Host
Start-Sleep -Seconds 1

$argsList = @('-Mode', $Mode)
if ($Config) { $argsList += @('-Config', $Config) }
if ($AllowedTaskId -gt 0) { $argsList += @('-AllowedTaskId', "$AllowedTaskId") }
& (Join-Path $PSScriptRoot 'start_agent_runtime.ps1') @argsList
