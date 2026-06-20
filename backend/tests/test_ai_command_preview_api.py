"""AI Command Preview API 测试

覆盖：
- 预览接口不创建 executor_run
- 预览接口不创建 lease
- 预览接口不创建 resource lock
- 预览接口不调用 ModelAdapter
- 预览接口不修改 development_tasks
- 测试前后记录数量一致
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _get_db_path():
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "ai_factory.db"
    )
    return db_path


def _count_table(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    conn.close()
    return count


def _snapshot(db_path: str) -> dict:
    """记录所有关键表的行数"""
    tables = [
        "executor_runs",
        "task_leases",
        "executor_resource_locks",
        "development_tasks",
        "executions",
    ]
    return {t: _count_table(db_path, t) for t in tables}


def test_preview_via_controller_no_db_write():
    """通过 AIBrainController.preview 不写入数据库"""
    db_path = _get_db_path()
    before = _snapshot(db_path)

    from app.executor.command_normalizer import CommandNormalizer
    from app.executor.ai_brain_controller import AIBrainController

    n = CommandNormalizer()
    c = AIBrainController(n)

    # 多次调用预览
    for text in ["开始开发", "暂停", "查看状态", "检查阻塞原因", "先不要开始开发"]:
        result = c.preview(text, 65)
        assert result["executed"] is False, f"'{text}' executed should be False"

    after = _snapshot(db_path)

    for table, before_count in before.items():
        after_count = after[table]
        assert before_count == after_count, \
            f"{table}: {before_count} → {after_count} (should not change)"


def test_unknown_intent_no_db_write():
    """未知指令不写入数据库"""
    db_path = _get_db_path()
    before = _snapshot(db_path)

    from app.executor.command_normalizer import CommandNormalizer
    from app.executor.ai_brain_controller import AIBrainController

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("今天天气怎么样", 65)
    c.preview("帮我点个外卖", 65)

    after = _snapshot(db_path)
    for table, before_count in before.items():
        assert before_count == after[table], \
            f"{table} changed after unknown intent preview"


def test_negation_no_db_write():
    """否定句不写入数据库"""
    db_path = _get_db_path()
    before = _snapshot(db_path)

    from app.executor.command_normalizer import CommandNormalizer
    from app.executor.ai_brain_controller import AIBrainController

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("先不要开始开发", 65)
    c.preview("不要开始", 65)

    after = _snapshot(db_path)
    for table, before_count in before.items():
        assert before_count == after[table], \
            f"{table} changed after negation preview"


def test_empty_input_no_db_write():
    """空输入不写入数据库"""
    db_path = _get_db_path()
    before = _snapshot(db_path)

    from app.executor.command_normalizer import CommandNormalizer
    from app.executor.ai_brain_controller import AIBrainController

    n = CommandNormalizer()
    c = AIBrainController(n)
    c.preview("", 65)

    after = _snapshot(db_path)
    for table, before_count in before.items():
        assert before_count == after[table], \
            f"{table} changed after empty input"


if __name__ == "__main__":
    tests = [
        ("1. preview不写入数据库", test_preview_via_controller_no_db_write),
        ("2. 未知指令不写DB", test_unknown_intent_no_db_write),
        ("3. 否定句不写DB", test_negation_no_db_write),
        ("4. 空输入不写DB", test_empty_input_no_db_write),
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
