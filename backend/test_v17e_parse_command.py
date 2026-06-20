"""V1.7E test_steps 解析测试

测试 LoopController._parse_command 支持多种格式。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.executor.loop_controller import LoopController

results = {"pass": 0, "fail": 0, "errors": []}


def assert_test(name, condition, detail=""):
    if condition:
        results["pass"] += 1
        print(f"  [PASS] {name}")
    else:
        results["fail"] += 1
        msg = f"  [FAIL] {name}" + (f" - {detail}" if detail else "")
        results["errors"].append(msg)
        print(msg)


def make_controller():
    """创建最小化的 LoopController 用于测试 _parse_command"""
    ctrl = LoopController.__new__(LoopController)
    return ctrl


# ── 测试: JSON 数组正确解析 ──
def test_json_array_parsed():
    ctrl = make_controller()
    result = ctrl._parse_command('["npm run typecheck", "npm run build"]')
    assert_test(
        "JSON 数组解析第一条",
        result == ["npm", "run", "typecheck"],
        f"got: {result}"
    )


# ── 测试: 单命令正确解析 ──
def test_single_command_parsed():
    ctrl = make_controller()
    result = ctrl._parse_command("npm run typecheck")
    assert_test(
        "单命令正确解析",
        result == ["npm", "run", "typecheck"],
        f"got: {result}"
    )


# ── 测试: 多行命令取第一行 ──
def test_multiline_command():
    ctrl = make_controller()
    result = ctrl._parse_command("npm run typecheck\nnpm run build")
    assert_test(
        "多行命令取第一行",
        result == ["npm", "run", "typecheck"],
        f"got: {result}"
    )


# ── 测试: 带引号参数正确保留 ──
def test_quoted_args_preserved():
    ctrl = make_controller()
    result = ctrl._parse_command('python -c "print(1)"')
    assert_test(
        "带引号参数正确保留",
        result == ["python", "-c", "print(1)"],
        f"got: {result}"
    )


# ── 测试: Windows cmd 命令正确执行 ──
def test_windows_cmd():
    ctrl = make_controller()
    result = ctrl._parse_command("cmd /c exit 0")
    assert_test(
        "Windows cmd 正确拆分",
        result == ["cmd", "/c", "exit", "0"],
        f"got: {result}"
    )


# ── 测试: Run 前缀格式 ──
def test_run_prefix():
    ctrl = make_controller()
    result = ctrl._parse_command("Run python test.py")
    assert_test(
        "Run 前缀格式",
        result == ["python", "test.py"],
        f"got: {result}"
    )


# ── 测试: 空字符串返回空列表 ──
def test_empty_string():
    ctrl = make_controller()
    result = ctrl._parse_command("")
    assert_test("空字符串返回空列表", result == [], f"got: {result}")


# ── 测试: None 返回空列表 ──
def test_none_value():
    ctrl = make_controller()
    result = ctrl._parse_command(None)
    assert_test("None 返回空列表", result == [], f"got: {result}")


# ── 测试: 空白字符串返回空列表 ──
def test_whitespace_only():
    ctrl = make_controller()
    result = ctrl._parse_command("   \n  \t  ")
    assert_test("空白字符串返回空列表", result == [], f"got: {result}")


# ── 测试: JSON 空数组 ──
def test_json_empty_array():
    ctrl = make_controller()
    result = ctrl._parse_command("[]")
    assert_test("JSON 空数组返回空列表", result == [], f"got: {result}")


# ── 测试: Python 列表格式 ──
def test_python_list_format():
    ctrl = make_controller()
    result = ctrl._parse_command("['npm run typecheck']")
    # Python 单引号列表不是合法 JSON，会走其他分支
    # 但会尝试 JSON 解析失败后走单条命令分支
    # 预期：被当作单条命令按空格分割
    assert_test(
        "Python 列表格式不崩溃",
        isinstance(result, list),
        f"got: {result}"
    )


# ── 测试: 带空格的参数正确拆分 ──
def test_spaced_args():
    ctrl = make_controller()
    result = ctrl._parse_command("pytest tests/ -v --tb=short")
    assert_test(
        "pytest 参数正确拆分",
        result == ["pytest", "tests/", "-v", "--tb=short"],
        f"got: {result}"
    )


if __name__ == "__main__":
    print("=" * 60)
    print("V1.7E test_steps 解析测试")
    print("=" * 60)

    test_json_array_parsed()
    test_single_command_parsed()
    test_multiline_command()
    test_quoted_args_preserved()
    test_windows_cmd()
    test_run_prefix()
    test_empty_string()
    test_none_value()
    test_whitespace_only()
    test_json_empty_array()
    test_python_list_format()
    test_spaced_args()

    print()
    print(f"结果: {results['pass']} passed, {results['fail']} failed")
    if results["errors"]:
        for e in results["errors"]:
            print(e)
    sys.exit(1 if results["fail"] > 0 else 0)
