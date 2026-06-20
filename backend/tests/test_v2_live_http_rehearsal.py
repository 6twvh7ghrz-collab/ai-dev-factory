"""Live HTTP sandbox rehearsal tests for V2 control plane."""

from __future__ import annotations

import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools.v2_sandbox_rehearsal import BACKEND_DIR, SandboxRehearsalRunner


def test_sandbox_rehearsal_runner_executes_all_scenarios_and_cleans_up():
    runner = SandboxRehearsalRunner(preferred_port=0)
    port = runner.harness.port
    report = runner.run()

    assert report["rehearsal_id"]
    assert report["temp_port"] == port
    assert report["formal_db_unchanged"] is True
    assert report["cleanup"]["backend_stopped"] is True
    assert report["cleanup"]["db_removed"] is True
    assert report["cleanup"]["workspace_removed"] is True
    assert set(report["scenarios"]) >= {
        "happy_path",
        "rework_handoff",
        "quota_and_expiry",
        "need_user_and_blocked",
        "conflicts",
    }
    assert report["scenarios"]["happy_path"]["task_state"] == "VERIFIED"
    assert report["scenarios"]["rework_handoff"]["task_state"] == "VERIFIED"
    assert report["scenarios"]["quota_and_expiry"]["claim_expiry_state"] == "QUEUED"
    assert report["scenarios"]["quota_and_expiry"]["running_expiry_state"] == "BLOCKED"
    assert report["scenarios"]["need_user_and_blocked"]["need_user"] == "NEED_USER"
    assert report["scenarios"]["need_user_and_blocked"]["blocked_action"] == "STOP_AND_REPORT_BLOCKER"
    assert report["event_count"] > 0
    assert report["api_request_count"] > 0


def test_sandbox_rehearsal_does_not_touch_live_services_or_leak_sensitive_data():
    session = requests.Session()
    session.trust_env = False
    before_8000 = session.get("http://127.0.0.1:8000/api/health", timeout=5).status_code
    before_5173 = session.get("http://127.0.0.1:5173", timeout=5).status_code

    runner = SandboxRehearsalRunner(preferred_port=0)
    report = runner.run()

    after_8000 = session.get("http://127.0.0.1:8000/api/health", timeout=5).status_code
    after_5173 = session.get("http://127.0.0.1:5173", timeout=5).status_code

    assert before_8000 == 200 and after_8000 == 200
    assert before_5173 == 200 and after_5173 == 200
    assert report["formal_db_unchanged"] is True


def test_run_script_exists_and_uses_rehearsal_module():
    script = BACKEND_DIR.parent / "scripts" / "run_v2_sandbox_rehearsal.ps1"
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert "app.tools.v2_sandbox_rehearsal" in content
