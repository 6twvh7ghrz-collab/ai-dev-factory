param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtimeDir = Join-Path $env:TEMP 'ai-dev-factory-public\agent-runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$stopFile = Join-Path $runtimeDir 'agent-runtime.stop'
$pidFile = Join-Path $runtimeDir 'agent-runtime.pid'

Set-Content -Path $stopFile -Value 'stop' -Encoding UTF8

if (Test-Path $pidFile) {
    $pidValue = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($pidValue) {
        for ($i = 0; $i -lt 30; $i++) {
            if (-not (Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)) {
                Write-Host "Agent runtime stopped: PID=$pidValue"
                exit 0
            }
            Start-Sleep -Milliseconds 500
        }
        Write-Host "Stop requested; process still exiting: PID=$pidValue"
        exit 1
    }
}

Write-Host 'Agent runtime stop requested; no PID file present.'
