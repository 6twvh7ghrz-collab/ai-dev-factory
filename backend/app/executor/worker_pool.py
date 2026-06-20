"""WorkerPool - 双 Worker 线程池

要求:
- 最多 2 个 Worker 线程
- 每个 Worker 独立心跳
- 每个 Worker 独立 execution
- 一个 Worker 失败不终止另一个
- 不得使用共享可变的当前任务变量
"""
import threading
import time
import uuid
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class WorkerContext:
    """单个 Worker 的上下文"""
    worker_id: str
    thread: Optional[threading.Thread] = None
    status: str = "idle"  # idle / executing / done / error
    current_task_id: Optional[int] = None
    current_execution_id: Optional[int] = None
    worktree_path: Optional[str] = None
    lock_ids: List[str] = field(default_factory=list)
    lock_tokens: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cli_started_at: Optional[str] = None
    cli_finished_at: Optional[str] = None
    test_started_at: Optional[str] = None
    test_finished_at: Optional[str] = None
    repair_count: int = 0
    task_status: Optional[str] = None
    error: Optional[str] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)


class WorkerPool:
    """双 Worker 线程池"""

    MAX_WORKERS = 2

    def __init__(self, db_path: str, repo_path: str):
        self.db_path = db_path
        self.repo_path = repo_path
        self.workers: Dict[str, WorkerContext] = {}
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()

    def spawn_worker(
        self,
        task_id: int,
        execution_id: int,
        worktree_path: str,
        lock_ids: List[str],
        lock_tokens: List[str],
        execute_fn: Callable,
        worker_id: str = None,
    ) -> Dict[str, Any]:
        """启动一个 Worker 执行任务

        Args:
            task_id: 任务 ID
            execution_id: 执行记录 ID
            worktree_path: Worktree 路径
            lock_ids: 持有的锁 ID 列表
            lock_tokens: 锁 token 列表
            execute_fn: 执行函数 (worker_ctx) -> Dict
            worker_id: 可选指定 worker_id

        Returns:
            {"success": bool, "worker_id": str, "error": str}
        """
        with self._lock:
            if len(self.workers) >= self.MAX_WORKERS:
                return {
                    "success": False,
                    "worker_id": None,
                    "error": f"已达最大 Worker 数 {self.MAX_WORKERS}",
                }

            wid = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

            ctx = WorkerContext(
                worker_id=wid,
                current_task_id=task_id,
                current_execution_id=execution_id,
                worktree_path=worktree_path,
                lock_ids=lock_ids,
                lock_tokens=lock_tokens,
                status="executing",
                started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            self.workers[wid] = ctx

        # 启动线程
        thread = threading.Thread(
            target=self._worker_runner,
            args=(ctx, execute_fn),
            daemon=True,
        )
        ctx.thread = thread
        thread.start()

        return {"success": True, "worker_id": wid, "error": None}

    def _worker_runner(self, ctx: WorkerContext, execute_fn: Callable):
        """Worker 线程执行体（含终结清理保障）"""
        try:
            result = execute_fn(ctx)
            ctx.task_status = result.get("task_status", "failed")
            ctx.repair_count = result.get("repair_count", 0)
            ctx.error = result.get("error")

            if ctx.task_status in ("completed",):
                ctx.status = "done"
            else:
                ctx.status = "error"

            # 如果 execute_fn 未调用 finalize_execution，做兜底清理
            exec_id = ctx.current_execution_id
            task_id = ctx.current_task_id
            if exec_id and task_id and not result.get("finalize"):
                self._fallback_finalize(exec_id, task_id, ctx)
        except Exception as e:
            ctx.status = "error"
            ctx.error = str(e)
            # 异常时兜底清理
            exec_id = ctx.current_execution_id
            task_id = ctx.current_task_id
            if exec_id and task_id:
                try:
                    self._fallback_finalize(exec_id, task_id, ctx, exit_status="worker_lost",
                                            error_message=str(e))
                except Exception:
                    pass
        finally:
            ctx.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _fallback_finalize(self, execution_id: int, task_id: int,
                           ctx: WorkerContext, exit_status: str = "completed",
                           error_message: str = ""):
        """兜底终结清理"""
        try:
            from .cleanup import ExecutionFinalizer
            finalizer = ExecutionFinalizer(self.db_path, self.repo_path)
            finalizer.finalize_execution(
                execution_id=execution_id,
                task_id=task_id,
                exit_status=exit_status,
                error_message=error_message,
                worktree_path=ctx.worktree_path or "",
                lock_ids=ctx.lock_ids,
                lock_tokens=ctx.lock_tokens,
                worker_id=ctx.worker_id,
            )
        except Exception:
            pass  # 兜底清理失败不抛异常

    def get_active_count(self) -> int:
        """获取活跃 Worker 数"""
        with self._lock:
            return sum(1 for w in self.workers.values() if w.status == "executing")

    def get_worker_ids(self) -> List[str]:
        """获取所有 Worker ID"""
        with self._lock:
            return list(self.workers.keys())

    def get_worker_status(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """获取单个 Worker 状态"""
        with self._lock:
            ctx = self.workers.get(worker_id)
            if not ctx:
                return None
            return {
                "worker_id": ctx.worker_id,
                "status": ctx.status,
                "current_task_id": ctx.current_task_id,
                "current_execution_id": ctx.current_execution_id,
                "worktree_path": ctx.worktree_path,
                "lock_ids": ctx.lock_ids,
                "repair_count": ctx.repair_count,
                "task_status": ctx.task_status,
                "started_at": ctx.started_at,
                "finished_at": ctx.finished_at,
                "cli_started_at": ctx.cli_started_at,
                "cli_finished_at": ctx.cli_finished_at,
                "test_started_at": ctx.test_started_at,
                "test_finished_at": ctx.test_finished_at,
                "error": ctx.error,
            }

    def get_all_worker_statuses(self) -> List[Dict[str, Any]]:
        """获取所有 Worker 状态"""
        with self._lock:
            return [self.get_worker_status(wid) for wid in self.workers]

    def wait_all(self, timeout: float = 300.0) -> Dict[str, Any]:
        """等待所有 Worker 完成"""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                active = [w for w in self.workers.values() if w.status == "executing"]
                if not active:
                    return {
                        "success": True,
                        "all_done": True,
                        "elapsed": time.time() - start,
                    }
            time.sleep(0.5)

        return {
            "success": False,
            "all_done": False,
            "elapsed": timeout,
            "timeout": True,
        }

    def stop_worker(self, worker_id: str):
        """停止单个 Worker"""
        with self._lock:
            ctx = self.workers.get(worker_id)
            if ctx:
                ctx._stop_event.set()

    def shutdown(self):
        """关闭所有 Worker"""
        self._shutdown_event.set()
        with self._lock:
            for ctx in self.workers.values():
                ctx._stop_event.set()

    def is_shutdown(self) -> bool:
        return self._shutdown_event.is_set()

    def cleanup(self):
        """清理所有 Worker 记录"""
        with self._lock:
            self.workers.clear()

    def can_spawn(self) -> bool:
        """检查是否可以创建新 Worker"""
        with self._lock:
            return len(self.workers) < self.MAX_WORKERS
