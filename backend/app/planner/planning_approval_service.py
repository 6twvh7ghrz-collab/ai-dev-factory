"""
PlanningApprovalService V1.6 - 规划审批与安全写回（风险等级与审批权限解耦）

职责：
  1. 审批预检（不修改数据）
  2. 安全审批（事务化写回）
  3. 一次性确认令牌管理
  4. 快照校验
  5. 基于审批权限的风险分级写回（V1.6：MEDIUM可经确认写回ready）

禁止：
  - 自动启动 Executor
  - 自动调用 Worker
  - 自动修改项目代码
  - 高风险任务强制转 ready
  - 删除任务
  - 部分写入
"""
import json
import uuid
import hashlib
import sqlite3
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass

from app.planner.planning_risk_policy import (
    assess_risk, RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_BLOCKED,
    is_approvable, can_write_ready, can_write_ready_v16, max_risk,
    POLICY_VERSION,
)

logger = logging.getLogger("planner.approval")

# ── 常量 ──

CONFIRMATION_TOKEN_TTL_SECONDS = 60  # 确认令牌有效期

# ── 确认令牌管理 ──

@dataclass
class ConfirmationToken:
    token: str
    project_id: int
    preview_id: str
    selected_task_ids_hash: str
    intent: str
    created_at: float
    expires_at: float
    used: bool = False


# 内存中的令牌存储（服务重启后清空）
_token_store: Dict[str, ConfirmationToken] = {}
_token_lock = threading.Lock()


class ConfirmationTokenManager:
    """一次性确认令牌管理器"""

    @staticmethod
    def generate(
        project_id: int,
        preview_id: str,
        selected_task_ids: List[int],
        intent: str = "approve_plan",
    ) -> str:
        """生成一次性确认令牌"""
        token = str(uuid.uuid4())
        sorted_ids = sorted(selected_task_ids)
        ids_hash = hashlib.sha256(
            json.dumps(sorted_ids, sort_keys=True).encode()
        ).hexdigest()

        now = time.time()
        ct = ConfirmationToken(
            token=token,
            project_id=project_id,
            preview_id=preview_id,
            selected_task_ids_hash=ids_hash,
            intent=intent,
            created_at=now,
            expires_at=now + CONFIRMATION_TOKEN_TTL_SECONDS,
        )

        with _token_lock:
            _token_store[token] = ct
            # 清理过期令牌
            expired = [k for k, v in _token_store.items() if v.expires_at < now]
            for k in expired:
                del _token_store[k]

        return token

    @staticmethod
    def validate_and_consume(
        token: str,
        project_id: int,
        preview_id: str,
        selected_task_ids: List[int],
    ) -> Optional[str]:
        """验证并消耗一次性令牌。返回 None 表示通过，否则返回错误信息。"""
        with _token_lock:
            ct = _token_store.get(token)
            if ct is None:
                return "INVALID_CONFIRMATION_TOKEN"

            if ct.used:
                return "INVALID_CONFIRMATION_TOKEN"

            if time.time() > ct.expires_at:
                del _token_store[token]
                return "INVALID_CONFIRMATION_TOKEN"

            if ct.project_id != project_id:
                return "INVALID_CONFIRMATION_TOKEN"

            if ct.preview_id != preview_id:
                return "INVALID_CONFIRMATION_TOKEN"

            sorted_ids = sorted(selected_task_ids)
            ids_hash = hashlib.sha256(
                json.dumps(sorted_ids, sort_keys=True).encode()
            ).hexdigest()
            if ct.selected_task_ids_hash != ids_hash:
                return "INVALID_CONFIRMATION_TOKEN"

            if ct.intent != "approve_plan":
                return "INVALID_CONFIRMATION_TOKEN"

            # 消耗令牌
            ct.used = True
            del _token_store[token]
            return None


# ── 数据类（已在上方导入 dataclass）──




# ── 主服务 ──

class PlanningApprovalService:
    """规划审批与安全写回服务"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._token_manager = ConfirmationTokenManager()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ── 快照计算 ──

    @staticmethod
    def _compute_tasks_snapshot(tasks: List[dict]) -> str:
        """计算任务快照哈希"""
        snapshots = []
        for t in tasks:
            snapshot = {
                "task_id": t.get("id") or t.get("task_id"),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "readiness_status": t.get("readiness_status", ""),
                "dependencies": t.get("dependencies", ""),
                "files_to_modify": t.get("files_to_modify", ""),
                "implementation_steps": t.get("implementation_steps", ""),
                "test_steps": t.get("test_steps", ""),
                "acceptance_criteria": t.get("acceptance_criteria", ""),
            }
            if "updated_at" in t:
                snapshot["updated_at"] = str(t["updated_at"])
            snapshots.append(snapshot)
        serialized = json.dumps(snapshots, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # ── 审批预检 ──

    def preview_approval(
        self,
        project_id: int,
        preview_id: str,
        selected_task_ids: List[int],
    ) -> Dict[str, Any]:
        """审批预检 - 不修改任何数据库数据"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 1. 获取规划预览
            cur.execute(
                "SELECT * FROM planning_previews WHERE preview_id = ?",
                (preview_id,),
            )
            preview_row = cur.fetchone()
            if not preview_row:
                return {"ok": False, "code": "PLAN_NOT_FOUND", "message": "规划预览不存在"}

            # 2. 检查预览属于该项目
            if preview_row["project_id"] != project_id:
                return {"ok": False, "code": "PLAN_PROJECT_MISMATCH", "message": "规划不属于该项目"}

            # 3. 检查预览状态
            if preview_row["status"] not in ("generated", "partially_approved"):
                return {
                    "ok": False,
                    "code": f"PLAN_{preview_row['status'].upper()}",
                    "message": f"规划状态为 {preview_row['status']}，不允许审批",
                }

            # 4. 检查预览是否过期
            expires_at = preview_row["expires_at"]
            if expires_at:
                try:
                    exp = datetime.fromisoformat(expires_at)
                    if exp < datetime.now():
                        # 标记为过期
                        conn.execute(
                            "UPDATE planning_previews SET status='expired', updated_at=? WHERE preview_id=?",
                            (datetime.now().isoformat(), preview_id),
                        )
                        conn.commit()
                        return {"ok": False, "code": "PLAN_EXPIRED", "message": "规划预览已过期"}
                except (ValueError, TypeError):
                    pass

            # 5. 解析预览 JSON
            try:
                preview_data = json.loads(preview_row["preview_json"])
            except json.JSONDecodeError:
                return {"ok": False, "code": "PLAN_CORRUPTED", "message": "规划数据损坏"}

            # 6. 检查 selected_task_ids 都在预览中
            preview_task_ids = {t.get("task_id") for t in preview_data.get("tasks", [])}
            for tid in selected_task_ids:
                if tid not in preview_task_ids:
                    return {
                        "ok": False,
                        "code": "TASK_NOT_IN_PLAN",
                        "message": f"任务 #{tid} 不在规划中",
                    }

            # 7. 检查重复
            if len(selected_task_ids) != len(set(selected_task_ids)):
                return {"ok": False, "code": "DUPLICATE_TASK_IDS", "message": "任务 ID 重复"}

            # 8. 获取当前任务状态
            placeholders = ",".join("?" * len(selected_task_ids))
            cur.execute(
                f"""SELECT id, title, description, status, readiness_status,
                           dependencies, files_to_modify, implementation_steps,
                           test_steps, acceptance_criteria, updated_at
                    FROM development_tasks
                    WHERE project_id = ? AND id IN ({placeholders})
                    ORDER BY id""",
                (project_id, *selected_task_ids),
            )
            current_tasks = [dict(row) for row in cur.fetchall()]

            if len(current_tasks) != len(selected_task_ids):
                found_ids = {t["id"] for t in current_tasks}
                missing = set(selected_task_ids) - found_ids
                return {
                    "ok": False,
                    "code": "TASK_NOT_FOUND",
                    "message": f"任务不存在: {missing}",
                }

            # 9. 检查任务当前状态
            for t in current_tasks:
                if t["status"] in ("completed", "cancelled"):
                    return {
                        "ok": False,
                        "code": "TASK_STATE_CHANGED",
                        "message": f"任务 #{t['id']} 状态为 {t['status']}，不可审批",
                    }
                if t["status"] == "executing":
                    return {
                        "ok": False,
                        "code": "TASK_STATE_CHANGED",
                        "message": f"任务 #{t['id']} 正在执行中，不可审批",
                    }
                if t["readiness_status"] != "needs_planning":
                    return {
                        "ok": False,
                        "code": "TASK_STATE_CHANGED",
                        "message": f"任务 #{t['id']} readiness_status 为 {t['readiness_status']}，不是 needs_planning",
                    }

            # 10. 检查活跃 run/lease/lock
            cur.execute(
                "SELECT COUNT(*) as cnt FROM executor_runs WHERE status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')"
            )
            if cur.fetchone()["cnt"] > 0:
                return {"ok": False, "code": "ACTIVE_RUN_EXISTS", "message": "存在活跃的 executor_run"}

            cur.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'")
            if cur.fetchone()["cnt"] > 0:
                return {"ok": False, "code": "ACTIVE_LEASE_EXISTS", "message": "存在活跃的 task_lease"}

            cur.execute("SELECT COUNT(*) as cnt FROM executor_resource_locks WHERE status='active'")
            if cur.fetchone()["cnt"] > 0:
                return {"ok": False, "code": "RESOURCE_CONFLICT", "message": "存在活跃的资源锁"}

            # 11. 快照一致性检查
            stored_tasks_hash = preview_row["tasks_snapshot_hash"]
            current_tasks_hash = self._compute_tasks_snapshot(current_tasks)
            if stored_tasks_hash and stored_tasks_hash != current_tasks_hash:
                return {
                    "ok": False,
                    "code": "PLAN_SNAPSHOT_CHANGED",
                    "message": "任务快照已变化，规划可能已过期，请重新生成规划",
                }

            # 12. 风险分级（V1.6 结构化）
            safe_tasks = []
            medium_risk_tasks = []
            high_risk_tasks = []
            blocked_tasks = []

            for t in current_tasks:
                tid = t["id"]
                # 从预览中获取任务信息
                preview_task = next(
                    (pt for pt in preview_data.get("tasks", []) if pt.get("task_id") == tid),
                    {},
                )
                files_suggestion = preview_task.get("files_to_modify_suggestion", [])

                # V1.8: 从 preview task 中读取显式 risk_level，而非根据 requires_approval 二值推断
                declared_risk = preview_task.get("risk_level", "").upper()
                if declared_risk not in ("LOW", "MEDIUM", "HIGH", "BLOCKED"):
                    # 回退兼容: 如果旧 preview 无 risk_level 字段，使用 requires_approval 推断
                    # requires_approval=true 且无 risk_level → 保守推断 MEDIUM
                    if preview_task.get("requires_approval", False):
                        declared_risk = "MEDIUM"
                    else:
                        declared_risk = "LOW"

                risk_result = assess_risk(
                    task_id=tid,
                    title=t["title"] or "",
                    description=t["description"] or "",
                    files_to_modify=files_suggestion,
                    implementation_strategy=preview_task.get("implementation_strategy", ""),
                    model_risk=declared_risk,
                )

                task_info = {
                    "task_id": tid,
                    "title": t["title"],
                    "risk_level": risk_result["risk_level"],
                    "reasons": risk_result["reasons"],
                    "allow_auto_ready": risk_result["allow_auto_ready"],
                    # V1.6 新增
                    "risk_signals": risk_result.get("risk_signals", []),
                    "risk_reason": risk_result.get("risk_reason", ""),
                    "approval_requirement": risk_result.get("approval_requirement", ""),
                    "auto_approvable": risk_result.get("auto_approvable", False),
                    "user_approvable": risk_result.get("user_approvable", False),
                    "can_write_ready_after_approval": risk_result.get("can_write_ready_after_approval", False),
                    "policy_version": risk_result.get("policy_version", ""),
                    "fields_to_write": {
                        "implementation_steps": json.dumps(
                            preview_task.get("implementation_strategy", "").split("\n")
                            if isinstance(preview_task.get("implementation_strategy"), str)
                            else preview_task.get("implementation_strategy", [])
                        ),
                        "files_to_modify": json.dumps(files_suggestion),
                        "test_steps": json.dumps(preview_task.get("test_strategy", [])),
                        "acceptance_criteria": json.dumps(
                            t.get("acceptance_criteria", "")
                            if t.get("acceptance_criteria")
                            else preview_task.get("implementation_strategy", "")[:200]
                        ),
                        "dependencies": t.get("dependencies", "[]"),
                        "readiness_status": "ready" if risk_result["allow_auto_ready"] else "needs_planning",
                    },
                }

                if risk_result["risk_level"] == RISK_BLOCKED:
                    blocked_tasks.append(task_info)
                elif risk_result["risk_level"] == RISK_HIGH:
                    high_risk_tasks.append(task_info)
                elif risk_result["risk_level"] == RISK_MEDIUM:
                    medium_risk_tasks.append(task_info)
                else:
                    safe_tasks.append(task_info)

            # 13. 生成确认令牌
            token = ConfirmationTokenManager.generate(
                project_id=project_id,
                preview_id=preview_id,
                selected_task_ids=selected_task_ids,
            )

            # V1.6: 可写回 ready 的任务（LOW + MEDIUM，但 MEDIUM 需确认）
            can_write_ready_ids = [
                t["task_id"] for t in safe_tasks + medium_risk_tasks
                if t.get("can_write_ready_after_approval", False)
            ]

            writeback_preview = {
                "will_write_ready": len(can_write_ready_ids),
                "will_keep_needs_planning": len(high_risk_tasks) + len(blocked_tasks),
                "safe_task_ids": [t["task_id"] for t in safe_tasks],
                "medium_risk_task_ids": [t["task_id"] for t in medium_risk_tasks],
                "high_risk_task_ids": [t["task_id"] for t in high_risk_tasks],
                "blocked_task_ids": [t["task_id"] for t in blocked_tasks],
                "can_write_ready_ids": can_write_ready_ids,
                "medium_risk_requires_explicit_ack": len(medium_risk_tasks) > 0,
                "will_not_start_executor": True,
                "will_not_modify_source": True,
                "policy_version": POLICY_VERSION,
            }

            return {
                "ok": True,
                "code": "APPROVAL_PREVIEW_READY",
                "confirmation_token": token,
                "expires_in": CONFIRMATION_TOKEN_TTL_SECONDS,
                "safe_tasks": safe_tasks,
                "medium_risk_tasks": medium_risk_tasks,
                "high_risk_tasks": high_risk_tasks,
                "blocked_tasks": blocked_tasks,
                "writeback_preview": writeback_preview,
            }

        finally:
            conn.close()

    # ── 正式审批 ──

    def approve(
        self,
        project_id: int,
        preview_id: str,
        selected_task_ids: List[int],
        confirmation_token: str,
        approval_mode: str = "selected_tasks",
        approved_by: str = "user",
        risk_acknowledged: bool = False,
        approval_reason: str = "",
    ) -> Dict[str, Any]:
        """正式审批并安全写回任务（V1.6：支持 MEDIUM 风险经确认写回 ready）

        Args:
            project_id: 项目 ID
            preview_id: 规划预览 ID
            selected_task_ids: 选中的任务 ID 列表
            confirmation_token: 确认令牌
            approval_mode: 审批模式 (selected_tasks/all_tasks)
            approved_by: 审批人 (user/system)
            risk_acknowledged: 是否已明确确认风险（MEDIUM 任务必须为 True）
            approval_reason: 审批原因（MEDIUM 任务必须非空）
        """
        # 1. 验证确认令牌
        token_error = ConfirmationTokenManager.validate_and_consume(
            token=confirmation_token,
            project_id=project_id,
            preview_id=preview_id,
            selected_task_ids=selected_task_ids,
        )
        if token_error:
            return {"ok": False, "code": token_error, "message": "确认令牌无效或已过期"}

        # 2. 重新执行预检
        preview_result = self.preview_approval(project_id, preview_id, selected_task_ids)
        if not preview_result.get("ok"):
            return preview_result

        safe_tasks = preview_result.get("safe_tasks", [])
        medium_risk_tasks = preview_result.get("medium_risk_tasks", [])
        high_risk_tasks = preview_result.get("high_risk_tasks", [])
        blocked_tasks = preview_result.get("blocked_tasks", [])

        # V1.6: 基于审批权限矩阵判断可写回任务
        # LOW → 可直接写回
        # MEDIUM → 需 approval_mode=selected_tasks + approved_by=user + risk_acknowledged=True + approval_reason非空
        # HIGH/BLOCKED → 不可写回

        # 检查 MEDIUM 任务的审批条件
        medium_ids = [t["task_id"] for t in medium_risk_tasks]
        if medium_ids:
            if approval_mode != "selected_tasks":
                return {
                    "ok": False,
                    "code": "INVALID_APPROVER",
                    "message": f"MEDIUM 风险任务 {medium_ids} 需要 selected_tasks 审批模式，当前为 {approval_mode}",
                    "approved_task_ids": [],
                    "kept_needs_planning_task_ids": medium_ids + [t["task_id"] for t in high_risk_tasks + blocked_tasks],
                }
            if approved_by != "user":
                return {
                    "ok": False,
                    "code": "INVALID_APPROVER",
                    "message": f"MEDIUM 风险任务 {medium_ids} 需要用户审批，当前审批人为 {approved_by}",
                    "approved_task_ids": [],
                    "kept_needs_planning_task_ids": medium_ids + [t["task_id"] for t in high_risk_tasks + blocked_tasks],
                }
            if not risk_acknowledged:
                return {
                    "ok": False,
                    "code": "MEDIUM_RISK_ACK_REQUIRED",
                    "message": f"MEDIUM 风险任务 {medium_ids} 需要明确风险确认 (risk_acknowledged=true)",
                    "approved_task_ids": [],
                    "kept_needs_planning_task_ids": medium_ids + [t["task_id"] for t in high_risk_tasks + blocked_tasks],
                }
            if not approval_reason or not approval_reason.strip():
                return {
                    "ok": False,
                    "code": "APPROVAL_REASON_REQUIRED",
                    "message": f"MEDIUM 风险任务 {medium_ids} 需要提供审批原因 (approval_reason 不能为空)",
                    "approved_task_ids": [],
                    "kept_needs_planning_task_ids": medium_ids + [t["task_id"] for t in high_risk_tasks + blocked_tasks],
                }

        # 使用 V1.6 权限决策确定可写回的任务
        writable_medium_ids = []
        for t in medium_risk_tasks:
            if can_write_ready_v16(t["risk_level"], approval_mode, approved_by, risk_acknowledged, approval_reason):
                writable_medium_ids.append(t["task_id"])

        approved_task_ids = [t["task_id"] for t in safe_tasks] + writable_medium_ids
        kept_needs_planning_ids = [t["task_id"] for t in high_risk_tasks + blocked_tasks]

        if not approved_task_ids:
            return {
                "ok": False,
                "code": "NO_APPROVABLE_TASKS",
                "message": "选中的任务中没有可转为 ready 的任务",
                "approved_task_ids": [],
                "kept_needs_planning_task_ids": kept_needs_planning_ids,
            }

        # 3. 事务化写回（V1.6：safe_tasks + writable medium_tasks）
        all_writable = safe_tasks + [t for t in medium_risk_tasks if t["task_id"] in writable_medium_ids]

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cur = conn.cursor()

            # 重新读取任务状态（事务内二次确认）
            placeholders = ",".join("?" * len(selected_task_ids))
            cur.execute(
                f"""SELECT id, title, status, readiness_status
                    FROM development_tasks
                    WHERE project_id = ? AND id IN ({placeholders})
                    ORDER BY id""",
                (project_id, *selected_task_ids),
            )
            tasks_before = [dict(row) for row in cur.fetchall()]
            before_snapshot = json.dumps(tasks_before, ensure_ascii=False, default=str)

            # 记录 before 快照
            before_snapshot_hash = self._compute_tasks_snapshot(tasks_before)

            # 写回所有可写回任务（LOW + 已确认 MEDIUM）
            for task_info in all_writable:
                tid = task_info["task_id"]
                fields = task_info["fields_to_write"]

                # 验证所有任务仍在事务内
                cur.execute(
                    "SELECT id, status, readiness_status FROM development_tasks WHERE id = ?",
                    (tid,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "TASK_NOT_FOUND",
                        "message": f"任务 #{tid} 在事务中消失",
                    }
                if row["status"] in ("completed", "cancelled", "executing"):
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "TASK_STATE_CHANGED",
                        "message": f"任务 #{tid} 状态已变更为 {row['status']}",
                    }
                if row["readiness_status"] != "needs_planning":
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "TASK_STATE_CHANGED",
                        "message": f"任务 #{tid} readiness_status 已变更",
                    }

                # 验证写回字段安全性
                files_str = fields.get("files_to_modify", "[]")
                try:
                    files_list = json.loads(files_str) if isinstance(files_str, str) else files_str
                except (json.JSONDecodeError, TypeError):
                    files_list = []

                # 验证文件路径安全
                for f in files_list:
                    if not isinstance(f, str) or not f:
                        conn.rollback()
                        return {"ok": False, "code": "INVALID_FILE_PATH", "message": f"任务 #{tid} 文件路径为空"}
                    if f.startswith("/") or (len(f) >= 2 and f[1] == ":"):
                        conn.rollback()
                        return {"ok": False, "code": "INVALID_FILE_PATH", "message": f"任务 #{tid} 文件路径为绝对路径: {f}"}
                    if ".." in f:
                        conn.rollback()
                        return {"ok": False, "code": "INVALID_FILE_PATH", "message": f"任务 #{tid} 文件路径穿越: {f}"}

                # 验证必要字段非空
                test_steps_str = fields.get("test_steps", "[]")
                try:
                    test_steps = json.loads(test_steps_str) if isinstance(test_steps_str, str) else test_steps_str
                except (json.JSONDecodeError, TypeError):
                    test_steps = []
                if not test_steps:
                    conn.rollback()
                    return {"ok": False, "code": "EMPTY_TEST_STEPS", "message": f"任务 #{tid} test_steps 为空"}

                acceptance_str = fields.get("acceptance_criteria", "")
                if not acceptance_str:
                    conn.rollback()
                    return {"ok": False, "code": "EMPTY_ACCEPTANCE_CRITERIA", "message": f"任务 #{tid} acceptance_criteria 为空"}

                # 执行写回
                cur.execute(
                    """UPDATE development_tasks
                       SET implementation_steps = ?,
                           files_to_modify = ?,
                           test_steps = ?,
                           acceptance_criteria = ?,
                           dependencies = ?,
                           readiness_status = 'ready',
                           updated_at = ?
                       WHERE id = ? AND project_id = ?""",
                    (
                        fields.get("implementation_steps", "[]"),
                        fields.get("files_to_modify", "[]"),
                        fields.get("test_steps", "[]"),
                        fields.get("acceptance_criteria", ""),
                        fields.get("dependencies", "[]"),
                        datetime.now().isoformat(),
                        tid,
                        project_id,
                    ),
                )

            # 读取 after 快照
            cur.execute(
                f"""SELECT id, title, status, readiness_status
                    FROM development_tasks
                    WHERE project_id = ? AND id IN ({placeholders})
                    ORDER BY id""",
                (project_id, *selected_task_ids),
            )
            tasks_after = [dict(row) for row in cur.fetchall()]
            after_snapshot = json.dumps(tasks_after, ensure_ascii=False, default=str)

            # 创建审批记录（V1.6：包含风险确认信息）
            approval_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO planning_approvals
                   (approval_id, preview_id, project_id,
                    approved_task_ids_json, rejected_task_ids_json, skipped_task_ids_json,
                    approval_mode, approval_summary_json,
                    before_snapshot_json, after_snapshot_json, approved_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id,
                    preview_id,
                    project_id,
                    json.dumps(approved_task_ids),
                    json.dumps([]),
                    json.dumps(kept_needs_planning_ids),
                    approval_mode,
                    json.dumps({
                        "safe_count": len(safe_tasks),
                        "medium_risk_count": len(medium_risk_tasks),
                        "medium_written_count": len(writable_medium_ids),
                        "high_risk_count": len(high_risk_tasks),
                        "blocked_count": len(blocked_tasks),
                        "risk_acknowledged": risk_acknowledged,
                        "approved_by": approved_by,
                        "approval_reason": approval_reason,
                        "policy_version": POLICY_VERSION,
                    }),
                    before_snapshot,
                    after_snapshot,
                    approved_by,
                    datetime.now().isoformat(),
                ),
            )

            # 更新规划预览状态
            new_status = "partially_approved" if kept_needs_planning_ids else "approved"
            cur.execute(
                """UPDATE planning_previews
                   SET status = ?, approved_at = ?, updated_at = ?
                   WHERE preview_id = ?""",
                (new_status, datetime.now().isoformat(), datetime.now().isoformat(), preview_id),
            )

            conn.commit()

            # 调用 Scheduler 检查可运行任务数
            runnable_count = 0
            try:
                from app.executor.task_scheduler import TaskScheduler
                scheduler = TaskScheduler(self.db_path)
                runnable = scheduler.find_runnable_tasks(project_id)
                runnable_count = len(runnable)
            except Exception as e:
                logger.warning(f"获取可运行任务数失败（非致命）: {e}")

            return {
                "ok": True,
                "code": "PLAN_PARTIALLY_APPROVED" if kept_needs_planning_ids else "PLAN_APPROVED",
                "approved_task_ids": approved_task_ids,
                "kept_needs_planning_task_ids": kept_needs_planning_ids,
                "runnable_count": runnable_count,
                "executed": False,
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"审批事务失败: {e}")
            return {"ok": False, "code": "APPROVAL_FAILED", "message": f"审批事务失败: {e}"}
        finally:
            conn.close()

    # ── 拒绝规划 ──

    def reject(self, project_id: int, preview_id: str) -> Dict[str, Any]:
        """拒绝规划预览（只改状态，不修改任务）"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM planning_previews WHERE preview_id = ? AND project_id = ?",
                (preview_id, project_id),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "code": "PLAN_NOT_FOUND", "message": "规划预览不存在"}

            if row["status"] not in ("generated", "partially_approved"):
                return {
                    "ok": False,
                    "code": f"PLAN_{row['status'].upper()}",
                    "message": f"规划状态为 {row['status']}，不可拒绝",
                }

            cur.execute(
                """UPDATE planning_previews
                   SET status = 'rejected', rejected_at = ?, updated_at = ?
                   WHERE preview_id = ?""",
                (datetime.now().isoformat(), datetime.now().isoformat(), preview_id),
            )
            conn.commit()
            return {"ok": True, "code": "PLAN_REJECTED", "message": "规划已拒绝"}
        finally:
            conn.close()

    # ── 获取规划预览 ──

    def get_preview(self, preview_id: str) -> Optional[Dict[str, Any]]:
        """获取规划预览详情"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM planning_previews WHERE preview_id = ?",
                (preview_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            preview_data = json.loads(row["preview_json"])
            return {
                "preview_id": row["preview_id"],
                "project_id": row["project_id"],
                "provider": row["provider"],
                "model": row["model"],
                "status": row["status"],
                "schema_version": row["schema_version"],
                "task_ids": json.loads(row["task_ids_json"]),
                "preview": preview_data,
                "risk_summary": json.loads(row["risk_summary_json"]),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "approved_at": row["approved_at"],
                "rejected_at": row["rejected_at"],
            }
        finally:
            conn.close()


# ── 全局单例 ──

_approval_service: Optional[PlanningApprovalService] = None


def get_planning_approval_service(db_path: str = None) -> PlanningApprovalService:
    """获取全局 PlanningApprovalService 单例"""
    global _approval_service
    if _approval_service is None:
        if db_path is None:
            db_path = str(
                Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db"
            )
        _approval_service = PlanningApprovalService(db_path)
    return _approval_service
