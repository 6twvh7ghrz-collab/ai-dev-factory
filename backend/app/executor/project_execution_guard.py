"""
ProjectExecutionGuard - 统一项目执行安全校验服务

为自然语言 execute API 和原有 executor start API 提供统一的安全校验。
替代名称硬编码白名单，基于数据库 project_execution_configs 表。

校验规则：
  1. 配置存在
  2. execution_enabled = true
  3. workspace_path 为绝对路径
  4. 路径位于允许根目录（通过 WorkspaceGuard）
  5. 目录真实存在
  6. 是有效 Git 仓库
  7. working tree clean
  8. 不是 AI 工厂自身目录
  9. 不是系统目录
  10. 不是用户主目录

注意：run-specific 检查（活跃 run、Lease、资源锁）由调用方负责，
本 Guard 只做配置和路径层面的安全检查。
"""
import os
import sqlite3
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from .workspace_guard import get_workspace_guard, WorkspaceGuard

logger = logging.getLogger("executor.project_execution_guard")


class ProjectExecutionGuard:
    """统一项目执行安全校验服务

    同时服务于：
    - AIBrainController.execute_write (自然语言 execute API)
    - executor/start API
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._workspace_guard = get_workspace_guard()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_config(self, project_id: int) -> Optional[Dict[str, Any]]:
        """获取项目执行配置

        Returns:
            dict or None if not configured
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM project_execution_configs WHERE project_id = ?",
                (project_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def validate_project(self, project_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """验证项目是否允许自动执行

        执行完整的配置和安全检查链。

        Args:
            project_id: 项目 ID

        Returns:
            (allowed, reason, config_dict)
        """
        # 1. 检查配置是否存在
        config = self.get_config(project_id)
        if config is None:
            return (False, "PROJECT_NOT_CONFIGURED",
                    {"code": "PROJECT_NOT_CONFIGURED",
                     "message": f"项目 #{project_id} 没有执行配置，请先在 project_execution_configs 表中配置"})

        # 2. 检查 execution_enabled
        if not config.get("execution_enabled"):
            return (False, "EXECUTION_NOT_ENABLED",
                    {"code": "EXECUTION_NOT_ENABLED",
                     "message": f"项目 #{project_id} 尚未授权 AI 自动执行"})

        # 3. 检查 workspace_path
        workspace_path = config.get("workspace_path")
        if not workspace_path:
            return (False, "WORKSPACE_NOT_CONFIGURED",
                    {"code": "WORKSPACE_NOT_CONFIGURED",
                     "message": f"项目 #{project_id} 未配置工作区路径"})

        # 4. 通过 WorkspaceGuard 验证路径安全
        allowed, reason, resolved = self._workspace_guard.validate(workspace_path)
        if not allowed:
            return (False, "WORKSPACE_FORBIDDEN",
                    {"code": "WORKSPACE_FORBIDDEN",
                     "message": f"工作区安全验证失败: {reason}",
                     "detail": reason})

        # 5. 验证目录真实存在
        ws_path = Path(resolved)
        if not ws_path.exists():
            return (False, "WORKSPACE_NOT_FOUND",
                    {"code": "WORKSPACE_NOT_FOUND",
                     "message": f"工作区路径不存在: {resolved}"})

        if not ws_path.is_dir():
            return (False, "WORKSPACE_NOT_DIRECTORY",
                    {"code": "WORKSPACE_NOT_DIRECTORY",
                     "message": f"工作区路径不是目录: {resolved}"})

        # 6. 验证是有效 Git 仓库
        git_dir = ws_path / ".git"
        if not git_dir.exists():
            return (False, "NOT_GIT_REPO",
                    {"code": "NOT_GIT_REPO",
                     "message": f"工作区不是有效的 Git 仓库: {resolved}"})

        # 7. 验证 working tree clean
        import subprocess
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(ws_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return (False, "GIT_STATUS_FAILED",
                        {"code": "GIT_STATUS_FAILED",
                         "message": f"Git 状态检查失败: {result.stderr.strip()[:200]}"})

            if result.stdout.strip():
                lines = result.stdout.strip().split('\n')[:5]
                preview = '; '.join(line.strip()[:60] for line in lines)
                return (False, "GIT_WORKING_TREE_DIRTY",
                        {"code": "GIT_WORKING_TREE_DIRTY",
                         "message": f"工作区有未提交的更改: {preview}"})
        except FileNotFoundError:
            return (False, "GIT_NOT_FOUND",
                    {"code": "GIT_NOT_FOUND",
                     "message": "系统中未找到 Git 命令"})
        except subprocess.TimeoutExpired:
            return (False, "GIT_TIMEOUT",
                    {"code": "GIT_TIMEOUT",
                     "message": "Git 状态检查超时"})

        # 全部通过
        return (True, "OK", {
            "code": "PROJECT_EXECUTION_ALLOWED",
            "project_id": project_id,
            "workspace_path": str(ws_path),
            "execution_mode": config.get("execution_mode", "sandbox"),
            "max_workers": config.get("max_workers", 1),
            "max_tasks": config.get("max_tasks", 10),
            "requires_confirmation": bool(config.get("requires_confirmation", 1)),
            "allowed_models": config.get("allowed_models_json", "[]"),
            "config": config,
        })

    def validate_for_start(self, project_id: int) -> Tuple[bool, str, str, Optional[Dict[str, Any]]]:
        """专门为 executor/start API 提供验证

        Returns:
            (allowed, reason, workspace_path, detail_dict)
        """
        allowed, reason, detail = self.validate_project(project_id)
        if not allowed:
            return (False, reason, None, detail)

        workspace_path = detail.get("workspace_path")
        return (True, "OK", workspace_path, detail)

    def check_any_project_enabled(self, project_ids: list) -> Dict[int, bool]:
        """批量检查哪些项目启用了执行

        Returns:
            {project_id: enabled}
        """
        if not project_ids:
            return {}

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in project_ids)
            cur.execute(
                f"SELECT project_id, execution_enabled FROM project_execution_configs WHERE project_id IN ({placeholders})",
                project_ids
            )
            result = {pid: False for pid in project_ids}
            for row in cur.fetchall():
                result[row["project_id"]] = bool(row["execution_enabled"])
            return result
        finally:
            conn.close()

    def get_all_enabled_projects(self) -> list:
        """获取所有启用了执行的项目的配置"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM project_execution_configs WHERE execution_enabled = 1"
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()


# 全局单例
_guard: Optional[ProjectExecutionGuard] = None


def get_project_execution_guard(db_path: str = None) -> ProjectExecutionGuard:
    """获取全局 ProjectExecutionGuard 单例"""
    global _guard
    if _guard is None:
        if db_path is None:
            from pathlib import Path as _Path
            db_path = str(_Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")
        _guard = ProjectExecutionGuard(db_path)
    return _guard
