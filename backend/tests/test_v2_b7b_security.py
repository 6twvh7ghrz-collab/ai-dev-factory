from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_runtime.b7a import PatchFile, PatchProposal, WorkspaceSnapshotBuilder
from app.agent_runtime.b7b import B7BDiffReviewService, B7BSafeIgnitionRunner


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


def _snapshot(repo: Path):
    return WorkspaceSnapshotBuilder(
        workspace_root=repo,
        allowed_files=["calculator.py"],
        allowed_test_commands=["python -m pytest test_calculator.py"],
        forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        temporary_project=True,
        project_id=9300,
        task_packet={
            "task_id": 9301,
            "project_id": 9300,
            "task_type": "SANDBOX_CODE_PATCH",
            "temporary_project": True,
            "allowed_files": ["calculator.py"],
            "allowed_test_commands": ["python -m pytest test_calculator.py"],
            "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
            "max_files_changed": 1,
            "max_patch_bytes": 1024,
            "evidence_required": True,
            "worker_id": "b7b-worker",
            "approval_token": "one-time",
            "mode": "mock",
            "sandbox_root": str(repo),
            "control_plane_url": "http://127.0.0.1:8000",
        },
    ).build()


def _proposal(rel_path: str, content: str, snapshot) -> PatchProposal:
    expected = snapshot.file_hashes.get(rel_path, hashlib.sha256(b"").hexdigest())
    return PatchProposal(
        proposal_id="pp-test",
        task_id=9301,
        provider="mock",
        files=[
            PatchFile(
                relative_path=rel_path,
                operation="modify",
                expected_sha256=expected,
                new_content=content,
                encoding="utf-8",
            )
        ],
        unified_diff=f"--- a/{rel_path}\n+++ b/{rel_path}\n@@\n+{content}",
        explanation="test proposal",
        expected_tests=["python -m pytest test_calculator.py"],
        risks=[],
        generated_at="2026-01-01T00:00:00Z",
        metadata={},
    )


def test_b7b_happy_path_restores_baseline_and_redacts_report(tmp_path):
    repo = _make_repo(tmp_path)
    report_path = tmp_path / "report.json"
    report = B7BSafeIgnitionRunner(repo).run(report_path=report_path)

    assert report.validate() == []
    text = report_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "lease_token" not in text
    assert "api_key" not in text.lower()
    assert "database_url" not in text.lower()
    assert data["summary"]["happy_path"]["changed_files"] == ["calculator.py"]
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a - b\n")


def test_b7b_diff_review_allows_only_the_allowed_file(tmp_path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo)
    review = B7BDiffReviewService().review(_proposal("calculator.py", "def add(a, b):\n    return a + b\n", snapshot), ["calculator.py"])
    assert review.ok is True
    assert review.code == "DIFF_REVIEW_APPROVED"


def test_b7b_diff_review_rejects_out_of_scope_paths(tmp_path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo)
    reviewer = B7BDiffReviewService()

    for rel_path in ["../escape.py", "/abs.py"]:
        review = reviewer.review(_proposal(rel_path, "x", snapshot), [rel_path])
        assert review.ok is False
        assert review.code == "DIFF_REVIEW_DENIED"

    for rel_path in [".env", ".git/config", "data.sqlite", "data.sqlite3", "payload.exe", "library.dll", "blob.bin"]:
        review = reviewer.review(_proposal(rel_path, "x", snapshot), [rel_path])
        assert review.ok is False
        assert review.code == "DIFF_REVIEW_DENIED"


def test_b7b_runner_rollback_and_unauthorized_verification_leave_workspace_unchanged(tmp_path):
    repo = _make_repo(tmp_path)
    before = (repo / "calculator.py").read_text(encoding="utf-8")
    report = B7BSafeIgnitionRunner(repo).run()
    after = (repo / "calculator.py").read_text(encoding="utf-8")

    assert before == after
    assert any(step.step == "rollback_drill" and step.ok for step in report.steps)
    assert any(step.step == "unauthorized_verification" and step.ok for step in report.steps)


def test_b7b_utf8_capture_handles_non_utf8_output(tmp_path):
    script = tmp_path / "emit_gbk.py"
    script.write_text("import sys\nsys.stdout.buffer.write('你好'.encode('gbk'))\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    assert result.returncode == 0
    assert result.stdout
