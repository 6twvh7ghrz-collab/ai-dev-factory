from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [
    ROOT / "scripts" / "start_factory.ps1",
    ROOT / "scripts" / "stop_factory.ps1",
    ROOT / "scripts" / "status_factory.ps1",
    ROOT / "scripts" / "restart_factory.ps1",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_runtime_scripts_exist():
    for script in SCRIPTS:
        assert script.exists(), script


@pytest.mark.parametrize("needle", [
    "taskkill /IM python.exe",
    "taskkill /IM node.exe",
    "execution_enabled = 1",
    "execution_enabled=1",
    "V2_CONTROL_PLANE_ENABLED=true",
    "api_key",
    "password",
    "authorization",
])
def test_runtime_scripts_do_not_contain_forbidden_patterns(needle):
    texts = "\n".join(_read(path) for path in SCRIPTS)
    assert needle not in texts.lower()


def test_start_script_handles_node_and_npm_fallbacks():
    text = _read(ROOT / "scripts" / "start_factory.ps1").lower()
    assert "get-nodeexecutable" in text
    assert "get-npmexecutable" in text
    assert "npm.cmd run dev" in text
    assert "api/health" in text
    assert "127.0.0.1:5173" in text


def test_stop_script_only_uses_runtime_file():
    text = _read(ROOT / "scripts" / "stop_factory.ps1").lower()
    assert "get-factoryruntime" in text
    assert "taskkill /pid" in text
    assert "taskkill /im" not in text


def test_status_script_reports_read_only_information():
    text = _read(ROOT / "scripts" / "status_factory.ps1").lower()
    assert "active_executor_runs" in text
    assert "active_task_leases" in text
    assert "active_executor_resource_locks" in text
    assert "project 56 execution_enabled" in text
    assert "project 118 execution_enabled" in text


def test_restart_script_composes_stop_then_start():
    text = _read(ROOT / "scripts" / "restart_factory.ps1").lower()
    assert "stop_factory.ps1" in text
    assert "start_factory.ps1" in text
