"""V1.7E 文件语法验证测试

测试 ModelAdapter._validate_file_content 按文件类型分别验证。
"""
import sys
import os
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.executor.model_adapter import ModelAdapter

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


# ── 测试: 合法 .py 通过 ──
def test_valid_py_passes():
    err = ModelAdapter._validate_file_content(
        "def hello():\n    return 'world'\n", "test_module.py"
    )
    assert_test("合法 .py 通过", err is None, f"error={err}")


# ── 测试: 非法 .py 拒绝 ──
def test_invalid_py_rejected():
    err = ModelAdapter._validate_file_content(
        "def hello(\n    return 'world'\n", "bad_syntax.py"
    )
    assert_test("非法 .py 拒绝", err is not None and "语法错误" in err, f"error={err}")


# ── 测试: 合法 .json 通过 ──
def test_valid_json_passes():
    err = ModelAdapter._validate_file_content(
        '{"name": "test", "value": 42}', "config.json"
    )
    assert_test("合法 .json 通过", err is None, f"error={err}")


# ── 测试: 非法 .json 拒绝 ──
def test_invalid_json_rejected():
    err = ModelAdapter._validate_file_content(
        '{name: test, invalid json}', "bad_config.json"
    )
    assert_test("非法 .json 拒绝", err is not None and "JSON" in err, f"error={err}")


# ── 测试: .txt 普通文本通过（不会被 Python compile 拒绝） ──
def test_txt_passes():
    err = ModelAdapter._validate_file_content(
        "AI Dev Factory executor pipeline verified.", "executor_probe.txt"
    )
    assert_test(".txt 普通文本通过", err is None, f"error={err}")


# ── 测试: .ts 不会被 Python compile 拒绝 ──
def test_ts_passes():
    err = ModelAdapter._validate_file_content(
        "const x: number = 1;\nexport function foo(): string { return 'bar'; }",
        "module.ts"
    )
    assert_test(".ts 不会被 Python compile 拒绝", err is None, f"error={err}")


# ── 测试: .tsx 不会被 Python compile 拒绝 ──
def test_tsx_passes():
    err = ModelAdapter._validate_file_content(
        "import React from 'react';\nexport const App: React.FC = () => <div>Hello</div>;",
        "App.tsx"
    )
    assert_test(".tsx 不会被 Python compile 拒绝", err is None, f"error={err}")


# ── 测试: .js 不会被 Python compile 拒绝 ──
def test_js_passes():
    err = ModelAdapter._validate_file_content(
        "function hello() { return 'world'; }", "module.js"
    )
    assert_test(".js 不会被 Python compile 拒绝", err is None, f"error={err}")


# ── 测试: .md 通过 ──
def test_md_passes():
    err = ModelAdapter._validate_file_content(
        "# Hello\n\nThis is markdown.", "README.md"
    )
    assert_test(".md 通过", err is None, f"error={err}")


# ── 测试: 空内容不报错 ──
def test_empty_content_passes():
    err = ModelAdapter._validate_file_content("", "empty.py")
    assert_test("空内容不报错", err is None, f"error={err}")


# ── 测试: 未知扩展名不报错 ──
def test_unknown_extension_passes():
    err = ModelAdapter._validate_file_content("some content", "file.xyz")
    assert_test("未知扩展名不报错", err is None, f"error={err}")


# ── 运行所有测试 ──
if __name__ == "__main__":
    print("=" * 60)
    print("V1.7E 文件语法验证测试")
    print("=" * 60)

    test_valid_py_passes()
    test_invalid_py_rejected()
    test_valid_json_passes()
    test_invalid_json_rejected()
    test_txt_passes()
    test_ts_passes()
    test_tsx_passes()
    test_js_passes()
    test_md_passes()
    test_empty_content_passes()
    test_unknown_extension_passes()

    print()
    print(f"结果: {results['pass']} passed, {results['fail']} failed")
    if results["errors"]:
        for e in results["errors"]:
            print(e)
    sys.exit(1 if results["fail"] > 0 else 0)
