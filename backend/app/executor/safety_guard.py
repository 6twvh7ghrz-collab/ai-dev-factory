"""安全检查器 - 检查文件修改范围是否在允许列表内

首轮只允许修改指定文件（如 calculator.py）。
禁止修改 README.md、测试文件、.git、正式项目代码、正式数据库等。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Set
from pathlib import Path


@dataclass
class SafetyResult:
    """安全检查结果"""
    passed: bool
    violations: List[str] = field(default_factory=list)
    allowed_files: Set[str] = field(default_factory=set)
    modified_files: List[str] = field(default_factory=list)
    blocked_files: List[str] = field(default_factory=list)
    reason: str = ""


class SafetyGuard:
    """安全检查器"""

    # 全局禁止修改的文件模式
    FORBIDDEN_PATTERNS = [
        ".git/",
        ".gitignore",
        ".env",
        ".env.example",
        "*.db",
        "node_modules/",
        "__pycache__/",
        "*.pyc",
        ".executor/",
        "checkpoint/",
    ]

    # 禁止修改的文件（精确匹配）
    FORBIDDEN_EXACT = [
        "README.md",
        "LICENSE",
    ]

    def __init__(self, allowed_files: List[str] = None):
        """
        Args:
            allowed_files: 允许修改的文件列表（相对路径）
        """
        self.allowed_files: Set[str] = set(allowed_files or [])

    def set_allowed_files(self, files: List[str]):
        """设置允许修改的文件列表"""
        self.allowed_files = set(files)

    def check_files(self, modified_files: List[str],
                    worktree_root: str = ".") -> SafetyResult:
        """
        检查修改的文件是否在允许范围内

        Args:
            modified_files: 实际修改的文件列表
            worktree_root: 工作区根目录

        Returns:
            SafetyResult
        """
        violations = []
        blocked = []
        allowed = []

        for f in modified_files:
            f_normalized = f.replace("\\", "/")

            # 检查精确禁止列表
            if f_normalized in self.FORBIDDEN_EXACT:
                blocked.append(f)
                violations.append(f"禁止修改文件: {f}")
                continue

            # 检查模式禁止列表
            is_forbidden = False
            for pattern in self.FORBIDDEN_PATTERNS:
                if pattern.endswith("/"):
                    if f_normalized.startswith(pattern) or f"/{pattern}" in f_normalized:
                        is_forbidden = True
                        break
                elif pattern.startswith("*."):
                    if f_normalized.endswith(pattern[1:]):
                        is_forbidden = True
                        break
                elif f_normalized == pattern or f_normalized.endswith(f"/{pattern}"):
                    is_forbidden = True
                    break

            if is_forbidden:
                blocked.append(f)
                violations.append(f"命中禁止模式: {f}")
                continue

            # 检查允许列表
            if self.allowed_files:
                is_allowed = False
                for af in self.allowed_files:
                    af_norm = af.replace("\\", "/")
                    if f_normalized == af_norm or f_normalized.endswith(f"/{af_norm}"):
                        is_allowed = True
                        break

                if is_allowed:
                    allowed.append(f)
                else:
                    blocked.append(f)
                    violations.append(f"不在允许列表中: {f}")
            else:
                # 无允许列表时，只要不命中禁止就算通过
                allowed.append(f)

        passed = len(violations) == 0
        reason = "安全检查通过" if passed else f"发现 {len(violations)} 个违规"

        return SafetyResult(
            passed=passed,
            violations=violations,
            allowed_files=self.allowed_files,
            modified_files=modified_files,
            blocked_files=blocked,
            reason=reason,
        )

    @staticmethod
    def check_command(command_parts: List[str]) -> SafetyResult:
        """
        检查命令是否安全（防止危险操作）

        Args:
            command_parts: 命令和参数列表
        """
        dangerous_patterns = [
            "rm -rf /",
            "del /f /s C:\\",
            "format",
            "> /dev/sda",
            "dd if=",
            "mkfs.",
            ":(){ :|:& };:",  # fork bomb
            "chmod 777 /",
            "DROP TABLE",
            "DROP DATABASE",
        ]

        cmd_str = " ".join(command_parts)
        for pattern in dangerous_patterns:
            if pattern.lower() in cmd_str.lower():
                return SafetyResult(
                    passed=False,
                    violations=[f"危险命令模式: {pattern}"],
                    reason=f"命令包含危险操作: {pattern}",
                )

        return SafetyResult(passed=True, reason="命令安全检查通过")
