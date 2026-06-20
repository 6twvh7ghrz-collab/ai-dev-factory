. $PSScriptRoot/factory_runtime.ps1

$root = Get-FactoryRoot
$backendPid = Get-ListenerPid -Port 8000
$frontendPid = Get-ListenerPid -Port 5173
$backendHealth = if ($backendPid) { (Test-HttpOk -Url 'http://127.0.0.1:8000/api/health' -TimeoutSeconds 3).StatusCode } else { $null }
$frontendHealth = if ($frontendPid) { (Test-HttpOk -Url 'http://127.0.0.1:5173/' -TimeoutSeconds 3).StatusCode } else { $null }
$runtime = Get-FactoryRuntime

Write-Host "Git Branch: $(git -C $root branch --show-current)"
Write-Host "Git HEAD: $(git -C $root log -1 --oneline)"
Write-Host "Backend PID: $backendPid"
Write-Host "Backend Health: $backendHealth"
Write-Host "Frontend PID: $frontendPid"
Write-Host "Frontend URL: http://127.0.0.1:5173/"
Write-Host "Frontend Health: $frontendHealth"

$dbPath = Get-ConfiguredDatabasePath
$python = Get-PythonExecutable
$code = @'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
cur = conn.cursor()
def one(sql):
    cur.execute(sql)
    return cur.fetchone()[0]
print(one("SELECT COALESCE((SELECT execution_enabled FROM project_execution_configs WHERE project_id=56), 0)"))
print(one("SELECT COALESCE((SELECT execution_enabled FROM project_execution_configs WHERE project_id=118), 0)"))
print(one("SELECT COUNT(*) FROM executor_runs WHERE status IN ('running','starting','claiming')"))
print(one("SELECT COUNT(*) FROM task_assignments WHERE status IN ('assigned','acknowledged','running','retrying')"))
print(one("SELECT COUNT(*) FROM executor_resource_locks"))
'@
$stats = & $python -c $code $dbPath
$vals = @($stats)
Write-Host "Project 56 execution_enabled: $($vals[0])"
Write-Host "Project 118 execution_enabled: $($vals[1])"
Write-Host "active_executor_runs: $($vals[2])"
Write-Host "active_task_leases: $($vals[3])"
Write-Host "active_executor_resource_locks: $($vals[4])"
if ($runtime) {
    Write-Host "Runtime File: present"
}
