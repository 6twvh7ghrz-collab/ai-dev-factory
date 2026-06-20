from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_runtime.b7b import B7BSafeIgnitionRunner
from app.agent_runtime.b7a import TaskExecutionPolicy


def _git(cmd, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *cmd],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Codex"], repo)
    _git(["config", "user.email", "codex@example.com"], repo)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text("from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "baseline"], repo)
    return repo


def test_b7b_safe_ignition_report_captures_all_required_steps(tmp_path):
    repo = _make_repo(tmp_path)
    runner = B7BSafeIgnitionRunner(repo)
    report_path = tmp_path / "b7b-report.json"

    report = runner.run(report_path=report_path)

    assert report.validate() == []
    assert report_path.exists()

    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["report_id"] == report.report_id
    assert loaded["summary"]["happy_path"]["changed_files"] == ["calculator.py"]
    assert loaded["summary"]["rollback_drill"]["restored"] is True

    steps = {step["step"]: step for step in loaded["steps"]}
    assert steps["policy_gate"]["ok"] is True
    assert steps["ai_patch"]["ok"] is True
    assert steps["diff_review"]["ok"] is True
    assert steps["apply_and_verify"]["ok"] is True
    assert steps["restore_checkpoint"]["ok"] is True
    assert steps["rollback_drill"]["ok"] is True
    assert steps["unauthorized_verification"]["ok"] is True
    assert steps["unauthorized_verification"]["code"] == "DIFF_REVIEW_DENIED"
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a - b\n")


def test_b7b_policy_gate_denies_unauthorized_configuration(tmp_path):
    repo = _make_repo(tmp_path)
    policy = TaskExecutionPolicy(
        task_type="SANDBOX_CODE_PATCH",
        temporary_project=True,
        project_id=56,
        sandbox_root=str(repo),
        allowed_files=["calculator.py"],
        forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        allowed_test_commands=["python -m pytest test_calculator.py"],
        max_files_changed=1,
        max_patch_bytes=1024,
        evidence_required=True,
        approval_token="one-time",
        mode="mock",
        control_plane_url="http://127.0.0.1:8000",
    )
    decision = policy.evaluate()
    assert decision.allowed is False
    assert decision.code == "TASK_EXECUTION_POLICY_DENIED"
