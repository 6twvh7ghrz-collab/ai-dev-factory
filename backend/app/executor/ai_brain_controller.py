"""AIBrainController - 自然语言指令控制器 V1.2.1

V1: 只做预览，不执行任何实际操作。
V1.1: 新增只读指令真实接入（show_status, diagnose_blocker）。
V1.2: 安全接入开始开发（start_development）。
V1.2.1: 移除名称硬编码白名单，改用 project_execution_configs 数据库配置。

本轮禁止：
- generate_plan / pause_executor / resume_executor / stop_executor
"""
import sqlite3
from typing import Optional, Dict, Any, List
from .command_normalizer import CommandNormalizer, CommandIntent, NormalizedCommand

# V1.1 只读白名单
_READONLY_INTENTS = {
    CommandIntent.SHOW_STATUS,
    CommandIntent.DIAGNOSE_BLOCKER,
}

# V1.2 已启用的写意图
_ENABLED_WRITE_INTENTS = {
    CommandIntent.START_DEVELOPMENT,
}


class AIBrainController:
    """自然语言指令控制器 V1.2

    支持：
    - 预览（preview）
    - 只读指令执行（show_status, diagnose_blocker）
    - 安全启动开发（start_development，需确认令牌 + 项目白名单）
    """

    def __init__(self, normalizer: CommandNormalizer, db_path: str = None):
        self.normalizer = normalizer
        self.db_path = db_path

    def _get_db_path(self) -> str:
        if self.db_path:
            return self.db_path
        from pathlib import Path
        return str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")

    def preview(
        self,
        user_input: str,
        project_id: int,
    ) -> dict:
        """预览用户指令，不执行任何操作

        Args:
            user_input: 用户自然语言输入
            project_id: 项目 ID

        Returns:
            dict: 预览结果，executed 永远为 False
        """
        # 基本校验
        if not user_input or not user_input.strip():
            return {
                "ok": False,
                "project_id": project_id,
                "intent": "unknown",
                "confidence": 0.0,
                "source": "fallback",
                "requires_confirmation": False,
                "action": "preview_only",
                "message": "输入为空，无法识别指令",
                "executed": False,
                "error": "EMPTY_INPUT",
            }

        if len(user_input) > 1000:
            return {
                "ok": False,
                "project_id": project_id,
                "intent": "unknown",
                "confidence": 0.0,
                "source": "fallback",
                "requires_confirmation": False,
                "action": "preview_only",
                "message": f"输入过长（{len(user_input)}字符），最大1000字符",
                "executed": False,
                "error": "TEXT_TOO_LONG",
            }

        # 标准化
        cmd: NormalizedCommand = self.normalizer.normalize(user_input)

        return {
            "ok": True,
            "project_id": project_id,
            "intent": cmd.intent.value,
            "confidence": cmd.confidence,
            "source": cmd.source,
            "requires_confirmation": cmd.requires_confirmation,
            "action": "preview_only",
            "message": cmd.message,
            "executed": False,
        }

    # ═══════════════════════════════════════════════════════════
    # V1.1 只读执行
    # ═══════════════════════════════════════════════════════════

    def execute_readonly(
        self,
        user_input: str,
        project_id: int,
        confirmed_intent: str,
    ) -> dict:
        """执行只读指令（show_status / diagnose_blocker）

        执行前必须重新解析并验证：
        1. 解析得到的 intent == confirmed_intent
        2. intent 属于只读白名单
        3. project_id 存在

        禁止：启动 Worker、调用 DeepSeek、写入数据库。
        """
        # 1. 重新解析
        cmd: NormalizedCommand = self.normalizer.normalize(user_input)
        parsed_intent = cmd.intent.value

        # 2. 校验 intent 一致性
        if parsed_intent != confirmed_intent:
            return {
                "ok": False,
                "code": "INTENT_MISMATCH",
                "project_id": project_id,
                "parsed_intent": parsed_intent,
                "confirmed_intent": confirmed_intent,
                "message": f"重新解析意图({parsed_intent})与确认意图({confirmed_intent})不一致，拒绝执行",
                "executed": False,
            }

        # 3. 校验只读白名单
        if cmd.intent not in _READONLY_INTENTS:
            return {
                "ok": False,
                "code": "READONLY_INTENT_REQUIRED",
                "project_id": project_id,
                "intent": parsed_intent,
                "message": f"意图 '{parsed_intent}' 不在只读白名单中，当前版本仅支持 show_status 和 diagnose_blocker",
                "executed": False,
            }

        # 4. 校验项目存在
        db_path = self._get_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, name, status FROM projects WHERE id = ?", (project_id,))
        proj = cur.fetchone()
        if not proj:
            conn.close()
            return {
                "ok": False,
                "code": "PROJECT_NOT_FOUND",
                "project_id": project_id,
                "message": f"项目 #{project_id} 不存在",
                "executed": False,
            }

        # 5. 执行只读操作
        if cmd.intent == CommandIntent.SHOW_STATUS:
            result = self._execute_show_status(conn, proj, project_id)
        elif cmd.intent == CommandIntent.DIAGNOSE_BLOCKER:
            result = self._execute_diagnose_blocker(conn, project_id)
        else:
            conn.close()
            return {
                "ok": False,
                "code": "READONLY_INTENT_REQUIRED",
                "project_id": project_id,
                "message": f"不支持的意图: {parsed_intent}",
                "executed": False,
            }

        conn.close()
        return result

    def _execute_show_status(self, conn, proj, project_id: int) -> dict:
        """执行 show_status 只读查询。

        复用项目真实的 status 查询服务（RunStore, TaskScheduler），
        只允许 SELECT 查询。
        """
        from .run_store import RunStore
        from .task_scheduler import TaskScheduler
        from .resource_lock_manager import ResourceLockManager

        db_path = self._get_db_path()
        store = RunStore(db_path)
        scheduler = TaskScheduler(db_path)
        lock_mgr = ResourceLockManager(db_path)

        # 项目名称
        project_name = proj["name"]

        # 活跃 run
        active_run = store.get_active_run(project_id)
        run_status = active_run["status"] if active_run else "idle"

        # 当前任务
        current_task_id = active_run.get("current_task_id") if active_run else None
        current_task = None
        if current_task_id:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, status FROM development_tasks WHERE id = ?",
                (current_task_id,)
            )
            task_row = cur.fetchone()
            if task_row:
                current_task = {
                    "task_id": task_row["id"],
                    "title": task_row["title"],
                    "status": task_row["status"],
                }

        # 队列状态
        queue = scheduler.get_queue_status(project_id)

        # 活跃 leases
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active' AND expires_at > datetime('now')")
        active_leases = cur.fetchone()["cnt"]

        # 活跃资源锁
        active_locks = lock_mgr.get_active_locks(project_id=project_id)

        # Worker 数量（本项目单 Worker 模式）
        worker_count = 1 if active_run and active_run["status"] not in ("completed", "blocked", "failed", "idle") else 0

        # last_error
        last_error = active_run.get("last_error") if active_run else None

        return {
            "ok": True,
            "project_id": project_id,
            "intent": "show_status",
            "executed": True,
            "action": "readonly_query",
            "data": {
                "project_id": project_id,
                "project_name": project_name,
                "run_status": run_status,
                "current_task": current_task,
                "worker_count": worker_count,
                "pending_count": queue.get("pending_count", 0),
                "ready_count": queue.get("runnable_count", 0),
                "needs_planning_count": self._count_by_readiness(conn, project_id, "needs_planning"),
                "completed_count": queue.get("completed_count", 0),
                "blocked_count": queue.get("blocked_count", 0),
                "total_count": queue.get("total_count", 0),
                "active_leases": active_leases,
                "active_resource_locks": len(active_locks),
                "last_error": last_error,
            },
        }

    def _execute_diagnose_blocker(self, conn, project_id: int) -> dict:
        """执行 diagnose_blocker 只读查询。

        复用 TaskScheduler.get_queue_status() 和真实依赖/Lease/资源锁检查。
        不得自动修改 readiness_status、补 files_to_modify、清除 Lease、创建任务或启动执行器。
        """
        from .task_scheduler import TaskScheduler

        db_path = self._get_db_path()
        scheduler = TaskScheduler(db_path)

        queue = scheduler.get_queue_status(project_id)
        blocked_tasks = queue.get("blocked_tasks", [])
        pending_count = queue.get("pending_count", 0)
        runnable_count = queue.get("runnable_count", 0)

        # 分类统计
        categories = {
            "needs_planning": 0,
            "dependency_incomplete": 0,
            "active_lease": 0,
            "missing_files": 0,
            "manual_approval": 0,
            "missing_prompt": 0,
            "missing_test_steps": 0,
            "missing_acceptance_criteria": 0,
            "missing_implementation_steps": 0,
        }

        task_details = []
        for bt in blocked_tasks:
            reasons = bt.get("blocked_reasons", [])
            for reason in reasons:
                if "尚未完成工程规划" in reason or "任务准备状态异常" in reason:
                    categories["needs_planning"] += 1
                elif "依赖未完成" in reason or "依赖不存在" in reason:
                    categories["dependency_incomplete"] += 1
                elif "已有活跃lease" in reason:
                    categories["active_lease"] += 1
                elif "缺少修改文件列表" in reason:
                    categories["missing_files"] += 1
                elif "需要人工审批" in reason:
                    categories["manual_approval"] += 1
                elif "缺少AI提示词" in reason:
                    categories["missing_prompt"] += 1
                elif "缺少测试方案" in reason:
                    categories["missing_test_steps"] += 1
                elif "缺少验收标准" in reason:
                    categories["missing_acceptance_criteria"] += 1
                elif "缺少实现步骤" in reason:
                    categories["missing_implementation_steps"] += 1

            task_details.append({
                "task_id": bt["id"],
                "title": bt["title"],
                "reason": reasons[0] if reasons else "unknown",
                "all_reasons": reasons,
                "readiness_status": bt.get("readiness_status", "unknown"),
            })

        # 确定状态
        if not blocked_tasks and pending_count == 0:
            status = "clear"
            summary = "所有任务已完成或可执行"
        elif not blocked_tasks:
            status = "ready"
            summary = f"{runnable_count}个任务可执行，无阻塞"
        else:
            status = "blocked"

            # 构建汇总
            parts = []
            if categories["needs_planning"] > 0:
                parts.append(f"{categories['needs_planning']}个任务尚未完成工程规划")
            if categories["dependency_incomplete"] > 0:
                parts.append(f"{categories['dependency_incomplete']}个依赖未完成")
            if categories["active_lease"] > 0:
                parts.append(f"{categories['active_lease']}个存在Lease")
            if categories["missing_files"] > 0:
                parts.append(f"{categories['missing_files']}个缺少工程文件")
            if categories["manual_approval"] > 0:
                parts.append(f"{categories['manual_approval']}个需人工审批")
            if categories["missing_prompt"] > 0:
                parts.append(f"{categories['missing_prompt']}个缺少AI提示词")

            if parts:
                summary = "；".join(parts)
            else:
                summary = f"{len(blocked_tasks)}个任务被阻塞"

        # 限制返回前20个任务详情
        return {
            "ok": True,
            "project_id": project_id,
            "intent": "diagnose_blocker",
            "executed": True,
            "action": "readonly_query",
            "data": {
                "status": status,
                "summary": summary,
                "categories": categories,
                "total_pending": pending_count,
                "runnable_count": runnable_count,
                "blocked_count": len(blocked_tasks),
                "tasks": task_details[:20],
            },
        }

    def _count_by_readiness(self, conn, project_id: int, readiness: str) -> int:
        """按 readiness_status 统计任务数"""
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as cnt FROM development_tasks "
            "WHERE project_id = ? AND status = 'pending' "
            "AND COALESCE(readiness_status, 'draft') = ?",
            (project_id, readiness)
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0

    # ═══════════════════════════════════════════════════════════
    # V1.2 安全写入执行
    # ═══════════════════════════════════════════════════════════

    def execute_write(
        self,
        user_input: str,
        project_id: int,
        confirmed_intent: str,
        confirmation_token: str,
    ) -> dict:
        """安全执行写指令（当前仅 start_development）。

        执行前必须通过全部安全检查（V1.2.1：使用 ProjectExecutionGuard 统一校验）：
        1. 重新解析 text，验证 intent == confirmed_intent
        2. intent 属于已启用的写意图白名单
        3. confirmation_token 有效且未使用
        4. project_id 存在
        5. ProjectExecutionGuard 统一验证（配置存在/execution_enabled/工作区/Git等）
        6. 真实调度器 runnable_tasks > 0
        7. 不存在活跃 run
        8. 不存在冲突 Lease 或资源锁
        """
        # 1. 重新解析
        cmd: NormalizedCommand = self.normalizer.normalize(user_input)
        parsed_intent = cmd.intent.value

        # 2. 校验 intent 一致性
        if parsed_intent != confirmed_intent:
            return {
                "ok": False,
                "code": "INTENT_MISMATCH",
                "project_id": project_id,
                "parsed_intent": parsed_intent,
                "confirmed_intent": confirmed_intent,
                "message": f"重新解析意图({parsed_intent})与确认意图({confirmed_intent})不一致，拒绝执行",
                "executed": False,
            }

        # 3. 校验已启用的写意图白名单
        if cmd.intent not in _ENABLED_WRITE_INTENTS:
            return {
                "ok": False,
                "code": "WRITE_INTENT_NOT_ENABLED",
                "project_id": project_id,
                "intent": parsed_intent,
                "message": f"意图 '{parsed_intent}' 尚未开放执行。当前版本仅支持 start_development",
                "executed": False,
            }

        # 4. 校验确认令牌
        from .confirmation_token import get_token_manager
        token_mgr = get_token_manager()
        token_result = token_mgr.validate_and_consume(
            token=confirmation_token,
            project_id=project_id,
            intent=confirmed_intent,
            text=user_input,
        )
        if not token_result["valid"]:
            return {
                "ok": False,
                "code": token_result["code"],
                "project_id": project_id,
                "message": token_result["message"],
                "executed": False,
            }

        # 5. 校验项目存在
        db_path = self._get_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, name, status FROM projects WHERE id = ?", (project_id,))
        proj = cur.fetchone()
        if not proj:
            conn.close()
            return {
                "ok": False,
                "code": "PROJECT_NOT_FOUND",
                "project_id": project_id,
                "message": f"项目 #{project_id} 不存在",
                "executed": False,
            }
        project_name = proj["name"]

        # 6. ProjectExecutionGuard 统一校验（替代名称硬编码白名单）
        from .project_execution_guard import get_project_execution_guard
        guard = get_project_execution_guard(db_path)
        allowed, reason, guard_detail = guard.validate_project(project_id)
        if not allowed:
            conn.close()
            return {
                "ok": False,
                "code": guard_detail["code"] if guard_detail else "EXECUTION_NOT_ALLOWED",
                "project_id": project_id,
                "message": guard_detail["message"] if guard_detail else reason,
                "executed": False,
            }
        workspace_path = guard_detail["workspace_path"]

        # 7. 不存在活跃 run（在 runnable 检查之前，确保 ALREADY_RUNNING 优先返回）
        from .run_store import RunStore
        store = RunStore(db_path)
        active_run = store.get_active_run(project_id)
        if active_run:
            conn.close()
            return {
                "ok": True,
                "code": "ALREADY_RUNNING",
                "project_id": project_id,
                "message": f"项目已有活跃 run: {active_run['status']}",
                "executed": False,
                "run_id": active_run["run_id"],
            }

        # 8. 真实调度器 runnable_tasks > 0
        from .task_scheduler import TaskScheduler
        scheduler = TaskScheduler(db_path)
        runnable = scheduler.find_runnable_tasks(project_id)
        if not runnable:
            conn.close()
            return {
                "ok": False,
                "code": "NO_RUNNABLE_TASKS",
                "project_id": project_id,
                "message": f"项目 '{project_name}' 当前没有可执行任务，无法启动自动开发",
                "executed": False,
            }

        # 9. 不存在冲突 Lease
        cur.execute(
            "SELECT COUNT(*) as cnt FROM task_leases "
            "WHERE status='active' AND expires_at > datetime('now')"
        )
        active_lease_count = cur.fetchone()["cnt"]
        if active_lease_count > 0:
            conn.close()
            return {
                "ok": False,
                "code": "ACTIVE_LEASES_EXIST",
                "project_id": project_id,
                "message": f"存在 {active_lease_count} 个活跃任务租约，请等待清理后再启动",
                "executed": False,
            }

        # 10. 不存在冲突资源锁
        from .resource_lock_manager import ResourceLockManager
        lock_mgr = ResourceLockManager(db_path)
        active_locks = lock_mgr.get_active_locks(project_id=project_id)
        if active_locks:
            conn.close()
            return {
                "ok": False,
                "code": "ACTIVE_RESOURCE_LOCKS_EXIST",
                "project_id": project_id,
                "message": f"存在 {len(active_locks)} 个活跃资源锁，请等待清理后再启动",
                "executed": False,
            }

        # 11. 验证 readiness_status
        not_ready_count = self._count_by_readiness(conn, project_id, "needs_planning")
        if not_ready_count > 0:
            conn.close()
            return {
                "ok": False,
                "code": "TASKS_NEED_PLANNING",
                "project_id": project_id,
                "message": f"项目有 {not_ready_count} 个任务尚未完成工程规划，无法自动执行",
                "executed": False,
            }

        conn.close()

        # 12. 所有检查通过，复用现有启动链路
        return self._execute_start_development(
            project_id=project_id,
            project_name=project_name,
            workspace_path=workspace_path,
            runnable_count=len(runnable),
            first_task_id=runnable[0].id,
        )

    def _execute_start_development(
        self,
        project_id: int,
        project_name: str,
        workspace_path: str,
        runnable_count: int,
        first_task_id: int,
    ) -> dict:
        """复用现有启动链路执行 start_development

        调用 LoopController.start（复用现有正式启动服务），
        不复制第二套启动实现。

        Returns:
            dict with ok, code, executed, run_id, task_id, message
        """
        from .loop_controller import LoopController
        from .run_store import RunStore
        from pathlib import Path

        db_path = self._get_db_path()

        try:
            # 创建 LoopController 并启动
            controller = LoopController(db_path, repo_path=workspace_path)
            result = controller.start(project_id, mode="auto_until_blocked")

            if not result.get("success"):
                # 启动失败
                error_msg = result.get("error", result.get("message", "启动失败"))
                return {
                    "ok": False,
                    "code": "START_FAILED",
                    "project_id": project_id,
                    "message": f"启动失败: {error_msg}",
                    "executed": False,
                }

            if result.get("already_running"):
                # 已有活跃 run（幂等保护触发）
                run = result.get("run", {})
                return {
                    "ok": True,
                    "code": "ALREADY_RUNNING",
                    "project_id": project_id,
                    "message": result.get("message", "已有活跃 run"),
                    "executed": False,
                    "run_id": run.get("run_id"),
                }

            # 成功创建 run
            run = result.get("run", {})
            run_id = run.get("run_id")

            return {
                "ok": True,
                "code": "STARTED",
                "project_id": project_id,
                "message": f"自动开发已启动（项目: {project_name}，可执行任务: {runnable_count}）",
                "executed": True,
                "run_id": run_id,
                "task_id": first_task_id,
            }

        except Exception as e:
            return {
                "ok": False,
                "code": "START_EXCEPTION",
                "project_id": project_id,
                "message": f"启动异常: {str(e)[:500]}",
                "executed": False,
            }
