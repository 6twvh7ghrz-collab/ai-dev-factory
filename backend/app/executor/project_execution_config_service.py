"""
ProjectExecutionConfigService - 正式项目执行配置写入服务 V1.0

提供受控的 execution_enabled 开关能力。
与 ProjectExecutionGuard（只读校验）配合使用：
  - Guard: 只读安全校验
  - Service: 受控写入

要求：
  1. 使用事务
  2. 校验项目存在
  3. 校验 workspace 存在
  4. 校验 workspace 是 Git 仓库
  5. 记录修改前后配置
  6. 记录 reason 和 changed_by
  7. 不允许修改其他项目
  8. 不允许直接对外暴露任意 SQL
"""

import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("executor.project_execution_config_service")


class ProjectExecutionConfigService:
    """正式项目执行配置服务

    只负责 execution_enabled 开关和其他配置字段的受控写入。
    不负责安全校验（安全校验由 ProjectExecutionGuard 负责）。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 读取 ──

    def get_config(self, project_id: int) -> Optional[Dict[str, Any]]:
        """获取项目执行配置"""
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

    # ── 写入 ──

    def set_execution_enabled(
        self,
        project_id: int,
        enabled: bool,
        reason: str = "",
        changed_by: str = "system",
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """设置项目的 execution_enabled 开关

        校验链：
          1. 项目在 projects 表中存在
          2. 项目在 project_execution_configs 中有配置
          3. workspace_path 非空
          4. workspace 目录存在
          5. workspace 是有效 Git 仓库

        Args:
            project_id: 项目 ID
            enabled: True 开启，False 关闭
            reason: 变更原因（会被记录）
            changed_by: 变更发起者

        Returns:
            (success, message, {before: dict, after: dict})
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 1. 校验项目存在
            cur.execute("SELECT id, name FROM projects WHERE id = ?", (project_id,))
            proj = cur.fetchone()
            if not proj:
                return (False, f"PROJECT_NOT_FOUND: 项目 #{project_id} 不存在", None)

            # 2. 获取当前配置
            cur.execute(
                "SELECT * FROM project_execution_configs WHERE project_id = ?",
                (project_id,)
            )
            config_row = cur.fetchone()
            if not config_row:
                return (False, f"CONFIG_NOT_FOUND: 项目 #{project_id} 没有执行配置", None)

            config_before = dict(config_row)

            # 3. 校验 workspace_path 非空
            workspace_path = config_before.get("workspace_path")
            if not workspace_path:
                return (False, "WORKSPACE_NOT_CONFIGURED: 工作区路径未配置", None)

            # 4. 校验 workspace 目录存在
            ws_path = Path(workspace_path)
            if not ws_path.exists():
                return (False, f"WORKSPACE_NOT_FOUND: 工作区路径不存在: {workspace_path}", None)
            if not ws_path.is_dir():
                return (False, f"WORKSPACE_NOT_DIRECTORY: 路径不是目录: {workspace_path}", None)

            # 5. 校验是 Git 仓库
            git_dir = ws_path / ".git"
            if not git_dir.exists():
                return (False, f"NOT_GIT_REPO: 工作区不是 Git 仓库: {workspace_path}", None)

            # 6. 开启时额外检查 Git clean
            if enabled:
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
                        return (False, f"GIT_STATUS_FAILED: {result.stderr.strip()[:200]}", None)
                    if result.stdout.strip():
                        preview = result.stdout.strip()[:200]
                        return (False, f"GIT_WORKING_TREE_DIRTY: 工作区有未提交更改: {preview}", None)
                except FileNotFoundError:
                    return (False, "GIT_NOT_FOUND: 系统中未找到 Git", None)

            # 7. 执行更新（事务内）
            enabled_int = 1 if enabled else 0
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            cur.execute("""
                UPDATE project_execution_configs
                SET execution_enabled = ?,
                    updated_at = ?
                WHERE project_id = ?
            """, (enabled_int, now, project_id))

            if cur.rowcount != 1:
                conn.rollback()
                return (False, "UPDATE_FAILED: 更新影响行数异常", None)

            conn.commit()

            # 8. 读取更新后配置
            cur.execute(
                "SELECT * FROM project_execution_configs WHERE project_id = ?",
                (project_id,)
            )
            config_after = dict(cur.fetchone())

            # 9. 记录操作日志
            self._log_operation(
                project_id=project_id,
                operation="set_execution_enabled",
                before_value=config_before.get("execution_enabled"),
                after_value=enabled_int,
                reason=reason,
                changed_by=changed_by,
            )

            logger.info(
                "ProjectExecutionConfigService: project_id=%s execution_enabled %s -> %s, "
                "reason=%s, changed_by=%s",
                project_id,
                config_before.get("execution_enabled"),
                enabled_int,
                reason,
                changed_by,
            )

            return (True, "OK", {
                "before": {
                    "execution_enabled": bool(config_before.get("execution_enabled")),
                    "workspace_path": config_before.get("workspace_path"),
                },
                "after": {
                    "execution_enabled": bool(config_after.get("execution_enabled")),
                    "workspace_path": config_after.get("workspace_path"),
                    "updated_at": config_after.get("updated_at"),
                },
                "reason": reason,
                "changed_by": changed_by,
            })

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("ProjectExecutionConfigService.set_execution_enabled error: %s", e)
            return (False, f"INTERNAL_ERROR: {str(e)}", None)
        finally:
            conn.close()

    def _log_operation(
        self,
        project_id: int,
        operation: str,
        before_value,
        after_value,
        reason: str,
        changed_by: str,
    ):
        """记录配置变更操作日志

        写入 executor_audit_log 表（如果存在），否则写入文件日志。
        """
        conn = self._get_conn()
        try:
            # 尝试写入 audit 表
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_audit_log'"
            )
            if cur.fetchone():
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("""
                    INSERT INTO executor_audit_log
                    (project_id, operation, before_value, after_value, reason, changed_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    project_id, operation,
                    json.dumps({"execution_enabled": before_value}),
                    json.dumps({"execution_enabled": after_value}),
                    reason, changed_by, now,
                ))
                conn.commit()
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)
        finally:
            conn.close()

        # 始终记录到 logger
        logger.info(
            "CONFIG_CHANGE | project=%s | op=%s | %s -> %s | by=%s | reason=%s",
            project_id, operation, before_value, after_value, changed_by, reason,
        )


# 全局单例
_config_service: Optional[ProjectExecutionConfigService] = None


def get_project_execution_config_service(db_path: str = None) -> ProjectExecutionConfigService:
    """获取全局 ProjectExecutionConfigService 单例"""
    global _config_service
    if _config_service is None:
        if db_path is None:
            db_path = str(
                Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db"
            )
        _config_service = ProjectExecutionConfigService(db_path)
    return _config_service
