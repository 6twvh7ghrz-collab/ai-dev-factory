"""TaskScheduler - 任务调度器

负责：
- 查找满足条件的可执行任务
- 确定性排序：priority → sort_order → task_id
- 原子领取（lease）
- 检查依赖、状态、lease 等条件

领取条件：
  status = pending
  依赖全部 completed
  没有有效 lease
  项目未暂停
  任务字段完整
  允许自动执行

V1.8 依赖标准化：
  - 支持整数 ID、数字字符串 ID、标题字符串三种历史格式
  - 明确拒绝非法类型（字典、嵌套数组等）
  - 跨项目同名标题不满足依赖
"""
import sqlite3
import json
from typing import Optional, List, Dict, Any, Union
from dataclasses import dataclass
from enum import Enum


class DependencyType(Enum):
    """依赖引用类型"""
    TASK_ID = "task_id"       # 整数或纯数字字符串 → 按 id 精确查询
    TASK_TITLE = "task_title"  # 非数字字符串 → 按 title 精确匹配
    INVALID = "invalid"        # 非法类型


@dataclass
class DependencyRef:
    """标准化依赖引用"""
    raw: Any                    # 原始值
    ref_type: DependencyType   # 引用类型
    task_id: Optional[int] = None   # 整数 ID
    title: Optional[str] = None     # 标题（精确匹配用）


@dataclass
class SchedulableTask:
    """可调度任务"""
    id: int
    project_id: int
    title: str
    priority: int
    sort_order: int
    status: str
    dependencies: List[DependencyRef]  # V1.8: 改为 DependencyRef 列表
    files_to_modify: List[str]  # parsed from JSON
    files_to_check: List[str]  # parsed from JSON
    codex_prompt: str
    implementation_steps: str
    test_steps: str
    task_type: str
    acceptance_criteria: str = ""  # 验收标准


def normalize_dependencies(raw_dependencies: List[Any]) -> List[DependencyRef]:
    """
    V1.8: 标准化依赖列表为 DependencyRef 列表。

    支持的输入格式：
      - 整数: 26 → task_id 引用
      - 纯数字字符串: "26" → task_id 引用
      - 非数字字符串: "搭建Electron项目基础框架" → task title 引用
      - 空字符串/null → 忽略（不加入结果）

    拒绝的格式：
      - 字典、嵌套数组及其他非法类型 → INVALID
    """
    result: List[DependencyRef] = []

    if not raw_dependencies:
        return result

    if not isinstance(raw_dependencies, list):
        # 整个输入不是列表 → 返回空
        return result

    for raw in raw_dependencies:
        # 跳过空值
        if raw is None or raw == "":
            continue

        # 整数（但排除 bool，因为 bool 是 int 子类）
        if isinstance(raw, int) and not isinstance(raw, bool):
            result.append(DependencyRef(
                raw=raw,
                ref_type=DependencyType.TASK_ID,
                task_id=raw,
            ))
            continue

        # 字符串
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                continue
            # 纯数字字符串 → task_id 引用
            if stripped.isdigit():
                result.append(DependencyRef(
                    raw=stripped,
                    ref_type=DependencyType.TASK_ID,
                    task_id=int(stripped),
                ))
            else:
                # 非数字字符串 → task title 引用
                result.append(DependencyRef(
                    raw=stripped,
                    ref_type=DependencyType.TASK_TITLE,
                    title=stripped,
                ))
            continue

        # 字典、列表、浮点数、布尔值等 → INVALID
        result.append(DependencyRef(
            raw=raw,
            ref_type=DependencyType.INVALID,
        ))

    return result


class TaskScheduler:
    """任务调度器"""

    # lease 有效期
    DEFAULT_LEASE_SECONDS = 3600

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def find_runnable_tasks(self, project_id: int) -> List[SchedulableTask]:
        """
        查找指定项目下所有可执行的任务。

        条件：
          1. status = 'pending'
          2. readiness_status = 'ready'   <-- 关键安全门
          3. 依赖全部 completed
          4. 没有有效 lease
          5. 项目未暂停
          6. 任务字段完整
          7. 允许自动执行（task_type != 'manual'）

        排序：priority DESC → sort_order ASC → id ASC
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 检查项目是否暂停
            cur.execute(
                "SELECT status FROM projects WHERE id = ?", (project_id,)
            )
            proj = cur.fetchone()
            if not proj:
                return []
            if proj["status"] == "paused":
                return []

            # 获取所有 status=pending 且 readiness_status='ready' 的任务
            cur.execute("""
                SELECT id, project_id, title, priority, sort_order, status,
                       dependencies, files_to_modify, files_to_check,
                       codex_prompt, implementation_steps, test_steps, task_type,
                       COALESCE(readiness_status,'draft') as readiness_status,
                       COALESCE(acceptance_criteria,'') as acceptance_criteria
                FROM development_tasks
                WHERE project_id = ?
                AND status = 'pending'
                AND readiness_status = 'ready'
                ORDER BY priority DESC, sort_order ASC, id ASC
            """, (project_id,))

            candidates = []
            for row in cur.fetchall():
                task = SchedulableTask(
                    id=row["id"],
                    project_id=row["project_id"],
                    title=row["title"],
                    priority=row["priority"] or 0,
                    sort_order=row["sort_order"] or 0,
                    status=row["status"],
                    dependencies=self._parse_deps(row["dependencies"]),
                    files_to_modify=self._parse_files(row["files_to_modify"]),
                    files_to_check=self._parse_files(row["files_to_check"]),
                    codex_prompt=row["codex_prompt"] or "",
                    implementation_steps=row["implementation_steps"] or "",
                    test_steps=row["test_steps"] or "",
                    task_type=row["task_type"] or "code",
                    acceptance_criteria=row["acceptance_criteria"] or "",
                )
                candidates.append(task)

            # 过滤：检查依赖、lease、字段完整性
            runnable = []
            for task in candidates:
                if not self._is_runnable(conn, task):
                    continue
                runnable.append(task)

            return runnable
        finally:
            conn.close()

    def _is_runnable(self, conn, task: SchedulableTask) -> bool:
        """检查单个任务是否可执行"""
        # 1. 依赖检查：所有依赖必须是 completed
        if task.dependencies:
            if not self._check_dependencies(conn, task):
                return False

        # 2. Lease 检查：没有有效 lease
        if self._has_active_lease(conn, task.id):
            return False

        # 3. 字段完整性：至少要有 files_to_modify
        if not task.files_to_modify:
            return False

        # 4. task_type 允许自动执行
        if task.task_type == "manual":
            return False

        return True

    def _check_dependencies(self, conn, task: SchedulableTask) -> bool:
        """
        V1.8: 检查依赖是否全部 completed。
        依赖已通过 normalize_dependencies 标准化为 DependencyRef 列表。
        """
        cur = conn.cursor()

        for dep_ref in task.dependencies:
            if dep_ref.ref_type == DependencyType.INVALID:
                # 非法依赖格式 → blocked
                return False

            if dep_ref.ref_type == DependencyType.TASK_ID:
                cur.execute(
                    "SELECT status FROM development_tasks WHERE id = ?",
                    (dep_ref.task_id,)
                )
                row = cur.fetchone()
                if not row:
                    # 依赖 ID 不存在 → blocked
                    return False
                if row["status"] != "completed":
                    return False

            elif dep_ref.ref_type == DependencyType.TASK_TITLE:
                # 按 title 精确匹配，同一 project_id 内
                cur.execute(
                    "SELECT status, COUNT(*) as cnt FROM development_tasks "
                    "WHERE project_id = ? AND title = ?",
                    (task.project_id, dep_ref.title)
                )
                row = cur.fetchone()
                if not row or row["cnt"] == 0:
                    # 标题不存在 → blocked
                    return False
                if row["cnt"] > 1:
                    # 同名标题多个 → AMBIGUOUS_DEPENDENCY_TITLE → blocked
                    return False
                if row["status"] != "completed":
                    return False

        return True

    def _has_active_lease(self, conn, task_id: int) -> bool:
        """检查任务是否有有效 lease"""
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM task_leases
            WHERE task_id = ?
            AND status = 'active'
            AND expires_at > datetime('now')
        """, (task_id,))
        return cur.fetchone() is not None

    def _parse_deps(self, deps_raw) -> List[DependencyRef]:
        """V1.8: 解析依赖 JSON 并标准化为 DependencyRef 列表"""
        raw_list = self._parse_deps_raw(deps_raw)
        return normalize_dependencies(raw_list)

    @staticmethod
    def _parse_deps_raw(deps_raw) -> List[Any]:
        """解析原始依赖 JSON 为 Python 列表"""
        if not deps_raw:
            return []
        try:
            deps = json.loads(deps_raw)
            if isinstance(deps, list):
                return deps
            return []
        except (json.JSONDecodeError, TypeError):
            return []

    def _parse_files(self, files_raw) -> List[str]:
        """解析文件列表 JSON"""
        if not files_raw:
            return []
        try:
            files = json.loads(files_raw)
            if isinstance(files, list):
                return files
            return []
        except (json.JSONDecodeError, TypeError):
            return []

    def claim_task(self, task_id: int, worker_id: str,
                   lease_seconds: int = None) -> bool:
        """
        原子领取任务（创建 lease）。
        检查任务状态为 pending 且无活跃 lease。
        """
        lease_seconds = lease_seconds or self.DEFAULT_LEASE_SECONDS
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # 检查是否已被领取
            cur.execute("""
                SELECT id FROM task_leases
                WHERE task_id = ?
                AND status = 'active'
                AND expires_at > datetime('now')
            """, (task_id,))
            if cur.fetchone():
                conn.rollback()
                return False

            # 检查任务状态
            cur.execute(
                "SELECT status FROM development_tasks WHERE id = ?",
                (task_id,)
            )
            row = cur.fetchone()
            if not row or row["status"] != "pending":
                conn.rollback()
                return False

            # 创建 lease
            cur.execute("""
                INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
                VALUES (?, ?, 'active', datetime('now'), datetime('now', ?))
            """, (task_id, worker_id, f"+{lease_seconds} seconds"))

            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    def release_lease(self, task_id: int):
        """释放任务租约"""
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE task_leases
                SET status='released', released_at=datetime('now')
                WHERE task_id=? AND status='active'
            """, (task_id,))
            conn.commit()
        finally:
            conn.close()

    def get_queue_status(self, project_id: int) -> Dict[str, Any]:
        """
        获取项目队列状态。
        返回：
          - pending_count: 待执行任务数
          - runnable_count: 当前可执行任务数
          - blocked_count: 阻塞任务数
          - completed_count: 已完成任务数
          - total_count: 总任务数
          - runnable_tasks: 可执行任务列表
          - blocked_tasks: 阻塞任务列表（含阻塞原因）
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 各状态计数
            cur.execute("""
                SELECT status, COUNT(*) as cnt
                FROM development_tasks
                WHERE project_id = ?
                GROUP BY status
            """, (project_id,))
            status_counts = {row["status"]: row["cnt"] for row in cur.fetchall()}

            # 可执行任务
            runnable = self.find_runnable_tasks(project_id)

            # 阻塞任务分析（含完整字段诊断 + readiness_status）
            cur.execute("""
                SELECT id, title, status, dependencies, files_to_modify,
                       task_type, codex_prompt, test_steps,
                       acceptance_criteria, implementation_steps,
                       COALESCE(readiness_status,'draft') as readiness_status
                FROM development_tasks
                WHERE project_id = ?
                AND status = 'pending'
                ORDER BY priority DESC, sort_order ASC, id ASC
            """, (project_id,))

            blocked_tasks = []
            for row in cur.fetchall():
                deps = self._parse_deps(row["dependencies"])
                reasons = []

                # 0. readiness_status 检查（最高优先级）
                if row["readiness_status"] != "ready":
                    if row["readiness_status"] == "draft":
                        reasons.append("尚未完成工程规划")
                    elif row["readiness_status"] == "needs_planning":
                        reasons.append("尚未完成工程规划")
                    else:
                        reasons.append(f"任务准备状态异常: {row['readiness_status']}")

                # 1. 依赖检查 (V1.8: 使用标准化 DependencyRef)
                if deps:
                    for dep_ref in deps:
                        if dep_ref.ref_type == DependencyType.INVALID:
                            reasons.append(f"非法依赖格式: {dep_ref.raw}")
                            continue
                        if dep_ref.ref_type == DependencyType.TASK_ID:
                            cur.execute(
                                "SELECT status FROM development_tasks WHERE id=?",
                                (dep_ref.task_id,)
                            )
                            dep_row = cur.fetchone()
                            if not dep_row:
                                reasons.append(f"依赖ID不存在: {dep_ref.task_id}")
                            elif dep_row["status"] != "completed":
                                reasons.append(f"依赖未完成: #{dep_ref.task_id} ({dep_row['status']})")
                        elif dep_ref.ref_type == DependencyType.TASK_TITLE:
                            cur.execute(
                                "SELECT status, COUNT(*) as cnt FROM development_tasks "
                                "WHERE project_id=? AND title=?",
                                (project_id, dep_ref.title)
                            )
                            dep_row = cur.fetchone()
                            if not dep_row or dep_row["cnt"] == 0:
                                reasons.append(f"依赖标题不存在: {dep_ref.title}")
                            elif dep_row["cnt"] > 1:
                                reasons.append(f"AMBIGUOUS_DEPENDENCY_TITLE: {dep_ref.title}")
                            elif dep_row["status"] != "completed":
                                reasons.append(f"依赖未完成: {dep_ref.title} ({dep_row['status']})")

                # 2. Lease检查
                if self._has_active_lease(conn, row["id"]):
                    reasons.append("已有活跃lease")

                # 3. files_to_modify检查
                ftm = row["files_to_modify"]
                if not ftm or ftm in ("[]", "null", ""):
                    reasons.append("缺少修改文件列表")

                # 4. task_type检查
                if row["task_type"] == "manual":
                    reasons.append("需要人工审批")

                # 5. 工程字段完整性检查
                if not row["codex_prompt"] or row["codex_prompt"] in ("[]", "null", ""):
                    reasons.append("缺少AI提示词")
                if not row["test_steps"] or row["test_steps"] in ("[]", "null", ""):
                    reasons.append("缺少测试方案")
                if not row["acceptance_criteria"] or row["acceptance_criteria"] in ("[]", "null", ""):
                    reasons.append("缺少验收标准")
                if not row["implementation_steps"] or row["implementation_steps"] in ("[]", "null", ""):
                    reasons.append("缺少实现步骤")

                if reasons:
                    blocked_tasks.append({
                        "id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "readiness_status": row["readiness_status"],
                        "blocked_reasons": reasons,
                    })

            return {
                "pending_count": status_counts.get("pending", 0),
                "runnable_count": len(runnable),
                "blocked_count": status_counts.get("blocked", 0),
                "completed_count": status_counts.get("completed", 0),
                "total_count": sum(status_counts.values()),
                "runnable_tasks": [
                    {"id": t.id, "title": t.title, "priority": t.priority,
                     "sort_order": t.sort_order}
                    for t in runnable
                ],
                "blocked_tasks": blocked_tasks,
            }
        finally:
            conn.close()
