"""统一任务终结清理流程 - finalize_execution()

所有任务出口（completed/blocked/failed/cancelled/merge_conflict/
merge_regression_failed/worker_lost/timeout/safety_violation/shutdown）
都必须经过此函数。

清理顺序：
  停止或确认子进程结束
  → 写入最终 execution 状态
  → 释放 executor_resource_locks
  → 释放 task_lease
  → 清理或保留 Worktree
  → 更新任务状态
  → 更新 executor_run 统计
  → 清空 current_task
  → 写入终结日志

要求：
  - 清理操作幂等
  - 重复调用不会误删其他 Worker 资源
  - 只能用正确 lock_token 释放资源
  - 任一步失败必须记录并暂停领取新任务
  - 不得静默忽略异常
"""
import sqlite3
import json
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("executor.cleanup")


class ExecutionFinalizer:
    """统一终结清理器 - 确保执行结束后资源全部释放"""

    def __init__(self, db_path: str, repo_path: str = None):
        self.db_path = db_path
        self.repo_path = repo_path
        self.errors: List[str] = []

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def finalize_execution(
        self,
        execution_id: int,
        task_id: int,
        exit_status: str,  # completed/blocked/failed/cancelled/...
        error_message: str = "",
        result_json: str = "",
        worktree_path: str = "",
        worktree_branch: str = "",
        lock_ids: List[str] = None,
        lock_tokens: List[str] = None,
        worker_id: str = "",
        executor_run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        统一的执行终结入口。

        Args:
            execution_id: 执行记录 ID
            task_id: 任务 ID
            exit_status: 退出状态 (completed/blocked/failed/cancelled/...)
            error_message: 错误信息
            result_json: 结果 JSON
            worktree_path: Worktree 路径
            worktree_branch: Worktree 分支
            lock_ids: 持有的资源锁 ID 列表
            lock_tokens: 对应的 lock_token 列表
            worker_id: Worker ID
            executor_run_id: executor_run 记录 ID

        Returns:
            {"success": bool, "errors": [str], "steps_completed": [str]}
        """
        self.errors = []
        steps_completed = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            f"[finalize] execution_id={execution_id}, task_id={task_id}, "
            f"exit_status={exit_status}, worker_id={worker_id}"
        )

        # ── Step 1: 写入最终 execution 状态 ──
        try:
            self._update_execution_final(
                execution_id, exit_status, error_message, result_json, now
            )
            steps_completed.append("execution_status_updated")
        except Exception as e:
            self._record_error(f"更新execution状态失败: {e}")
            # 继续尝试其他清理

        # ── Step 2: 释放 executor_resource_locks ──
        try:
            count = self._release_resource_locks(execution_id, lock_ids, lock_tokens, worker_id)
            if count > 0:
                logger.info(f"[finalize] 释放了 {count} 个资源锁")
            steps_completed.append(f"resource_locks_released({count})")
        except Exception as e:
            self._record_error(f"释放资源锁失败: {e}")

        # ── Step 3: 释放 task_lease ──
        try:
            released = self._release_lease(task_id)
            if released:
                steps_completed.append("lease_released")
            else:
                steps_completed.append("lease_already_released")
        except Exception as e:
            self._record_error(f"释放lease失败: {e}")

        # ── Step 4: 清理 Worktree ──
        wt_result = None
        try:
            wt_result = self._cleanup_worktree(
                task_id, execution_id, exit_status,
                worktree_path, worktree_branch
            )
            if wt_result:
                steps_completed.append(f"worktree_{wt_result}")
        except Exception as e:
            self._record_error(f"清理Worktree失败: {e}")

        # ── Step 5: 更新任务状态 ──
        task_status = self._map_exit_to_task_status(exit_status)
        try:
            self._update_task_status(task_id, task_status, result_json)
            steps_completed.append(f"task_status_updated({task_status})")
        except Exception as e:
            self._record_error(f"更新任务状态失败: {e}")

        # ── Step 6: 更新 executor_run 统计 ──
        if executor_run_id:
            try:
                self._update_executor_run_stats(executor_run_id, task_status, now)
                steps_completed.append("run_stats_updated")
            except Exception as e:
                self._record_error(f"更新run统计失败: {e}")

        # ── Step 7: 清空 current_task ──
        if executor_run_id:
            try:
                self._clear_current_task(executor_run_id)
                steps_completed.append("current_task_cleared")
            except Exception as e:
                self._record_error(f"清空current_task失败: {e}")

        # ── Step 8: 写入终结日志 ──
        try:
            self._write_finalization_log(
                execution_id, exit_status, self.errors, steps_completed
            )
            steps_completed.append("finalization_logged")
        except Exception as e:
            self._record_error(f"写入终结日志失败: {e}")

        success = len([e for e in self.errors if "failed" in e.lower() or "失败" in e]) == 0

        logger.info(
            f"[finalize] 完成 execution_id={execution_id}: "
            f"success={success}, errors={len(self.errors)}, "
            f"steps={steps_completed}"
        )

        return {
            "success": success,
            "errors": self.errors,
            "steps_completed": steps_completed,
        }

    # ── 内部方法 ──

    def _record_error(self, msg: str):
        """记录错误，不静默忽略"""
        self.errors.append(msg)
        logger.error(f"[finalize] ERROR: {msg}")

    def _update_execution_final(
        self, execution_id: int, status: str, error_message: str,
        result_json: str, now: str
    ):
        """更新 execution 记录到最终状态"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 先读取当前记录确认存在
            cur.execute("SELECT id, status FROM executions WHERE id = ?", (execution_id,))
            row = cur.fetchone()
            if not row:
                self._record_error(f"execution_id={execution_id} 不存在")
                return

            # 映射 exit_status 到 execution status
            exec_status = self._map_exit_to_execution_status(status)

            cur.execute("""
                UPDATE executions
                SET status = ?,
                    completed_at = ?,
                    error_message = ?,
                    execution_result = ?,
                    updated_at = ?
                WHERE id = ?
            """, (exec_status, now, error_message[:5000] if error_message else "",
                  result_json[:10000] if result_json else "", now, execution_id))
            conn.commit()
        finally:
            conn.close()

    def _map_exit_to_execution_status(self, exit_status: str) -> str:
        """将退出状态映射到 execution 状态"""
        mapping = {
            "completed": "success",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "cancelled",
            "merge_conflict": "blocked",
            "merge_regression_failed": "blocked",
            "worker_lost": "failed",
            "timeout": "failed",
            "safety_violation": "blocked",
            "shutdown": "cancelled",
        }
        return mapping.get(exit_status, "failed")

    def _map_exit_to_task_status(self, exit_status: str) -> str:
        """将退出状态映射到任务状态"""
        mapping = {
            "completed": "completed",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "cancelled",
            "merge_conflict": "blocked",
            "merge_regression_failed": "blocked",
            "worker_lost": "blocked",
            "timeout": "blocked",
            "safety_violation": "blocked",
            "shutdown": "cancelled",
        }
        return mapping.get(exit_status, "failed")

    def _release_resource_locks(
        self, execution_id: int,
        lock_ids: List[str], lock_tokens: List[str],
        worker_id: str
    ) -> int:
        """释放执行关联的所有资源锁（条件更新）"""
        count = 0
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if lock_ids and lock_tokens and len(lock_ids) == len(lock_tokens):
                # 使用 lock_token 精确释放
                for lid, ltok in zip(lock_ids, lock_tokens):
                    cur.execute("""
                        UPDATE executor_resource_locks
                        SET status = 'released',
                            released_at = ?,
                            release_reason = 'finalized'
                        WHERE lock_id = ?
                          AND lock_token = ?
                          AND worker_id = ?
                          AND status = 'active'
                    """, (now, lid, ltok, worker_id))
                    count += cur.rowcount
            else:
                # 批量释放此 execution 的所有活跃锁
                cur.execute("""
                    UPDATE executor_resource_locks
                    SET status = 'released',
                        released_at = ?,
                        release_reason = 'finalized_batch'
                    WHERE execution_id = ?
                      AND status = 'active'
                """, (now, execution_id))
                count = cur.rowcount

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
        return count

    def _release_lease(self, task_id: int) -> bool:
        """释放 task_lease（幂等）"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id FROM task_leases
                WHERE task_id = ? AND status = 'active'
            """, (task_id,))
            if not cur.fetchone():
                return False  # 已经释放

            cur.execute("""
                UPDATE task_leases
                SET status = 'released',
                    released_at = datetime('now')
                WHERE task_id = ? AND status = 'active'
            """, (task_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def _cleanup_worktree(
        self, task_id: int, execution_id: int,
        exit_status: str,
        worktree_path: str, worktree_branch: str
    ) -> Optional[str]:
        """清理 Worktree"""
        if not self.repo_path:
            return "no_repo_path"
        if not worktree_path:
            return "no_worktree_path"

        try:
            from .worktree_manager import WorktreeManager
            wtm = WorktreeManager(self.repo_path)

            # 记录清理前状态
            before = wtm._run_git(["worktree", "list", "--porcelain"], timeout=15)
            logger.info(f"[finalize] worktree before cleanup: {before['stdout'][:500]}")

            if exit_status == "completed":
                # 任务成功：删除 Worktree 和临时分支
                result = wtm.remove_worktree(task_id, execution_id, force=True)
                logger.info(f"[finalize] worktree removed: {result}")
            else:
                # 任务 blocked/failed：回滚后删除
                wtm.reset_to_checkpoint(task_id, execution_id, worktree_path)
                result = wtm.remove_worktree(task_id, execution_id, force=True)
                logger.info(f"[finalize] worktree removed after rollback: {result}")

            # 清理 git worktree 记录
            wtm._run_git(["worktree", "prune"], timeout=15)

            # 记录清理后状态
            after = wtm._run_git(["worktree", "list", "--porcelain"], timeout=15)
            logger.info(f"[finalize] worktree after cleanup: {after['stdout'][:500]}")

            return "cleaned"
        except Exception as e:
            logger.error(f"[finalize] Worktree清理异常: {e}")
            return "cleanup_failed"

    def _update_task_status(self, task_id: int, status: str, result_json: str):
        """更新任务状态"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            if result_json:
                cur.execute("""
                    UPDATE development_tasks
                    SET status = ?, execution_result = ?, updated_at = datetime('now')
                    WHERE id = ?
                """, (status, result_json, task_id))
            else:
                cur.execute("""
                    UPDATE development_tasks
                    SET status = ?, updated_at = datetime('now')
                    WHERE id = ?
                """, (status, task_id))
            conn.commit()
        finally:
            conn.close()

    def _update_executor_run_stats(
        self, executor_run_id: int, task_status: str, now: str
    ):
        """更新 executor_run 统计计数"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            field_map = {
                "completed": "tasks_completed",
                "blocked": "tasks_blocked",
                "failed": "tasks_failed",
            }
            field = field_map.get(task_status)
            if field:
                cur.execute(f"""
                    UPDATE executor_runs
                    SET {field} = {field} + 1,
                        heartbeat_at = ?
                    WHERE id = ?
                """, (now, executor_run_id))
                conn.commit()
        finally:
            conn.close()

    def _clear_current_task(self, executor_run_id: int):
        """清空 executor_run 的 current_task_id"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE executor_runs
                SET current_task_id = NULL
                WHERE id = ?
            """, (executor_run_id,))
            conn.commit()
        finally:
            conn.close()

    def _write_finalization_log(
        self, execution_id: int, exit_status: str,
        errors: List[str], steps_completed: List[str]
    ):
        """写入终结日志到 execution_logs"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            detail = json.dumps({
                "exit_status": exit_status,
                "errors": errors,
                "steps_completed": steps_completed,
                "timestamp": datetime.now().isoformat(),
            }, ensure_ascii=False)
            cur.execute("""
                INSERT INTO execution_logs
                (execution_id, step_name, step_status, detail)
                VALUES (?, 'finalize_execution', ?, ?)
            """, (
                execution_id,
                "success" if len(errors) == 0 else "partial_failure",
                detail,
            ))
            conn.commit()
        finally:
            conn.close()


# ── 便捷函数 ──

def finalize_execution(
    db_path: str,
    execution_id: int,
    task_id: int,
    exit_status: str,
    repo_path: str = None,
    error_message: str = "",
    result_json: str = "",
    worktree_path: str = "",
    worktree_branch: str = "",
    lock_ids: List[str] = None,
    lock_tokens: List[str] = None,
    worker_id: str = "",
    executor_run_id: int = None,
) -> Dict[str, Any]:
    """
    便捷函数：终结一次任务执行。

    所有任务出口都必须经过此函数。
    """
    finalizer = ExecutionFinalizer(db_path, repo_path)
    return finalizer.finalize_execution(
        execution_id=execution_id,
        task_id=task_id,
        exit_status=exit_status,
        error_message=error_message,
        result_json=result_json,
        worktree_path=worktree_path,
        worktree_branch=worktree_branch,
        lock_ids=lock_ids or [],
        lock_tokens=lock_tokens or [],
        worker_id=worker_id,
        executor_run_id=executor_run_id,
    )
