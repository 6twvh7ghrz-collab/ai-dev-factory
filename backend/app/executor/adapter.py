"""外部CLI适配器 - 检测和封装外部可执行工具

设计原则：
  1. 真实检测 CLI 可用性 (--version / --help)
  2. 不假装 CodeBuddy/Claude Code 等 AI CLI 可用
  3. 每个适配器明确标记 is_ai_adapter / adapter_type
"""
import subprocess
import shutil
import json
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from pathlib import Path


@dataclass
class ExecutorAdapter:
    """外部CLI适配器"""
    name: str
    available: bool
    is_ai_adapter: bool
    command: str
    version: str = ""
    supports_non_interactive: bool = True
    adapter_type: str = "generic_cli"  # generic_cli / deterministic_test_cli / ai_cli
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "is_ai_adapter": self.is_ai_adapter,
            "command": self.command,
            "version": self.version,
            "supports_non_interactive": self.supports_non_interactive,
            "adapter_type": self.adapter_type,
            "metadata": self.metadata,
        }

    def execute(self, args: List[str], cwd: str = None,
                timeout: int = 60) -> subprocess.CompletedProcess:
        """执行 CLI 命令"""
        cmd = [self.command] + args
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def check_output(self, args: List[str], cwd: str = None,
                     timeout: int = 60) -> str:
        """执行并返回 stdout"""
        result = self.execute(args, cwd=cwd, timeout=timeout)
        return result.stdout.strip()


def _detect_version(command: str, version_args: List[str] = None) -> str:
    """检测 CLI 版本"""
    if version_args is None:
        version_args = ["--version"]

    for args in [version_args, ["-V"], ["version"]]:
        try:
            result = subprocess.run(
                [command] + args,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("\n")[0][:100]
            if result.stderr.strip() and "version" in result.stderr.lower():
                return result.stderr.strip().split("\n")[0][:100]
        except Exception:
            continue
    return ""


def _detect_help(command: str) -> bool:
    """检测 --help 是否可用"""
    for args in [["--help"], ["-h"], ["/?"]]:
        try:
            result = subprocess.run(
                [command] + args,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
        except Exception:
            continue
    return False


def detect_available_adapters() -> List[ExecutorAdapter]:
    """检测所有可用的外部CLI适配器"""
    adapters = []

    # ── Python 解释器 ──
    python_path = shutil.which("python") or shutil.which("python3") or "python"
    python_version = _detect_version(python_path)
    adapters.append(ExecutorAdapter(
        name="Python",
        available=bool(python_version),
        is_ai_adapter=False,
        command=python_path,
        version=python_version,
        adapter_type="generic_cli",
    ))

    # ── pytest ──
    pytest_path = shutil.which("pytest") or shutil.which("pytest.exe")
    pytest_version = _detect_version(pytest_path) if pytest_path else ""
    adapters.append(ExecutorAdapter(
        name="pytest",
        available=bool(pytest_path and pytest_version),
        is_ai_adapter=False,
        command=pytest_path or "pytest",
        version=pytest_version,
        adapter_type="deterministic_test_cli",
    ))

    # ── git ──
    git_path = shutil.which("git") or shutil.which("git.exe") or "git"
    git_version = _detect_version(git_path)
    adapters.append(ExecutorAdapter(
        name="Git",
        available=bool(git_version),
        is_ai_adapter=False,
        command=git_path,
        version=git_version,
        adapter_type="generic_cli",
    ))

    # ── Node / npm ──
    node_path = shutil.which("node") or shutil.which("node.exe")
    node_version = _detect_version(node_path) if node_path else ""
    adapters.append(ExecutorAdapter(
        name="Node.js",
        available=bool(node_path and node_version),
        is_ai_adapter=False,
        command=node_path or "node",
        version=node_version,
        adapter_type="generic_cli",
    ))

    # ── 确定性测试CLI（自定义脚本） ──
    # 用于沙箱测试，不依赖真实AI
    adapters.append(ExecutorAdapter(
        name="DeterministicTestCLI",
        available=True,  # 由 Python 自身提供
        is_ai_adapter=False,
        command="python",
        version=python_version,
        adapter_type="deterministic_test_cli",
        metadata={"description": "使用 Python subprocess 运行确定性测试脚本"},
    ))

    return adapters
