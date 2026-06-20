"""LoopController - 单Worker自动循环控制器

负责：
  start   - 创建 starting run 并启动循环
  pause   - 设置暂停请求
  resume  - 恢复暂停的 run
  stop    - 设置停止请求
  run_loop - 主循环：scanning → claiming → executing → testing/repairing → ...

循环流程：
  创建 starting run
  → scanning (扫描可执行任务)
  → 查找可执行任务
  → claiming (原子领取任务)
  → executing (调用 TaskWorker)
  → testing/repairing 状态同步
  → 写回任务和 run 统计
  → 继续 scanning
  → 直到完成、阻塞、暂停或预算耗尽

队列终止判定：
  - 全部完成: status=completed, finish_reason=all_completed
  - 有阻塞且无可执行: status=blocked, finish_reason=blocked_no_runnable_tasks
  - 没有任务: status=completed, finish_reason=no_tasks
"""
import json
import time
import threading
import logging
from typing import Optional, Dict, Any, List, Callable
from pathlib import Path

from .run_store import RunStore
from .task_scheduler import TaskScheduler
from .budget_guard import BudgetGuard
from .recovery_manager import RecoveryManager
from .task_worker import TaskWorker
from .safety_guard import SafetyGuard
from .test_runner import TestRunner
from .git_manager import GitManager
from .execution_approval_service import ExecutionApprovalService

logger = logging.getLogger(__name__)


class LoopController:
    """单 Worker 自动循环控制器"""

    def __init__(self, db_path: str, repo_path: str = None,
                 budget_overrides: Dict[str, Any] = None):
        self.db_path = db_path
        self.repo_path = repo_path
        self.store = RunStore(db_path)
        self.scheduler = TaskScheduler(db_path)
        self.budget = BudgetGuard(budget_overrides)
        self.recovery = RecoveryManager(db_path, repo_path)

        # 运行时状态
        self._loop_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._current_run_id: Optional[str] = None
        self._run_start_time: float = 0.0
        self._consecutive_failures: int = 0

        # V1.8C-R: Approval tracking (consumed once per run)
        self._approval_consumed: bool = False
        self._approval_task_scope: set = None  # cached allowed_task_ids after consumption
        self._approval_service: Optional[ExecutionApprovalService] = None

        # 回调（用于测试/日志）
        self._on_state_change: Optional[Callable] = None
        self._on_task_done: Optional[Callable] = None

    @property
    def current_run_id(self) -> Optional[str]:
        return self._current_run_id

    def set_callbacks(self, on_state_change: Callable = None,
                      on_task_done: Callable = None):
        """设置状态变更和任务完成回调"""
        self._on_state_change = on_state_change
        self._on_task_done = on_task_done

    def _notify_state(self, state: str, detail: str = ""):
        if self._on_state_change:
            try:
                self._on_state_change(state, detail)
            except Exception:
                pass

    def _notify_task_done(self, task_id: int, status: str, detail: str = ""):
        if self._on_task_done:
            try:
                self._on_task_done(task_id, status, detail)
            except Exception:
                pass

    # ── 控制 API ──

    def start(self, project_id: int,
              mode: str = "auto_until_blocked") -> Dict[str, Any]:
        """
        启动自动循环。

        - 检查是否有活跃 run
        - 无活跃 run：原子创建 starting 并启动
        - 已有活跃 run：返回 already_running=true
        - paused：提示使用 resume
        - 同项目不能启动第二线程
        """
        # 检查是否已有活跃 run
        active = self.store.get_active_run(project_id)
        if active:
            if active["status"] == "paused":
                return {
                    "success": True,
                    "already_running": True,
                    "message": "已有暂停的 run，请使用 resume 恢复",
                    "run": active,
                }
            return {
                "success": True,
                "already_running": True,
                "message": f"已有活跃 run: {active['status']}",
                "run": active,
            }

        # 原子创建 starting run
        result = self.store.create_starting_run(project_id, mode)
        if not result["success"]:
            return {
                "success": False,
                "error": result["error"],
            }

        run = result["run"]
        self._current_run_id = run["run_id"]

        # V1.8C-R: Reset approval tracking for this run
        self._approval_consumed = False
        self._approval_task_scope = None

        # 启动循环线程
        self._stop_event.clear()
        self._pause_event.clear()
        self._run_start_time = time.time()
        self._consecutive_failures = 0

        self._loop_thread = threading.Thread(
            target=self._run_loop,
            args=(run, project_id),
            daemon=True
        )
        self._loop_thread.start()

        return {
            "success": True,
            "already_running": False,
            "message": "循环已启动",
            "run": run,
        }

    def pause(self) -> Dict[str, Any]:
        """
        暂停循环。
        - 设置暂停请求
        - 不再领取新任务
        - 当前任务到达安全点后暂停
        """
        if not self._current_run_id:
            return {"success": False, "error": "no active run"}

        run = self.store.get_run_by_id(self._current_run_id)
        if not run:
            return {"success": False, "error": "run not found"}

        if run["status"] == "paused":
            return {"success": True, "message": "already paused", "run": run}

        if run["status"] in ("completed", "blocked", "failed"):
            return {"success": False, "error": f"run already in terminal state: {run['status']}"}

        # 设置暂停标志
        self._pause_event.set()
        self.store.set_pause_reason(self._current_run_id, "user_requested")

        return {
            "success": True,
            "message": "pause requested",
            "run_id": self._current_run_id,
        }

    def resume(self) -> Dict[str, Any]:
        """
        恢复暂停的循环。
        - 原子取得 paused run 控制权
        - 更新 worker_id 和 heartbeat
        - 不重复执行 completed 任务
        """
        if not self._current_run_id:
            return {"success": False, "error": "no run to resume"}

        run = self.store.get_run_by_id(self._current_run_id)
        if not run:
            return {"success": False, "error": "run not found"}

        if run["status"] != "paused":
            return {"success": False,
                    "error": f"run is not paused, current status: {run['status']}"}

        # 更新状态为 scanning
        self.store.update_status(self._current_run_id, "scanning",
                                 pause_reason=None)
        self.store.update_heartbeat(self._current_run_id)

        # 清除暂停标志
        self._pause_event.clear()

        return {
            "success": True,
            "message": "resumed",
            "run_id": self._current_run_id,
        }

    def stop(self) -> Dict[str, Any]:
        """
        安全停止循环。
        - 设置 stop_requested
        - 不再领取新任务
        - 不粗暴中断数据库事务
        """
        if not self._current_run_id:
            return {"success": False, "error": "no active run"}

        run = self.store.get_run_by_id(self._current_run_id)
        if not run:
            return {"success": False, "error": "run not found"}

        if run["status"] in ("completed", "blocked", "failed"):
            return {"success": True, "message": f"run already in terminal state: {run['status']}"}

        # 设置停止请求
        self.store.set_stop_requested(self._current_run_id)
        self.store.update_status(self._current_run_id, "stopping")
        self._stop_event.set()

        return {
            "success": True,
            "message": "stop requested",
            "run_id": self._current_run_id,
        }

    def get_status(self) -> Dict[str, Any]:
        """获取当前循环状态"""
        if not self._current_run_id:
            return {
                "running": False,
                "run": None,
                "is_paused": False,
                "message": "no active run",
            }

        run = self.store.get_run_by_id(self._current_run_id)
        if not run:
            return {
                "running": False,
                "run": None,
                "message": "run not found in database",
            }

        db_status = run.get("status", "")
        # is_paused 考虑两种来源：_pause_event 主动暂停 + DB 状态被动 paused (如 budget_exceeded)
        is_paused = self._pause_event.is_set() or db_status == "paused"

        return {
            "running": run["status"] not in ("completed", "blocked", "failed", "idle", "paused"),
            "run": run,
            "is_paused": is_paused,
            "is_stopping": self._stop_event.is_set(),
        }

    # ── 主循环 ──

    def _run_loop(self, run: Dict[str, Any], project_id: int):
        """主循环：持续扫描并执行任务直到阻塞/完成/暂停/预算耗尽"""
        run_id = run["run_id"]
        executor_run_id = run.get("id")  # executor_runs 表的主键 ID

        try:
            # 进入 scanning 阶段
            self.store.update_status(run_id, "scanning",
                                     current_step="scan_queue")
            self._notify_state("run_created", f"run_id={run_id}")

            while not self._stop_event.is_set():
                # 检查暂停
                if self._pause_event.is_set():
                    self.store.update_status(run_id, "paused",
                                             current_step="paused")
                    self._notify_state("pause_requested", f"run_id={run_id}")

                    # 等待恢复
                    while self._pause_event.is_set() and not self._stop_event.is_set():
                        time.sleep(1)
                        self.store.update_heartbeat(run_id)

                    if self._stop_event.is_set():
                        break

                    # 恢复
                    self.store.update_status(run_id, "scanning",
                                             pause_reason=None)
                    self.store.update_heartbeat(run_id)
                    self._notify_state("resume", f"run_id={run_id}")

                # 更新心跳
                self.store.update_heartbeat(run_id)
                self._notify_state("heartbeat", f"run_id={run_id}")

                # 检查预算
                budget_check = self.budget.check_run_budget(run, self._run_start_time)
                if not budget_check["ok"]:
                    self.store.update_status(run_id, "paused",
                                             pause_reason=f"budget_exceeded: {budget_check['reason']}")
                    self.store.finalize_run(run_id, "paused",
                                            finish_reason=f"budget_exceeded: {budget_check['reason']}")
                    self._notify_state("run_completed",
                                       f"budget_exceeded: {budget_check['reason']}")
                    return

                # scanning: 查找可执行任务
                self.store.update_status(run_id, "scanning",
                                         current_step="scan_queue")
                self._notify_state("scan_queue", f"run_id={run_id}")

                runnable = self.scheduler.find_runnable_tasks(project_id)

                # V1.8C-R: Filter runnable tasks by execution approval scope
                runnable = self._filter_by_approval_scope(project_id, runnable)

                if not runnable:
                    # 没有可执行任务 - 判断终止原因
                    self._handle_no_runnable(run_id, project_id)
                    return

                # 选择第一个可执行任务
                task = runnable[0]
                self._notify_state("task_selected",
                                   f"task_id={task.id}, title={task.title}")

                # claiming: 进入 claiming 状态（实际 lease 由 TaskWorker 内部创建）
                self.store.update_status(run_id, "claiming",
                                         current_step="claim_task")
                self._notify_state("task_claimed", f"task_id={task.id}")

                # executing: 执行任务
                self.store.update_status(run_id, "executing",
                                         current_step="execute_task",
                                         current_task_id=task.id)
                self.store.increment_counter(run_id, "tasks_total")
                self._notify_state("task_started", f"task_id={task.id}")

                # 解析执行命令
                exec_cmd = self._parse_command(task.implementation_steps)

                # 分离源文件和测试文件
                all_files = task.files_to_modify or []
                # 测试文件：以 test_ 开头 或 在 files_to_check 中
                files_to_check = task.files_to_check or []
                test_only_files = [f for f in all_files if f.startswith("test_")]
                test_only_files.extend([f for f in files_to_check if f.startswith("test_") and f not in test_only_files])
                test_only_files = list(set(test_only_files))

                # 解析测试命令
                # 优先使用实际测试文件构建 pytest 命令
                # test_steps 包含的是测试函数描述（如 "test_xxx: 描述"），不是可执行命令
                test_cmd = None
                if test_only_files:
                    test_cmd = ["pytest"] + test_only_files + ["-v"]
                elif task.test_steps:
                    test_cmd = self._parse_command(task.test_steps)

                # 调用 TaskWorker（传入 executor_run_id + 明确分离的 test_files）
                task_result = self._execute_task(task, exec_cmd, test_cmd, executor_run_id,
                                                 test_files=test_only_files)

                # V1.8C-R: If approval consumption failed inside TaskWorker,
                # stop the entire run immediately
                if task_result.get("block_reason") == "approval_consumption_failed":
                    logger.error(
                        f"V1.8C-R: Approval consumption failed inside TaskWorker "
                        f"for project={project_id}, task={task.id}: "
                        f"{task_result.get('error')}"
                    )
                    self.store.update_status(
                        run_id, "blocked",
                        last_error=(
                            f"Execution approval consumption failed: "
                            f"{task_result.get('error', 'unknown error')}"
                        )
                    )
                    self.store.finalize_run(
                        run_id, "blocked",
                        finish_reason="approval_consumption_failed",
                        last_error=task_result.get('error', '')
                    )
                    self._notify_state("run_blocked",
                                       f"approval_consumption_failed")
                    return

                # 处理结果
                self._handle_task_result(run_id, task, task_result)

                # 重新读取 run 以获取最新计数
                run = self.store.get_run_by_id(run_id)
                if not run:
                    return

                # 小延迟避免忙循环
                time.sleep(0.1)

            # stop_event 触发
            if self._stop_event.is_set():
                self.store.update_status(run_id, "completed",
                                         current_step="stopped")
                self.store.finalize_run(run_id, "completed",
                                        finish_reason="stopped_by_user")
                self._notify_state("run_completed", "stopped_by_user")

        except Exception as e:
            # 未预期错误
            self.store.update_status(run_id, "failed",
                                     last_error=str(e)[:1000])
            self.store.finalize_run(run_id, "failed",
                                    last_error=str(e)[:1000])
            self._notify_state("run_failed", str(e))

    # ── V1.8C-R: Approval integration ──

    def _get_approval_service(self) -> ExecutionApprovalService:
        """Lazy-init approval service."""
        if self._approval_service is None:
            self._approval_service = ExecutionApprovalService(self.db_path)
        return self._approval_service

    def _filter_by_approval_scope(self, project_id: int,
                                   runnable: list) -> list:
        """V1.8C-R: Filter runnable tasks by execution approval scope.

        If the project has a valid execution approval, only tasks within
        allowed_task_ids can be executed. Tasks outside the scope are silently
        excluded.

        After the approval is consumed in this run, uses the cached scope
        to ensure the same restrictions apply throughout the entire run.

        If no approval exists (or was already consumed), all runnable tasks
        pass through (backward compat).

        Args:
            project_id: Project ID
            runnable: List of SchedulableTask from scheduler

        Returns:
            Filtered list of SchedulableTask (only tasks within approval scope,
            or all if no approval required)
        """
        if not runnable:
            return runnable

        svc = self._get_approval_service()

        # V1.8C-R: Use cached scope if already consumed in this run
        if self._approval_task_scope is not None:
            allowed_ids = self._approval_task_scope
            if not allowed_ids:
                logger.warning(
                    f"V1.8C-R: Project {project_id} has cached empty scope. "
                    f"Blocking all {len(runnable)} tasks."
                )
                return []
            runnable_ids = {t.id for t in runnable}
            filtered = [t for t in runnable if t.id in allowed_ids]
            excluded = runnable_ids - allowed_ids
            if excluded:
                logger.info(
                    f"V1.8C-R: Cached approval scope for project {project_id}: "
                    f"excluded tasks {sorted(excluded)}, "
                    f"keeping {len(filtered)}/{len(runnable)} tasks "
                    f"(cached_scope={sorted(allowed_ids)})"
                )
            return filtered

        # Pre-consumption: check live approval
        approval = svc.get_valid_approval(project_id)

        if not approval:
            # No approval → no filtering needed (backward compat)
            return runnable

        allowed_ids = set(approval.get("allowed_task_ids", []))
        if not allowed_ids:
            # Empty allowed_ids → nothing can run
            logger.warning(
                f"V1.8C-R: Project {project_id} has valid approval but "
                f"empty allowed_task_ids. Blocking all {len(runnable)} tasks."
            )
            return []

        runnable_ids = {t.id for t in runnable}
        filtered = [t for t in runnable if t.id in allowed_ids]

        excluded = runnable_ids - allowed_ids
        if excluded:
            logger.info(
                f"V1.8C-R: Approval scope filter for project {project_id}: "
                f"excluded tasks {sorted(excluded)}, "
                f"keeping {len(filtered)}/{len(runnable)} tasks "
                f"(allowed={sorted(allowed_ids)})"
            )

        return filtered

    def _consume_approval_if_needed(self, project_id: int,
                                     executor_run_id: int,
                                     task_id: int) -> dict:
        """V1.8C-R: Atomically consume execution approval.

        Called AFTER run is created and task is selected but BEFORE
        TaskWorker execution begins. Only consumes once per run.

        Pre-conditions:
          - executor_run exists (executor_run_id is real DB id)
          - task_id is within approval scope (verified by _filter_by_approval_scope)
          - No prior consumption in this run

        Post-conditions on success:
          - approval status → 'consumed'
          - consumed_at set
          - consumed_by_run_id set
          - consumed_by_task_id set
          - self._approval_consumed = True

        Args:
            project_id: Project ID
            executor_run_id: executor_runs.id
            task_id: Task ID being claimed

        Returns:
            dict with ok=True if consumed or not needed, ok=False on failure
        """
        # Already consumed → skip
        if self._approval_consumed:
            return {"ok": True, "message": "Already consumed"}

        svc = self._get_approval_service()

        # Only consume if a valid approval exists
        if not svc.has_valid_approval(project_id, [task_id]):
            # No valid approval → no consumption needed (backward compat)
            self._approval_consumed = True  # Mark done to avoid re-checking
            return {"ok": True, "message": "No approval to consume"}

        # Atomic consumption
        result = svc.consume_approval(
            project_id=project_id,
            executor_run_id=executor_run_id,
            task_id=task_id,
        )

        if result.get("ok"):
            self._approval_consumed = True
            logger.info(
                f"V1.8C-R: Approval consumed for project={project_id}, "
                f"run={executor_run_id}, task={task_id}, "
                f"approval_id={result.get('approval_id')}"
            )

        return result

    def _handle_no_runnable(self, run_id: str, project_id: int):
        """处理无可用任务的情况"""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()

            # 检查总任务数
            cur.execute(
                "SELECT COUNT(*) FROM development_tasks WHERE project_id = ?",
                (project_id,)
            )
            total = cur.fetchone()[0]

            if total == 0:
                # 没有任务
                self.store.finalize_run(run_id, "completed",
                                        finish_reason="no_tasks",
                                        last_error="项目没有任何开发任务，请先生成任务")
                self._notify_state("run_completed", "no_tasks")
                return

            # 检查 pending 任务
            cur.execute("""
                SELECT COUNT(*) FROM development_tasks
                WHERE project_id = ? AND status = 'pending'
            """, (project_id,))
            pending = cur.fetchone()[0]

            if pending == 0:
                # 检查是否全部完成
                cur.execute("""
                    SELECT COUNT(*) FROM development_tasks
                    WHERE project_id = ? AND status != 'completed'
                """, (project_id,))
                not_completed = cur.fetchone()[0]

                if not_completed == 0:
                    # 全部完成
                    self.store.finalize_run(run_id, "completed",
                                            finish_reason="all_completed")
                    self._notify_state("run_completed", "all_completed")
                    return

                # 有 blocked/failed 任务但没有 pending
                self.store.finalize_run(run_id, "blocked",
                                        finish_reason="blocked_no_runnable_tasks",
                                        last_error=f"有 {not_completed} 个未完成任务但没有 pending 状态任务，无法调度")
                self._notify_state("run_blocked", "blocked_no_runnable_tasks")
                return

            # 有 pending 但不可执行（依赖未满足/字段不完整等）
            self.store.finalize_run(run_id, "blocked",
                                    finish_reason="blocked_no_runnable_tasks",
                                    last_error=f"有 {total} 个任务、{pending} 个待执行，但均不满足调度条件（依赖未完成/缺少files_to_modify/有活跃lease）")
            self._notify_state("run_blocked", "blocked_no_runnable_tasks")

        finally:
            if conn:
                conn.close()

    def _parse_command(self, steps_text: str) -> List[str]:
        """从 test_steps 或 implementation_steps 解析命令。

        支持格式：
        1. JSON 字符串数组：'["npm run typecheck", "npm run build"]'
        2. Python 列表字符串："['npm run typecheck']"
        3. 单条纯文本命令："npm run typecheck"
        4. 多行命令："npm run typecheck\\nnpm run build"
        5. 空值/None

        Windows 命令（如 "cmd /c exit 0"）正确处理空格分隔。
        使用 shlex.split 处理引号包裹的参数。
        """
        import shlex

        if not steps_text:
            return []

        steps_text = steps_text.strip()
        if not steps_text:
            return []

        # ── 1. JSON 字符串数组：'["cmd1", "cmd2"]' ──
        if steps_text.startswith("[") and steps_text.endswith("]"):
            try:
                parsed = json.loads(steps_text)
                if isinstance(parsed, list):
                    # 每个元素可能是单条命令字符串
                    if parsed and isinstance(parsed[0], str):
                        # 返回第一条命令（调度器每次只执行一条命令）
                        first = parsed[0].strip()
                        if first:
                            try:
                                return shlex.split(first)
                            except ValueError:
                                return first.split()
                    return []
            except (json.JSONDecodeError, Exception):
                pass  # 不是有效 JSON，继续尝试其他格式

        # ── 2. 多行命令：按换行分割 ──
        if "\n" in steps_text:
            lines = [line.strip() for line in steps_text.split("\n") if line.strip()]
            if lines:
                first = lines[0]
                try:
                    return shlex.split(first)
                except ValueError:
                    return first.split()

        # ── 3. "Run xxx" 格式 ──
        if steps_text.lower().startswith("run "):
            cmd_part = steps_text[4:].strip()
            try:
                return shlex.split(cmd_part)
            except ValueError:
                return cmd_part.split()

        # ── 4. 单条命令：使用 shlex 处理引号参数 ──
        try:
            return shlex.split(steps_text)
        except ValueError:
            # shlex 无法解析（如不匹配的引号），回退到 split
            return steps_text.split()

    def _execute_task(self, task, exec_cmd: List[str],
                      test_cmd: List[str],
                      executor_run_id: int = None,
                      test_files: List[str] = None) -> Dict[str, Any]:
        """调用 TaskWorker 执行单个任务（优先使用内置 AI 生成代码）"""
        try:
            worker = TaskWorker(
                self.db_path,
                repo_path=self.repo_path,
                worker_id=self._current_run_id,
                max_repairs=1,  # 沙箱模式：最多 1 次自动修复
            )

            # 构建 AI 提示词（优先于外部命令）
            prompt = None
            if task.codex_prompt:
                prompt = task.codex_prompt
            elif task.description:
                prompt = task.description

            # 测试文件：只传递真正的测试文件（以 test_ 开头）
            if test_files is None:
                test_files = [f for f in (task.files_to_modify or []) if f.startswith("test_")]

            # V1.8C-R: Pass approval service so TaskWorker can consume
            # AFTER lease claim (NOT before)
            approval_svc = None
            approval_project_id = None
            if not self._approval_consumed:
                approval_svc = self._get_approval_service()
                approval_project_id = task.project_id

            result = worker.run_task(
                task_id=task.id,
                project_id=task.project_id,
                allowed_files=task.files_to_modify,
                test_command=test_cmd,
                prompt=prompt,
                test_files=test_files,
                executor_run_id=executor_run_id,
                acceptance_criteria=getattr(task, 'acceptance_criteria', None),
                approval_svc=approval_svc,
                approval_project_id=approval_project_id,
            )

            # V1.8C-R: Track consumption state and cache approval scope
            if result.get("approval_consumed"):
                self._approval_consumed = True
                # Use the scope returned from consumption transaction
                approved_ids = result.get("approved_task_ids", [])
                if approved_ids:
                    self._approval_task_scope = set(approved_ids)
                else:
                    self._approval_task_scope = {task.id}

            return result
        except Exception as e:
            # 异常兜底：TaskWorker 未创建或 run_task 异常时的清理
            import json
            try:
                from .cleanup import ExecutionFinalizer
                finalizer = ExecutionFinalizer(self.db_path, self.repo_path)
                finalizer.finalize_execution(
                    execution_id=0,
                    task_id=task.id,
                    exit_status="worker_lost",
                    error_message=str(e),
                    worker_id=self._current_run_id,
                    executor_run_id=executor_run_id,
                )
            except Exception:
                pass
            return {
                "success": False,
                "task_id": task.id,
                "error": str(e),
                "task_status": "failed",
            }

    def _handle_task_result(self, run_id: str, task,
                            task_result: Dict[str, Any]):
        """处理任务执行结果，更新 run 计数

        防重复：同一 run 中同一 task_id 只计数一次。
        如果同一任务在同一 run 中被重复执行（如 retry 后重新 scheduling），
        只更新通知，不重复递增计数器。
        """
        task_id = task.id
        task_status = task_result.get("task_status", "failed")

        # 防重复计数：同一 run 内同一 task_id 只计数一次
        if not hasattr(self, '_counted_task_ids'):
            self._counted_task_ids = set()
        already_counted = task_id in self._counted_task_ids

        if task_status == "completed":
            if not already_counted:
                self.store.increment_counter(run_id, "tasks_completed")
                if task_result.get("repair_count", 0) > 0:
                    self.store.increment_counter(run_id, "tasks_repaired")
                self._counted_task_ids.add(task_id)
            self._consecutive_failures = 0
            self._notify_task_done(task_id, "completed",
                                   f"repairs={task_result.get('repair_count', 0)}")
            self._notify_state("task_completed", f"task_id={task_id}")

        elif task_status == "blocked":
            if not already_counted:
                self.store.increment_counter(run_id, "tasks_blocked")
                self._counted_task_ids.add(task_id)
            self._notify_task_done(task_id, "blocked",
                                   task_result.get("error", ""))
            self._notify_state("task_blocked", f"task_id={task_id}")

        elif task_status in ("test_failed", "failed"):
            if not already_counted:
                self.store.increment_counter(run_id, "tasks_failed")
                self._counted_task_ids.add(task_id)
            self._consecutive_failures += 1
            self._notify_task_done(task_id, task_status,
                                   task_result.get("error", ""))
            self._notify_state("task_completed", f"task_id={task_id} status={task_status}")

        else:
            if not already_counted:
                self.store.increment_counter(run_id, "tasks_skipped")
                self._counted_task_ids.add(task_id)
            self._notify_task_done(task_id, task_status,
                                   task_result.get("error", ""))

        # 清除 current_task_id
        self.store.set_current_task(run_id, None)
        self.store.update_heartbeat(run_id)

    def wait_for_completion(self, timeout: float = 120.0,
                            poll_interval: float = 0.5) -> Dict[str, Any]:
        """
        等待循环完成。
        轮询数据库真实状态，不依赖固定 sleep。
        """
        start = time.time()
        while time.time() - start < timeout:
            if not self._current_run_id:
                time.sleep(poll_interval)
                continue

            run = self.store.get_run_by_id(self._current_run_id)
            if not run:
                time.sleep(poll_interval)
                continue

            status = run.get("status", "")
            if status in ("completed", "blocked", "failed"):
                return {
                    "completed": True,
                    "status": status,
                    "run": run,
                    "elapsed": time.time() - start,
                }

            time.sleep(poll_interval)

        return {
            "completed": False,
            "status": "timeout",
            "run": self.store.get_run_by_id(self._current_run_id),
            "elapsed": timeout,
        }
