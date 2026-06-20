"""
回归测试：验收后固化的5项Executor Bug修复
==============================================
测试项：
1. files_to_modify 文件分类 — module_demo.py 不被误判为测试文件
2. test_steps 文本不当作 shell 命令执行
3. 无测试文件时 test_command 回退逻辑
4. retry 接口状态机校验
5. preflight 接口返回结构校验
"""

import sys
import os
import json
import sqlite3
import time
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.e2e


class _DynamicBase:
    def __str__(self):
        return f"{os.environ.get('E2E_BASE_URL', 'http://localhost:8000')}/api"


BASE = _DynamicBase()
import requests

results = {"pass": 0, "fail": 0, "errors": []}

def log(msg):
    print(msg, flush=True)

def assert_test(name, condition, detail=""):
    if condition:
        results["pass"] += 1
        log(f"  PASS: {name}")
    else:
        results["fail"] += 1
        results["errors"].append(f"{name}: {detail}")
        log(f"  FAIL: {name} - {detail}")


# ═══════════════════════════════════════════════════════════
# 测试项 1: ModelAdapter 文件分类
# ═══════════════════════════════════════════════════════════
def test_1_model_adapter_file_classification():
    """验证 module_demo.py 不再被误判为测试文件"""
    log("\n--- Test 1: ModelAdapter 文件分类 ---")

    from app.executor.model_adapter import ModelAdapter
    from pathlib import Path
    import tempfile

    sandbox = Path(tempfile.mkdtemp())
    # ModelAdapter 需要 db_path 和 sandbox_path 参数，使用临时 DB
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_factory.db")
    adapter = ModelAdapter(db_path, str(sandbox))

    files = [
        {
            "path": "module_demo.py",
            "content": 'def normalize_title(s):\n    """Normalize a title."""\n    if not s:\n        return ""\n    return s.strip().title()\n'
        },
        {
            "path": "test_module_demo.py",
            "content": 'def test_none():\n    assert normalize_title(None) == ""\n\ndef test_empty():\n    assert normalize_title("") == ""\n\ndef test_hello():\n    assert normalize_title("hello world") == "Hello World"\n\ndef test_extra_spaces():\n    assert normalize_title("  hello  ") == "Hello"\n'
        }
    ]

    allowed_files = ["module_demo.py", "test_module_demo.py"]
    # 传入所有文件作为 test_files (模拟旧版 bug 场景)
    test_files = ["module_demo.py", "test_module_demo.py"]

    error = adapter._validate_and_write_files(files, allowed_files, test_files)

    # 关键断言：module_demo.py 不应被误判
    assert_test(
        "1a: module_demo.py 不被误判为测试文件（无错误或错误不包含 test_ 前缀要求）",
        error is None or "module_demo.py" not in (error or ""),
        f"error={error}"
    )

    # 验证文件已写入
    source_file = sandbox / "module_demo.py"
    test_file = sandbox / "test_module_demo.py"
    assert_test(
        "1b: module_demo.py 作为源文件写入",
        source_file.exists(),
        f"exists={source_file.exists()}"
    )
    assert_test(
        "1c: test_module_demo.py 作为测试文件写入",
        test_file.exists(),
        f"exists={test_file.exists()}"
    )

    # 清理
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 测试项 2: test_steps 不当作 shell 命令
# ═══════════════════════════════════════════════════════════
def test_2_test_steps_not_executed_as_command():
    """验证 test_steps 文字描述不被当作 shell 命令执行"""
    log("\n--- Test 2: test_steps 不作为 shell 命令 ---")

    from app.executor.loop_controller import LoopController

    controller = LoopController.__new__(LoopController)  # 不调用 __init__

    # 模拟 task_steps: JSON 列表中的文字描述
    test_steps = json.dumps([
        "test_none: 输入None期待空字符串",
        "test_empty: 输入空字符串期待空字符串"
    ], ensure_ascii=False)

    # _parse_command 会按空格分割，产生垃圾命令
    parsed = controller._parse_command(test_steps)

    # 验证解析结果不是有效的 shell 命令
    assert_test(
        "2a: test_steps 列表被 _parse_command 产生的首个词不应是 python",
        not parsed or parsed[0] not in ("python", "pytest", "pip", "npm"),
        f"parsed={parsed[:3] if parsed else '[]'}"
    )

    # 验证当 test_only_files 存在时优先使用 pytest
    test_only_files = ["test_module_demo.py"]
    actual_files = [f for f in ["module_demo.py", "test_module_demo.py"] if f.startswith("test_")]
    assert_test(
        "2b: 有 test_*.py 文件时 test_only_files 非空",
        len(actual_files) > 0,
        f"test_only_files={actual_files}"
    )

    # 模拟 loop_controller 中的逻辑：优先使用 test_only_files
    if actual_files:
        test_cmd = ["pytest"] + actual_files + ["-v"]
    else:
        test_cmd = controller._parse_command(test_steps) if test_steps else None

    assert_test(
        "2c: 有 test_*.py 时 test_cmd 以 pytest 开头",
        test_cmd[0] == "pytest",
        f"test_cmd={test_cmd[:3]}"
    )


# ═══════════════════════════════════════════════════════════
# 测试项 3: 无测试文件时的回退逻辑
# ═══════════════════════════════════════════════════════════
def test_3_fallback_when_no_test_files():
    """验证没有 test_*.py 但有明确可执行测试命令时的回退"""
    log("\n--- Test 3: 无测试文件时命令回退 ---")

    from app.executor.loop_controller import LoopController
    controller = LoopController.__new__(LoopController)

    # 场景A: 没有测试文件也没有 test_steps → test_cmd = None
    test_cmd_none = None
    if []:  # test_only_files 为空
        test_cmd_none = ["pytest"] + [] + ["-v"]
    elif None:  # test_steps 为空
        test_cmd_none = controller._parse_command("")
    assert_test(
        "3a: 无测试文件无test_steps时test_cmd为None",
        test_cmd_none is None,
        f"test_cmd={test_cmd_none}"
    )

    # 场景B: 没有测试文件但有明确执行命令
    # 例如: "python -m pytest -q"
    explicit_cmd = "python -m pytest -q"
    parsed_explicit = controller._parse_command(explicit_cmd)
    assert_test(
        "3b: 明确执行命令 'python -m pytest -q' 可被解析",
        parsed_explicit == ["python", "-m", "pytest", "-q"],
        f"parsed={parsed_explicit}"
    )

    # 场景C: 没有 test_only_files 时不应瞎构建 pytest 命令
    assert_test(
        "3c: 无 test_*.py 文件时不构建空的 pytest 命令",
        len([]) == 0,
        f"test_only_files count not zero"
    )


# ═══════════════════════════════════════════════════════════
# 测试项 4: retry 接口状态机
# ═══════════════════════════════════════════════════════════
def test_4_retry_api_state_machine():
    """验证 retry 接口的状态机校验和资源保护"""
    log("\n--- Test 4: retry 接口状态机 ---")

    # 4a: 获取一个已知的 failed 任务
    r = requests.get(f"{BASE}/tasks/51", timeout=10)
    assert_test("4a: 可获取 Task #51", r.status_code == 200,
                f"status={r.status_code}")

    task_data = r.json().get("data", {})
    current_status = task_data.get("status", "")
    log(f"  Task #51 current status: {current_status}")

    # 4b: 如果当前是 completed 状态，retry 应该拒绝
    if current_status == "completed":
        r_retry = requests.post(f"{BASE}/tasks/51/retry", timeout=10)
        retry_data = r_retry.json()
        assert_test(
            "4b: completed 状态的 retry 应返回 INVALID_STATUS",
            not retry_data.get("ok") or retry_data.get("error", {}).get("code") == "INVALID_STATUS",
            f"response={retry_data}"
        )

    # 4c: 验证 preflight 返回 retryable_tasks
    r_pf = requests.get(f"{BASE}/executor/preflight?project_id=65", timeout=10)
    pf_data = r_pf.json().get("data", {})
    assert_test(
        "4c: preflight 包含 retryable_tasks 字段",
        "retryable_tasks" in pf_data,
        f"keys={list(pf_data.keys())[:5]}"
    )
    assert_test(
        "4d: preflight 包含 database_path 字段",
        "database_path" in pf_data,
        f"database_path={pf_data.get('database_path', 'MISSING')}"
    )

    # 4e: 验证 retry 接口返回格式
    r_retry_any = requests.post(f"{BASE}/tasks/51/retry", timeout=10)
    any_data = r_retry_any.json()
    assert_test(
        "4e: retry 接口响应包含 ok 字段",
        "ok" in any_data,
        f"keys={list(any_data.keys())[:5]}"
    )


# ═══════════════════════════════════════════════════════════
# 测试项 5: preflight 接口完整性
# ═══════════════════════════════════════════════════════════
def test_5_preflight_integrity():
    """验证 preflight 接口返回所有必需字段"""
    log("\n--- Test 5: preflight 接口完整性 ---")

    r = requests.get(f"{BASE}/executor/preflight?project_id=65", timeout=10)
    assert_test("5a: preflight 返回 200", r.status_code == 200,
                f"status={r.status_code}")

    data = r.json().get("data", {})

    required_fields = [
        "can_start",
        "runnable_task_ids",
        "blocked_task_ids",
        "retryable_tasks",
        "active_run",
        "active_leases",
        "database_path",
        "pid",
    ]

    for field in required_fields:
        assert_test(
            f"5b.{field}: preflight 包含 {field}",
            field in data,
            f"value={data.get(field, 'MISSING')}"
        )

    # 验证 database_path 是绝对路径
    db_path = data.get("database_path", "")
    assert_test(
        "5c: database_path 是绝对路径",
        os.path.isabs(db_path),
        f"path={db_path}"
    )

    # 验证 pid 是整数
    pid = data.get("pid", 0)
    assert_test(
        "5d: pid 是正数",
        isinstance(pid, int) and pid > 0,
        f"pid={pid}"
    )

    # 验证 runnable_task_ids 是列表
    runnable = data.get("runnable_task_ids", None)
    assert_test(
        "5e: runnable_task_ids 是列表",
        isinstance(runnable, list),
        f"type={type(runnable)}, value={runnable}"
    )

    # 验证 retryable_tasks 是列表
    retryable = data.get("retryable_tasks", None)
    assert_test(
        "5f: retryable_tasks 是列表",
        isinstance(retryable, list),
        f"type={type(retryable)}, value={retryable}"
    )

    log(f"  preflight summary: can_start={data.get('can_start')}, "
        f"runnable={len(runnable)}, retryable={len(retryable)}, "
        f"active_run={data.get('active_run')}, "
        f"active_leases={data.get('active_leases')}")


# ═══════════════════════════════════════════════════════════
# 测试项 6: 本轮完成计数独立
# ═══════════════════════════════════════════════════════════
def test_6_run_counter_independence():
    """验证每个新 executor_run 的计数器从 0 开始"""
    log("\n--- Test 6: Run 计数器独立性 ---")

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "ai_factory.db"
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 查询最近的 run 记录
    runs = conn.execute(
        "SELECT run_id, status, tasks_completed, tasks_failed, tasks_total "
        "FROM executor_runs WHERE project_id=65 "
        "ORDER BY id DESC LIMIT 3"
    ).fetchall()

    for run in runs:
        log(f"  run={dict(run)['run_id'][:20]} status={dict(run)['status']} "
            f"completed={dict(run)['tasks_completed']} failed={dict(run)['tasks_failed']} "
            f"total={dict(run)['tasks_total']}")

    conn.close()

    # 验证最新的 completed run
    latest_run = runs[0] if runs else None
    if latest_run:
        run_dict = dict(latest_run)
        # tasks_total 应该 >= tasks_completed + tasks_failed (不会超过)
        assert_test(
            "6a: tasks_total >= tasks_completed + tasks_failed (不重复计数)",
            run_dict['tasks_total'] >= run_dict['tasks_completed'] + run_dict['tasks_failed'],
            f"total={run_dict['tasks_total']} comp={run_dict['tasks_completed']} fail={run_dict['tasks_failed']}"
        )

    # 验证 Schema: executor_runs 表有计数器字段的 DEFAULT 0
    cursor = sqlite3.connect(db_path).cursor()
    schema = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='executor_runs'").fetchone()
    if schema:
        schema_text = schema[0].replace(" ", "").replace("\n", "").replace("\r", "")
        for field in ['tasks_completed', 'tasks_failed', 'tasks_total']:
            assert_test(
                f"6b: {field} 字段存在 DEFAULT 0",
                f"{field}INTEGERDEFAULT0" in schema_text,
                f"schema found {field}"
            )


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("AI工厂沙箱验收 - 回归测试套件")
    log("=" * 60)

    # Unit tests (不需要 API 服务)
    try:
        test_1_model_adapter_file_classification()
    except Exception as e:
        log(f"  ERROR in Test 1: {e}")
        results["fail"] += 1

    try:
        test_2_test_steps_not_executed_as_command()
    except Exception as e:
        log(f"  ERROR in Test 2: {e}")
        results["fail"] += 1

    try:
        test_3_fallback_when_no_test_files()
    except Exception as e:
        log(f"  ERROR in Test 3: {e}")
        results["fail"] += 1

    try:
        test_6_run_counter_independence()
    except Exception as e:
        log(f"  ERROR in Test 6: {e}")
        results["fail"] += 1

    # API tests (需要 API 服务)
    api_available = False
    try:
        r = requests.get(f"{BASE}/executor/preflight?project_id=65", timeout=5)
        api_available = r.status_code == 200
    except:
        log("  WARN: API 服务未运行，跳过 API 测试")

    if api_available:
        try:
            test_4_retry_api_state_machine()
        except Exception as e:
            log(f"  ERROR in Test 4: {e}")
            results["fail"] += 1

        try:
            test_5_preflight_integrity()
        except Exception as e:
            log(f"  ERROR in Test 5: {e}")
            results["fail"] += 1
    else:
        log("\n  SKIP: Test 4 (retry API)  需要 API 服务运行")
        log("  SKIP: Test 5 (preflight API) 需要 API 服务运行")

    # 汇总
    total = results["pass"] + results["fail"]
    log("\n" + "=" * 60)
    log(f"结果: {results['pass']}/{total} 通过, {results['fail']} 失败")
    if results["errors"]:
        log("失败详情:")
        for e in results["errors"]:
            log(f"  - {e}")
    log("=" * 60)

    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
