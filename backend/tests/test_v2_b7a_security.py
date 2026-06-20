from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_runtime.b7a import (
    B7AExecutionBridge,
    B7ARuntimeService,
    EnvSecretProvider,
    MemorySecretProvider,
    MockProvider,
    OpenAICompatibleProvider,
    PatchApplicationService,
    PatchFile,
    PatchProposal,
    TaskExecutionPolicy,
    WorkspaceSnapshotBuilder,
)
from app.agent_runtime.b7a.patch_application import PatchApplicationError


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
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / "notes.txt").write_text("workspace notes\n", encoding="utf-8")
    (repo / "bundle.png").write_text("pretend binary payload\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "baseline"], repo)
    return repo


def _snapshot(repo: Path, **overrides):
    task_packet = {
        "task_id": 1,
        "project_id": 9001,
        "task_type": "SANDBOX_CODE_PATCH",
        "temporary_project": True,
        "allowed_files": ["calculator.py"],
        "allowed_test_commands": ["python -m pytest test_calculator.py"],
        "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        "max_files_changed": 1,
        "max_patch_bytes": 1024,
        "evidence_required": True,
        "worker_id": "b7a-worker",
    }
    task_packet.update(overrides)
    return WorkspaceSnapshotBuilder(
        workspace_root=repo,
        allowed_files=task_packet["allowed_files"],
        allowed_test_commands=task_packet["allowed_test_commands"],
        forbidden_actions=task_packet["forbidden_actions"],
        temporary_project=task_packet["temporary_project"],
        project_id=task_packet["project_id"],
        task_packet=task_packet,
    ).build()


def _good_policy(repo: Path, **overrides):
    data = {
        "task_type": "SANDBOX_CODE_PATCH",
        "temporary_project": True,
        "project_id": 9001,
        "sandbox_root": str(repo),
        "allowed_files": ["calculator.py"],
        "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        "allowed_test_commands": ["python -m pytest test_calculator.py"],
        "max_files_changed": 1,
        "max_patch_bytes": 1024,
        "evidence_required": True,
        "approval_token": "one-time",
        "mode": "mock",
        "control_plane_url": "http://127.0.0.1:8000",
    }
    data.update(overrides)
    return TaskExecutionPolicy(**data)


def test_mock_provider_happy_path_and_redaction(tmp_path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo)
    provider = MockProvider()
    proposal = provider.generate_patch(snapshot.task_packet, snapshot)
    assert proposal.provider == "mock"
    assert proposal.files[0].relative_path == "calculator.py"
    assert "return a + b" in proposal.files[0].new_content
    assert provider.health_check()["redacted"]["provider"] == "mock"


def test_openai_compatible_provider_defaults_closed_and_requires_secret():
    provider = OpenAICompatibleProvider()
    assert provider.validate_config()["error_code"] == "PROVIDER_DISABLED"
    provider = OpenAICompatibleProvider(enabled=True, secret_configured=False)
    assert provider.validate_config()["error_code"] == "SECRET_NOT_CONFIGURED"
    assert provider.redact_config()["configured"] is False


def test_secret_provider_status_is_boolean_only(monkeypatch):
    monkeypatch.setenv("B7A_SECRET_TOKEN", "super-secret")
    env = EnvSecretProvider({"token": "env:B7A_SECRET_TOKEN"})
    mem = MemorySecretProvider({"token": "value"})
    assert env.status() == {"configured": True}
    assert mem.status() == {"configured": True}
    assert env.resolve("token") == "super-secret"
    assert mem.resolve("token") == "value"
    assert "secret" not in str(env.status()).lower()


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"task_type": None}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"task_type": "OTHER"}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"temporary_project": False}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"project_id": 56}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"project_id": 118}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"allowed_files": []}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"forbidden_actions": []}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"allowed_test_commands": []}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"max_files_changed": 0}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"max_patch_bytes": 0}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"evidence_required": False}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"approval_token": None, "approval_record": None}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"mode": "live"}, "TASK_EXECUTION_POLICY_DENIED"),
        ({"control_plane_url": "http://example.com"}, "TASK_EXECUTION_POLICY_DENIED"),
    ],
)
def test_task_execution_policy_denials(tmp_path, overrides, code):
    repo = _make_repo(tmp_path)
    policy = _good_policy(repo, **overrides)
    decision = policy.evaluate()
    assert decision.code == code
    assert decision.allowed is False


def test_task_execution_policy_allows_mock_patch(tmp_path):
    repo = _make_repo(tmp_path)
    assert _good_policy(repo).evaluate().allowed is True


def test_patch_application_allows_scoped_text_patch_and_test(tmp_path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo)
    service = PatchApplicationService(repo)
    service.verify_workspace()
    checkpoint = service.create_checkpoint()
    proposal = PatchProposal(
        proposal_id="pp-1",
        task_id=1,
        provider="mock",
        files=[
            PatchFile(
                relative_path="calculator.py",
                operation="modify",
                expected_sha256=snapshot.file_hashes["calculator.py"],
                new_content="def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
        ],
        unified_diff="--- a/calculator.py\n+++ b/calculator.py\n@@\n-    return a - b\n+    return a + b\n",
        explanation="fix add",
        expected_tests=["python -m pytest test_calculator.py"],
        risks=["none"],
        generated_at="2026-06-20T00:00:00Z",
        metadata={},
    )
    service.validate_proposal(proposal, snapshot)
    changed = service.apply_patch_proposal(proposal)
    assert changed == ["calculator.py"]
    test_result = service.run_allowed_test("python -m pytest test_calculator.py")
    assert test_result["ok"] is True
    evidence = service.finalize_evidence(
        proposal=proposal,
        changed_files=changed,
        tests_run=[test_result],
        summary="passed",
    )
    assert evidence.files_changed == ["calculator.py"]
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a + b\n")
    service.rollback(checkpoint)
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a - b\n")


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        r"..\\escape.py",
        r"C:\\Windows\\escape.py",
        r"\\\\server\\share\\escape.py",
        "file://escape.py",
        ".env",
        ".git/config",
        "data.db",
        "bundle.png",
    ],
)
def test_patch_paths_rejected(tmp_path, path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo)
    service = PatchApplicationService(repo)
    proposal = PatchProposal(
        proposal_id="pp-bad",
        task_id=1,
        provider="mock",
        files=[
            PatchFile(
                relative_path=path,
                operation="modify",
                expected_sha256="0" * 64,
                new_content="x",
                encoding="utf-8",
            )
        ],
        unified_diff="",
        explanation="bad",
        expected_tests=[],
        risks=[],
        generated_at="2026-06-20T00:00:00Z",
        metadata={},
    )
    with pytest.raises(PatchApplicationError):
        service.validate_proposal(proposal, snapshot)


def test_hash_mismatch_binary_and_size_limits_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    snapshot = _snapshot(repo, allowed_files=["calculator.py", "bundle.png"], max_patch_bytes=4)
    service = PatchApplicationService(repo)
    bad_hash = PatchProposal(
        proposal_id="pp-hash",
        task_id=1,
        provider="mock",
        files=[
            PatchFile(
                relative_path="calculator.py",
                operation="modify",
                expected_sha256="0" * 64,
                new_content="def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
        ],
        unified_diff="",
        explanation="bad hash",
        expected_tests=[],
        risks=[],
        generated_at="2026-06-20T00:00:00Z",
        metadata={},
    )
    with pytest.raises(PatchApplicationError):
        service.validate_proposal(bad_hash, snapshot)

    binary_bad = PatchProposal(
        proposal_id="pp-bin",
        task_id=1,
        provider="mock",
        files=[
            PatchFile(
                relative_path="bundle.png",
                operation="modify",
                expected_sha256="0" * 64,
                new_content="not a real binary",
                encoding="utf-8",
            )
        ],
        unified_diff="",
        explanation="binary",
        expected_tests=[],
        risks=[],
        generated_at="2026-06-20T00:00:00Z",
        metadata={},
    )
    with pytest.raises(PatchApplicationError):
        service.validate_proposal(binary_bad, snapshot)

    oversized = PatchProposal(
        proposal_id="pp-size",
        task_id=1,
        provider="mock",
        files=[
            PatchFile(
                relative_path="calculator.py",
                operation="modify",
                expected_sha256=snapshot.file_hashes["calculator.py"],
                new_content="x" * 100,
                encoding="utf-8",
            )
        ],
        unified_diff="",
        explanation="oversized",
        expected_tests=[],
        risks=[],
        generated_at="2026-06-20T00:00:00Z",
        metadata={},
    )
    with pytest.raises(PatchApplicationError):
        service.validate_proposal(oversized, snapshot)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest test_calculator.py",
        "npm run typecheck",
        "npm test",
        "npm run build",
    ],
)
def test_command_whitelist_accepts_predefined_templates(tmp_path, command):
    repo = _make_repo(tmp_path)
    service = PatchApplicationService(repo)
    assert service._normalize_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "npm install",
        "cmd /c echo hi",
        "powershell -Command echo hi",
        "curl http://example.com",
        "wget http://example.com",
    ],
)
def test_command_whitelist_rejects_forbidden_templates(tmp_path, command):
    repo = _make_repo(tmp_path)
    service = PatchApplicationService(repo)
    with pytest.raises(PatchApplicationError):
        service._normalize_command(command)


def test_bridge_happy_path_and_runtime_idles(tmp_path):
    repo = _make_repo(tmp_path)
    policy = _good_policy(repo)
    bridge = B7AExecutionBridge(
        provider=MockProvider(),
        secret_provider=MemorySecretProvider({"ignored": "value"}),
    )
    runtime = B7ARuntimeService(bridge=bridge, runtime_dir=tmp_path / "runtime")
    result = runtime.run_one_cycle(
        task_packet={
            "task_id": 1,
            "project_id": 9001,
            "task_type": "SANDBOX_CODE_PATCH",
            "temporary_project": True,
            "allowed_files": ["calculator.py"],
            "allowed_test_commands": ["python -m pytest test_calculator.py"],
            "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
            "max_files_changed": 1,
            "max_patch_bytes": 1024,
            "evidence_required": True,
            "worker_id": "runtime-worker",
        },
        workspace_root=repo,
        policy=policy,
    )
    assert result["ok"] is True
    assert runtime.status()["runtime_status"] == "IDLE"
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a + b\n")
    runtime.shutdown()
    assert not runtime.pid_file.exists()


def test_bridge_rejects_out_of_scope_patch_and_rolls_back(tmp_path):
    repo = _make_repo(tmp_path)
    policy = _good_policy(repo)

    class MaliciousProvider(MockProvider):
        def generate_patch(self, task_packet, workspace_snapshot):
            proposal = super().generate_patch(task_packet, workspace_snapshot)
            proposal.files = [
                PatchFile(
                    relative_path="test_calculator.py",
                    operation="modify",
                    expected_sha256=workspace_snapshot.file_hashes["calculator.py"],
                    new_content="print('bad')\n",
                )
            ]
            return proposal

    bridge = B7AExecutionBridge(provider=MaliciousProvider(), secret_provider=MemorySecretProvider({}))
    service = PatchApplicationService(repo)
    before = (repo / "calculator.py").read_text(encoding="utf-8")
    checkpoint = service.create_checkpoint()
    result = bridge.execute(
        task_packet={
            "task_id": 1,
            "project_id": 9001,
            "task_type": "SANDBOX_CODE_PATCH",
            "temporary_project": True,
            "allowed_files": ["calculator.py"],
            "allowed_test_commands": ["python -m pytest test_calculator.py"],
            "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
            "max_files_changed": 1,
            "max_patch_bytes": 1024,
            "evidence_required": True,
            "worker_id": "runtime-worker",
        },
        workspace_root=repo,
        policy=policy,
    )
    assert result["ok"] is False
    assert result["error_code"] == "PATCH_APPLICATION_FAILED"
    service.rollback(checkpoint)
    assert (repo / "calculator.py").read_text(encoding="utf-8") == before
