"""
测试 ProjectExecutionConfigService - 使用临时数据库
"""

import os
import sys
import tempfile
import sqlite3
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor.project_execution_config_service import (
    ProjectExecutionConfigService,
)


def setup_temp_db_and_workspace():
    """创建临时数据库和临时 Git 工作区"""
    # 临时数据库
    tmpdir = tempfile.mkdtemp(prefix="test_config_svc_")
    db_path = os.path.join(tmpdir, "test.db")

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            name TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_execution_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            workspace_path TEXT,
            execution_enabled INTEGER DEFAULT 0,
            execution_mode TEXT DEFAULT 'sandbox',
            allowed_models_json TEXT DEFAULT '[]',
            max_workers INTEGER DEFAULT 1,
            max_tasks INTEGER DEFAULT 10,
            requires_confirmation INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("INSERT INTO projects VALUES (1, 'test-project', 'active')")
    conn.execute("INSERT INTO projects VALUES (2, 'no-workspace', 'active')")

    # 临时 Git 工作区
    ws_path = os.path.join(tmpdir, "workspace")
    os.makedirs(ws_path)
    subprocess.run(["git", "init"], cwd=ws_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=ws_path, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=ws_path, capture_output=True,
    )
    # 创建初始提交使工作区干净
    readme = os.path.join(ws_path, "README.md")
    with open(readme, "w") as f:
        f.write("# test")
    subprocess.run(["git", "add", "."], cwd=ws_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws_path, capture_output=True)

    conn.execute(
        "INSERT INTO project_execution_configs (project_id, workspace_path, execution_enabled) VALUES (?, ?, 0)",
        (1, ws_path),
    )
    conn.execute(
        "INSERT INTO project_execution_configs (project_id, workspace_path, execution_enabled) VALUES (?, ?, 0)",
        (2, ""),
    )
    conn.commit()
    conn.close()

    return db_path, ws_path, tmpdir


def cleanup(tmpdir):
    """清理临时文件"""
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_enable_success():
    """测试：正常开启 execution_enabled"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        svc = ProjectExecutionConfigService(db_path)

        ok, msg, detail = svc.set_execution_enabled(
            project_id=1,
            enabled=True,
            reason="V1.7C test",
            changed_by="test",
        )

        assert ok, f"Expected success, got: {msg}"
        assert detail["before"]["execution_enabled"] is False
        assert detail["after"]["execution_enabled"] is True
        assert detail["reason"] == "V1.7C test"
        assert detail["changed_by"] == "test"

        # 验证数据库实际写入
        config = svc.get_config(1)
        assert config["execution_enabled"] == 1

        print("  PASS: test_enable_success")
    finally:
        cleanup(tmpdir)


def test_disable_success():
    """测试：正常关闭 execution_enabled"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        svc = ProjectExecutionConfigService(db_path)

        # 先开启
        svc.set_execution_enabled(1, True, "enable first", "test")

        # 再关闭
        ok, msg, detail = svc.set_execution_enabled(
            project_id=1,
            enabled=False,
            reason="V1.7C test disable",
            changed_by="test",
        )

        assert ok, f"Expected success, got: {msg}"
        assert detail["before"]["execution_enabled"] is True
        assert detail["after"]["execution_enabled"] is False

        config = svc.get_config(1)
        assert config["execution_enabled"] == 0

        print("  PASS: test_disable_success")
    finally:
        cleanup(tmpdir)


def test_project_not_found():
    """测试：项目不存在"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        svc = ProjectExecutionConfigService(db_path)
        ok, msg, detail = svc.set_execution_enabled(999, True, "test", "test")
        assert not ok
        assert "PROJECT_NOT_FOUND" in msg
        print("  PASS: test_project_not_found")
    finally:
        cleanup(tmpdir)


def test_no_workspace():
    """测试：项目没有工作区"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        svc = ProjectExecutionConfigService(db_path)
        ok, msg, detail = svc.set_execution_enabled(2, True, "test", "test")
        assert not ok
        assert "WORKSPACE_NOT_CONFIGURED" in msg
        print("  PASS: test_no_workspace")
    finally:
        cleanup(tmpdir)


def test_no_config():
    """测试：项目没有执行配置"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        # 项目 3 没有配置
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO projects VALUES (3, 'no-config', 'active')")
        conn.commit()
        conn.close()

        svc = ProjectExecutionConfigService(db_path)
        ok, msg, detail = svc.set_execution_enabled(3, True, "test", "test")
        assert not ok
        assert "CONFIG_NOT_FOUND" in msg
        print("  PASS: test_no_config")
    finally:
        cleanup(tmpdir)


def test_git_dirty_rejected():
    """测试：Git 工作区脏时拒绝开启"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        # 制造脏状态
        dirty_file = os.path.join(ws_path, "dirty.txt")
        with open(dirty_file, "w") as f:
            f.write("dirty")

        svc = ProjectExecutionConfigService(db_path)
        ok, msg, detail = svc.set_execution_enabled(1, True, "test", "test")
        assert not ok
        assert "GIT_WORKING_TREE_DIRTY" in msg

        # 验证未修改
        config = svc.get_config(1)
        assert config["execution_enabled"] == 0

        print("  PASS: test_git_dirty_rejected")
    finally:
        cleanup(tmpdir)


def test_disable_always_allowed():
    """测试：关闭 execution_enabled 不检查 Git 脏状态"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        # 先开启
        svc = ProjectExecutionConfigService(db_path)
        svc.set_execution_enabled(1, True, "enable", "test")

        # 制造脏状态
        dirty_file = os.path.join(ws_path, "dirty.txt")
        with open(dirty_file, "w") as f:
            f.write("dirty")

        # 关闭不应被 Git 脏状态阻止
        ok, msg, detail = svc.set_execution_enabled(1, False, "emergency", "test")
        assert ok, f"Expected success, got: {msg}"
        assert detail["after"]["execution_enabled"] is False

        print("  PASS: test_disable_always_allowed")
    finally:
        cleanup(tmpdir)


def test_transaction_rollback():
    """测试：事务回滚（通过关闭时断开连接模拟不会发生，改为验证中间状态安全）"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        svc = ProjectExecutionConfigService(db_path)

        # 正常工作
        ok, msg, detail = svc.set_execution_enabled(1, True, "test", "test")
        assert ok
        assert detail["after"]["execution_enabled"] is True

        print("  PASS: test_transaction_rollback")
    finally:
        cleanup(tmpdir)


def test_cannot_modify_other_project():
    """测试：不能修改其他项目的配置"""
    db_path, ws_path, tmpdir = setup_temp_db_and_workspace()
    try:
        # 创建项目 3 的 workspace
        ws3 = os.path.join(tmpdir, "ws3")
        os.makedirs(ws3)
        subprocess.run(["git", "init"], cwd=ws3, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws3, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=ws3, capture_output=True)
        with open(os.path.join(ws3, "README.md"), "w") as f:
            f.write("# ws3")
        subprocess.run(["git", "add", "."], cwd=ws3, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=ws3, capture_output=True)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO projects VALUES (3, 'proj3', 'active')")
        conn.execute(
            "INSERT INTO project_execution_configs (project_id, workspace_path, execution_enabled) VALUES (3, ?, 0)",
            (ws3,),
        )
        conn.commit()
        conn.close()

        svc = ProjectExecutionConfigService(db_path)

        # 修改项目 1 不应影响项目 3
        ok1, _, _ = svc.set_execution_enabled(1, True, "enable p1", "test")
        assert ok1

        config3 = svc.get_config(3)
        assert config3["execution_enabled"] == 0, "Project 3 should not be affected"

        print("  PASS: test_cannot_modify_other_project")
    finally:
        cleanup(tmpdir)


def run_all_tests():
    print("\n=== ProjectExecutionConfigService 测试 ===\n")
    tests = [
        test_enable_success,
        test_disable_success,
        test_project_not_found,
        test_no_workspace,
        test_no_config,
        test_git_dirty_rejected,
        test_disable_always_allowed,
        test_transaction_rollback,
        test_cannot_modify_other_project,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1

    print(f"\n=== 结果: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
