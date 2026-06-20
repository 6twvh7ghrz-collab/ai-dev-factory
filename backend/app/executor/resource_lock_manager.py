"""ResourceLockManager - 资源锁管理器

复用 executor_resource_locks 表，封装资源锁操作：
- 规范化资源
- 批量原子领取
- 续租 / 心跳
- 释放
- 回收过期锁
- Token 所有权检查
- 查询冲突

资源领取顺序必须固定排序，防止死锁。
"""
import sqlite3
import uuid
import json
import importlib.util
import os
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pathlib import Path


# 动态导入迁移文件中的函数（文件名以数字开头，不能直接 import）
def _load_migration_functions():
    """动态加载 005_executor_resource_locks 中的函数"""
    migration_path = Path(__file__).resolve().parent.parent / "migrations" / "005_executor_resource_locks.py"
    spec = importlib.util.spec_from_file_location(
        "_005_executor_resource_locks", str(migration_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_migration_mod = None

def _get_migration():
    global _migration_mod
    if _migration_mod is None:
        _migration_mod = _load_migration_functions()
    return _migration_mod

# 直接使用迁移文件中定义的同名函数
def normalize_path(raw_path: str) -> str:
    return _get_migration().normalize_path(raw_path)

def normalize_resource_key(resource_type: str, raw_key: str) -> str:
    return _get_migration().normalize_resource_key(resource_type, raw_key)

def acquire_resource_locks(*args, **kwargs):
    return _get_migration().acquire_resource_locks(*args, **kwargs)

def renew_resource_lock(*args, **kwargs):
    return _get_migration().renew_resource_lock(*args, **kwargs)

def release_resource_lock(*args, **kwargs):
    return _get_migration().release_resource_lock(*args, **kwargs)

def release_all_locks_for_execution(*args, **kwargs):
    return _get_migration().release_all_locks_for_execution(*args, **kwargs)

def takeover_expired_lock(*args, **kwargs):
    return _get_migration().takeover_expired_lock(*args, **kwargs)


class ResourceLockManager:
    """资源锁管理器 - 封装 executor_resource_locks 表操作"""

    DEFAULT_LOCK_TTL = 300  # 默认锁有效期 5 分钟

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ── 资源声明生成 ──

    @staticmethod
    def build_resource_declaration(
        files: List[str] = None,
        modules: List[str] = None,
        database_tables: List[str] = None,
        ports: List[int] = None,
        services: List[str] = None,
        package_managers: List[str] = None,
    ) -> Dict[str, Any]:
        """生成任务资源声明"""
        return {
            "files": files or [],
            "modules": modules or [],
            "database_tables": database_tables or [],
            "ports": ports or [],
            "services": services or [],
            "package_managers": package_managers or [],
        }

    @staticmethod
    def declaration_to_resource_list(
        declaration: Dict[str, Any],
        project_id: int,
        workspace_path: str = "",
    ) -> List[Dict[str, str]]:
        """将资源声明转换为资源锁列表

        转换规则:
          files           → project 作用域 file 锁
          modules         → project 作用域 module 锁
          database_tables → project 作用域 db_table 锁
          ports           → global 作用域 port 锁
          services        → global 作用域 service 锁
          package_managers → workspace 作用域 pkg_mgr 锁
        """
        resources = []
        scope_key = f"project:{project_id}"

        for f in declaration.get("files", []):
            resources.append({
                "resource_scope": "project",
                "scope_key": scope_key,
                "resource_type": "file",
                "resource_key": f,
            })

        for m in declaration.get("modules", []):
            resources.append({
                "resource_scope": "project",
                "scope_key": scope_key,
                "resource_type": "module",
                "resource_key": m,
            })

        for t in declaration.get("database_tables", []):
            resources.append({
                "resource_scope": "project",
                "scope_key": scope_key,
                "resource_type": "db_table",
                "resource_key": t,
            })

        for p in declaration.get("ports", []):
            resources.append({
                "resource_scope": "global",
                "scope_key": "global",
                "resource_type": "port",
                "resource_key": str(p),
            })

        for s in declaration.get("services", []):
            resources.append({
                "resource_scope": "global",
                "scope_key": "global",
                "resource_type": "service",
                "resource_key": s,
            })

        for pm in declaration.get("package_managers", []):
            resources.append({
                "resource_scope": "workspace",
                "scope_key": workspace_path or "workspace",
                "resource_type": "pkg_mgr",
                "resource_key": pm,
            })

        return resources

    @staticmethod
    def files_to_resource_list(
        files: List[str],
        project_id: int,
    ) -> List[Dict[str, str]]:
        """文件列表转资源锁列表（便捷方法）"""
        return ResourceLockManager.declaration_to_resource_list(
            {"files": files}, project_id
        )

    # ── 资源锁操作 ──

    def acquire(
        self,
        resources: List[Dict[str, str]],
        project_id: int,
        task_id: int,
        execution_id: int,
        executor_run_id: int,
        worker_id: str,
        ttl_seconds: int = None,
    ) -> Dict[str, Any]:
        """原子领取多个资源锁"""
        ttl = ttl_seconds or self.DEFAULT_LOCK_TTL
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = acquire_resource_locks(
                conn, resources,
                project_id, task_id, execution_id,
                executor_run_id, worker_id, ttl,
            )
            if result["success"]:
                conn.commit()
            else:
                conn.rollback()
            return result
        except Exception as e:
            conn.rollback()
            return {"success": False, "lock_ids": [], "error": str(e)}
        finally:
            conn.close()

    def renew(
        self,
        lock_id: str,
        lock_token: str,
        worker_id: str,
        ttl_seconds: int = None,
    ) -> bool:
        """续租单个资源锁（条件更新）"""
        ttl = ttl_seconds or self.DEFAULT_LOCK_TTL
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ok = renew_resource_lock(conn, lock_id, lock_token, worker_id, ttl)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    def release(
        self,
        lock_id: str,
        lock_token: str,
        worker_id: str,
        reason: str = "completed",
    ) -> bool:
        """释放单个资源锁（条件更新）"""
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ok = release_resource_lock(conn, lock_id, lock_token, worker_id, reason)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    def release_all_for_execution(
        self,
        execution_id: int,
        reason: str = "completed",
    ) -> int:
        """释放某个执行记录的所有活跃锁"""
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = release_all_locks_for_execution(conn, execution_id, reason)
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            return 0
        finally:
            conn.close()

    def renew_all_for_worker(
        self,
        worker_id: str,
        ttl_seconds: int = None,
    ) -> int:
        """续租某个 Worker 的所有活跃锁"""
        ttl = ttl_seconds or self.DEFAULT_LOCK_TTL
        expires_at = (datetime.now() + timedelta(seconds=ttl)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE executor_resource_locks
                   SET heartbeat_at = ?,
                       expires_at = ?
                   WHERE worker_id = ?
                     AND status = 'active'""",
                (now, expires_at, worker_id),
            )
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            return 0
        finally:
            conn.close()

    def get_active_locks(
        self,
        project_id: int = None,
        worker_id: str = None,
        execution_id: int = None,
    ) -> List[Dict[str, Any]]:
        """查询活跃锁"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            query = "SELECT * FROM executor_resource_locks WHERE status = 'active'"
            params = []

            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if worker_id is not None:
                query += " AND worker_id = ?"
                params.append(worker_id)
            if execution_id is not None:
                query += " AND execution_id = ?"
                params.append(execution_id)

            query += " ORDER BY id"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_lock_by_id(self, lock_id: str) -> Optional[Dict[str, Any]]:
        """通过 lock_id 查询锁"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM executor_resource_locks WHERE lock_id = ?",
                (lock_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def check_conflicts(
        self,
        resources: List[Dict[str, str]],
        exclude_execution_id: int = None,
    ) -> List[Dict[str, Any]]:
        """检查资源冲突（不领取）"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            conflicts = []
            for r in resources:
                nkey = normalize_resource_key(r["resource_type"], r["resource_key"])
                query = """SELECT * FROM executor_resource_locks
                           WHERE resource_scope = ?
                             AND scope_key = ?
                             AND resource_type = ?
                             AND normalized_key = ?
                             AND status = 'active'"""
                params = [r["resource_scope"], r["scope_key"],
                          r["resource_type"], nkey]

                if exclude_execution_id is not None:
                    query += " AND execution_id != ?"
                    params.append(exclude_execution_id)

                cur.execute(query, params)
                for row in cur.fetchall():
                    conflicts.append(dict(row))

            return conflicts
        finally:
            conn.close()

    def cleanup_expired(self) -> int:
        """清理所有过期锁"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.cursor()
            cur.execute(
                """UPDATE executor_resource_locks
                   SET status = 'expired',
                       released_at = ?,
                       release_reason = 'lease_expired'
                   WHERE status = 'active'
                     AND expires_at <= ?""",
                (now, now),
            )
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            return 0
        finally:
            conn.close()

    def check_task_end_status(self, task_status: str) -> bool:
        """检查任务结束状态是否需要释放资源"""
        release_statuses = {
            "completed", "blocked", "failed",
            "cancelled", "merge_failed", "worker_lost",
        }
        return task_status in release_statuses

    def force_release_orphaned(self, project_id: int = None) -> int:
        """强制释放所有无主的活跃锁（用于清理）"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.cursor()
            query = """UPDATE executor_resource_locks
                       SET status = 'released',
                           released_at = ?,
                           release_reason = 'force_cleanup'
                       WHERE status = 'active'"""
            params = [now]

            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)

            cur.execute(query, params)
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            return 0
        finally:
            conn.close()

    @staticmethod
    def normalize_path(path: str) -> str:
        return normalize_path(path)

    @staticmethod
    def normalize_resource_key(resource_type: str, raw_key: str) -> str:
        return normalize_resource_key(resource_type, raw_key)
