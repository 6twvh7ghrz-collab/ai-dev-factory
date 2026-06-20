from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_agent_runtime_scripts_exist():
    for name in [
        "start_agent_runtime.ps1",
        "stop_agent_runtime.ps1",
        "status_agent_runtime.ps1",
        "restart_agent_runtime.ps1",
    ]:
        assert (SCRIPTS / name).exists()


def test_scripts_manage_only_agent_runtime_pid_and_do_not_kill_global_processes():
    combined = "\n".join(
        _read(name)
        for name in [
            "start_agent_runtime.ps1",
            "stop_agent_runtime.ps1",
            "status_agent_runtime.ps1",
            "restart_agent_runtime.ps1",
        ]
    )
    lowered = combined.lower()
    assert "taskkill /im python.exe" not in lowered
    assert "taskkill" not in lowered
    assert "stop-process" not in lowered
    assert "uvicorn" not in lowered
    assert "vite" not in lowered
    assert "5173" not in lowered
    assert "8000" not in lowered
    assert "agent-runtime.pid" in combined
    assert "agent-runtime.stop" in combined


def test_start_script_defaults_to_mock_and_uses_agent_runtime_module():
    content = _read("start_agent_runtime.ps1")
    assert '[string]$Mode = "mock"' in content
    assert "app.agent_runtime" in content
    assert "Start-Process" in content


def test_status_and_stop_scripts_are_file_based():
    status = _read("status_agent_runtime.ps1")
    stop = _read("stop_agent_runtime.ps1")
    assert "agent-runtime.status.json" in status
    assert "agent-runtime.stop" in stop
    assert "Get-Process -Id" in stop


def test_config_example_is_safe_and_real_configs_ignored():
    example = ROOT / "config" / "agent_runtime.example.json"
    assert example.exists()
    text = example.read_text(encoding="utf-8")
    assert "api_key" not in text.lower()
    assert "database_url" not in text.lower()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/agent_runtime*.json" in gitignore
    assert "!config/agent_runtime.example.json" in gitignore
