"""AIBrainController 回归测试

验证：
- preview 不创建 executor_run
- preview 不创建 lease
- preview 不创建 resource lock
- preview 不修改 development_tasks
- executed 永远为 False
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.executor.command_normalizer import CommandNormalizer
from app.executor.ai_brain_controller import AIBrainController


def _get_db_path():
    """获取数据库路径"""
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "ai_factory.db"
    )
    return db_path


def _count_table(db_path: str, table: str) -> int:
    """统计表中记录数"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    conn.close()
    return count


def test_preview_returns_executed_false():
    """executed 永远为 False"""
    n = CommandNormalizer()
    c = AIBrainController(n)
    result = c.preview("开始开发", 65)
    assert result["executed"] is False
    assert result["action"] == "preview_only"


def test_preview_no_executor_run_created():
    """preview 不创建 executor_run"""
    db_path = _get_db_path()
    before = _count_table(db_path, "executor_runs")

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("开始开发", 65)
    c.preview("暂停", 65)
    c.preview("查看状态", 65)

    after = _count_table(db_path, "executor_runs")
    assert before == after, f"executor_runs: {before} → {after} (should not change)"


def test_preview_no_lease_created():
    """preview 不创建 lease"""
    db_path = _get_db_path()
    before = _count_table(db_path, "task_leases")

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("开始开发", 65)

    after = _count_table(db_path, "task_leases")
    assert before == after, f"task_leases: {before} → {after} (should not change)"


def test_preview_no_resource_lock_created():
    """preview 不创建 resource lock"""
    db_path = _get_db_path()
    before = _count_table(db_path, "executor_resource_locks")

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("开始开发", 65)

    after = _count_table(db_path, "executor_resource_locks")
    assert before == after, \
        f"executor_resource_locks: {before} → {after} (should not change)"


def test_preview_no_development_tasks_modified():
    """preview 不修改 development_tasks"""
    db_path = _get_db_path()
    before = _count_table(db_path, "development_tasks")

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("开始开发", 65)

    after = _count_table(db_path, "development_tasks")
    assert before == after, \
        f"development_tasks: {before} → {after} (should not change)"


def test_preview_no_executions_created():
    """preview 不创建 executions"""
    db_path = _get_db_path()
    before = _count_table(db_path, "executions")

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("开始开发", 65)

    after = _count_table(db_path, "executions")
    assert before == after, \
        f"executions: {before} → {after} (should not change)"


def test_preview_unknown_intent():
    """未知指令返回 unknown"""
    n = CommandNormalizer()
    c = AIBrainController(n)
    result = c.preview("今天天气怎么样", 65)
    assert result["intent"] == "unknown"
    assert result["executed"] is False


def test_preview_known_intent():
    """已知指令返回正确意图"""
    n = CommandNormalizer()
    c = AIBrainController(n)
    result = c.preview("开始开发", 65)
    assert result["intent"] == "start_development"
    assert result["ok"] is True
    assert result["executed"] is False


def test_preview_empty_input():
    """空输入返回错误"""
    n = CommandNormalizer()
    c = AIBrainController(n)
    result = c.preview("", 65)
    assert result["ok"] is False
    assert result["error"] == "EMPTY_INPUT"

    result = c.preview("   ", 65)
    assert result["ok"] is False


def test_preview_too_long():
    """超长输入返回错误"""
    n = CommandNormalizer()
    c = AIBrainController(n)
    long_text = "x" * 1001
    result = c.preview(long_text, 65)
    assert result["ok"] is False
    assert result["error"] == "TEXT_TOO_LONG"


if __name__ == "__main__":
    tests = [
        ("1. executed永远为False", test_preview_returns_executed_false),
        ("2. 不创建executor_run", test_preview_no_executor_run_created),
        ("3. 不创建lease", test_preview_no_lease_created),
        ("4. 不创建resource_lock", test_preview_no_resource_lock_created),
        ("5. 不修改development_tasks", test_preview_no_development_tasks_modified),
        ("6. 不创建executions", test_preview_no_executions_created),
        ("7. 未知指令→unknown", test_preview_unknown_intent),
        ("8. 已知指令→正确意图", test_preview_known_intent),
        ("9. 空输入→错误", test_preview_empty_input),
        ("10. 超长输入→错误", test_preview_too_long),
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
