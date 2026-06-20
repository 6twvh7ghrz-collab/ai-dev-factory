$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $scriptDir "..\backend"

Push-Location $backendDir
try {
    python -m app.tools.v2_sandbox_rehearsal @args
}
finally {
    Pop-Location
}

