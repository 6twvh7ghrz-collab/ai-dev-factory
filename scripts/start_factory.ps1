. $PSScriptRoot/factory_runtime.ps1

$root = Get-FactoryRoot
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$python = Get-PythonExecutable
$node = Get-NodeExecutable
$npm = Get-NpmExecutable
$nodeDir = Split-Path $node -Parent
$backendUrl = 'http://127.0.0.1:8000/api/health'
$frontendUrl = 'http://127.0.0.1:5173/'
$backendLog = Join-Path (Get-FactoryLogsDir) 'backend.log'
$frontendLog = Join-Path (Get-FactoryLogsDir) 'frontend.log'
$backendShellPid = $null
$frontendShellPid = $null

$backendPid = Get-ListenerPid -Port 8000
$frontendPid = Get-ListenerPid -Port 5173

$backendHealthy = $false
if ($backendPid) {
    $backendHealthy = (Test-HttpOk -Url $backendUrl -TimeoutSeconds 3).Ok
}

$frontendHealthy = $false
if ($frontendPid) {
    $frontendHealthy = (Test-HttpOk -Url $frontendUrl -TimeoutSeconds 3).Ok
}

if ($backendPid -and $backendHealthy -and $frontendPid -and $frontendHealthy) {
    Save-FactoryRuntime @{
        backend_shell_pid = $backendPid
        frontend_shell_pid = $frontendPid
        backend_pid = $backendPid
        frontend_pid = $frontendPid
        backend_port = 8000
        frontend_port = 5173
        backend_url = $backendUrl
        frontend_url = $frontendUrl
        started_at = (Get-Date).ToString('s')
    } | Out-Null
    Write-Host '后端和前端已在运行，未重复启动。'
    Write-Host "Backend PID: $backendPid"
    Write-Host "Frontend PID: $frontendPid"
    Write-Host 'Health: OK'
    Start-Process $frontendUrl | Out-Null
    exit 0
}

if ($backendPid -and -not $backendHealthy) {
    $snap = Get-ProcessSnapshot -Pid $backendPid
    Write-Host "端口 8000 已被进程占用: PID=$backendPid Name=$($snap.Name)"
    exit 1
}
if ($frontendPid -and -not $frontendHealthy) {
    $snap = Get-ProcessSnapshot -Pid $frontendPid
    Write-Host "端口 5173 已被进程占用: PID=$frontendPid Name=$($snap.Name)"
    exit 1
}

if (-not $backendPid) {
    $backendArgs = '-NoExit', '-Command', "Set-Location '$backendDir'; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 *> '$backendLog'"
    $backendProc = Start-Process powershell.exe -WindowStyle Hidden -PassThru -WorkingDirectory $backendDir -ArgumentList $backendArgs
    $backendShellPid = $backendProc.Id
    Wait-HttpOk -Url $backendUrl -TimeoutSeconds 60 | Out-Null
    $backendPid = Get-ListenerPid -Port 8000
}

if (-not $frontendPid) {
    $env:PATH = "$nodeDir;$env:PATH"
    $frontendArgs = '-NoExit', '-Command', "Set-Location '$frontendDir'; npm.cmd run dev -- --host 127.0.0.1 *> '$frontendLog'"
    $frontendProc = Start-Process powershell.exe -WindowStyle Hidden -PassThru -WorkingDirectory $frontendDir -ArgumentList $frontendArgs
    $frontendShellPid = $frontendProc.Id
    Wait-HttpOk -Url $frontendUrl -TimeoutSeconds 90 | Out-Null
    $frontendPid = Get-ListenerPid -Port 5173
}

Save-FactoryRuntime @{
    backend_shell_pid = $backendShellPid
    frontend_shell_pid = $frontendShellPid
    backend_pid = $backendPid
    frontend_pid = $frontendPid
    backend_port = 8000
    frontend_port = 5173
    backend_url = $backendUrl
    frontend_url = $frontendUrl
    backend_log = $backendLog
    frontend_log = $frontendLog
    started_at = (Get-Date).ToString('s')
} | Out-Null

Start-Process $frontendUrl | Out-Null

Write-Host '启动完成。'
Write-Host "Backend PID: $backendPid"
Write-Host "Frontend PID: $frontendPid"
Write-Host "Frontend URL: $frontendUrl"
