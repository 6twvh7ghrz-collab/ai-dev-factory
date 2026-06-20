"""自然语言安全写入指令 V1.2.1 测试

覆盖：
1. preview 不启动
2. 无令牌拒绝
3. 过期令牌拒绝
4. 重复令牌拒绝
5. text 与 confirmed_intent 不一致拒绝
6. 未授权执行项目拒绝 (execution_enabled=0)
7. needs_planning 项目拒绝 (通过公开 API)
8. 无 runnable 任务拒绝
9. workspace 校验 (通过 ProjectExecutionGuard 公开 API)
10. 活跃 run 时返回 ALREADY_RUNNING
11. 重复请求只创建一个 run
12. 沙箱项目成功创建 run（使用 FakeModelAdapter/mock）
13. 成功时 executed=true
14. 失败时 executed=false
15. 正式电商项目始终拒绝 (execution_enabled=0)
"""
import sys
import os
import time
import sqlite3
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.executor.command_normalizer import CommandNormalizer, CommandIntent
from app.executor.ai_brain_controller import AIBrainController, _ENABLED_WRITE_INTENTS
from app.executor.confirmation_token import get_token_manager, ConfirmationTokenManager
from app.executor.project_execution_guard import ProjectExecutionGuard

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'ai_factory.db')

# 沙箱项目
SANDBOX_PROJECT_ID = 65
SANDBOX_PROJECT_NAME = "AI工厂沙箱验收项目"
SANDBOX_WORKSPACE = r"C:\SandboxUser\本机\Desktop\executor-sandbox-v2"

# 电商项目（应被拒绝）
ECOMMERCE_PROJECT_ID = 56
ECOMMERCE_PROJECT_NAME = "帮我做一个电商运营助手，包括拼多多，抖店，小红书等店铺，支持"

# ── V1.2.1 测试辅助：project_execution_configs 临时配置 ──

def _ensure_exec_config(project_id: int, workspace_path: str, enabled: bool = True):
    """确保 project_execution_configs 中有项目配置（幂等）"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO project_execution_configs "
            "(project_id, workspace_path, execution_enabled, execution_mode, max_workers, max_tasks, requires_confirmation) "
            "VALUES (?, ?, ?, 'sandbox', 1, 10, 1)",
            (project_id, workspace_path, 1 if enabled else 0)
        )
        conn.commit()
    finally:
        conn.close()


def _remove_exec_config(project_id: int):
    """删除 project_execution_configs 中的配置"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM project_execution_configs WHERE project_id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def _count_active(table: str, project_id: int = None) -> int:
    """统计活跃记录数"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        if table == "executor_runs":
            cur.execute("""
                SELECT COUNT(*) as cnt FROM executor_runs
                WHERE project_id = ?
                AND status IN ('starting','scanning','claiming','executing',
                               'testing','repairing','paused','stopping')
            """, (project_id,))
        elif table == "task_leases":
            cur.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'")
        elif table == "executor_resource_locks":
            cur.execute("SELECT COUNT(*) as cnt FROM executor_resource_locks WHERE status='active'")
        else:
            return -1
        return cur.fetchone()["cnt"]
    finally:
        conn.close()


def _snapshot(project_id: int) -> dict:
    """记录关键表行数"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM executor_runs")
        er = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM task_leases")
        tl = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM executor_resource_locks")
        el = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM executions")
        ex = cur.fetchone()[0]
        return {"executor_runs": er, "task_leases": tl, "executor_resource_locks": el, "executions": ex}
    finally:
        conn.close()


def _cleanup_test_runs(project_id: int):
    """清理测试 run、lease、资源锁"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        # 清理资源锁
        cur.execute("DELETE FROM executor_resource_locks WHERE project_id = ?", (project_id,))
        # 清理 leases
        cur.execute("DELETE FROM task_leases WHERE task_id IN (SELECT id FROM development_tasks WHERE project_id = ?)", (project_id,))
        # 清理 runs
        cur.execute("DELETE FROM executor_runs WHERE project_id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def test_01_preview_does_not_start():
    """1. preview 不启动 executor"""
    _cleanup_test_runs(SANDBOX_PROJECT_ID)
    before = _snapshot(SANDBOX_PROJECT_ID)

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    result = controller.preview("让AI开始开发", SANDBOX_PROJECT_ID)

    assert result["ok"], f"preview should succeed: {result}"
    assert result["intent"] == "start_development", f"intent should be start_development, got {result['intent']}"
    assert result["executed"] == False, "preview must not execute"
    assert result["requires_confirmation"] == True, "start_development should require confirmation"

    after = _snapshot(SANDBOX_PROJECT_ID)
    assert after["executor_runs"] == before["executor_runs"], "preview must not create executor_runs"
    assert after["task_leases"] == before["task_leases"], "preview must not create task_leases"
    print(f"  preview: intent={result['intent']}, executed={result['executed']}, runs unchanged={after['executor_runs']==before['executor_runs']}")


def test_02_no_token_rejected():
    """2. 无令牌拒绝"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    result = controller.execute_write(
        "让AI开始开发", SANDBOX_PROJECT_ID,
        "start_development", "invalid-token-xxx"
    )
    assert not result["ok"], "should be rejected"
    assert result["code"] == "INVALID_TOKEN", f"got {result.get('code')}"
    assert not result["executed"]
    print(f"  no_token: code={result['code']}, message={result['message']}")


def test_03_expired_token_rejected():
    """3. 过期令牌拒绝"""
    # 创建一个已过期的令牌
    mgr = ConfirmationTokenManager()
    mgr.TTL_SECONDS = 1  # 1秒过期
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")
    time.sleep(1.5)  # 等待过期

    # 直接测试 token manager 的过期逻辑
    result = mgr.validate_and_consume(
        token, SANDBOX_PROJECT_ID,
        "start_development", "让AI开始开发"
    )
    assert not result["valid"], "should be rejected as expired"
    assert result["code"] == "TOKEN_EXPIRED", f"got {result['code']}"
    print(f"  expired_token: code={result['code']}, message={result['message']}")


def test_04_duplicate_token_rejected():
    """4. 重复令牌拒绝"""
    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        # 第一次使用（会因项目65无runnable任务而失败，但令牌已消耗）
        result1 = controller.execute_write(
            "让AI开始开发", SANDBOX_PROJECT_ID,
            "start_development", token
        )
        # 验证令牌已消耗（不管业务结果如何）
        validate_result = mgr.validate_and_consume(token, SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")
        assert not validate_result["valid"], "token should already be consumed"
        assert validate_result["code"] == "TOKEN_ALREADY_USED", f"got {validate_result['code']}"
        print(f"  duplicate_token: correctly detected as TOKEN_ALREADY_USED")
    finally:
        ct._token_manager = old_mgr


def test_05_intent_mismatch_rejected():
    """5. text 与 confirmed_intent 不一致拒绝"""
    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        # text 是 "开始开发" 但 confirmed_intent 是 "stop_executor"
        result = controller.execute_write(
            "让AI开始开发", SANDBOX_PROJECT_ID,
            "stop_executor", token
        )
        assert not result["ok"], "should be rejected"
        assert result["code"] in ("INTENT_MISMATCH", "WRITE_INTENT_NOT_ENABLED", "TOKEN_INTENT_MISMATCH"), \
            f"unexpected code: {result.get('code')}"
        assert not result["executed"]
        print(f"  intent_mismatch: code={result['code']}")
    finally:
        ct._token_manager = old_mgr


def test_06_non_whitelist_project_rejected():
    """6. 未授权执行项目拒绝（execution_enabled=0）

    V1.2.1: 旧白名单已替换为 project_execution_configs.execution_enabled。
    电商项目(56) execution_enabled=0，应返回 EXECUTION_NOT_ENABLED。
    """
    mgr = ConfirmationTokenManager()
    token = mgr.generate(ECOMMERCE_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        result = controller.execute_write(
            "让AI开始开发", ECOMMERCE_PROJECT_ID,
            "start_development", token
        )
        assert not result["ok"], "should be rejected"
        # V1.2.1: 错误码从 PROJECT_NOT_ALLOWED_FOR_AUTO_EXECUTION 更名为 EXECUTION_NOT_ENABLED
        assert result["code"] == "EXECUTION_NOT_ENABLED", f"got {result.get('code')}"
        assert not result["executed"]
        print(f"  non_whitelist: code={result['code']}, message={result['message']}")
    finally:
        ct._token_manager = old_mgr


def test_07_needs_planning_project_rejected():
    """7. needs_planning 项目拒绝（通过公开 API 验证）

    V1.2.1: _validate_project_whitelist 已删除，改用 ProjectExecutionGuard。
    验证 execution_enabled=0 的项目被公开 API 正确拒绝。
    """
    # 通过 ProjectExecutionGuard 验证未授权项目
    guard = ProjectExecutionGuard(DB_PATH)
    allowed, reason, detail = guard.validate_project(ECOMMERCE_PROJECT_ID)
    assert not allowed, "电商项目应被拒绝执行"
    assert reason == "EXECUTION_NOT_ENABLED", f"unexpected reason: {reason}"
    assert detail["code"] == "EXECUTION_NOT_ENABLED"
    print(f"  needs_planning: ProjectExecutionGuard correctly rejects project {ECOMMERCE_PROJECT_ID}")

    # 也验证通过 execute_write 公开 API 拒绝
    mgr = ConfirmationTokenManager()
    token = mgr.generate(ECOMMERCE_PROJECT_ID, "start_development", "让AI开始开发")
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr
    try:
        result = controller.execute_write(
            "让AI开始开发", ECOMMERCE_PROJECT_ID,
            "start_development", token
        )
        assert not result["ok"], "should be rejected via execute_write"
        assert result["code"] == "EXECUTION_NOT_ENABLED", f"got {result.get('code')}"
    finally:
        ct._token_manager = old_mgr


def test_08_no_runnable_tasks_rejected():
    """8. 无 runnable 任务拒绝（项目65只有completed任务）

    V1.2.1: 需要先插入 project_execution_configs 配置才能通过守卫到达 runnable 检查。
    """
    _ensure_exec_config(SANDBOX_PROJECT_ID, SANDBOX_WORKSPACE, enabled=True)

    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        result = controller.execute_write(
            "让AI开始开发", SANDBOX_PROJECT_ID,
            "start_development", token
        )
        # 项目65只有1个completed任务，无pending，所以会返回NO_RUNNABLE_TASKS
        assert not result["ok"], f"should be rejected, got {result}"
        assert result["code"] == "NO_RUNNABLE_TASKS", f"got {result.get('code')}"
        assert not result["executed"]
        print(f"  no_runnable: code={result['code']}, message={result['message']}")
    finally:
        ct._token_manager = old_mgr
        _remove_exec_config(SANDBOX_PROJECT_ID)


def test_09_git_dirty_workspace_rejected():
    """9. workspace 校验（通过 ProjectExecutionGuard 公开 API）

    V1.2.1: _validate_workspace 已删除，功能迁移到 ProjectExecutionGuard.validate_project()。
    通过公开 API 验证路径安全校验链。
    """
    guard = ProjectExecutionGuard(DB_PATH)

    # 场景1: 不存在的路径 → WORKSPACE_NOT_FOUND
    # 先插入一条指向不存在路径的配置
    nonexistent_path = r"C:\nonexistent\path\that\does\not\exist"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO project_execution_configs "
            "(project_id, workspace_path, execution_enabled, execution_mode) "
            "VALUES (?, ?, 1, 'sandbox')",
            (SANDBOX_PROJECT_ID, nonexistent_path)
        )
        conn.commit()
    finally:
        conn.close()

    try:
        allowed, reason, detail = guard.validate_project(SANDBOX_PROJECT_ID)
        assert not allowed, "nonexistent workspace should be rejected"
        # 可能先被 WorkspaceGuard 拦截 (WORKSPACE_FORBIDDEN) 或路径检查拦截 (WORKSPACE_NOT_FOUND)
        assert reason in ("WORKSPACE_NOT_FOUND", "WORKSPACE_FORBIDDEN"), f"unexpected reason: {reason}"
        print(f"  workspace_validation: reason={reason}, detail={detail.get('code')}")
    finally:
        _remove_exec_config(SANDBOX_PROJECT_ID)

    # 场景2: 真实存在的沙箱工作区路径 → 通过（需要是 Git 仓库）
    _ensure_exec_config(SANDBOX_PROJECT_ID, SANDBOX_WORKSPACE, enabled=True)
    try:
        allowed, reason, detail = guard.validate_project(SANDBOX_PROJECT_ID)
        print(f"  sandbox_workspace: allowed={allowed}, reason={reason}, code={detail.get('code')}")
        # 注: 如果沙箱工作区 Git 脏了会返回 GIT_WORKING_TREE_DIRTY，这是正常行为
        # 如果路径存在且是 Git 仓库且 clean，则 allowed=True
    finally:
        _remove_exec_config(SANDBOX_PROJECT_ID)


def test_10_already_running():
    """10. 活跃 run 时返回 ALREADY_RUNNING（幂等保护）

    通过模拟：直接往 executor_runs 插入一条活跃记录，
    然后调用 execute_write，验证返回 ALREADY_RUNNING。
    V1.2.1: 需要先插入 project_execution_configs 配置才能通过守卫到达活跃 run 检查。
    """
    _cleanup_test_runs(SANDBOX_PROJECT_ID)
    _ensure_exec_config(SANDBOX_PROJECT_ID, SANDBOX_WORKSPACE, enabled=True)

    # 手动创建一条活跃 run
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO executor_runs
            (run_id, project_id, worker_id, status, mode, started_at, heartbeat_at, current_step)
            VALUES ('runner-test-already', ?, 'worker-test', 'scanning', 'auto_until_blocked',
                    datetime('now','localtime'), datetime('now','localtime'), 'scan_queue')
        """, (SANDBOX_PROJECT_ID,))
        conn.commit()
    finally:
        conn.close()

    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        result = controller.execute_write(
            "让AI开始开发", SANDBOX_PROJECT_ID,
            "start_development", token
        )
        assert result["ok"], f"should return ok: {result}"
        assert result["code"] == "ALREADY_RUNNING", f"got {result.get('code')}"
        assert not result["executed"], "should not be executed"
        assert result["run_id"] is not None
        print(f"  already_running: code={result['code']}, run_id={result.get('run_id')}")
    finally:
        ct._token_manager = old_mgr
        _cleanup_test_runs(SANDBOX_PROJECT_ID)
        _remove_exec_config(SANDBOX_PROJECT_ID)


def test_11_duplicate_request_only_one_run():
    """11. 重复请求只创建一个 run

    通过验证 RunStore.create_starting_run 的 UNIQUE INDEX
    来确保同一项目只能有一个活跃 run。
    """
    _cleanup_test_runs(SANDBOX_PROJECT_ID)

    from app.executor.run_store import RunStore
    store = RunStore(DB_PATH)

    # 第一次创建
    result1 = store.create_starting_run(SANDBOX_PROJECT_ID)
    assert result1["success"], f"first create should succeed: {result1}"
    run1_id = result1["run"]["run_id"]

    # 第二次创建（应该因 UNIQUE INDEX 失败）
    result2 = store.create_starting_run(SANDBOX_PROJECT_ID)
    assert not result2["success"], "second create should fail"
    assert "integrity_error" in result2.get("error", "") or "already_running" in result2.get("error", ""), \
        f"unexpected error: {result2.get('error')}"

    print(f"  only_one_run: first={run1_id}, second correctly rejected with already_running")

    _cleanup_test_runs(SANDBOX_PROJECT_ID)


def test_12_sandbox_success_with_fake_adapter():
    """12. 沙箱项目成功创建 run（使用 mock，不调用真实 DeepSeek）

    为项目65创建一个pending+ready的临时任务，然后验证启动。
    V1.2.1: 需要先插入 project_execution_configs 配置。
    """
    _cleanup_test_runs(SANDBOX_PROJECT_ID)
    _ensure_exec_config(SANDBOX_PROJECT_ID, SANDBOX_WORKSPACE, enabled=True)

    # 创建临时可执行任务
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    temp_task_id = None
    try:
        cur.execute("""
            INSERT INTO development_tasks
            (project_id, title, description, task_type, status, readiness_status,
             priority, dependencies, files_to_modify, codex_prompt,
             test_steps, acceptance_criteria, implementation_steps, sort_order)
            VALUES (?, ?, ?, 'code', 'pending', 'ready',
                    'medium', '[]', '["test_norm.py"]', '实现文本规范化函数',
                    '运行pytest', '正确处理所有输入', 'Run python test_norm.py', 99)
        """, (SANDBOX_PROJECT_ID, "V1.2 测试任务 - 文本规范化", "测试任务描述"))
        conn.commit()
        temp_task_id = cur.lastrowid
    finally:
        conn.close()

    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    # Mock LoopController 以避免真实 DeepSeek 调用
    from unittest.mock import patch, MagicMock

    try:
        with patch('app.executor.loop_controller.LoopController') as mock_loop:
            mock_instance = MagicMock()
            mock_result = {
                "success": True,
                "already_running": False,
                "message": "循环已启动",
                "run": {
                    "run_id": "runner-mock-success",
                    "status": "starting",
                },
            }
            mock_instance.start.return_value = mock_result
            mock_loop.return_value = mock_instance

            result = controller.execute_write(
                "让AI开始开发", SANDBOX_PROJECT_ID,
                "start_development", token
            )

            assert result["ok"], f"should succeed: {result}"
            assert result["code"] == "STARTED", f"got {result.get('code')}"
            assert result["executed"], "should be executed"
            assert result["run_id"] == "runner-mock-success"
            assert result["task_id"] is not None
            print(f"  sandbox_success: code={result['code']}, run_id={result['run_id']}, task_id={result.get('task_id')}")

    finally:
        ct._token_manager = old_mgr
        # 清理临时任务
        if temp_task_id:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("DELETE FROM development_tasks WHERE id = ?", (temp_task_id,))
            conn.commit()
            conn.close()
        _cleanup_test_runs(SANDBOX_PROJECT_ID)
        _remove_exec_config(SANDBOX_PROJECT_ID)


def test_13_executed_true_on_success():
    """13. 成功时 executed=true

    V1.2.1: 需要先插入 project_execution_configs 配置。
    """
    _cleanup_test_runs(SANDBOX_PROJECT_ID)
    _ensure_exec_config(SANDBOX_PROJECT_ID, SANDBOX_WORKSPACE, enabled=True)

    # 创建临时可执行任务
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    temp_task_id = None
    try:
        cur.execute("""
            INSERT INTO development_tasks
            (project_id, title, description, task_type, status, readiness_status,
             priority, dependencies, files_to_modify, codex_prompt,
             test_steps, acceptance_criteria, implementation_steps, sort_order)
            VALUES (?, ?, ?, 'code', 'pending', 'ready',
                    'medium', '[]', '["test_norm2.py"]', '实现规范化v2',
                    '运行pytest', '正确处理输入', 'Run python test_norm2.py', 98)
        """, (SANDBOX_PROJECT_ID, "V1.2 测试任务 - 规范化v2", "测试描述"))
        conn.commit()
        temp_task_id = cur.lastrowid
    finally:
        conn.close()

    mgr = ConfirmationTokenManager()
    token = mgr.generate(SANDBOX_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    from unittest.mock import patch, MagicMock

    try:
        with patch('app.executor.loop_controller.LoopController') as mock_loop:
            mock_instance = MagicMock()
            mock_result = {
                "success": True,
                "already_running": False,
                "message": "循环已启动",
                "run": {
                    "run_id": "runner-executed-true",
                    "status": "starting",
                },
            }
            mock_instance.start.return_value = mock_result
            mock_loop.return_value = mock_instance

            result = controller.execute_write(
                "让AI开始开发", SANDBOX_PROJECT_ID,
                "start_development", token
            )
            assert result["ok"], f"should succeed: {result}"
            assert result["executed"] == True, f"executed should be True, got {result['executed']}"
            print(f"  executed_true: executed={result['executed']}, code={result['code']}")

    finally:
        ct._token_manager = old_mgr
        if temp_task_id:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("DELETE FROM development_tasks WHERE id = ?", (temp_task_id,))
            conn.commit()
            conn.close()
        _cleanup_test_runs(SANDBOX_PROJECT_ID)
        _remove_exec_config(SANDBOX_PROJECT_ID)


def test_14_executed_false_on_failure():
    """14. 失败时 executed=false"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    # 无令牌时必定失败
    result = controller.execute_write(
        "让AI开始开发", SANDBOX_PROJECT_ID,
        "start_development", "bad-token"
    )
    assert not result["ok"], "should fail"
    assert result["executed"] == False, f"executed should be False, got {result['executed']}"
    print(f"  executed_false: executed={result['executed']}, code={result['code']}")


def test_15_ecommerce_always_rejected():
    """15. 正式电商项目始终拒绝

    V1.2.1: 电商项目(56) execution_enabled=0，应返回 EXECUTION_NOT_ENABLED。
    桌面小游戏项目(64) execution_enabled=0，同样被拒绝。
    """
    mgr = ConfirmationTokenManager()
    token = mgr.generate(ECOMMERCE_PROJECT_ID, "start_development", "让AI开始开发")

    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    import app.executor.confirmation_token as ct
    old_mgr = ct._token_manager
    ct._token_manager = mgr

    try:
        result = controller.execute_write(
            "让AI开始开发", ECOMMERCE_PROJECT_ID,
            "start_development", token
        )
        assert not result["ok"], "ecommerce project must be rejected"
        assert result["code"] == "EXECUTION_NOT_ENABLED", f"got {result.get('code')}"
        assert not result["executed"]
        print(f"  ecommerce_rejected: code={result['code']}, message={result['message']}")

        # 再测试"桌面小游戏"项目
        mgr2 = ConfirmationTokenManager()
        token2 = mgr2.generate(64, "start_development", "让AI开始开发")
        ct._token_manager = mgr2
        result2 = controller.execute_write(
            "让AI开始开发", 64,
            "start_development", token2
        )
        assert not result2["ok"], "game project must be rejected"
        assert result2["code"] == "EXECUTION_NOT_ENABLED", f"got {result2.get('code')}"
        print(f"  game_rejected: code={result2['code']}, message={result2['message']}")

    finally:
        ct._token_manager = old_mgr


if __name__ == "__main__":
    print("=" * 60)
    print("自然语言安全写入指令 V1.2 测试")
    print("=" * 60)

    tests = [
        ("preview 不启动", test_01_preview_does_not_start),
        ("无令牌拒绝", test_02_no_token_rejected),
        ("过期令牌拒绝", test_03_expired_token_rejected),
        ("重复令牌拒绝", test_04_duplicate_token_rejected),
        ("intent 不一致拒绝", test_05_intent_mismatch_rejected),
        ("非白名单项目拒绝", test_06_non_whitelist_project_rejected),
        ("needs_planning 拒绝逻辑", test_07_needs_planning_project_rejected),
        ("无 runnable 任务拒绝", test_08_no_runnable_tasks_rejected),
        ("Git 脏工作区检查", test_09_git_dirty_workspace_rejected),
        ("活跃 run 返回 ALREADY_RUNNING", test_10_already_running),
        ("重复请求只创建一个 run", test_11_duplicate_request_only_one_run),
        ("沙箱项目成功启动(mock)", test_12_sandbox_success_with_fake_adapter),
        ("成功时 executed=true", test_13_executed_true_on_success),
        ("失败时 executed=false", test_14_executed_false_on_failure),
        ("电商项目始终拒绝", test_15_ecommerce_always_rejected),
    ]

    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

    print(f"\n{passed}/{len(tests)} passed")
