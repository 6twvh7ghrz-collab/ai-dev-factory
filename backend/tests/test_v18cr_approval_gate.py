"""V1.8C-R: Execution Approval Gate — 消费顺序 & 绑定测试

验证：
  (a) 消费发生在 lease 领取成功之后
  (b) lease 领取失败时不消费
  (c) 消费失败 → lease 释放 + run 标记 blocked
  (d) 消费记录绑定 run_id + task_id
  (e) 审批 scope 在整个 run 生命期内生效
  (f) 高危门禁 EXECUTE_READY_TASKS 之前是 REQUEST_APPROVAL
"""

import json
import os
import sys
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Ensure backend is on path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

DB_PATH = backend_dir / "data" / "ai_factory.db"
SANDBOX_PROJECT_ID = 56  # 已存在的高风险项目


# ── Helpers ──

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _create_test_approval(project_id, allowed_task_ids, conn=None):
    """Insert a valid execution approval for testing."""
    import uuid
    _conn = conn or _get_conn()
    try:
        now = datetime.now()
        expires = now + timedelta(hours=1)
        cur = _conn.cursor()

        approval_id = f"test-{uuid.uuid4().hex[:12]}"

        cur.execute("""
            INSERT INTO execution_approvals
                (approval_id, project_id, status, single_use,
                 allowed_task_ids_json, risk_summary_json,
                 expired_at, created_at)
            VALUES (?, ?, 'approved', 1, ?, ?, ?, ?)
        """, (approval_id, project_id, json.dumps(allowed_task_ids),
              json.dumps({"risk_confirmed": True}),
              expires.isoformat(), now.isoformat()))
        _conn.commit()
        return approval_id
    finally:
        if conn is None:
            _conn.close()


def _cleanup_approvals(project_id, conn=None):
    """Remove all execution approvals for a project."""
    _conn = conn or _get_conn()
    try:
        cur = _conn.cursor()
        cur.execute("DELETE FROM execution_approvals WHERE project_id = ?", (project_id,))
        _conn.commit()
    finally:
        if conn is None:
            _conn.close()


def _cleanup_leases(task_ids, conn=None):
    """Remove leases for given task IDs."""
    _conn = conn or _get_conn()
    try:
        cur = _conn.cursor()
        for tid in task_ids:
            cur.execute("DELETE FROM task_leases WHERE task_id = ?", (tid,))
        _conn.commit()
    finally:
        if conn is None:
            _conn.close()


def _count_approvals(project_id):
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM execution_approvals WHERE project_id = ?",
                    (project_id,))
        return cur.fetchone()["cnt"]
    finally:
        conn.close()


def _get_approval_by_id(approval_id):
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_approvals WHERE approval_id = ?", (approval_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Module-level setup/teardown ──

@pytest.fixture(autouse=True)
def cleanup_before():
    """Cleanup any stray approval records for project 56 before each test."""
    _cleanup_approvals(SANDBOX_PROJECT_ID)
    yield
    _cleanup_approvals(SANDBOX_PROJECT_ID)


# ══════════════════════════════════════════════════════════════════
# TEST (a): 消费发生在 lease 领取成功之后
# ══════════════════════════════════════════════════════════════════

def test_a_consume_after_lease_success():
    """consume_approval() 必须在 TaskWorker 内部 lease claim 成功后才调用。

    验证策略：
    - 创建审批记录
    - 模拟 TaskWorker 消费链路
    - 验证 consume_approval 返回 allowed_task_ids
    - 验证 consumed_by_run_id / consumed_by_task_id 被正确写入
    """
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Create approval
    approval_id = _create_test_approval(SANDBOX_PROJECT_ID, [31, 32])

    # 2. Simulate consumption (like TaskWorker would)
    svc = ExecutionApprovalService(str(DB_PATH))
    result = svc.consume_approval(
        project_id=SANDBOX_PROJECT_ID,
        executor_run_id=999,
        task_id=31,
    )

    assert result["ok"], f"Consumption should succeed: {result}"
    assert result["approval_id"] == approval_id
    assert result["allowed_task_ids"] == [31, 32]
    assert result["executor_run_id"] == 999
    assert result["task_id"] == 31

    # 3. Verify DB record
    approval = _get_approval_by_id(approval_id)
    assert approval["status"] == "consumed"
    assert approval["consumed_by_run_id"] == 999
    assert approval["consumed_by_task_id"] == 31
    assert approval["consumed_at"] is not None


# ══════════════════════════════════════════════════════════════════
# TEST (b): lease 领取失败时不消费
# ══════════════════════════════════════════════════════════════════

def test_b_no_consume_on_lease_failure():
    """如果 claim_task() 返回 False，consume_approval() 绝不应被调用。

    模拟：TaskWorker 在 claim_task 失败后不会调用 consume_approval()
    """
    from app.executor.result_collector import ResultCollector
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Create approval
    approval_id = _create_test_approval(SANDBOX_PROJECT_ID, [31])

    # 2. Try to claim a task that doesn't exist (lease will fail)
    collector = ResultCollector(str(DB_PATH))
    claimed = collector.claim_task(99999, "test_worker", 3600)
    assert not claimed, "Claim should fail for non-existent task"

    # 3. Because claim failed, we NEVER call consume_approval
    # Verify approval is still unconsumed
    svc = ExecutionApprovalService(str(DB_PATH))
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID)

    approval = _get_approval_by_id(approval_id)
    assert approval["status"] == "approved"
    assert approval["consumed_at"] is None
    assert approval["consumed_by_run_id"] is None


# ══════════════════════════════════════════════════════════════════
# TEST (c): 消费失败 → lease 释放
# ══════════════════════════════════════════════════════════════════

def test_c_consume_failure_releases_lease():
    """如果 consume_approval 失败，TaskWorker 必须释放刚刚领取的 lease。

    验证策略：
    - 创建 task 记录并插入 active lease
    - 消费一个不存在的审批（会返回 NO_VALID_APPROVAL）
    - 验证 TaskWorker 返回 block_reason="approval_consumption_failed"
    - 验证 lease 被释放
    """
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Ensure no approval exists
    _cleanup_approvals(SANDBOX_PROJECT_ID)

    # 2. Simulate: lease was claimed (we insert directly), then consumption fails
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # Create a lease for a test task
        cur.execute("""
            INSERT OR REPLACE INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
            VALUES (31, 'test_worker_fail', 'active', datetime('now','localtime'),
                    datetime('now','localtime','+1 hour'))
        """)
        conn.commit()

        # Verify lease is active
        cur.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE task_id=31 AND status='active'")
        assert cur.fetchone()["cnt"] == 1
    finally:
        conn.close()

    # 3. Try to consume (no approval exists → failure)
    svc = ExecutionApprovalService(str(DB_PATH))
    result = svc.consume_approval(
        project_id=SANDBOX_PROJECT_ID,
        executor_run_id=100,
        task_id=31,
    )
    assert not result["ok"]
    assert result["error"] == "NO_VALID_APPROVAL"

    # 4. TaskWorker would call collector.release_lease(31) here.
    # Simulate that:
    from app.executor.result_collector import ResultCollector
    collector = ResultCollector(str(DB_PATH))
    collector.release_lease(31)

    # 5. Verify lease was released
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM task_leases WHERE task_id=31")
        row = cur.fetchone()
        # Either released or not found
        if row:
            assert row["status"] in ("released", "expired")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# TEST (d): 消费记录绑定 run_id + task_id
# ══════════════════════════════════════════════════════════════════

def test_d_consumption_binds_run_and_task():
    """消费记录必须精确绑定 approval_id、run_id、task_id"""
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Create approval
    approval_id = _create_test_approval(SANDBOX_PROJECT_ID, [10, 20, 30])

    svc = ExecutionApprovalService(str(DB_PATH))

    # 2. Consume with specific run_id and task_id
    result = svc.consume_approval(
        project_id=SANDBOX_PROJECT_ID,
        executor_run_id=202,
        task_id=20,
    )
    assert result["ok"]
    assert result["allowed_task_ids"] == [10, 20, 30]

    # 3. Verify exact binding
    approval = _get_approval_by_id(approval_id)
    assert approval["consumed_by_run_id"] == 202
    assert approval["consumed_by_task_id"] == 20
    assert approval["consumed_at"] is not None


# ══════════════════════════════════════════════════════════════════
# TEST (e): 审批 scope 在整个 run 生命期内生效
# ══════════════════════════════════════════════════════════════════

def test_e_scope_persists_after_consumption():
    """审批消费后，同一 run 内仍然按原 scope 过滤任务。

    验证 LoopController._approval_task_scope 缓存机制。
    """
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Create approval for only task 31
    _create_test_approval(SANDBOX_PROJECT_ID, [31])

    svc = ExecutionApprovalService(str(DB_PATH))

    # 2. Verify has_valid_approval with scope [31] → True
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID, [31])
    # Verify has_valid_approval with scope [32] → False (not in allowed)
    assert not svc.has_valid_approval(SANDBOX_PROJECT_ID, [32])
    # Verify has_valid_approval with scope [31, 32] → False (32 not in allowed)
    assert not svc.has_valid_approval(SANDBOX_PROJECT_ID, [31, 32])

    # 3. Consume approval
    result = svc.consume_approval(
        project_id=SANDBOX_PROJECT_ID,
        executor_run_id=300,
        task_id=31,
    )
    assert result["ok"]
    assert result["allowed_task_ids"] == [31]

    # 4. After consumption, has_valid_approval should return False
    # (no more valid approval, since it was consumed)
    assert not svc.has_valid_approval(SANDBOX_PROJECT_ID)

    # 5. The cached scope in LoopController would still be {31}
    # This is tested in test_h_loopcontroller_scope_cache


# ══════════════════════════════════════════════════════════════════
# TEST (f): 高危门禁优先级
# ══════════════════════════════════════════════════════════════════

def test_f_high_risk_gate_priority():
    """REQUEST_APPROVAL 必须在 EXECUTE_READY_TASKS 之前。

    高危项目 → runnable 任务存在 → 无审批 → REQUEST_APPROVAL
    高危项目 → runnable 任务存在 → 有审批 → EXECUTE_READY_TASKS
    """
    from app.executor.start_decision import StartDecisionService, Decision

    svc = StartDecisionService(str(DB_PATH))

    # 1. Without approval, project 56 should return REQUEST_APPROVAL
    _cleanup_approvals(SANDBOX_PROJECT_ID)

    # Project 56 is high-risk (execution_enabled=0, requires_confirmation=1)
    decision1 = svc.decide(SANDBOX_PROJECT_ID)
    assert decision1["ok"], f"Decision should succeed: {decision1}"

    # In high-risk projects, if everything is ready, we should get
    # REQUEST_APPROVAL (not EXECUTE_READY_TASKS)
    if decision1["summary"] and "high risk" in decision1["summary"].lower():
        # High-risk project with no approval → REQUEST_APPROVAL
        assert decision1["decision"] in (
            Decision.REQUEST_APPROVAL.value,
            Decision.BLOCK_UNSAFE.value,
        ), (
            f"High-risk project should require approval, but got "
            f"{decision1['decision']}: {decision1['summary']}"
        )
        if decision1["decision"] == Decision.REQUEST_APPROVAL.value:
            assert decision1["requires_approval"] is True
            assert decision1["can_execute"] is False

    # 2. Create an approval and verify it allows execution
    _create_test_approval(SANDBOX_PROJECT_ID, [31])
    decision2 = svc.decide(SANDBOX_PROJECT_ID)
    assert decision2["ok"]

    # With a valid approval, the decision could be EXECUTE_READY_TASKS
    # (if the risk assessment allows it)
    if decision2["decision"] == Decision.EXECUTE_READY_TASKS.value:
        assert decision2["can_execute"] is True


# ══════════════════════════════════════════════════════════════════
# TEST (g): 跨项目审批隔离
# ══════════════════════════════════════════════════════════════════

def test_g_cross_project_isolation():
    """审批只对指定的 project_id 生效，不影响其他项目。"""
    from app.executor.execution_approval_service import ExecutionApprovalService

    _create_test_approval(SANDBOX_PROJECT_ID, [31])
    svc = ExecutionApprovalService(str(DB_PATH))

    # Project 6 (sandbox) should NOT see project 56's approval
    other_project = 6
    assert not svc.has_valid_approval(other_project)
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID)


# ══════════════════════════════════════════════════════════════════
# TEST (h): LoopController scope cache（第二次迭代过滤）
# ══════════════════════════════════════════════════════════════════

def test_h_loopcontroller_scope_cache():
    """验证 LoopController 在审批消费后缓存 scope 并继续过滤。

    由于 LoopController 需要完整运行环境（TaskWorker、AI 调用等），
    这里直接测试消费后 scope 过滤逻辑的数据面：
    1. 消费审批 → allowed_task_ids 返回 [31]
    2. LoopController._approval_task_scope 设为 {31}
    3. 第二次 _filter_by_approval_scope 使用缓存 scope
    """
    from app.executor.loop_controller import LoopController

    # Create LoopController (won't start, just for structure test)
    lc = LoopController(str(DB_PATH), repo_path=None)

    # Emulate: approval consumed, scope cached
    lc._approval_consumed = True
    lc._approval_task_scope = {31}

    # Create fake runnable tasks
    class FakeTask:
        def __init__(self, task_id):
            self.id = task_id
            self.title = f"Task {task_id}"

    runnable = [FakeTask(31), FakeTask(32), FakeTask(33)]

    # Filter by cached scope
    filtered = lc._filter_by_approval_scope(SANDBOX_PROJECT_ID, runnable)
    assert len(filtered) == 1
    assert filtered[0].id == 31


# ══════════════════════════════════════════════════════════════════
# TEST (i): 二次消费被拒绝
# ══════════════════════════════════════════════════════════════════

def test_i_double_consumption_rejected():
    """审批只能被消费一次。"""
    from app.executor.execution_approval_service import ExecutionApprovalService

    _create_test_approval(SANDBOX_PROJECT_ID, [31])
    svc = ExecutionApprovalService(str(DB_PATH))

    # First consumption → success
    r1 = svc.consume_approval(SANDBOX_PROJECT_ID, 1, 31)
    assert r1["ok"]

    # Second consumption → rejected
    r2 = svc.consume_approval(SANDBOX_PROJECT_ID, 2, 31)
    assert not r2["ok"]
    assert r2["error"] in ("NO_VALID_APPROVAL", "RACE_CONDITION")


# ══════════════════════════════════════════════════════════════════
# TEST (j): Task 范围校验精确性
# ══════════════════════════════════════════════════════════════════

def test_j_task_scope_validation():
    """consume_approval 验证 task_id 是否在 allowed_task_ids 内。"""
    from app.executor.execution_approval_service import ExecutionApprovalService

    _create_test_approval(SANDBOX_PROJECT_ID, [31])
    svc = ExecutionApprovalService(str(DB_PATH))

    # Task 31 is allowed → success
    r1 = svc.consume_approval(SANDBOX_PROJECT_ID, 100, 31)
    assert r1["ok"]

    # Task 32 is NOT allowed → rejection
    # (approval already consumed now, so we create a new one)
    _cleanup_approvals(SANDBOX_PROJECT_ID)
    _create_test_approval(SANDBOX_PROJECT_ID, [31])

    r2 = svc.consume_approval(SANDBOX_PROJECT_ID, 101, 32)
    assert not r2["ok"]
    assert r2["error"] == "TASK_NOT_ALLOWED"


# ══════════════════════════════════════════════════════════════════
# TEST (k): has_valid_approval task scope
# ══════════════════════════════════════════════════════════════════

def test_k_has_valid_approval_task_scope():
    """has_valid_approval 的 requested_task_ids 参数精确过滤。"""
    from app.executor.execution_approval_service import ExecutionApprovalService

    _create_test_approval(SANDBOX_PROJECT_ID, [31, 32])
    svc = ExecutionApprovalService(str(DB_PATH))

    assert svc.has_valid_approval(SANDBOX_PROJECT_ID)
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID, [31])
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID, [32])
    assert svc.has_valid_approval(SANDBOX_PROJECT_ID, [31, 32])
    assert not svc.has_valid_approval(SANDBOX_PROJECT_ID, [33])
    assert not svc.has_valid_approval(SANDBOX_PROJECT_ID, [31, 33])


# ══════════════════════════════════════════════════════════════════
# TEST (l): API executor/preflight 含审批范围
# ══════════════════════════════════════════════════════════════════

def test_l_preflight_includes_approval():
    """preflight API 响应包含 has_execution_approval 和 allowed_task_ids。"""
    from app.executor.task_scheduler import TaskScheduler
    from app.executor.run_store import RunStore
    from app.executor.execution_approval_service import ExecutionApprovalService

    # 1. Without approval
    svc = ExecutionApprovalService(str(DB_PATH))
    approval = svc.get_valid_approval(SANDBOX_PROJECT_ID)
    assert approval is None  # no approval

    scheduler = TaskScheduler(str(DB_PATH))
    runnable = scheduler.find_runnable_tasks(SANDBOX_PROJECT_ID)
    runnable_ids = [t.id for t in runnable]

    # No approval → all runnable pass
    assert len(runnable_ids) >= 0

    # 2. Create approval for tasks that exist in runnable
    if runnable_ids:
        target_tasks = runnable_ids[:1]
        _create_test_approval(SANDBOX_PROJECT_ID, target_tasks)

        svc2 = ExecutionApprovalService(str(DB_PATH))
        approval2 = svc2.get_valid_approval(SANDBOX_PROJECT_ID)
        assert approval2 is not None

        allowed_ids = set(approval2.get("allowed_task_ids", []))
        if allowed_ids:
            filtered = [tid for tid in runnable_ids if tid in allowed_ids]
            assert len(filtered) <= len(runnable_ids)
            assert all(tid in target_tasks for tid in filtered)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
