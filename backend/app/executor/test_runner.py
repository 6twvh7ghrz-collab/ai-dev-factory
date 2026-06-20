"""测试运行器 - 独立运行测试，不依赖 AI 自报结果

必须真实运行 pytest 并检查退出码。

V1.8B-R: 支持工具链环境注入（通过 CommandRunner 的 use_toolchain_env）。
"""
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from pathlib import Path
from .command_runner import CommandRunner, CommandResult


@dataclass
class TestResult:
    """测试结果"""
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    test_summary: str = ""
    failures: List[str] = field(default_factory=list)
    error: Optional[str] = None
    resolved_executable: str = ""  # V1.8B-R: 实际解析的可执行文件


class TestRunner:
    """独立测试运行器（V1.8B-R: 支持工具链环境注入）"""

    def __init__(self, worktree_path: str, use_toolchain_env: bool = True):
        self.worktree_path = Path(worktree_path).resolve()
        self.runner = CommandRunner(use_toolchain_env=use_toolchain_env)

    def run_pytest(self, test_path: str = None,
                   extra_args: List[str] = None,
                   timeout: int = 120) -> TestResult:
        """
        运行 pytest

        Args:
            test_path: 测试文件或目录路径（相对于 worktree_path）
            extra_args: 额外 pytest 参数
            timeout: 超时秒数
        """
        args = ["-v", "--tb=short"]

        if test_path:
            args.append(str(test_path))

        if extra_args:
            args.extend(extra_args)

        result = self.runner.run(
            ["pytest"] + args,
            cwd=str(self.worktree_path),
            timeout=timeout,
        )

        # 解析测试摘要
        failures = []
        if not result.success:
            for line in result.stdout.split("\n"):
                if "FAILED" in line:
                    failures.append(line.strip())

        return TestResult(
            passed=result.success and result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            test_summary=self._extract_summary(result.stdout),
            failures=failures,
            error=result.error,
            resolved_executable=result.resolved_executable,
        )

    def run_python_test(self, test_script: str,
                        timeout: int = 120) -> TestResult:
        """
        运行单个 Python 测试脚本

        Args:
            test_script: Python 测试脚本路径（相对于 worktree_path）
        """
        result = self.runner.run(
            ["python", str(test_script)],
            cwd=str(self.worktree_path),
            timeout=timeout,
        )

        return TestResult(
            passed=result.success and result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            test_summary="PASS" if result.success else "FAIL",
            error=result.error,
            resolved_executable=result.resolved_executable,
        )

    def run_command_test(self, command: List[str],
                         timeout: int = 120) -> TestResult:
        """
        运行自定义命令作为测试

        Args:
            command: 命令和参数列表
        """
        result = self.runner.run(
            command,
            cwd=str(self.worktree_path),
            timeout=timeout,
        )

        return TestResult(
            passed=result.success and result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            test_summary="PASS" if result.success else "FAIL",
            error=result.error,
            resolved_executable=result.resolved_executable,
        )

    @staticmethod
    def _extract_summary(stdout: str) -> str:
        """从 pytest 输出提取摘要"""
        for line in stdout.split("\n"):
            line = line.strip()
            if "passed" in line.lower() or "failed" in line.lower():
                if "=" in line and ("passed" in line or "failed" in line or "error" in line):
                    return line.strip("=").strip()
        return stdout.strip().split("\n")[-1] if stdout.strip() else "no output"
