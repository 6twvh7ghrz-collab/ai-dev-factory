param(
    [string]$Config = "",
    [string]$Mode = "mock",
    [int]$AllowedTaskId = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backend = Join-Path $root 'backend'
$runtimeDir = Join-Path $env:TEMP 'ai-dev-factory-public\agent-runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$pidFile = Join-Path $runtimeDir 'agent-runtime.pid'
$statusFile = Join-Path $runtimeDir 'agent-runtime.status.json'

if (Test-Path $pidFile) {
    $existing = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($existing -and (Get-Process -Id ([int]$existing) -ErrorAction SilentlyContinue)) {
        Write-Host "Agent runtime already running: PID=$existing"
        exit 1
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw 'Python not found' }

$argsList = @('-m', 'app.agent_runtime', 'start', '--mode', $Mode)
if ($Config) { $argsList += @('--config', $Config) }
if ($AllowedTaskId -gt 0) { $argsList += @('--allowed-task-id', "$AllowedTaskId") }

$proc = Start-Process -FilePath $python -ArgumentList $argsList -WorkingDirectory $backend -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 500

Write-Host "Agent runtime launch requested: PID=$($proc.Id)"
Write-Host "Status file: $statusFile"
