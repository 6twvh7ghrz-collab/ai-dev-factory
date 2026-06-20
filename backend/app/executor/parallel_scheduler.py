"""ParallelScheduler - 并行任务调度器

职责:
1. 扫描 pending 任务
2. 检查依赖
3. 生成资源声明
4. 找出最多 2 个互不冲突的任务
5. 原子领取任务和资源
6. 分配不同 worker_id
7. 分配不同 execution_id
8. 无安全组合时降级为一个任务

确定性排序: priority → sort_order → task_id

强制串行情况:
- 修改相同文件
- 修改同一核心模块
- 写入同一数据库表
- 使用同一端口
- 启动或停止同一服务
- 执行依赖安装
- 修改 package.json / 锁文件
- 数据库迁移
- 认证权限密钥
- 部署配置
- 删除文件
- 大范围重构
"""
import sqlite3
import json
import uuid
from typing import Optional, List, Dict, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .resource_lock_manager import ResourceLockManager
from .task_scheduler import TaskScheduler, SchedulableTask


@dataclass
class TaskGroup:
    """任务组 - 包含 1-2 个可并行执行的任务"""
    tasks: List[SchedulableTask] = field(default_factory=list)
    resources: List[Dict[str, str]] = field(default_factory=list)
    worker_ids: List[str] = field(default_factory=list)
    execution_ids: List[int] = field(default_factory=list)
    lock_ids: List[str] = field(default_factory=list)
    lock_tokens: List[str] = field(default_factory=list)
    is_parallel: bool = False


class ParallelScheduler:
    """并行任务调度器"""

    MAX_PARALLEL = 2

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.task_scheduler = TaskScheduler(db_path)
        self.lock_manager = ResourceLockManager(db_path)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def find_parallel_tasks(
        self,
        project_id: int,
    ) -> List[SchedulableTask]:
        """查找可并行执行的任务组合（最多 2 个）"""
        runnable = self.task_scheduler.find_runnable_tasks(project_id)
        if not runnable:
            return []

        if len(runnable) == 1:
            return runnable[:1]

        # 尝试找到 2 个不冲突的任务
        best_pair = self._find_non_conflicting_pair(runnable, project_id)
        if best_pair:
            return best_pair

        # 无法找到并行组合，降级为单任务
        return runnable[:1]

    def _find_non_conflicting_pair(
        self,
        tasks: List[SchedulableTask],
        project_id: int,
    ) -> Optional[List[SchedulableTask]]:
        """找到 2 个互不冲突的任务组合"""
        for i, task_a in enumerate(tasks):
            resources_a = self._get_task_resources(task_a, project_id)
            for j, task_b in enumerate(tasks[i + 1:], i + 1):
                resources_b = self._get_task_resources(task_b, project_id)

                if not self._resources_conflict(resources_a, resources_b):
                    return [task_a, task_b]

        return None

    def _resources_conflict(
        self,
        resources_a: List[Dict[str, str]],
        resources_b: List[Dict[str, str]],
    ) -> bool:
        """检查两组资源是否冲突"""
        set_a = set()
        for r in resources_a:
            key = (
                r["resource_scope"],
                r["scope_key"],
                r["resource_type"],
                ResourceLockManager.normalize_resource_key(
                    r["resource_type"], r["resource_key"]
                ),
            )
            set_a.add(key)

        for r in resources_b:
            key = (
                r["resource_scope"],
                r["scope_key"],
                r["resource_type"],
                ResourceLockManager.normalize_resource_key(
                    r["resource_type"], r["resource_key"]
                ),
            )
            if key in set_a:
                return True

        return False

    def _get_task_resources(
        self,
        task: SchedulableTask,
        project_id: int,
    ) -> List[Dict[str, str]]:
        """从任务中提取资源声明"""
        files = task.files_to_modify or []

        # 检查强制串行情境
        return ResourceLockManager.files_to_resource_list(files, project_id)

    def _check_force_serial(self, task: SchedulableTask) -> bool:
        """检查是否强制串行"""
        files = [f.lower() for f in (task.files_to_modify or [])]

        force_serial_patterns = [
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "poetry.lock",
            "requirements.txt",
            ".env",
            "config.py",
            "settings.py",
            "migrations/",
            "auth",
            "deploy",
        ]

        for f in files:
            for pattern in force_serial_patterns:
                if pattern in f:
                    return True

        return False

    def claim_task_group(
        self,
        tasks: List[SchedulableTask],
        project_id: int,
        executor_run_id: int,
    ) -> Dict[str, Any]:
        """原子领取一组任务 + 资源锁

        流程:
        1. BEGIN IMMEDIATE
        2. 领取每个任务的 lease
        3. 创建每个任务的 execution
        4. 领取所有任务的资源锁
        5. 任一失败 → ROLLBACK
        6. COMMIT

        返回:
            {"success": bool, "group": TaskGroup, "error": str}
        """
        if not tasks:
            return {"success": False, "error": "任务列表为空", "group": None}

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            worker_ids = []
            execution_ids = []
            all_locks = []
            all_lock_tokens = []

            for task in tasks:
                worker_id = f"worker-{uuid.uuid4().hex[:8]}"
                worker_ids.append(worker_id)

                # 1. 原子领取任务 lease
                cur.execute("""
                    SELECT id FROM task_leases
                    WHERE task_id = ? AND status = 'active'
                    AND expires_at > datetime('now')
                """, (task.id,))
                if cur.fetchone():
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Task {task.id} 已被领取",
                        "group": None,
                    }

                cur.execute(
                    "SELECT status FROM development_tasks WHERE id = ?",
                    (task.id,),
                )
                row = cur.fetchone()
                if not row or row[0] != "pending":
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Task {task.id} 状态不是 pending: {row[0] if row else 'not found'}",
                        "group": None,
                    }

                cur.execute("""
                    INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
                    VALUES (?, ?, 'active', datetime('now'), datetime('now', '+3600 seconds'))
                """, (task.id, worker_id))

                # 2. 创建 execution 记录
                cur.execute("""
                    INSERT INTO executions (task_id, project_id, worker_id, status, started_at)
                    VALUES (?, ?, ?, 'running', datetime('now'))
                """, (task.id, project_id, worker_id))
                execution_id = cur.lastrowid
                execution_ids.append(execution_id)

                # 3. 获取资源声明
                resources = self._get_task_resources(task, project_id)
                if resources:
                    from .resource_lock_manager import acquire_resource_locks as _acl
                    lock_result = _acl(
                        conn, resources,
                        project_id, task.id, execution_id,
                        executor_run_id, worker_id,
                        lock_ttl_seconds=300,
                    )
                    if not lock_result["success"]:
                        conn.rollback()
                        return {
                            "success": False,
                            "error": f"Task {task.id} 资源锁领取失败: {lock_result['error']}",
                            "group": None,
                        }
                    all_locks.extend(lock_result["lock_ids"])
                    # 获取对应的 lock_tokens
                    for lid in lock_result["lock_ids"]:
                        cur.execute(
                            "SELECT lock_token FROM executor_resource_locks WHERE lock_id = ?",
                            (lid,),
                        )
                        token_row = cur.fetchone()
                        if token_row:
                            all_lock_tokens.append(token_row[0])

            conn.commit()

            group = TaskGroup(
                tasks=list(tasks),
                resources=[],
                worker_ids=worker_ids,
                execution_ids=execution_ids,
                lock_ids=all_locks,
                lock_tokens=all_lock_tokens,
                is_parallel=len(tasks) == 2,
            )

            return {"success": True, "group": group, "error": None}

        except Exception as e:
            conn.rollback()
            return {"success": False, "error": str(e), "group": None}
        finally:
            conn.close()

    def release_task_group(
        self,
        execution_ids: List[int],
        reason: str = "completed",
    ):
        """释放一组任务的所有资源"""
        for eid in execution_ids:
            self.lock_manager.release_all_for_execution(eid, reason)

        # 释放 task_leases
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            for eid in execution_ids:
                cur.execute(
                    "SELECT task_id FROM executions WHERE id = ?", (eid,)
                )
                row = cur.fetchone()
                if row:
                    cur.execute("""
                        UPDATE task_leases
                        SET status = 'released', released_at = datetime('now')
                        WHERE task_id = ? AND status = 'active'
                    """, (row[0],))
            conn.commit()
        finally:
            conn.close()

    def get_resource_declaration(
        self,
        task: SchedulableTask,
        project_id: int,
    ) -> Dict[str, Any]:
        """获取任务的完整资源声明 JSON"""
        resources = self._get_task_resources(task, project_id)
        declaration = {
            "files": task.files_to_modify or [],
            "modules": [],
            "database_tables": [],
            "ports": [],
            "services": [],
            "package_managers": [],
            "workspace": str(project_id),
        }

        for r in resources:
            rtype = r["resource_type"]
            rkey = r["resource_key"]
            if rtype == "file":
                if rkey not in declaration["files"]:
                    declaration["files"].append(rkey)
            elif rtype == "module":
                declaration["modules"].append(rkey)
            elif rtype == "db_table":
                declaration["database_tables"].append(rkey)
            elif rtype == "port":
                declaration["ports"].append(int(rkey) if rkey.isdigit() else rkey)
            elif rtype == "service":
                declaration["services"].append(rkey)
            elif rtype == "pkg_mgr":
                declaration["package_managers"].append(rkey)

        return declaration
