"""V1.7E 测试命令选择测试

测试 TaskWorker._detect_test_command 和 _run_tests 的行为。
"""
import sys
import os
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 不直接导入 TaskWorker（它需要 db_path），而是测试 _detect_test_command 逻辑
from app.executor.task_worker import TaskWorker

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


def test_no_project_path():
    """没有项目路径时返回 NO_TEST_COMMAND_CONFIGURED"""
    # 创建一个不需要 db_path 的最小化 worker 来测试静态方法
    # _detect_test_command 是实例方法但只使用 repo_path
    worker = TaskWorker.__new__(TaskWorker)
    worker.repo_path = None
    cmd, label = worker._detect_test_command(None)
    assert_test("无项目路径返回空", cmd is None and label == "no_project_path",
                f"cmd={cmd}, label={label}")


def test_python_project_with_pytest_ini():
    """Python 项目有 pytest.ini → 返回 pytest"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "test_example.py").write_text("def test_pass(): pass\n")

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("Python pytest.ini → pytest", cmd == ["pytest", "-v", "--tb=short"] and label == "pytest",
                    f"cmd={cmd}, label={label}")


def test_python_project_with_test_dir():
    """Python 项目有 tests/ 目录 → 返回 pytest"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_example.py").write_text("def test_pass(): pass\n")

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("Python tests/ → pytest", cmd == ["pytest", "-v", "--tb=short"] and label == "pytest",
                    f"cmd={cmd}, label={label}")


def test_node_project_with_test_script():
    """Node 项目有 npm test → 返回 npm test"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg = {"scripts": {"test": "jest", "build": "tsc"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("Node npm test → npm test", cmd == ["npm", "test"] and label == "npm_test",
                    f"cmd={cmd}, label={label}")


def test_node_project_with_typecheck():
    """Node 项目无 test 有 typecheck → 返回 npm run typecheck"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg = {"scripts": {"typecheck": "tsc --noEmit", "build": "tsc"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("Node npm typecheck → npm run typecheck",
                    cmd == ["npm", "run", "typecheck"] and label == "npm_typecheck",
                    f"cmd={cmd}, label={label}")


def test_node_project_with_build():
    """Node 项目无 test/typecheck 有 build → 返回 npm run build"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg = {"scripts": {"build": "tsc"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("Node npm build → npm run build",
                    cmd == ["npm", "run", "build"] and label == "npm_build",
                    f"cmd={cmd}, label={label}")


def test_empty_project_no_test_config():
    """空项目无任何测试配置 → NO_TEST_COMMAND_CONFIGURED"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "main.py").write_text("print('hello')")

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("空项目 → NO_TEST_COMMAND_CONFIGURED",
                    cmd is None and label == "NO_TEST_COMMAND_CONFIGURED",
                    f"cmd={cmd}, label={label}")


def test_invalid_package_json():
    """无效 package.json 不崩溃"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "package.json").write_text("{invalid json")

        worker = TaskWorker.__new__(TaskWorker)
        worker.repo_path = tmp_path
        cmd, label = worker._detect_test_command(str(tmp_path))

        assert_test("无效 package.json → NO_TEST_COMMAND_CONFIGURED",
                    cmd is None and label == "NO_TEST_COMMAND_CONFIGURED",
                    f"cmd={cmd}, label={label}")


if __name__ == "__main__":
    print("=" * 60)
    print("V1.7E 测试命令选择测试")
    print("=" * 60)

    test_no_project_path()
    test_python_project_with_pytest_ini()
    test_python_project_with_test_dir()
    test_node_project_with_test_script()
    test_node_project_with_typecheck()
    test_node_project_with_build()
    test_empty_project_no_test_config()
    test_invalid_package_json()

    print()
    print(f"结果: {results['pass']} passed, {results['fail']} failed")
    if results["errors"]:
        for e in results["errors"]:
            print(e)
    sys.exit(1 if results["fail"] > 0 else 0)
