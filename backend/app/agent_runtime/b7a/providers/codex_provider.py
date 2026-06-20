"""Guarded Codex CLI provider for a single sandbox patch proposal."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..models import PatchFile, PatchProposal, WorkspaceSnapshot, build_unified_diff, sanitize_task_packet_for_provider, utc_now
from ..patch_application import PatchApplicationError
from ..policy import TaskExecutionPolicy
from .base import PatchProvider
from .schema import SchemaValidationError, build_codex_output_schema, validate_strict_schema


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _detect_nested_codex_context() -> Dict[str, Any]:
    thread_id = os.getenv("CODEX_THREAD_ID")
    managed_by_npm = _env_flag("CODEX_MANAGED_BY_NPM")
    nested = bool(thread_id or managed_by_npm)
    return {
        "nested": nested,
        "signals": {
            "CODEX_THREAD_ID": bool(thread_id),
            "CODEX_MANAGED_BY_NPM": managed_by_npm,
        },
    }


_ABSOLUTE_PATH_RE = re.compile(r"(?i)\b[a-z]:\\[^\s\"']+")
_URL_RE = re.compile(r"https?://[^\s\"']+")
_TOKEN_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization|lease[_-]?token)\b[^\n]*")


def _redact_diagnostic_text(text: str, *, max_lines: int = 8, max_chars: int = 1200) -> str:
    if not text:
        return ""
    redacted = _URL_RE.sub("<redacted-url>", text)
    redacted = _ABSOLUTE_PATH_RE.sub("<redacted-path>", redacted)
    redacted = _TOKEN_RE.sub("<redacted>", redacted)
    lines = [line.rstrip() for line in redacted.replace("\r", "").split("\n") if line.strip()]
    if not lines:
        return ""
    summary = "\n".join(lines[-max_lines:])
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return summary


def _extract_event_type(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("type", "event_type", "phase", "kind", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(slots=True)
class _CodexProcessDiagnostics:
    process_id: int
    elapsed_seconds: float
    return_code: Optional[int]
    last_event_type: Optional[str]
    stderr_summary: str
    stdout_event_count: int = 0

    def as_timeout_details(self) -> Dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "process_id": self.process_id,
            "return_code": self.return_code,
            "last_event_type": self.last_event_type,
            "stderr_summary": self.stderr_summary,
        }


class CodexInvocationError(PatchApplicationError):
    def __init__(self, error_code: str, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}

    def __str__(self) -> str:
        base = f"{self.error_code}: {self.args[0]}"
        if self.details:
            keys = [
                key
                for key in ("elapsed_seconds", "process_id", "return_code", "last_event_type", "stderr_summary")
                if key in self.details
            ]
            if keys:
                detail = ", ".join(f"{key}={self.details[key]!r}" for key in keys)
                return f"{base} ({detail})"
        return base


@dataclass(slots=True)
class CodexProviderBridge(PatchProvider):
    provider_name: str = "codex"
    enabled: bool = False
    model: str = "gpt-5.4-mini"
    timeout_seconds: int = 180
    max_retries: int = 1
    codex_executable: Optional[str] = None
    _cancel_requested: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    invocation_count: int = field(default=0, init=False)
    _last_invocation_context: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def cancel(self) -> None:
        self._cancel_requested.set()

    def validate_config(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error_code": "PROVIDER_DISABLED", "message": "Codex provider is disabled"}
        gate = self._security_gate()
        if not gate["ok"]:
            return gate
        auth = self._auth_status()
        if not auth["ok"]:
            return auth
        exe = self._resolve_executable()
        if exe is None:
            return {"ok": False, "error_code": "CODEX_EXECUTABLE_NOT_FOUND", "message": "Codex CLI executable not found"}
        return {
            "ok": True,
            "provider": self.provider_name,
            "cli_version": self._cli_version(exe),
            "auth_mode": auth["auth_mode"],
            "model": self.model,
        }

    def health_check(self) -> Dict[str, Any]:
        gate = self._security_gate()
        auth = self._auth_status()
        exe = self._resolve_executable()
        return {
            "ok": bool(self.enabled and gate["ok"] and auth["ok"] and exe),
            "provider": self.provider_name,
            "redacted": {
                "enabled": self.enabled,
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_retries": self.max_retries,
                "configured": auth["ok"],
                "auth_mode": auth.get("auth_mode", "unknown"),
                "cli_available": bool(exe),
            },
        }

    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot: WorkspaceSnapshot) -> PatchProposal:
        schema_status = self.validate_output_schema()
        if not schema_status["ok"]:
            pointer = schema_status.get("pointer", "/")
            message = schema_status.get("message", "schema is invalid")
            raise CodexInvocationError("CODEX_OUTPUT_SCHEMA_INVALID", f"{pointer}: {message}")
        self._reject_if_not_authorized(task_packet)
        allowed_files = list(workspace_snapshot.allowed_files.keys())
        if len(allowed_files) != 1:
            raise PatchApplicationError("Codex provider requires exactly one allowed file")

        prompt = self._build_prompt(task_packet, workspace_snapshot)
        response_text = self._invoke_codex(prompt)
        proposal = self._parse_response(response_text, task_packet, workspace_snapshot)
        self._validate_proposal(proposal, task_packet, workspace_snapshot)
        return proposal

    def repair_patch(self, proposal: PatchProposal, reason: str) -> PatchProposal:
        proposal.metadata = dict(proposal.metadata)
        proposal.metadata["repair_reason"] = reason
        proposal.explanation = f"{proposal.explanation} | repair: {reason}"
        return proposal

    def replay_from_diagnostics(
        self,
        diagnostics_dir: Path,
        task_packet: Dict[str, Any],
        workspace_snapshot: WorkspaceSnapshot,
    ) -> PatchProposal:
        target = Path(diagnostics_dir)
        final_output_path = target / "final-output.json"
        events_path = target / "events.jsonl"
        if not final_output_path.exists():
            raise CodexInvocationError("CODEX_OUTPUT_MISSING", "diagnostic final output is missing")
        response_text = final_output_path.read_text(encoding="utf-8", errors="replace")
        self._last_invocation_context = self._load_invocation_context_from_events(events_path)
        proposal = self._parse_response(response_text, task_packet, workspace_snapshot)
        self._validate_proposal(proposal, task_packet, workspace_snapshot)
        return proposal

    def redact_config(self) -> Dict[str, Any]:
        auth = self._auth_status()
        return {
            "provider": self.provider_name,
            "configured": bool(self.enabled and auth["ok"]),
            "model": self.model,
            "auth_mode": auth.get("auth_mode", "unknown"),
        }

    def runtime_context(self) -> Dict[str, Any]:
        return _detect_nested_codex_context()

    def is_nested_invocation(self) -> bool:
        return bool(self.runtime_context()["nested"])

    def allow_live_model_call(self) -> bool:
        return not self.is_nested_invocation()

    def _security_gate(self) -> Dict[str, Any]:
        required = [
            "V2_CONTROL_PLANE_ENABLED",
            "V2_AGENT_RUNTIME_ENABLED",
            "V2_REAL_AI_WORKER_ENABLED",
            "V2_CODEX_PROVIDER_ENABLED",
        ]
        missing = [name for name in required if not _env_flag(name)]
        if missing:
            return {
                "ok": False,
                "error_code": "CODEX_PROVIDER_DISABLED",
                "message": "Codex provider requires all V2 runtime flags",
                "details": {"missing_flags": missing},
            }
        return {"ok": True}

    def _auth_status(self) -> Dict[str, Any]:
        exe = self._resolve_executable()
        if exe is None:
            return {"ok": False, "error_code": "CODEX_EXECUTABLE_NOT_FOUND", "message": "Codex CLI executable not found"}
        try:
            proc = subprocess.run(
                [exe, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                shell=False,
            )
        except Exception as exc:
            return {"ok": False, "error_code": "CODEX_AUTH_UNAVAILABLE", "message": str(exc)}
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0 and "Logged in using ChatGPT" in output:
            return {"ok": True, "auth_mode": "chatgpt"}
        if proc.returncode == 0 and "Logged in using API key" in output:
            return {"ok": True, "auth_mode": "api_key"}
        return {"ok": False, "error_code": "CODEX_AUTH_UNAVAILABLE", "message": "Codex authentication is required"}

    def _resolve_executable(self) -> Optional[str]:
        if self.codex_executable:
            path = Path(self.codex_executable)
            return str(path) if path.exists() else None
        found = shutil.which("codex")
        if found:
            return found
        localapp = os.getenv("LOCALAPPDATA")
        if not localapp:
            return None
        base = Path(localapp) / "OpenAI" / "Codex" / "bin"
        if not base.exists():
            return None
        for candidate in sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True):
            if candidate.exists():
                return str(candidate)
        return None

    def _cli_version(self, exe: str) -> str:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            shell=False,
        )
        return (proc.stdout or proc.stderr or "").strip()

    def _reject_if_not_authorized(self, task_packet: Dict[str, Any]) -> None:
        gate = self._security_gate()
        if not gate["ok"]:
            raise CodexInvocationError("CODEX_PROCESS_FAILED", gate["message"], details=gate.get("details"))
        auth = self._auth_status()
        if not auth["ok"]:
            raise CodexInvocationError("CODEX_AUTH_UNAVAILABLE", auth["message"])
        if str(task_packet.get("mode", "")).lower() != "sandbox":
            raise CodexInvocationError("CODEX_APPROVAL_REQUIRED", "Codex provider only permits sandbox mode")
        if not task_packet.get("temporary_project"):
            raise CodexInvocationError("CODEX_APPROVAL_REQUIRED", "Codex provider requires a temporary project")
        if int(task_packet.get("project_id", 0)) in {56, 118}:
            raise CodexInvocationError("CODEX_PROCESS_FAILED", "project is blocked")
        if not (task_packet.get("approval_token") or task_packet.get("approval_record")):
            raise CodexInvocationError("CODEX_APPROVAL_REQUIRED", "approval is required")

    def _build_prompt(self, task_packet: Dict[str, Any], workspace_snapshot: WorkspaceSnapshot) -> str:
        safe_task = sanitize_task_packet_for_provider(task_packet)
        payload = {
            "task_packet": safe_task,
            "allowed_files": workspace_snapshot.allowed_files,
            "file_hashes": workspace_snapshot.file_hashes,
            "directory_listing": workspace_snapshot.directory_listing,
            "allowed_test_commands": workspace_snapshot.allowed_test_commands,
            "forbidden_actions": workspace_snapshot.forbidden_actions,
            "rules": [
                "Modify exactly one allowed file.",
                "Never modify test files, .git, .env, database files, binaries, or absolute/parent paths.",
                "Do not include shell commands or network commands.",
                "Return JSON only.",
                "Do not emit any fields beyond the fixed schema.",
            ],
            "output_shape": self._output_schema(),
        }
        return (
            "You are Codex acting as a guarded patch proposal generator.\n"
            "Return exactly one JSON object matching the requested output shape.\n"
            "No markdown, no prose, no code fences.\n"
            "Do not mention secrets, tokens, or paths outside the snapshot.\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _invoke_codex(self, prompt: str) -> str:
        schema_status = self.validate_output_schema()
        if not schema_status["ok"]:
            pointer = schema_status.get("pointer", "/")
            message = schema_status.get("message", "schema is invalid")
            raise CodexInvocationError("CODEX_OUTPUT_SCHEMA_INVALID", f"{pointer}: {message}")
        exe = self._resolve_executable()
        if exe is None:
            raise CodexInvocationError("CODEX_EXECUTABLE_NOT_FOUND", "Codex CLI executable not found")
        self.invocation_count += 1
        diagnostics_dir = os.getenv("CODEX_DIAGNOSTICS_DIR")

        with tempfile.TemporaryDirectory(prefix="codex-provider-cwd-") as cwd_dir, tempfile.TemporaryDirectory(
            prefix="codex-provider-out-"
        ) as out_dir:
            cwd = Path(cwd_dir)
            self._ensure_git_repo(cwd)
            last_message_path = Path(out_dir) / "last-message.json"
            schema_path = Path(out_dir) / "proposal.schema.json"
            schema_path.write_text(json.dumps(self._output_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
            cmd = [
                exe,
                "-a",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(last_message_path),
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
            start_time = time.monotonic()
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            event_state: Dict[str, Any] = {"last_event_type": None, "stdout_event_count": 0}
            state_lock = threading.Lock()
            stdout_thread = self._start_stream_reader(
                proc.stdout,
                stdout_lines,
                event_state=event_state,
                state_lock=state_lock,
                is_stdout=True,
            )
            stderr_thread = self._start_stream_reader(proc.stderr, stderr_lines)
            try:
                self._write_prompt(proc, prompt)
                self._wait_for_process_exit(proc, start_time, event_state, stderr_lines, stdout_lines, diagnostics_dir)
            except CodexInvocationError:
                self._join_reader_threads(stdout_thread, stderr_thread)
                raise
            finally:
                self._join_reader_threads(stdout_thread, stderr_thread)
                self._close_process_streams(proc)

            diagnostics = self._build_diagnostics(proc, start_time, event_state, stderr_lines)
            if proc.returncode != 0:
                error = CodexInvocationError(
                    "CODEX_PROCESS_FAILED",
                    "Codex process failed",
                    details=diagnostics.as_timeout_details(),
                )
                self._export_diagnostics(
                    diagnostics_dir,
                    stdout_lines=stdout_lines,
                    stderr_lines=stderr_lines,
                    error=error,
                )
                raise error

            if not last_message_path.exists():
                error = CodexInvocationError(
                    "CODEX_OUTPUT_MISSING",
                    "Codex did not produce output-last-message",
                    details=diagnostics.as_timeout_details(),
                )
                self._export_diagnostics(
                    diagnostics_dir,
                    stdout_lines=stdout_lines,
                    stderr_lines=stderr_lines,
                    error=error,
                )
                raise error

            response_text = last_message_path.read_text(encoding="utf-8", errors="replace")
            if not response_text.strip():
                error = CodexInvocationError(
                    "CODEX_OUTPUT_MISSING",
                    "Codex did not produce output-last-message",
                    details=diagnostics.as_timeout_details(),
                )
                self._export_diagnostics(
                    diagnostics_dir,
                    stdout_lines=stdout_lines,
                    stderr_lines=stderr_lines,
                    error=error,
                )
                raise error
            self._last_invocation_context = self._build_invocation_context(
                event_state=event_state,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                diagnostics=diagnostics,
            )
            self._export_diagnostics(
                diagnostics_dir,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                response_text=response_text,
            )
        return response_text

    def _build_invocation_context(
        self,
        *,
        event_state: Dict[str, Any],
        stdout_lines: list[str],
        stderr_lines: list[str],
        diagnostics: _CodexProcessDiagnostics,
    ) -> Dict[str, Any]:
        request_id: Optional[str] = None
        finish_reason: Optional[str] = None
        for line in stdout_lines:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("request_id", "response_id", "id", "run_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    request_id = value.strip()
            for key in ("finish_reason", "reason", "status"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    finish_reason = value.strip()
        return {
            "provider": self.provider_name,
            "model": self.model,
            "request_id": request_id,
            "finish_reason": finish_reason,
            "last_event_type": event_state.get("last_event_type"),
            "stdout_event_count": int(event_state.get("stdout_event_count", 0) or 0),
            "stderr_summary": diagnostics.stderr_summary,
            "elapsed_seconds": round(diagnostics.elapsed_seconds, 3),
        }

    def _load_invocation_context_from_events(self, events_path: Path) -> Dict[str, Any]:
        if not events_path.exists():
            return {}
        stdout_lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
        context: Dict[str, Any] = {}
        for line in stdout_lines:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("request_id", "response_id", "id", "run_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    context["request_id"] = value.strip()
            for key in ("finish_reason", "reason", "status"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    context["finish_reason"] = value.strip()
            event_type = _extract_event_type(payload)
            if event_type:
                context["last_event_type"] = event_type
        return context

    def _write_prompt(self, proc: subprocess.Popen[str], prompt: str) -> None:
        if proc.stdin is None:
            return
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()

    def _wait_for_process_exit(
        self,
        proc: subprocess.Popen[str],
        start_time: float,
        event_state: Dict[str, Any],
        stderr_lines: list[str],
        stdout_lines: list[str],
        diagnostics_dir: Optional[str],
    ) -> None:
        deadline = start_time + max(float(self.timeout_seconds), 1.0)
        while True:
            if self._cancel_requested.is_set():
                self._terminate_process_tree(proc)
                diagnostics = self._build_diagnostics(proc, start_time, event_state, stderr_lines)
                error = CodexInvocationError(
                    "CODEX_PROCESS_FAILED",
                    "Codex invocation cancelled",
                    details=diagnostics.as_timeout_details(),
                )
                self._export_diagnostics(
                    diagnostics_dir,
                    stdout_lines=stdout_lines,
                    stderr_lines=stderr_lines,
                    error=error,
                )
                raise error
            rc = proc.poll()
            if rc is not None:
                return
            if time.monotonic() >= deadline:
                self._terminate_process_tree(proc)
                diagnostics = self._build_diagnostics(proc, start_time, event_state, stderr_lines)
                error = CodexInvocationError(
                    "CODEX_PROCESS_TIMEOUT",
                    "Codex invocation timed out",
                    details=diagnostics.as_timeout_details(),
                )
                self._export_diagnostics(
                    diagnostics_dir,
                    stdout_lines=stdout_lines,
                    stderr_lines=stderr_lines,
                    error=error,
                )
                raise error
            time.sleep(0.1)

    def _build_diagnostics(
        self,
        proc: subprocess.Popen[str],
        start_time: float,
        event_state: Dict[str, Any],
        stderr_lines: list[str],
    ) -> _CodexProcessDiagnostics:
        elapsed = max(time.monotonic() - start_time, 0.0)
        stderr_text = "".join(stderr_lines)
        return _CodexProcessDiagnostics(
            process_id=int(getattr(proc, "pid", 0) or 0),
            elapsed_seconds=elapsed,
            return_code=proc.returncode,
            last_event_type=event_state.get("last_event_type"),
            stderr_summary=_redact_diagnostic_text(stderr_text),
            stdout_event_count=int(event_state.get("stdout_event_count", 0) or 0),
        )

    def _export_diagnostics(
        self,
        diagnostics_dir: Optional[str],
        *,
        stdout_lines: list[str],
        stderr_lines: list[str],
        response_text: Optional[str] = None,
        error: Optional[CodexInvocationError] = None,
    ) -> None:
        if not diagnostics_dir:
            return
        target = Path(diagnostics_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        try:
            if stdout_lines:
                (target / "events.jsonl").write_text("".join(stdout_lines), encoding="utf-8")
            if stderr_lines:
                (target / "stderr-summary.txt").write_text(_redact_diagnostic_text("".join(stderr_lines)), encoding="utf-8")
            if response_text is not None:
                (target / "final-output.json").write_text(response_text, encoding="utf-8")
            if error is not None:
                (target / "error.json").write_text(
                    json.dumps(
                        {
                            "error_code": error.error_code,
                            "message": error.args[0],
                            "details": error.details,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except Exception:
            return

    def _start_stream_reader(
        self,
        stream: Any,
        sink: list[str],
        *,
        event_state: Optional[Dict[str, Any]] = None,
        state_lock: Optional[threading.Lock] = None,
        is_stdout: bool = False,
    ) -> Optional[threading.Thread]:
        if stream is None:
            return None

        def _run() -> None:
            while True:
                line = stream.readline()
                if line == "":
                    break
                sink.append(line)
                if not is_stdout or event_state is None or state_lock is None:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    event_type = _extract_event_type(payload)
                    if event_type:
                        with state_lock:
                            event_state["last_event_type"] = event_type
                            event_state["stdout_event_count"] = int(event_state.get("stdout_event_count", 0) or 0) + 1

        thread = threading.Thread(target=_run, name="codex-stream-reader", daemon=True)
        thread.start()
        return thread

    def _join_reader_threads(self, *threads: Optional[threading.Thread]) -> None:
        for thread in threads:
            if thread is not None:
                thread.join(timeout=5)

    def _close_process_streams(self, proc: subprocess.Popen[str]) -> None:
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def _wait_for_exit_window(self, proc: subprocess.Popen[str], timeout_seconds: float = 2.0) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        return proc.poll() is not None

    def _taskkill_process_tree(self, pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
        )

    def _terminate_process_tree(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        if self._wait_for_exit_window(proc, timeout_seconds=2.0):
            return
        try:
            self._taskkill_process_tree(int(getattr(proc, "pid", 0) or 0))
        except Exception:
            pass
        if self._wait_for_exit_window(proc, timeout_seconds=2.0):
            return
        try:
            proc.kill()
        except Exception:
            pass

    def _ensure_git_repo(self, cwd: Path) -> None:
        proc = subprocess.run(
            ["git", "init"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            raise CodexInvocationError(
                "CODEX_PROCESS_FAILED",
                proc.stderr.strip() or "failed to initialize temporary git repository",
            )

    def _parse_response(
        self,
        response_text: str,
        task_packet: Dict[str, Any],
        workspace_snapshot: WorkspaceSnapshot,
    ) -> PatchProposal:
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise CodexInvocationError("CODEX_OUTPUT_PARSE_FAILED", "Codex output was not valid JSON") from exc
        if not isinstance(data, dict):
            raise CodexInvocationError("CODEX_OUTPUT_PARSE_FAILED", "Codex output must be a JSON object")
        files = data.get("files")
        if not isinstance(files, list) or len(files) != 1:
            raise CodexInvocationError("CODEX_OUTPUT_PARSE_FAILED", "Codex proposal must contain exactly one file")
        file_item = files[0]
        if not isinstance(file_item, dict):
            raise CodexInvocationError("CODEX_OUTPUT_PARSE_FAILED", "Codex file entry must be an object")
        provider_metadata = data.get("provider_metadata")
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}
        relative_path = str(file_item.get("relative_path", ""))
        operation = str(file_item.get("operation", ""))
        new_content = file_item.get("new_content", "")
        if new_content is None:
            new_content = ""
        new_content_text = str(new_content)
        encoding = str(file_item.get("encoding", "utf-8") or "utf-8")
        original_content = workspace_snapshot.allowed_files.get(relative_path, "")
        invocation_context = dict(self._last_invocation_context)
        proposal_files = [
            PatchFile(
                relative_path=relative_path,
                operation=operation,
                expected_sha256=workspace_snapshot.file_hashes.get(relative_path, ""),
                new_content=new_content_text,
                encoding=encoding,
            )
        ]
        return PatchProposal(
            proposal_id=f"pp-{uuid.uuid4().hex[:16]}",
            task_id=int(task_packet.get("task_id", 0)),
            provider=self.provider_name,
            files=proposal_files,
            unified_diff=build_unified_diff(relative_path, original_content, new_content_text),
            explanation=str(data.get("explanation", "")),
            expected_tests=list(workspace_snapshot.allowed_test_commands),
            risks=[str(item) for item in data.get("risks", []) if isinstance(item, str)],
            generated_at=utc_now(),
            metadata={
                "provider": self.provider_name,
                "model": self.model,
                "request_id": invocation_context.get("request_id"),
                "finish_reason": invocation_context.get("finish_reason"),
                "bridge": {
                    "provider_name": self.provider_name,
                    "model": self.model,
                    "enabled": self.enabled,
                    "timeout_seconds": self.timeout_seconds,
                    "max_retries": self.max_retries,
                },
                "invocation": {
                    "last_event_type": invocation_context.get("last_event_type"),
                    "stdout_event_count": invocation_context.get("stdout_event_count", 0),
                    "stderr_summary": invocation_context.get("stderr_summary", ""),
                    "elapsed_seconds": invocation_context.get("elapsed_seconds", 0.0),
                },
                "source_provider_metadata": {
                    key: value
                    for key, value in provider_metadata.items()
                    if key not in {"provider", "model", "request_id", "finish_reason"}
                },
            },
        )

    def _output_schema(self) -> Dict[str, Any]:
        return build_codex_output_schema()

    def validate_output_schema(self, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        candidate = schema if schema is not None else self._output_schema()
        try:
            validate_strict_schema(candidate)
        except SchemaValidationError as exc:
            return {
                "ok": False,
                "error_code": "CODEX_OUTPUT_SCHEMA_INVALID",
                "pointer": exc.pointer,
                "message": exc.reason,
            }
        return {"ok": True}

    def _validate_proposal(
        self,
        proposal: PatchProposal,
        task_packet: Dict[str, Any],
        workspace_snapshot: WorkspaceSnapshot,
    ) -> None:
        if proposal.provider != "codex":
            raise PatchApplicationError("unexpected provider")
        if len(proposal.files) != 1:
            raise PatchApplicationError("Codex proposal must contain one file")
        file_item = proposal.files[0]
        allowed = set(workspace_snapshot.allowed_files.keys())
        if file_item.relative_path not in allowed:
            raise PatchApplicationError("proposal targets a disallowed file")
        if file_item.relative_path != "calculator.py":
            raise PatchApplicationError("Codex provider only permits calculator.py in this runtime")
        if file_item.operation != "modify":
            raise PatchApplicationError("Codex provider only permits modify operations")
        if file_item.expected_sha256 != workspace_snapshot.file_hashes[file_item.relative_path]:
            raise PatchApplicationError("expected_sha256 mismatch")
        if file_item.new_content is None or not file_item.new_content.strip():
            raise PatchApplicationError("new_content is required")
        if len(file_item.new_content.encode("utf-8")) > int(task_packet.get("max_patch_bytes", 0) or 0):
            raise PatchApplicationError("patch exceeds size limit")
        if len(proposal.files) > int(task_packet.get("max_files_changed", 0) or 0):
            raise PatchApplicationError("file count exceeds limit")
