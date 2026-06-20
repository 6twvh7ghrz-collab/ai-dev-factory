. $PSScriptRoot/factory_runtime.ps1

$runtime = Get-FactoryRuntime
if (-not $runtime) {
    Write-Host '未找到运行时记录，未执行停止。'
    exit 0
}

$stopped = @()
foreach ($entry in @(
    @{ pid = $runtime.backend_shell_pid; kind = 'shell' },
    @{ pid = $runtime.frontend_shell_pid; kind = 'shell' },
    @{ pid = $runtime.backend_pid; kind = 'listener' },
    @{ pid = $runtime.frontend_pid; kind = 'listener' }
)) {
    $pid = $entry.pid
    if (-not $pid) { continue }
    $pid = [int]$pid
    $snap = Get-ProcessSnapshot -Pid $pid
    if (-not $snap) { continue }
    if ($entry.kind -eq 'listener') {
        $needles = @('uvicorn app.main:app', 'vite')
        if (-not (Test-ProjectProcess -Pid $pid -Needles $needles)) {
            Write-Host "跳过非本项目进程: PID=$pid Name=$($snap.Name)"
            continue
        }
    }
    & taskkill /PID $pid /T /F | Out-Null
    $stopped += $pid
}

Remove-FactoryRuntime
if ($stopped.Count -gt 0) {
    Write-Host "已停止进程: $($stopped -join ', ')"
} else {
    Write-Host '没有需要停止的本项目进程。'
}
