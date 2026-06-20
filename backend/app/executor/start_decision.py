"""
StartDecisionService - 统一启动决策编排服务 V1.2.2

将"开始自动开发"改为统一决策入口，而不是只允许已有 ready 任务的项目启动。

决策类型：
  EXECUTE_READY_TASKS  - 有可执行任务，可进入执行器
  PLAN_EXISTING_TASKS  - 有 needs_planning 任务，需要规划
  GENERATE_TASKS       - 没有任务，需要生成
  BIND_WORKSPACE       - 缺少工作区绑定
  WAIT_DEPENDENCIES    - 依赖未完成
  REQUEST_APPROVAL     - 高风险项目需确认
  ALREADY_RUNNING      - 已有活跃 run
  PROJECT_COMPLETED    - 全部完成
  BLOCK_UNSAFE         - 真正不安全（系统目录、路径穿越、Git损坏等）
"""
import os
import sqlite3
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("executor.start_decision")


class Decision(str, Enum):
    """启动决策枚举"""
    EXECUTE_READY_TASKS = "EXECUTE_READY_TASKS"
    PLAN_EXISTING_TASKS = "PLAN_EXISTING_TASKS"
    GENERATE_TASKS = "GENERATE_TASKS"
    BIND_WORKSPACE = "BIND_WORKSPACE"
    WAIT_DEPENDENCIES = "WAIT_DEPENDENCIES"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"
    BLOCK_UNSAFE = "BLOCK_UNSAFE"


@dataclass
class ProjectAudit:
    """项目审计结果"""
    project_id: int
    project_name: str
    execution_enabled: bool
    workspace_path: str = ""
    workspace_exists: bool = False
    git_valid: bool = False
    git_clean: bool = False
    pending_count: int = 0
    ready_count: int = 0
    needs_planning_count: int = 0
    completed_count: int = 0
    blocked_count: int = 0
    runnable_count: int = 0
    runnable_task_ids: list = field(default_factory=list)  # V1.8C-R: actual runnable task IDs
    active_run: bool = False
    active_run_status: str = ""
    decision: str = ""
    decision_reason: str = ""
    is_high_risk: bool = False


# 高风险项目关键词
_HIGH_RISK_KEYWORDS = [
    "电商", "采集", "爬虫", "数据库迁移", "迁移",
    "部署", "删除", "删除文件", "外部平台", "自动操作",
    "支付", "订单", "交易", "退款", "结算",
    "用户数据", "密码", "密钥", "认证",
    "生产环境", "线上", "prod", "production",
    "定时任务", "cron", "调度",
]

# 真正不安全的工作区条件（返回 BLOCK_UNSAFE）
_UNSAFE_WORKSPACE_CODES = {
    "WORKSPACE_FORBIDDEN",
    "WORKSPACE_NOT_FOUND",
    "WORKSPACE_NOT_DIRECTORY",
    "NOT_GIT_REPO",
    "GIT_STATUS_FAILED",
    "GIT_NOT_FOUND",
    "GIT_TIMEOUT",
    "GIT_WORKING_TREE_DIRTY",
}


class StartDecisionService:
    """统一启动决策编排服务

    负责：
    1. 全局审计所有项目
    2. 为单个项目生成启动决策
    3. 决策规则按优先级顺序执行
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 全局审计 ──

    def audit_all_projects(self) -> List[ProjectAudit]:
        """审计所有项目并返回每个项目的完整状态"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 获取所有项目
            cur.execute("SELECT id, name, status FROM projects ORDER BY id")
            projects = cur.fetchall()

            results = []
            for proj in projects:
                audit = self._audit_single_project(cur, proj)
                results.append(audit)

            return results
        finally:
            conn.close()

    def _audit_single_project(self, cur, proj) -> ProjectAudit:
        """审计单个项目"""
        pid = proj["id"]
        pname = proj["name"]

        audit = ProjectAudit(
            project_id=pid,
            project_name=pname,
            execution_enabled=False,
        )

        # 1. 获取执行配置
        cur.execute(
            "SELECT * FROM project_execution_configs WHERE project_id = ?",
            (pid,)
        )
        cfg_row = cur.fetchone()
        if cfg_row:
            cfg = dict(cfg_row)
            audit.execution_enabled = bool(cfg.get("execution_enabled", 0))
            audit.workspace_path = cfg.get("workspace_path", "") or ""

        # 2. 检查工作区
        if audit.workspace_path:
            ws = Path(audit.workspace_path)
            audit.workspace_exists = ws.exists() and ws.is_dir()
            if audit.workspace_exists:
                git_dir = ws / ".git"
                audit.git_valid = git_dir.exists()
                if audit.git_valid:
                    import subprocess
                    try:
                        result = subprocess.run(
                            ["git", "status", "--porcelain"],
                            cwd=str(ws),
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        audit.git_clean = result.returncode == 0 and not result.stdout.strip()
                    except Exception:
                        pass

        # 3. 任务统计
        cur.execute("""
            SELECT status, COUNT(*) as cnt
            FROM development_tasks
            WHERE project_id = ?
            GROUP BY status
        """, (pid,))
        status_counts = {row["status"]: row["cnt"] for row in cur.fetchall()}
        audit.pending_count = status_counts.get("pending", 0)
        audit.completed_count = status_counts.get("completed", 0)
        audit.blocked_count = status_counts.get("blocked", 0)

        # readiness_status 统计
        cur.execute("""
            SELECT COALESCE(readiness_status, 'draft') as rs, COUNT(*) as cnt
            FROM development_tasks
            WHERE project_id = ? AND status = 'pending'
            GROUP BY rs
        """, (pid,))
        for row in cur.fetchall():
            if row["rs"] == "ready":
                audit.ready_count = row["cnt"]
            elif row["rs"] == "needs_planning":
                audit.needs_planning_count = row["cnt"]

        # 4. 可执行任务数 + V1.8C-R: 捕获 runnable task IDs
        audit.runnable_count = audit.ready_count  # 简化：ready 即 runnable
        # 实际 runnable 还要考虑依赖、lease 等，这里做近似统计
        if audit.ready_count > 0:
            # 检查依赖和 lease
            from .task_scheduler import TaskScheduler
            scheduler = TaskScheduler(self.db_path)
            runnable = scheduler.find_runnable_tasks(pid)
            audit.runnable_count = len(runnable)
            audit.runnable_task_ids = [t.id for t in runnable]  # V1.8C-R

        # 5. 活跃 run
        cur.execute("""
            SELECT status FROM executor_runs
            WHERE project_id = ?
            AND status IN ('starting','scanning','claiming','executing',
                           'testing','repairing','paused','stopping')
            ORDER BY id DESC LIMIT 1
        """, (pid,))
        run_row = cur.fetchone()
        if run_row:
            audit.active_run = True
            audit.active_run_status = run_row["status"]

        # 6. 高风险检测
        audit.is_high_risk = self._detect_high_risk(pname, audit.workspace_path)

        # 7. 生成决策
        audit.decision, audit.decision_reason = self._compute_decision(audit)

        return audit

    def _detect_high_risk(self, project_name: str, workspace_path: str) -> bool:
        """检测高风险项目"""
        name_lower = project_name.lower()
        for kw in _HIGH_RISK_KEYWORDS:
            if kw.lower() in name_lower:
                return True
        return False

    def _compute_decision(self, audit: ProjectAudit) -> Tuple[str, str]:
        """根据审计结果计算决策

        V1.8C-R 决策优先级（从高到低）：
        1. ALREADY_RUNNING     - 已有活跃 run
        2. BLOCK_UNSAFE        - 真正不安全
        3. REQUEST_APPROVAL    - 高风险项目（在执行前拦截，优先于任务调度）
        4. EXECUTE_READY_TASKS - 有可执行任务
        5. PLAN_EXISTING_TASKS - 有 needs_planning 任务
        6. BIND_WORKSPACE      - 缺少工作区
        7. WAIT_DEPENDENCIES   - 有依赖阻塞
        8. PROJECT_COMPLETED   - 全部完成
        9. GENERATE_TASKS      - 默认：需要生成任务
        """
        pid = audit.project_id

        # 1. 已有活跃 run
        if audit.active_run:
            return (Decision.ALREADY_RUNNING.value,
                    f"项目已有活跃执行器运行 (状态: {audit.active_run_status})")

        # 2. 真正不安全
        unsafe_reason = self._check_unsafe(audit)
        if unsafe_reason:
            return (Decision.BLOCK_UNSAFE.value, unsafe_reason)

        # 3. V1.8C-R: 高风险项目门禁必须优先于 EXECUTE_READY_TASKS
        # 即使有 runnable 任务，高风险项目也必须先通过审批
        if audit.is_high_risk and audit.execution_enabled:
            if self._has_valid_execution_approval(pid, audit.runnable_task_ids):
                # Has valid execution approval covering all runnable tasks
                logger.info(
                    f"Project {pid}: high risk but has valid execution approval "
                    f"covering tasks {audit.runnable_task_ids}, "
                    f"skipping REQUEST_APPROVAL"
                )
                # Fall through to EXECUTE_READY_TASKS check below
            else:
                return (Decision.REQUEST_APPROVAL.value,
                        "此项目被识别为高风险项目，需要人工审批后才能规划或执行。"
                        f"当前可执行任务: {audit.runnable_task_ids}")

        # 4. 有可执行任务
        if audit.runnable_count > 0:
            return (Decision.EXECUTE_READY_TASKS.value,
                    f"有 {audit.runnable_count} 个任务已就绪，可以开始执行")

        # 5. 有 needs_planning 任务
        if audit.needs_planning_count > 0:
            return (Decision.PLAN_EXISTING_TASKS.value,
                    f"{audit.needs_planning_count} 个任务尚未完成工程规划")

        # 6. 缺少工作区
        if audit.execution_enabled and (not audit.workspace_path or not audit.workspace_exists):
            return (Decision.BIND_WORKSPACE.value,
                    "此项目尚未绑定代码工作区，请先配置工作区路径")

        # 7. 有依赖阻塞（pending > 0 但 runnable = 0）
        if audit.pending_count > 0 and audit.runnable_count == 0:
            return (Decision.WAIT_DEPENDENCIES.value,
                    f"{audit.pending_count} 个待处理任务存在依赖阻塞")

        # 8. 全部 completed
        if audit.pending_count == 0 and audit.completed_count > 0:
            return (Decision.PROJECT_COMPLETED.value,
                    f"所有 {audit.completed_count} 个任务已完成，可以创建新需求继续迭代")

        # 9. 默认：需要生成任务
        return (Decision.GENERATE_TASKS.value,
                "当前项目没有开发任务，可以根据项目目标生成新的开发任务")

    def _has_valid_execution_approval(self, project_id: int,
                                       requested_task_ids: list = None) -> bool:
        """V1.8C-R: Check if project has a valid execution approval
        that covers the requested task IDs.

        A valid approval must satisfy ALL conditions:
          - project_id matches
          - status = 'approved'
          - not expired (expired_at > now)
          - not consumed (consumed_at IS NULL)
          - single_use = 1
          - risk_acknowledged = true (in risk_summary_json)
          - ALL requested_task_ids are in allowed_task_ids_json
          - No extra tasks beyond allowed_task_ids

        Args:
            project_id: Project ID
            requested_task_ids: Task IDs being requested for execution.
                If None/empty, ANY valid approval passes (backward compat).

        Returns:
            True if a valid scoped approval exists
        """
        try:
            conn = self._get_conn()
            try:
                from datetime import datetime
                cur = conn.cursor()
                now = datetime.now()

                # Find the latest valid approval
                cur.execute("""
                    SELECT allowed_task_ids_json, risk_summary_json
                    FROM execution_approvals
                    WHERE project_id = ?
                    AND status = 'approved'
                    AND (expired_at IS NULL OR expired_at > ?)
                    AND consumed_at IS NULL
                    AND single_use = 1
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (project_id, now.isoformat()))

                row = cur.fetchone()
                if not row:
                    return False

                # Check risk_acknowledged
                try:
                    risk = json.loads(row["risk_summary_json"] or "{}")
                    if not risk.get("risk_confirmed", False):
                        logger.warning(
                            f"Project {project_id}: approval exists but "
                            f"risk not acknowledged"
                        )
                        return False
                except (json.JSONDecodeError, TypeError):
                    return False

                # Check task ID scope
                if requested_task_ids:
                    try:
                        allowed = json.loads(
                            row["allowed_task_ids_json"] or "[]"
                        )
                        if not allowed:
                            # Empty allowed_task_ids → reject
                            logger.warning(
                                f"Project {project_id}: approval has empty "
                                f"allowed_task_ids"
                            )
                            return False

                        allowed_set = set(allowed)
                        requested_set = set(requested_task_ids)

                        # All requested tasks must be in allowed
                        if not requested_set.issubset(allowed_set):
                            extra = requested_set - allowed_set
                            logger.warning(
                                f"Project {project_id}: approval does not cover "
                                f"tasks {extra}. Allowed: {allowed}, "
                                f"Requested: {requested_task_ids}"
                            )
                            return False
                    except (json.JSONDecodeError, TypeError):
                        return False

                return True
            finally:
                conn.close()
        except Exception as e:
            # If table doesn't exist or query fails, fallback: no approval
            logger.debug(f"Execution approval check failed (may be normal): {e}")
            return False

    def _check_unsafe(self, audit: ProjectAudit) -> Optional[str]:
        """检查是否真正不安全（返回原因或 None）"""
        # 检查系统目录
        if audit.workspace_path:
            ws_lower = audit.workspace_path.lower()
            system_prefixes = [
                r"c:\windows", r"c:\program files", r"c:\program files (x86)",
                r"c:\programdata", r"c:\system",
            ]
            for sp in system_prefixes:
                if ws_lower.startswith(sp):
                    return f"工作区位于系统目录: {audit.workspace_path}"

            # 检查 AI 工厂自身目录
            factory_root = str(Path(__file__).resolve().parent.parent.parent.parent).lower()
            if ws_lower.startswith(factory_root):
                return f"禁止操作 AI 工厂自身目录: {audit.workspace_path}"

            # 检查路径穿越
            try:
                resolved = str(Path(audit.workspace_path).resolve())
                if ".." in audit.workspace_path and resolved != audit.workspace_path:
                    return f"检测到路径穿越: {audit.workspace_path} → {resolved}"
            except Exception:
                return f"无法解析工作区路径: {audit.workspace_path}"

        # 检查工作区是否存在但 Git 损坏
        if audit.workspace_exists and not audit.git_valid:
            return f"工作区不是有效的 Git 仓库: {audit.workspace_path}"

        # 检查工作区脏
        if audit.workspace_exists and audit.git_valid and not audit.git_clean:
            return f"工作区有未提交的更改: {audit.workspace_path}"

        return None

    # ── 单项目决策 API ──

    def decide(self, project_id: int) -> Dict[str, Any]:
        """为单个项目生成启动决策

        Returns:
            dict with decision info for API response
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 获取项目信息
            cur.execute("SELECT id, name, status FROM projects WHERE id = ?", (project_id,))
            proj = cur.fetchone()
            if not proj:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "decision": Decision.BLOCK_UNSAFE.value,
                    "can_execute": False,
                    "can_plan": False,
                    "can_generate_tasks": False,
                    "requires_workspace": False,
                    "requires_approval": False,
                    "summary": f"项目 #{project_id} 不存在",
                    "details": {},
                    "error": "PROJECT_NOT_FOUND",
                }

            # 执行审计
            audit = self._audit_single_project(cur, proj)
        finally:
            conn.close()

        # 构建响应
        decision = audit.decision  # 现在是纯字符串

        # V1.8C-R: Include approval scope info in response
        allowed_task_ids = []
        if audit.is_high_risk and decision == Decision.EXECUTE_READY_TASKS.value:
            # Project passed the approval gate → extract allowed task IDs
            allowed_task_ids = audit.runnable_task_ids

        return {
            "ok": True,
            "project_id": project_id,
            "project_name": audit.project_name,
            "decision": decision,
            "can_execute": decision == Decision.EXECUTE_READY_TASKS.value,
            "can_plan": decision == Decision.PLAN_EXISTING_TASKS.value,
            "can_generate_tasks": decision == Decision.GENERATE_TASKS.value,
            "requires_workspace": decision == Decision.BIND_WORKSPACE.value,
            "requires_approval": decision == Decision.REQUEST_APPROVAL.value,
            "summary": audit.decision_reason,
            "details": {
                "pending": audit.pending_count,
                "ready": audit.ready_count,
                "needs_planning": audit.needs_planning_count,
                "runnable": audit.runnable_count,
                "runnable_task_ids": audit.runnable_task_ids,
                "allowed_task_ids": allowed_task_ids,
                "completed": audit.completed_count,
                "blocked": audit.blocked_count,
                "execution_enabled": audit.execution_enabled,
                "workspace_path": audit.workspace_path,
                "workspace_exists": audit.workspace_exists,
                "git_valid": audit.git_valid,
                "git_clean": audit.git_clean,
                "active_run": audit.active_run,
                "active_run_status": audit.active_run_status,
                "is_high_risk": audit.is_high_risk,
            },
        }

    def get_dependency_details(self, project_id: int) -> List[Dict[str, Any]]:
        """获取依赖阻塞详情（用于 WAIT_DEPENDENCIES 决策）"""
        from .task_scheduler import TaskScheduler
        scheduler = TaskScheduler(self.db_path)
        queue = scheduler.get_queue_status(project_id)

        blocked = queue.get("blocked_tasks", [])
        dep_blocked = []
        for bt in blocked:
            reasons = bt.get("blocked_reasons", [])
            dep_reasons = [r for r in reasons if "依赖" in r]
            if dep_reasons:
                dep_blocked.append({
                    "task_id": bt["id"],
                    "title": bt["title"],
                    "blocked_reasons": dep_reasons,
                    "readiness_status": bt.get("readiness_status", "unknown"),
                })

        return dep_blocked


# 全局单例
_decision_service: Optional[StartDecisionService] = None


def get_start_decision_service(db_path: str = None) -> StartDecisionService:
    """获取全局 StartDecisionService 单例"""
    global _decision_service
    if _decision_service is None:
        if db_path is None:
            db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")
        _decision_service = StartDecisionService(db_path)
    return _decision_service
