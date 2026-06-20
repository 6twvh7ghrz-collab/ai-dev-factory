param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-FactoryRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-FactoryRuntimeDir {
    $root = Join-Path $env:TEMP 'ai-dev-factory-public'
    if (-not (Test-Path $root)) {
        New-Item -ItemType Directory -Path $root | Out-Null
    }
    return $root
}

function Get-FactoryRuntimeFile {
    return (Join-Path (Get-FactoryRuntimeDir) 'factory-runtime.json')
}

function Get-FactoryLogsDir {
    $dir = Join-Path (Get-FactoryRuntimeDir) 'logs'
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    return $dir
}

function Get-NodeExecutable {
    $cmd = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        'C:\Program Files\nodejs\node.exe',
        'C:\Program Files (x86)\nodejs\node.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw 'Node executable not found'
}

function Get-NpmExecutable {
    $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $nodeDir = Split-Path (Get-NodeExecutable) -Parent
    $candidate = Join-Path $nodeDir 'npm.cmd'
    if (Test-Path $candidate) { return $candidate }
    throw 'npm.cmd not found'
}

function Get-PythonExecutable {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    throw 'Python not found'
}

function Convert-SqliteUrlToPath {
    param([Parameter(Mandatory=$true)][string]$DatabaseUrl)
    $raw = $DatabaseUrl.Trim()
    if (-not $raw) { throw 'DATABASE_URL missing' }
    if ($raw -match '^sqlite:////') {
        return $raw.Substring('sqlite:////'.Length)
    }
    if ($raw -match '^sqlite:///') {
        return $raw.Substring('sqlite:///'.Length)
    }
    if ($raw -match '^sqlite:') {
        throw 'Unsupported database URL scheme'
    }
    if ($raw -match '^[A-Za-z][A-Za-z0-9+.-]*://') {
        throw 'Unsupported database URL scheme'
    }
    return $raw
}

function Get-ConfiguredDatabasePath {
    $backendDir = Join-Path (Get-FactoryRoot) 'backend'
    $python = Get-PythonExecutable
    $code = @'
from app.core.config import settings
value = getattr(settings, "DATABASE_URL", "") or ""
print(value)
'@
    Push-Location $backendDir
    try {
        $value = & $python -c $code 2>$null
    } finally {
        Pop-Location
    }
    if (-not $value) {
        throw 'Unable to resolve configured database URL'
    }
    return Convert-SqliteUrlToPath -DatabaseUrl ([string]$value)
}

function Get-ListenerPid {
    param([Parameter(Mandatory=$true)][int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Test-PortListening {
    param([Parameter(Mandatory=$true)][int]$Port)
    return [bool](Get-ListenerPid -Port $Port)
}

function Get-ProcessSnapshot {
    param([Parameter(Mandatory=$true)][int]$Pid)
    try {
        $proc = Get-Process -Id $Pid -ErrorAction Stop
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$Pid" -ErrorAction SilentlyContinue).CommandLine
        return [pscustomobject]@{
            Pid = $Pid
            Name = $proc.ProcessName
            CommandLine = $cmd
        }
    } catch {
        return $null
    }
}

function Get-FactoryRuntime {
    $file = Get-FactoryRuntimeFile
    if (-not (Test-Path $file)) { return $null }
    try {
        return (Get-Content $file -Raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Save-FactoryRuntime {
    param([Parameter(Mandatory=$true)][hashtable]$Data)
    $file = Get-FactoryRuntimeFile
    ($Data | ConvertTo-Json -Depth 8) | Set-Content -Path $file -Encoding UTF8
    return $file
}

function Remove-FactoryRuntime {
    $file = Get-FactoryRuntimeFile
    if (Test-Path $file) {
        Remove-Item $file -Force
    }
}

function Test-HttpOk {
    param(
        [Parameter(Mandatory=$true)][string]$Url,
        [int]$TimeoutSeconds = 3
    )
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSeconds -Uri $Url
        return [pscustomobject]@{
            Ok = ($resp.StatusCode -eq 200)
            StatusCode = $resp.StatusCode
            Content = $resp.Content
        }
    } catch {
        return [pscustomobject]@{
            Ok = $false
            StatusCode = $null
            Content = $_.Exception.Message
        }
    }
}

function Wait-HttpOk {
    param(
        [Parameter(Mandatory=$true)][string]$Url,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $r = Test-HttpOk -Url $Url -TimeoutSeconds 3
        if ($r.Ok) { return $r }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Timeout waiting for $Url"
}

function Test-ProjectProcess {
    param(
        [Parameter(Mandatory=$true)][int]$Pid,
        [Parameter(Mandatory=$true)][string[]]$Needles
    )
    $snap = Get-ProcessSnapshot -Pid $Pid
    if (-not $snap) { return $false }
    $hay = [string]$snap.CommandLine
    foreach ($needle in $Needles) {
        if ($hay -notlike "*$needle*") { return $false }
    }
    return $true
}
