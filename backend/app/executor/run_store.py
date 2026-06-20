"""RunStore - executor_runs 表的原子操作层

负责：
- 原子创建 starting run（直接插入 status=starting，禁止 idle→starting 两步走）
- 查询 run
- 状态更新（所有 UPDATE 必须触发 updated_at）
- heartbeat 更新
- current_task_id / current_step 更新
- 计数更新
- pause_reason / stop_requested 设置
- finished_at 写回
- worker 接管（原子 UPDATE WHERE）
- 终态写回
"""
import sqlite3
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path


class RunStore:
    """executor_runs 表的原子操作层"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    # ── 原子创建 starting run ──

    def create_starting_run(self, project_id: int,
                            mode: str = "auto_until_blocked") -> Dict[str, Any]:
        """
        原子创建 starting run。
        直接插入 status='starting'，禁止先 idle 再 starting 的两步操作。

        Returns:
            {"success": bool, "run": dict, "error": str}
        """
        conn = self._get_conn()
        try:
            run_id = f"runner-{uuid.uuid4().hex[:12]}"
            worker_id = run_id  # worker_id == run_id for single-worker
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO executor_runs
                (run_id, project_id, worker_id, status, mode,
                 started_at, heartbeat_at, current_step,
                 tasks_completed, tasks_failed, tasks_blocked,
                 tasks_repaired, tasks_skipped, tasks_total)
                VALUES (?, ?, ?, 'starting', ?, ?, ?, 'starting',
                 0, 0, 0, 0, 0, 0)
            """, (run_id, project_id, worker_id, mode, now, now))
            conn.commit()

            run = self._get_run_by_run_id(conn, run_id)
            return {"success": True, "run": run, "error": None}
        except sqlite3.IntegrityError as e:
            conn.rollback()
            if "uq_executor_runs_active_project" in str(e):
                return {"success": False, "run": None, "error": "already_running"}
            return {"success": False, "run": None, "error": f"integrity_error: {e}"}
        except Exception as e:
            conn.rollback()
            return {"success": False, "run": None, "error": str(e)}
        finally:
            conn.close()

    # ── 查询 ──

    def _get_run_by_run_id(self, conn, run_id: str) -> Optional[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute("SELECT * FROM executor_runs WHERE run_id = ?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_active_run(self, project_id: int) -> Optional[Dict[str, Any]]:
        """获取指定项目的活跃 run（8 种活跃状态）"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM executor_runs
                WHERE project_id = ?
                AND status IN ('starting','scanning','claiming','executing',
                               'testing','repairing','paused','stopping')
                ORDER BY id DESC LIMIT 1
            """, (project_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_run_by_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        """通过 run_id 获取 run"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM executor_runs WHERE run_id = ?", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_runs(self, project_id: int = None, status: str = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
        """查询 run 列表"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            query = "SELECT * FROM executor_runs WHERE 1=1"
            params = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── 状态更新（所有 UPDATE 必须触发 updated_at trigger）──

    def update_status(self, run_id: str, status: str = None,
                      **extra_fields) -> bool:
        """
        原子更新 run 状态。
        UPDATE 语句触发 trg_executor_runs_updated_at。
        如果 status 为 None，则不更新 status 字段。
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            sets = []
            values = []

            if status is not None:
                sets.append("status = ?")
                values.append(status)

            for k, v in extra_fields.items():
                if v is not None:
                    sets.append(f"{k} = ?")
                    values.append(v)

            if not sets:
                conn.rollback()
                return False

            values.append(run_id)

            cur.execute(
                f"UPDATE executor_runs SET {', '.join(sets)} WHERE run_id = ?",
                values
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            conn.rollback()
            print(f"[RunStore] update_status error: {e}")
            return False
        finally:
            conn.close()

    def update_heartbeat(self, run_id: str) -> bool:
        """更新心跳时间戳"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE executor_runs
                SET heartbeat_at = datetime('now','localtime')
                WHERE run_id = ?
            """, (run_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_current_task(self, run_id: str, task_id: int) -> bool:
        """设置当前任务"""
        return self.update_status(run_id, None,  # status unchanged
                                  current_task_id=task_id)

    def set_current_step(self, run_id: str, step: str) -> bool:
        """设置当前步骤"""
        return self.update_status(run_id, None, current_step=step)

    def increment_counter(self, run_id: str, field: str) -> bool:
        """原子递增计数器（completed/blocked/failed/repaired/skipped/total）"""
        allowed = ['tasks_completed', 'tasks_blocked', 'tasks_failed',
                   'tasks_repaired', 'tasks_skipped', 'tasks_total']
        if field not in allowed:
            return False

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE executor_runs SET {field} = {field} + 1 WHERE run_id = ?",
                (run_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_pause_reason(self, run_id: str, reason: str) -> bool:
        """设置暂停原因"""
        return self.update_status(run_id, None, pause_reason=reason)

    def set_stop_requested(self, run_id: str) -> bool:
        """设置停止请求标志"""
        return self.update_status(run_id, None, stop_requested=1)

    def set_finished(self, run_id: str) -> bool:
        """设置完成时间"""
        return self.update_status(
            run_id, None,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def set_last_error(self, run_id: str, error: str) -> bool:
        """设置最后错误"""
        return self.update_status(run_id, None, last_error=error[:1000])

    def update_budget(self, run_id: str, budget_json: str) -> bool:
        """更新预算 JSON"""
        return self.update_status(run_id, None, budget_json=budget_json)

    # ── Worker 接管 ──

    def takeover_expired_run(self, project_id: int, new_worker_id: str,
                             heartbeat_timeout: int = 120) -> Dict[str, Any]:
        """
        原子接管心跳过期的活跃 run。
        条件：status 为活跃状态 且 heartbeat 超时。
        返回：{"success": bool, "run": dict, "error": str}
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # 查找可接管的 run
            cur.execute("""
                SELECT run_id FROM executor_runs
                WHERE project_id = ?
                AND status IN ('starting','scanning','claiming','executing',
                               'testing','repairing','paused','stopping')
                AND (
                    heartbeat_at IS NULL
                    OR heartbeat_at < datetime('now','localtime',?)
                )
                ORDER BY id DESC LIMIT 1
            """, (project_id, f'-{heartbeat_timeout} seconds'))

            row = cur.fetchone()
            if not row:
                conn.rollback()
                return {"success": False, "run": None,
                        "error": "no_expired_run"}

            target_run_id = row[0]

            # 原子更新 worker_id 和 heartbeat
            cur.execute("""
                UPDATE executor_runs
                SET worker_id = ?,
                    heartbeat_at = datetime('now','localtime')
                WHERE run_id = ?
                AND status IN ('starting','scanning','claiming','executing',
                               'testing','repairing','paused','stopping')
                AND (
                    heartbeat_at IS NULL
                    OR heartbeat_at < datetime('now','localtime',?)
                )
            """, (new_worker_id, target_run_id, f'-{heartbeat_timeout} seconds'))

            if cur.rowcount == 0:
                conn.rollback()
                return {"success": False, "run": None,
                        "error": "takeover_race_lost"}

            conn.commit()

            # 读取接管后的 run
            run = self._get_run_by_run_id(conn, target_run_id)
            return {"success": True, "run": run, "error": None}
        except Exception as e:
            conn.rollback()
            return {"success": False, "run": None, "error": str(e)}
        finally:
            conn.close()

    # ── 终态写回 ──

    def finalize_run(self, run_id: str, status: str,
                     finish_reason: str = "",
                     last_error: str = "") -> bool:
        """
        将 run 标记为终态（completed/blocked/failed）。
        终态不可再被接管或修改为活跃状态。
        finish_reason 存入 last_error 或 pause_reason 字段。
        """
        extra = {
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if finish_reason:
            extra["pause_reason"] = finish_reason
        if last_error:
            extra["last_error"] = last_error[:1000]
        return self.update_status(run_id, status, **extra)
