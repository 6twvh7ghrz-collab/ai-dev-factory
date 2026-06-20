. $PSScriptRoot/factory_runtime.ps1

# Restart is a stop-then-start wrapper with the same safety checks.
& $PSScriptRoot/stop_factory.ps1
Start-Sleep -Seconds 2
& $PSScriptRoot/start_factory.ps1
