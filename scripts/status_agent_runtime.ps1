param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtimeDir = Join-Path $env:TEMP 'ai-dev-factory-public\agent-runtime'
$statusFile = Join-Path $runtimeDir 'agent-runtime.status.json'
$pidFile = Join-Path $runtimeDir 'agent-runtime.pid'

if (Test-Path $statusFile) {
    Get-Content $statusFile -Raw
} else {
    Write-Host '{"runtime_status":"STOPPED"}'
}

if (Test-Path $pidFile) {
    $pidValue = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($pidValue -and (Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)) {
        Write-Host "Process: RUNNING PID=$pidValue"
    } else {
        Write-Host 'Process: STOPPED'
    }
} else {
    Write-Host 'Process: STOPPED'
}
