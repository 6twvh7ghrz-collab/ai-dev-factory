"""
工作区安全边界

防止任意目录执行：
1. repo_path 必须使用绝对路径
2. resolve() 后比较，防止 .. 目录穿越
3. 只能位于允许根目录内
4. 禁止操作 AI 工厂自身 / 用户主目录 / 系统目录
5. 拒绝时返回明确 403 和原因
6. 记录安全日志
"""
import os
import logging
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger("executor.workspace_guard")


class WorkspaceGuard:
    """
    执行器工作区安全守护

    只允许在 EXECUTOR_ALLOWED_WORKSPACES 中登记的目录执行
    """

    # 默认只允许沙箱（可通过环境变量扩展）
    DEFAULT_ALLOWED: List[str] = [
        r"C:\SandboxUser\本机\Desktop\executor-sandbox-v2",
    ]

    # 绝对禁止的目录前缀
    FORBIDDEN_PREFIXES: List[str] = [
        r"C:\Windows",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"C:\System",
    ]

    # 绝对禁止的路径特征（不区分大小写检查）
    FORBIDDEN_KEYWORDS: List[str] = [
        ".env",
        ".ssh",
        "AppData",
    ]

    def __init__(self, allowed_workspaces: Optional[List[str]] = None):
        # 优先从环境变量读取
        env_ws = os.getenv("EXECUTOR_ALLOWED_WORKSPACES", "")
        if env_ws:
            self.allowed = [p.strip() for p in env_ws.split(";") if p.strip()]
        else:
            self.allowed = allowed_workspaces or self.DEFAULT_ALLOWED

        # 全部 resolve 为绝对路径
        self.allowed = [str(Path(p).resolve()) for p in self.allowed]

        # AI 工厂自身目录（禁止修改自己）
        self.factory_root = str(Path(__file__).resolve().parent.parent.parent.parent)

        # 用户主目录
        self.home_dir = str(Path.home())

    def validate(self, repo_path: str) -> Tuple[bool, str, str]:
        """
        验证 repo_path 是否在允许的工作区内

        Args:
            repo_path: 用户提交的仓库路径

        Returns:
            (is_allowed, reason, resolved_path)
        """
        if not repo_path:
            return (False, "repo_path 不能为空", "")

        # 1. 必须是绝对路径
        p = Path(repo_path)
        if not p.is_absolute():
            return (False, f"repo_path 必须是绝对路径: {repo_path}", "")

        # 2. resolve() 消除 .. 和符号链接
        try:
            resolved = str(p.resolve())
        except Exception as e:
            return (False, f"无法解析路径: {e}", "")

        # 3. 先检查是否在允许列表内（允许列表优先级高于禁止列表）
        in_allowlist = False
        for allowed in self.allowed:
            if self._is_under_or_equal(resolved, allowed):
                in_allowlist = True
                break

        if not in_allowlist:
            return (False, f"路径不在允许的工作区内: {resolved}", resolved)

        # 4. 禁止 AI 工厂自身目录（即使不在允许列表也会在前面被拦截）
        if self._is_under_or_equal(resolved, self.factory_root):
            return (False, f"禁止操作 AI 工厂自身目录: {resolved}", resolved)

        # 5. 禁止用户主目录（但允许列表内的子目录豁免）
        if self._is_under_or_equal(resolved, self.home_dir):
            # 如果路径就是 home_dir 本身或接近 home_dir（深度 <= 1），拒绝
            p_rel = Path(resolved).relative_to(self.home_dir)
            if len(p_rel.parts) <= 1:
                return (False, f"禁止操作靠近用户主目录根的位置: {resolved}", resolved)

        # 6. 禁止系统目录
        for forbidden in self.FORBIDDEN_PREFIXES:
            if self._is_under_or_equal(resolved, forbidden):
                return (False, f"禁止操作系统目录: {resolved} (匹配 {forbidden})", resolved)

        # 7. 禁止包含敏感关键词
        resolved_lower = resolved.lower()
        for kw in self.FORBIDDEN_KEYWORDS:
            if kw.lower() in resolved_lower:
                return (False, f"路径包含禁止关键词 '{kw}': {resolved}", resolved)

        # 8. 通过
        logger.info(f"工作区验证通过: {resolved} (允许根: {[a for a in self.allowed if self._is_under_or_equal(resolved, a)][0]})")
        return (True, "ok", resolved)

    @staticmethod
    def _is_under_or_equal(path: str, root: str) -> bool:
        """检查 path 是否等于 root 或在其子目录下"""
        p = Path(path).resolve()
        r = Path(root).resolve()
        try:
            p.relative_to(r)
            return True
        except ValueError:
            return False


# 全局单例
_guard: Optional[WorkspaceGuard] = None


def get_workspace_guard() -> WorkspaceGuard:
    global _guard
    if _guard is None:
        _guard = WorkspaceGuard()
    return _guard
