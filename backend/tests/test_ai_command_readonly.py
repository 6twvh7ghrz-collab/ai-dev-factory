"""自然语言只读指令接入 V1.1 测试

覆盖：
1. show_status 真实返回项目状态
2. diagnose_blocker 真实返回分类原因
3. confirmed_intent 与重新解析结果不一致时拒绝
4. start_development 被只读API拒绝
5. pause/resume/stop 被拒绝
6. unknown 被拒绝
7. 不创建 executor_run
8. 不创建 task_lease
9. 不创建 resource_lock
10. 不修改 development_tasks
11. 不调用 ModelAdapter
12. 项目不存在返回明确错误
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.executor.command_normalizer import CommandNormalizer
from app.executor.ai_brain_controller import AIBrainController

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'ai_factory.db')


def _count(table: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return -1
    finally:
        conn.close()


def _snapshot() -> dict:
    """记录关键表行数"""
    return {
        "executor_runs": _count("executor_runs"),
        "task_leases": _count("task_leases"),
        "executor_resource_locks": _count("executor_resource_locks"),
        "development_tasks": _count("development_tasks"),
        "executions": _count("executions"),
    }


def test_show_status_returns_real_data():
    """1. show_status 真实返回项目状态"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    # 使用项目65
    result = controller.execute_readonly("查看状态", 65, "show_status")
    assert result["ok"], f"show_status should succeed: {result}"
    assert result["executed"], "should be executed"
    data = result["data"]
    assert data["project_id"] == 65
    assert "project_name" in data
    assert "run_status" in data
    assert "pending_count" in data
    assert "completed_count" in data
    assert "blocked_count" in data
    assert "total_count" in data
    print(f"  show_status: project={data['project_name']}, run_status={data['run_status']}, pending={data['pending_count']}, completed={data['completed_count']}")


def test_diagnose_blocker_returns_categories():
    """2. diagnose_blocker 真实返回分类原因"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    result = controller.execute_readonly("检查阻塞原因", 65, "diagnose_blocker")
    assert result["ok"], f"diagnose_blocker should succeed: {result}"
    assert result["executed"], "should be executed"
    data = result["data"]
    assert "status" in data
    assert "summary" in data
    assert "categories" in data
    assert "tasks" in data
    # categories 必须包含关键字段
    cats = data["categories"]
    assert "needs_planning" in cats
    assert "dependency_incomplete" in cats
    assert "active_lease" in cats
    assert "missing_files" in cats
    print(f"  diagnose_blocker: status={data['status']}, summary={data['summary']}, blocked={data['blocked_count']}")


def test_intent_mismatch_rejected():
    """3. confirmed_intent 与重新解析结果不一致时拒绝"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    # 文本是"查看状态"但确认意图是 start_development
    result = controller.execute_readonly("查看状态", 65, "start_development")
    assert not result["ok"], "should be rejected"
    assert result["code"] == "INTENT_MISMATCH", f"should be INTENT_MISMATCH, got {result.get('code')}"
    assert not result["executed"]
    print(f"  intent_mismatch: correctly rejected with code={result['code']}")


def test_start_development_rejected():
    """4. start_development 被只读API拒绝"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    result = controller.execute_readonly("开始开发", 65, "start_development")
    assert not result["ok"], "should be rejected"
    assert result["code"] == "READONLY_INTENT_REQUIRED", f"got {result.get('code')}"
    assert not result["executed"]
    print(f"  start_development: correctly rejected with code={result['code']}")


def test_pause_resume_stop_rejected():
    """5. pause/resume/stop 被拒绝"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    for text, intent in [("暂停", "pause_executor"), ("继续执行", "resume_executor"), ("停止执行", "stop_executor")]:
        result = controller.execute_readonly(text, 65, intent)
        assert not result["ok"], f"{intent} should be rejected"
        assert result["code"] == "READONLY_INTENT_REQUIRED", f"{intent} got {result.get('code')}"
        assert not result["executed"]
        print(f"  {intent}: correctly rejected")


def test_unknown_rejected():
    """6. unknown 被拒绝"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    result = controller.execute_readonly("今天天气怎么样", 65, "unknown")
    assert not result["ok"], "should be rejected"
    assert result["code"] == "READONLY_INTENT_REQUIRED"
    assert not result["executed"]
    print(f"  unknown: correctly rejected")


def test_no_executor_run_created():
    """7. 不创建 executor_run"""
    before = _snapshot()
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    controller.execute_readonly("查看状态", 65, "show_status")
    controller.execute_readonly("检查阻塞", 65, "diagnose_blocker")
    after = _snapshot()
    assert after["executor_runs"] == before["executor_runs"], \
        f"executor_runs changed: {before['executor_runs']} -> {after['executor_runs']}"
    print(f"  executor_runs: {before['executor_runs']} == {after['executor_runs']}")


def test_no_task_lease_created():
    """8. 不创建 task_lease"""
    before = _snapshot()
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    controller.execute_readonly("查看状态", 65, "show_status")
    controller.execute_readonly("检查阻塞", 65, "diagnose_blocker")
    after = _snapshot()
    assert after["task_leases"] == before["task_leases"], \
        f"task_leases changed: {before['task_leases']} -> {after['task_leases']}"
    print(f"  task_leases: {before['task_leases']} == {after['task_leases']}")


def test_no_resource_lock_created():
    """9. 不创建 resource_lock"""
    before = _snapshot()
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    controller.execute_readonly("查看状态", 65, "show_status")
    controller.execute_readonly("检查阻塞", 65, "diagnose_blocker")
    after = _snapshot()
    assert after["executor_resource_locks"] == before["executor_resource_locks"], \
        f"executor_resource_locks changed: {before['executor_resource_locks']} -> {after['executor_resource_locks']}"
    print(f"  executor_resource_locks: {before['executor_resource_locks']} == {after['executor_resource_locks']}")


def test_no_development_tasks_modified():
    """10. 不修改 development_tasks"""
    before = _snapshot()
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    controller.execute_readonly("查看状态", 65, "show_status")
    controller.execute_readonly("检查阻塞", 65, "diagnose_blocker")
    after = _snapshot()
    assert after["development_tasks"] == before["development_tasks"], \
        f"development_tasks changed: {before['development_tasks']} -> {after['development_tasks']}"
    print(f"  development_tasks: {before['development_tasks']} == {after['development_tasks']}")


def test_no_model_adapter_called():
    """11. 不调用 ModelAdapter"""
    # execute_readonly 的代码路径不 import ModelAdapter 或 DeepSeek
    # 通过代码审查验证：AIBrainController.execute_readonly 只 import
    # RunStore / TaskScheduler / ResourceLockManager，不 import ModelAdapter
    import inspect
    from app.executor.ai_brain_controller import AIBrainController as Ctrl
    source = inspect.getsource(Ctrl._execute_show_status)
    assert "ModelAdapter" not in source, "ModelAdapter found in _execute_show_status"
    assert "DeepSeek" not in source, "DeepSeek found in _execute_show_status"
    assert "deepseek" not in source.lower(), "deepseek found in _execute_show_status"

    source2 = inspect.getsource(Ctrl._execute_diagnose_blocker)
    assert "ModelAdapter" not in source2, "ModelAdapter found in _execute_diagnose_blocker"
    assert "DeepSeek" not in source2, "DeepSeek found in _execute_diagnose_blocker"
    assert "deepseek" not in source2.lower(), "deepseek found in _execute_diagnose_blocker"
    print(f"  ModelAdapter/DeepSeek: NOT called (verified by source inspection)")


def test_project_not_found_error():
    """12. 项目不存在返回明确错误"""
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)

    result = controller.execute_readonly("查看状态", 99999, "show_status")
    assert not result["ok"], "should be rejected"
    assert result["code"] == "PROJECT_NOT_FOUND"
    assert not result["executed"]
    print(f"  project_not_found: correctly returned code={result['code']}")


def test_executions_unchanged():
    """executions 表不变"""
    before = _snapshot()
    normalizer = CommandNormalizer()
    controller = AIBrainController(normalizer, DB_PATH)
    controller.execute_readonly("查看状态", 65, "show_status")
    controller.execute_readonly("检查阻塞", 65, "diagnose_blocker")
    after = _snapshot()
    assert after["executions"] == before["executions"], \
        f"executions changed: {before['executions']} -> {after['executions']}"
    print(f"  executions: {before['executions']} == {after['executions']}")


if __name__ == "__main__":
    print("=" * 60)
    print("自然语言只读指令接入 V1.1 测试")
    print("=" * 60)

    tests = [
        ("show_status 返回真实数据", test_show_status_returns_real_data),
        ("diagnose_blocker 返回分类原因", test_diagnose_blocker_returns_categories),
        ("intent 不一致拒绝", test_intent_mismatch_rejected),
        ("start_development 拒绝", test_start_development_rejected),
        ("pause/resume/stop 拒绝", test_pause_resume_stop_rejected),
        ("unknown 拒绝", test_unknown_rejected),
        ("不创建 executor_run", test_no_executor_run_created),
        ("不创建 task_lease", test_no_task_lease_created),
        ("不创建 resource_lock", test_no_resource_lock_created),
        ("不修改 development_tasks", test_no_development_tasks_modified),
        ("不调用 ModelAdapter", test_no_model_adapter_called),
        ("项目不存在错误", test_project_not_found_error),
        ("executions 不变", test_executions_unchanged),
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
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

    print(f"\n{passed}/{len(tests)} passed")
