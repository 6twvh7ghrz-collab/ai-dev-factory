from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_runtime.b7a import (
    CodexProviderBridge,
    PatchApplicationService,
    PatchFile,
    PatchProposal,
    TaskExecutionPolicy,
    WorkspaceSnapshotBuilder,
    sanitize_task_packet_for_provider,
)
from app.agent_runtime.b7a.providers.codex_provider import CodexInvocationError
from app.agent_runtime.b7a.patch_application import PatchApplicationError
from app.agent_runtime.b7b import B7BDiffReviewService


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


def _git_status_porcelain(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return result.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Codex"], repo)
    _git(["config", "user.email", "codex@example.com"], repo)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-m", "baseline"], repo)
    return repo


def _task_packet(repo: Path) -> dict[str, object]:
    return {
        "task_id": 9301,
        "project_id": 9300,
        "task_type": "SANDBOX_CODE_PATCH",
        "temporary_project": True,
        "allowed_files": ["calculator.py"],
        "allowed_test_commands": ["python -m pytest test_calculator.py -q"],
        "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        "max_files_changed": 1,
        "max_patch_bytes": 1024,
        "evidence_required": True,
        "worker_id": "b7c-worker",
        "approval_token": "one-time",
        "mode": "sandbox",
        "sandbox_root": str(repo),
        "control_plane_url": "http://127.0.0.1:8000",
        "api_key": "secret-api-key",
        "authorization": "Bearer secret-token",
        "lease_token": "lease-secret",
        "database_url": "sqlite:///formal.db",
    }


def _snapshot(repo: Path, packet: dict[str, object]) -> object:
    return WorkspaceSnapshotBuilder(
        workspace_root=repo,
        allowed_files=["calculator.py"],
        allowed_test_commands=["python -m pytest test_calculator.py -q"],
        forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        temporary_project=True,
        project_id=9300,
        task_packet=packet,
    ).build()


def _proposal_payload(rel_path: str, content: str, snapshot) -> str:
    return json.dumps(
        {
            "proposal_id": "pp-codex-test",
            "task_id": 9301,
            "explanation": "codex bridge patch",
            "files": [
                {
                    "relative_path": rel_path,
                    "operation": "modify",
                    "expected_sha256": snapshot.file_hashes[rel_path],
                    "new_content": content,
                    "encoding": "utf-8",
                }
            ],
            "expected_tests": ["npm install", "python -m pytest test_calculator.py -q"],
            "risks": ["single-file patch"],
            "provider_metadata": {
                "provider": "codex",
                "model": "gpt-5.4-mini",
                "request_id": "req-test",
                "finish_reason": "stop",
            },
        },
        ensure_ascii=False,
    )


def _malicious_draft_payload(rel_path: str, content: str, snapshot) -> str:
    return json.dumps(
        {
            "proposal_id": "model-proposal-id",
            "task_id": 999999,
            "provider": "model-provider",
            "generated_at": "1999-01-01T00:00:00Z",
            "explanation": "model explanation",
            "files": [
                {
                    "relative_path": rel_path,
                    "operation": "modify",
                    "expected_sha256": "0" * 64,
                    "new_content": content,
                    "encoding": "utf-8",
                }
            ],
            "expected_tests": ["npm install", "curl http://evil.invalid"],
            "risks": ["model risk"],
            "provider_metadata": {
                "provider": "openai",
                "model": "gpt-99",
                "request_id": "model-request",
                "finish_reason": "length",
            },
            "metadata": {
                "provider": "openai",
                "model": "gpt-99",
                "request_id": "model-request",
                "finish_reason": "length",
            },
        },
        ensure_ascii=False,
    )


def _policy(repo: Path) -> TaskExecutionPolicy:
    return TaskExecutionPolicy(
        task_type="SANDBOX_CODE_PATCH",
        temporary_project=True,
        project_id=9300,
        sandbox_root=str(repo),
        allowed_files=["calculator.py"],
        forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        allowed_test_commands=["python -m pytest test_calculator.py -q"],
        max_files_changed=1,
        max_patch_bytes=1024,
        evidence_required=True,
        approval_token="one-time",
        mode="sandbox",
        control_plane_url="http://127.0.0.1:8000",
    )


def _schema_status(schema: dict[str, object]) -> dict[str, object]:
    return CodexProviderBridge(enabled=True).validate_output_schema(schema)


class _FakeProc:
    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        pid: int = 4321,
        keep_running: bool = False,
    ):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None if keep_running else returncode
        self._final_code = returncode
        self._keep_running = keep_running
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


def _enable_codex_runtime(monkeypatch) -> None:
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")


def _fake_popen_factory(
    *,
    fake_proc: _FakeProc,
    last_message_text: str | None = None,
    last_message_exists: bool = True,
):
    created: dict[str, object] = {}

    def _factory(cmd, cwd=None, **kwargs):
        created["cmd"] = cmd
        created["cwd"] = cwd
        created["kwargs"] = kwargs
        if last_message_exists and "--output-last-message" in cmd:
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            if last_message_text is not None:
                out_path.write_text(last_message_text, encoding="utf-8")
        created["proc"] = fake_proc
        return fake_proc

    return created, _factory


def _mock_codex_subprocess_run(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent_runtime.b7a.providers.codex_provider.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )


def test_codex_provider_defaults_disabled():
    provider = CodexProviderBridge()
    status = provider.validate_config()
    assert status["ok"] is False
    assert status["error_code"] == "PROVIDER_DISABLED"


def test_codex_provider_output_schema_is_strict_json():
    schema = CodexProviderBridge(enabled=True)._output_schema()
    assert schema["type"] == "object"
    assert schema["properties"]["provider_metadata"]["additionalProperties"] is False
    assert schema["properties"]["files"]["maxItems"] == 1
    assert schema["properties"]["files"]["items"]["properties"]["operation"]["const"] == "modify"
    assert schema["properties"]["files"]["items"]["properties"]["encoding"]["const"] == "utf-8"
    assert schema["properties"]["provider_metadata"]["properties"]["request_id"]["type"] == ["string", "null"]


def test_codex_provider_output_schema_validates_strict_structure():
    status = _schema_status(CodexProviderBridge(enabled=True)._output_schema())
    assert status["ok"] is True


def test_codex_provider_output_schema_allows_null_union_optional_field():
    schema = copy.deepcopy(CodexProviderBridge(enabled=True)._output_schema())
    schema["properties"]["provider_metadata"]["properties"]["request_id"]["type"] = ["string", "null"]
    schema["properties"]["provider_metadata"]["properties"]["finish_reason"]["type"] = ["string", "null"]
    status = _schema_status(schema)
    assert status["ok"] is True


@pytest.mark.parametrize(
    "mutator,pointer,reason_fragment",
    [
        (lambda s: s.pop("type", None), "/", "root schema must be an object"),
        (lambda s: s.pop("additionalProperties", None), "/", "additionalProperties"),
        (lambda s: s["properties"].pop("explanation", None), "/required", "required must include"),
        (lambda s: s["properties"]["files"].pop("items", None), "/properties/files", "items"),
        (lambda s: s["properties"]["files"]["items"].pop("additionalProperties", None), "/properties/files/items", "additionalProperties"),
        (lambda s: s["properties"]["files"]["items"]["properties"].__setitem__("extra", {"type": "string"}), "/properties/files/items/required", "required must include"),
        (lambda s: s["properties"]["provider_metadata"].pop("additionalProperties", None), "/properties/provider_metadata", "additionalProperties"),
        (lambda s: s["$defs"].__setitem__("bad", {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": True}), "/$defs/bad", "additionalProperties"),
        (lambda s: s.__setitem__("anyOf", [{"type": "object"}]), "/anyOf", "supported"),
        (lambda s: s["properties"]["files"]["items"].__setitem__("patternProperties", {}), "/properties/files/items/patternProperties", "supported"),
        (lambda s: s["properties"]["provider_metadata"].__setitem__("if", {"type": "object"}), "/properties/provider_metadata/if", "supported"),
        (lambda s: s["properties"]["provider_metadata"].__setitem__("then", {"type": "object"}), "/properties/provider_metadata/then", "supported"),
    ],
)
def test_codex_provider_output_schema_rejects_bad_shapes(mutator, pointer, reason_fragment):
    schema = copy.deepcopy(CodexProviderBridge(enabled=True)._output_schema())
    if "$defs" not in schema:
        schema["$defs"] = {}
    mutator(schema)
    status = _schema_status(schema)
    assert status["ok"] is False
    assert status["error_code"] == "CODEX_OUTPUT_SCHEMA_INVALID"
    assert status["pointer"] == pointer
    assert reason_fragment in status["message"]


def test_codex_provider_output_schema_rejects_top_level_anyof():
    schema = {"anyOf": [{"type": "object"}]}
    status = _schema_status(schema)
    assert status["ok"] is False
    assert status["error_code"] == "CODEX_OUTPUT_SCHEMA_INVALID"
    assert status["pointer"] == "/anyOf"


def test_codex_provider_output_schema_invalid_never_starts_subprocess(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    monkeypatch.setattr(provider, "_output_schema", lambda: {"type": "object"})
    monkeypatch.setattr(provider, "_invoke_codex", lambda prompt: (_ for _ in ()).throw(AssertionError("should not run")))

    with pytest.raises(PatchApplicationError, match="CODEX_OUTPUT_SCHEMA_INVALID"):
        provider.generate_patch(packet, snapshot)


def test_codex_provider_output_schema_error_message_is_redacted():
    status = _schema_status({"type": "object", "required": [], "properties": {}})
    assert status["ok"] is False
    assert "secret" not in str(status).lower()
    assert "C:\\" not in str(status)


def test_codex_provider_requires_auth_when_enabled(monkeypatch):
    _enable_codex_runtime(monkeypatch)

    provider = CodexProviderBridge(enabled=True)
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    monkeypatch.setattr(
        provider,
        "_auth_status",
        lambda: {"ok": False, "error_code": "CODEX_AUTH_UNAVAILABLE", "message": "Codex authentication is required"},
    )

    status = provider.validate_config()
    assert status["ok"] is False
    assert status["error_code"] == "CODEX_AUTH_UNAVAILABLE"


def test_codex_provider_auth_status_reports_missing_executable(monkeypatch):
    provider = CodexProviderBridge(enabled=True)
    monkeypatch.setattr(provider, "_resolve_executable", lambda: None)

    status = provider._auth_status()
    assert status["ok"] is False
    assert status["error_code"] == "CODEX_EXECUTABLE_NOT_FOUND"


def test_codex_provider_runtime_context_detects_nested_invocation(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    monkeypatch.setenv("CODEX_MANAGED_BY_NPM", "1")

    provider = CodexProviderBridge(enabled=True)
    context = provider.runtime_context()
    assert context["nested"] is True
    assert context["signals"]["CODEX_THREAD_ID"] is True
    assert context["signals"]["CODEX_MANAGED_BY_NPM"] is True
    assert provider.is_nested_invocation() is True
    assert provider.allow_live_model_call() is False


def test_codex_provider_runtime_context_allows_live_call_when_not_nested(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_MANAGED_BY_NPM", raising=False)

    provider = CodexProviderBridge(enabled=True)
    context = provider.runtime_context()
    assert context["nested"] is False
    assert provider.is_nested_invocation() is False
    assert provider.allow_live_model_call() is True


def test_codex_provider_invocation_uses_json_events_and_temp_git_repo(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(
        stdout_text='{"type":"session.started"}\n{"phase":"planning"}\n',
        stderr_text="stderr trace",
        returncode=0,
        pid=2468,
    )
    expected_output = _proposal_payload(
        "calculator.py",
        "def add(a, b):\n    return a + b\n",
        snapshot,
    )
    created, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=expected_output)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)

    response = provider._invoke_codex("safe prompt")
    assert json.loads(response)["provider_metadata"]["provider"] == "codex"
    assert created["cmd"][0] == "codex.exe"
    assert "-a" in created["cmd"]
    assert "never" in created["cmd"]
    assert "--json" in created["cmd"]
    assert "--skip-git-repo-check" in created["cmd"]
    assert "--output-schema" in created["cmd"]
    assert "--output-last-message" in created["cmd"]
    assert created["cmd"][created["cmd"].index("--sandbox") + 1] == "read-only"
    assert created["cwd"] is not None
    assert Path(created["cwd"]).name.startswith("codex-provider-cwd-")
    assert provider.invocation_count == 1


def test_codex_provider_stdout_and_stderr_drain_without_deadlock(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    stdout_text = "\n".join(f'{{"type":"progress","step":{i}}}' for i in range(2500)) + "\n"
    stderr_text = "\n".join(f"stderr-{i}" for i in range(2500)) + "\n"
    fake_proc = _FakeProc(stdout_text=stdout_text, stderr_text=stderr_text, returncode=0, pid=2469)
    expected_output = _proposal_payload("calculator.py", "def add(a, b):\n    return a + b\n", snapshot)
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=expected_output)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)

    response = provider._invoke_codex("safe prompt")
    proposal = json.loads(response)
    assert proposal["files"][0]["relative_path"] == "calculator.py"


def test_codex_provider_output_missing_is_explicit(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(stdout_text='{"type":"session.started"}\n', stderr_text="", returncode=0, pid=2470)
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=None, last_message_exists=False)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)

    with pytest.raises(CodexInvocationError) as exc_info:
        provider._invoke_codex("safe prompt")
    assert exc_info.value.error_code == "CODEX_OUTPUT_MISSING"


def test_codex_provider_timeout_reports_redacted_diagnostics(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True, timeout_seconds=1)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(
        stdout_text='{"type":"session.started"}\n{"phase":"planning"}\n',
        stderr_text="C:\\Users\\本机\\secrets\\trace.txt token=super-secret authorization=Bearer abc123",
        keep_running=True,
        pid=2471,
    )
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=None, last_message_exists=False)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.time.sleep", lambda _seconds: None)

    clock = {"values": [0.0, 0.2, 0.4, 1.5, 1.6, 1.7], "current": 1.7}

    def fake_monotonic():
        if clock["values"]:
            clock["current"] = clock["values"].pop(0)
        else:
            clock["current"] += 0.5
        return clock["current"]

    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.time.monotonic", fake_monotonic)

    taskkill_calls: list[int] = []
    monkeypatch.setattr(provider, "_taskkill_process_tree", lambda pid: taskkill_calls.append(pid))

    with pytest.raises(CodexInvocationError) as exc_info:
        provider._invoke_codex("prompt should stay hidden")

    err = exc_info.value
    assert err.error_code == "CODEX_PROCESS_TIMEOUT"
    assert err.details["process_id"] == 2471
    assert err.details["last_event_type"] == "planning"
    assert "prompt should stay hidden" not in str(err)
    assert "super-secret" not in str(err)
    assert "C:\\Users\\本机" not in str(err)
    assert taskkill_calls == [2471]
    assert fake_proc.terminated is True
    assert fake_proc.killed is True


def test_codex_provider_nonzero_exit_is_reported(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(stdout_text='{"type":"session.started"}\n', stderr_text="fatal error", returncode=17, pid=2472)
    expected_output = _proposal_payload("calculator.py", "def add(a, b):\n    return a + b\n", snapshot)
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=expected_output)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)

    with pytest.raises(CodexInvocationError) as exc_info:
        provider._invoke_codex("safe prompt")
    assert exc_info.value.error_code == "CODEX_PROCESS_FAILED"
    assert exc_info.value.details["process_id"] == 2472


def test_codex_provider_json_event_stream_tracks_last_phase(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(
        stdout_text='{"type":"session.started"}\n{"phase":"planning"}\n{"kind":"finalize"}\n',
        stderr_text="",
        returncode=0,
        pid=2473,
    )
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=None, last_message_exists=False)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)

    with pytest.raises(CodexInvocationError) as exc_info:
        provider._invoke_codex("safe prompt")
    assert exc_info.value.error_code == "CODEX_OUTPUT_MISSING"
    assert exc_info.value.details["last_event_type"] == "finalize"


def test_codex_provider_error_text_is_redacted(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True, timeout_seconds=1)
    _enable_codex_runtime(monkeypatch)
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    _mock_codex_subprocess_run(monkeypatch)

    fake_proc = _FakeProc(
        stdout_text='{"type":"session.started"}\n',
        stderr_text="C:\\Users\\本机\\Desktop\\secrets\\prompt.txt token=abc123",
        keep_running=True,
        pid=2474,
    )
    _, factory = _fake_popen_factory(fake_proc=fake_proc, last_message_text=None, last_message_exists=False)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.subprocess.Popen", factory)
    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.time.sleep", lambda _seconds: None)

    clock = {"values": [0.0, 0.3, 1.2, 1.3], "current": 1.3}

    def fake_monotonic():
        if clock["values"]:
            clock["current"] = clock["values"].pop(0)
        else:
            clock["current"] += 0.5
        return clock["current"]

    monkeypatch.setattr("app.agent_runtime.b7a.providers.codex_provider.time.monotonic", fake_monotonic)

    with pytest.raises(CodexInvocationError) as exc_info:
        provider._invoke_codex("prompt should not leak")
    text = str(exc_info.value)
    assert "prompt should not leak" not in text
    assert "abc123" not in text
    assert "C:\\Users\\本机" not in text
    assert "<redacted-path>" in text or "<redacted>" in text


def test_codex_provider_redacts_sensitive_context_and_parses_structured_output(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    packet["nested"] = {
        "Authorization": "Bearer nested-secret",
        "inner": {"token": "nested-token", "keep": "value"},
        "items": [{"lease_token": "nested-lease", "note": "safe"}],
    }
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)

    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")

    seen = {}

    def fake_invoke(prompt: str) -> str:
        seen["prompt"] = prompt
        return _proposal_payload("calculator.py", "def add(a, b):\n    return a + b\n", snapshot)

    monkeypatch.setattr(provider, "_invoke_codex", fake_invoke)

    proposal = provider.generate_patch(packet, snapshot)
    prompt = seen["prompt"]
    assert "secret-api-key" not in prompt
    assert "secret-token" not in prompt
    assert "lease-secret" not in prompt
    assert "nested-secret" not in prompt
    assert "nested-token" not in prompt
    assert "nested-lease" not in prompt
    assert "approval_token" not in prompt
    assert "DATABASE_URL" not in prompt
    assert "formal.db" not in prompt
    assert proposal.provider == "codex"
    assert proposal.files[0].relative_path == "calculator.py"
    assert proposal.files[0].new_content.endswith("return a + b\n")
    assert proposal.expected_tests == ["python -m pytest test_calculator.py -q"]
    assert proposal.metadata["provider"] == "codex"
    assert proposal.metadata["model"] == "gpt-5.4-mini"
    assert proposal.metadata["request_id"] is None or proposal.metadata["request_id"] == ""


def test_codex_provider_trusts_only_system_fields_and_rebuilds_diff(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)

    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    monkeypatch.setattr(
        provider,
        "_invoke_codex",
        lambda prompt: _malicious_draft_payload(
            "calculator.py",
            "def add(a, b):\n    return a + b\n",
            snapshot,
        ),
    )
    provider._last_invocation_context = {
        "request_id": "bridge-request",
        "finish_reason": "stop",
        "last_event_type": "final",
        "stdout_event_count": 2,
        "stderr_summary": "",
        "elapsed_seconds": 0.1,
    }

    proposal = provider.generate_patch(packet, snapshot)
    assert proposal.proposal_id.startswith("pp-")
    assert proposal.task_id == packet["task_id"]
    assert proposal.provider == "codex"
    assert proposal.files[0].expected_sha256 == snapshot.file_hashes["calculator.py"]
    assert proposal.expected_tests == snapshot.allowed_test_commands
    assert proposal.metadata["provider"] == "codex"
    assert proposal.metadata["model"] == "gpt-5.4-mini"
    assert proposal.metadata["request_id"] == "bridge-request"
    assert proposal.metadata["finish_reason"] == "stop"
    assert isinstance(proposal.metadata["source_provider_metadata"], dict)
    assert proposal.files[0].relative_path == "calculator.py"
    assert proposal.unified_diff.startswith("--- a/calculator.py")
    assert "+    return a + b" in proposal.unified_diff
    assert "npm install" not in proposal.expected_tests


def test_codex_provider_diagnostics_replay_reconstructs_trusted_proposal(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    diagnostics = tmp_path / "diag"
    diagnostics.mkdir()
    events = [
        '{"type":"session.started","request_id":"event-request"}',
        '{"phase":"planning","status":"running"}',
        '{"kind":"finalize","finish_reason":"stop"}',
    ]
    (diagnostics / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    (diagnostics / "final-output.json").write_text(
        _malicious_draft_payload("calculator.py", "def add(a, b):\n    return a + b\n", snapshot),
        encoding="utf-8",
    )

    provider = CodexProviderBridge(enabled=True)
    provider._last_invocation_context = {}
    proposal = provider.replay_from_diagnostics(diagnostics, packet, snapshot)

    assert proposal.task_id == packet["task_id"]
    assert proposal.provider == "codex"
    assert proposal.files[0].expected_sha256 == snapshot.file_hashes["calculator.py"]
    assert proposal.expected_tests == snapshot.allowed_test_commands
    assert proposal.metadata["request_id"] == "event-request"
    assert proposal.metadata["finish_reason"] == "stop"
    assert "model-provider" not in json.dumps(proposal.as_dict())


def test_sanitize_task_packet_for_provider_removes_nested_sensitive_keys():
    packet = {
        "task_id": 1,
        "api_key": "top-secret",
        "nested": {
            "Authorization": "Bearer secret",
            "auth": {"token": "abc", "keep": "value"},
            "credential": "super-secret",
            "list": [{"lease_token": "lease", "ok": True}],
        },
        "DATABASE_URL": "sqlite:///secret.db",
        "ok": True,
    }
    safe = sanitize_task_packet_for_provider(packet)
    text = json.dumps(safe)
    assert "api_key" not in text.lower()
    assert "authorization" not in text.lower()
    assert "token" not in text.lower()
    assert "lease_token" not in text.lower()
    assert "database_url" not in text.lower()
    assert safe["task_id"] == 1
    assert safe["nested"]["auth"]["keep"] == "value"
    assert safe["nested"]["list"][0]["ok"] is True
    assert "credential" not in safe["nested"]


def test_codex_provider_rejects_non_json_output(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)

    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    monkeypatch.setattr(provider, "_invoke_codex", lambda prompt: "not-json")

    with pytest.raises(PatchApplicationError, match="valid JSON"):
        provider.generate_patch(packet, snapshot)


def test_codex_provider_rejects_out_of_scope_target(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    provider = CodexProviderBridge(enabled=True)

    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")
    monkeypatch.setattr(provider, "_auth_status", lambda: {"ok": True, "auth_mode": "chatgpt"})
    monkeypatch.setattr(provider, "_resolve_executable", lambda: "codex.exe")
    monkeypatch.setattr(
        provider,
        "_invoke_codex",
        lambda prompt: json.dumps(
            {
                "proposal_id": "pp-codex-test",
                "task_id": 9301,
                "explanation": "out of scope",
                "files": [
                    {
                        "relative_path": "test_calculator.py",
                        "operation": "modify",
                        "expected_sha256": snapshot.file_hashes["calculator.py"],
                        "new_content": "assert False\n",
                        "encoding": "utf-8",
                    }
                ],
                "expected_tests": [],
                "risks": [],
                "provider_metadata": {
                    "provider": "codex",
                    "model": "gpt-5.4-mini",
                    "request_id": "req-test",
                    "finish_reason": "stop",
                },
            },
            ensure_ascii=False,
        ),
    )

    with pytest.raises(PatchApplicationError, match="disallowed file"):
        provider.generate_patch(packet, snapshot)
@pytest.mark.live_model
def test_codex_provider_live_model_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("V2_REAL_AI_WORKER_ENABLED", "true")
    monkeypatch.setenv("V2_CODEX_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("V2_RUN_LIVE_MODEL", "1")

    provider = CodexProviderBridge(enabled=True, timeout_seconds=300)
    config = provider.validate_config()
    assert config["ok"] is True
    assert config["auth_mode"] == "chatgpt"

    repo = _make_repo(tmp_path)
    packet = _task_packet(repo)
    snapshot = _snapshot(repo, packet)
    policy = _policy(repo)
    decision = policy.evaluate()
    assert decision.allowed is True

    app = PatchApplicationService(repo)
    app.verify_workspace()
    checkpoint = app.create_checkpoint()
    review = B7BDiffReviewService().review(
        PatchProposal(
            proposal_id="placeholder",
            task_id=packet["task_id"],
            provider="codex",
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
            explanation="preflight review",
            expected_tests=["python -m pytest test_calculator.py -q"],
            risks=[],
            generated_at="2026-01-01T00:00:00Z",
            metadata={},
        ),
        ["calculator.py"],
    )
    assert review.ok is True

    proposal = provider.generate_patch(packet, snapshot)
    assert provider.invocation_count == 1
    assert proposal.provider == "codex"
    assert len(proposal.files) == 1
    assert proposal.files[0].relative_path == "calculator.py"

    review = B7BDiffReviewService().review(proposal, ["calculator.py"])
    assert review.ok is True

    app.validate_proposal(proposal, snapshot)
    changed = app.apply_patch_proposal(proposal)
    test_result = app.run_allowed_test("python -m pytest test_calculator.py -q")
    assert test_result["ok"] is True
    evidence = app.finalize_evidence(
        proposal=proposal,
        changed_files=changed,
        tests_run=[test_result],
        summary="live codex patch verified",
    )
    assert evidence.files_changed == ["calculator.py"]
    assert evidence.tests_run[0]["ok"] is True
    assert "lease_token" not in json.dumps(evidence.as_dict()).lower()

    app.rollback(checkpoint)
    assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a - b\n")
    assert _git_status_porcelain(repo).strip() == ""
