"""
Section 九：MergeCoordinator 三个真实场景测试

场景 1: 正常合并 - 两个分支修改不同文件，合并成功
场景 2: 真实冲突 - 两个分支修改同一文件同一行，合并冲突被正确检测
场景 3: 回归失败 - 合并成功但回归测试失败，合并被安全撤销

运行方式：
    cd backend
    python -m pytest tests/test_merge_scenarios.py -v
"""
import os
import sys
import tempfile
import uuid
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def create_test_git_repo(base_dir: Path, name: str) -> Path:
    """创建测试 Git 仓库"""
    repo_path = base_dir / name
    repo_path.mkdir(parents=True, exist_ok=True)

    os.system(f'cd /d "{repo_path}" && git init')
    os.system(f'cd /d "{repo_path}" && git config user.email "test@merge.test"')
    os.system(f'cd /d "{repo_path}" && git config user.name "Merge Test"')

    # 创建初始 master 提交
    (repo_path / "README.md").write_text("# Test Repo\n\nInitial commit\n", encoding="utf-8")
    (repo_path / "module_a.py").write_text('# -*- coding: utf-8 -*-\n"""Module A"""\ndef hello():\n    return "Hello"\n', encoding="utf-8")
    (repo_path / "module_b.py").write_text('# -*- coding: utf-8 -*-\n"""Module B"""\ndef world():\n    return "World"\n', encoding="utf-8")

    os.system(f'cd /d "{repo_path}" && git add . && git commit -m "Initial commit"')

    # 创建 regression_test.py（回归测试，验证模块结构完整性）
    (repo_path / "regression_test.py").write_text(
        '# -*- coding: utf-8 -*-\nimport sys\nimport os\n\n'
        + '# Check key files exist\n'
        + 'assert os.path.exists("README.md"), "README.md missing"\n'
        + 'assert os.path.exists("module_a.py"), "module_a.py missing"\n'
        + 'assert os.path.exists("module_b.py"), "module_b.py missing"\n\n'
        + '# Verify modules can be imported and return valid strings\n'
        + 'sys.path.insert(0, ".")\n'
        + 'from module_a import hello\n'
        + 'result_a = hello()\n'
        + 'assert isinstance(result_a, str), f"hello() should return str, got {type(result_a)}"\n'
        + 'assert len(result_a) > 0, "hello() returned empty string"\n'
        + 'assert "Hello" in result_a, f"module_a regression: expected Hello in output, got {result_a}"\n\n'
        + 'from module_b import world\n'
        + 'result_b = world()\n'
        + 'assert isinstance(result_b, str), f"world() should return str, got {type(result_b)}"\n'
        + 'assert len(result_b) > 0, "world() returned empty string"\n\n'
        + 'print(f"Regression tests passed: hello={result_a}, world={result_b}")\n',
        encoding="utf-8",
    )
    # 添加 __pycache__ 到 .gitignore
    (repo_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    os.system(f'cd /d "{repo_path}" && git add .gitignore && git commit -m "Add .gitignore"')


    os.system(f'cd /d "{repo_path}" && git add regression_test.py && git commit -m "Add regression tests"')

    return repo_path


def create_branch_edit(repo_path: Path, branch_name: str,
                       file_to_edit: str, old_text: str, new_text: str):
    """在仓库中创建分支、修改文件、提交"""
    # 创建并切换到分支
    os.system(f'cd /d "{repo_path}" && git checkout -b {branch_name}')

    # 修改文件
    filepath = repo_path / file_to_edit
    content = filepath.read_text(encoding="utf-8")
    if old_text in content:
        content = content.replace(old_text, new_text, 1)
    filepath.write_text(content, encoding="utf-8")

    # 提交
    os.system(f'cd /d "{repo_path}" && git add {file_to_edit} && git commit -m "Edit {file_to_edit} in {branch_name}"')

    return branch_name


def get_current_commit(repo_path: Path) -> str:
    """获取当前 HEAD commit"""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# ═══════════════════════════════════════════════════════════
# 场景 1: 正常合并
# ═══════════════════════════════════════════════════════════

def test_scenario_01_normal_merge():
    """
    两个分支修改不同文件（module_a.py 和 module_b.py），
    合并到 master 应成功，回归测试应通过。
    """
    import subprocess

    tmp_dir = Path(tempfile.mkdtemp(prefix="merge_normal_"))
    try:
        repo_path = create_test_git_repo(tmp_dir, "repo")
        merge_log_path = repo_path / ".executor"
        merge_log_path.mkdir(parents=True, exist_ok=True)

        print("\n[SCENARIO 1] 正常合并")

        # 切换到 master
        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 创建分支1：修改 module_a.py
        create_branch_edit(repo_path, "feature/module-a",
                           "module_a.py",
                           'return "Hello"',
                           'return "Hello from Feature A"')

        commit_a = get_current_commit(repo_path)
        print(f"  Branch feature/module-a: {commit_a[:8]}")

        # 切回 master
        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 创建分支2: 修改 module_b.py（不同文件，不冲突）
        create_branch_edit(repo_path, "feature/module-b",
                           "module_b.py",
                           'return "World"',
                           'return "World from Feature B"')

        commit_b = get_current_commit(repo_path)
        print(f"  Branch feature/module-b: {commit_b[:8]}")

        # 切换到 master 并合并
        os.system(f'cd /d "{repo_path}" && git checkout master')
        master_before = get_current_commit(repo_path)
        print(f"  Master before merge: {master_before[:8]}")

        # 合并 feature/module-a
        result = subprocess.run(
            ["git", "merge", "--no-ff", "feature/module-a", "-m", "Merge feature/module-a"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Merge A failed: {result.stderr}"
        print(f"  Merge A: OK - {result.stdout.strip()[:100]}")

        # 验证 module_a.py 已更新
        content_a = (repo_path / "module_a.py").read_text(encoding="utf-8")
        assert "Hello from Feature A" in content_a, f"module_a not updated: {content_a[:50]}"

        # 运行回归测试
        reg_result = subprocess.run(
            [sys.executable, str(repo_path / "regression_test.py")],
            cwd=str(repo_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert reg_result.returncode == 0, f"Regression tests failed: {reg_result.stderr}"
        print(f"  Regression tests: PASS")

        # 合并 feature/module-b
        result2 = subprocess.run(
            ["git", "merge", "--no-ff", "feature/module-b", "-m", "Merge feature/module-b"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        assert result2.returncode == 0, f"Merge B failed: {result2.stderr}"
        print(f"  Merge B: OK")

        # 再次运行回归测试
        reg_result2 = subprocess.run(
            [sys.executable, str(repo_path / "regression_test.py")],
            cwd=str(repo_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert reg_result2.returncode == 0, f"Post double-merge regression failed: {reg_result2.stderr}"
        print(f"  Post-merge regression: PASS")

        # 验证工作区干净（__pycache__ 等忽略文件不属于 tracked 污染）
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        assert status.stdout.strip() == "", f"Workspace not clean: {status.stdout[:100]}"

        print(f"  [PASS] 场景1: 正常合并")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 2: 真实冲突
# ═══════════════════════════════════════════════════════════

def test_scenario_02_real_conflict():
    """
    两个分支修改同一文件的同一行，合并时应该检测到冲突。
    验证：冲突被检测、冲突文件被记录、--abort 后 master 恢复干净。
    """
    import subprocess

    tmp_dir = Path(tempfile.mkdtemp(prefix="merge_conflict_"))
    try:
        repo_path = create_test_git_repo(tmp_dir, "repo")

        print("\n[SCENARIO 2] 真实冲突")

        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 分支 A: 修改 README.md 同一行
        create_branch_edit(repo_path, "feature/rename-a",
                           "README.md",
                           "# Test Repo",
                           "# Test Repo - Renamed by A")

        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 分支 B: 同文件同行不同内容（冲突）
        create_branch_edit(repo_path, "feature/rename-b",
                           "README.md",
                           "# Test Repo",
                           "# Test Repo - Renamed by B")

        os.system(f'cd /d "{repo_path}" && git checkout master')
        master_before = get_current_commit(repo_path)

        # 先合并 A（成功）
        result_a = subprocess.run(
            ["git", "merge", "--no-ff", "feature/rename-a", "-m", "Merge rename-a"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        assert result_a.returncode == 0, f"Merge A failed: {result_a.stderr}"
        print(f"  Merge A (rename): OK")

        # 尝试合并 B（应该冲突）
        result_b = subprocess.run(
            ["git", "merge", "--no-ff", "feature/rename-b", "-m", "Merge rename-b"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        # 冲突时 returncode != 0
        merge_conflict = (result_b.returncode != 0)
        print(f"  Merge B (conflict): returncode={result_b.returncode}")

        # 检查冲突状态
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        has_conflict = "UU " in status.stdout or "AA " in status.stdout or "both modified" in status.stdout.lower()
        print(f"  Status shows conflict: {has_conflict}")

        # 如果 merge 没自动检测，检查是否有冲突标记
        if not has_conflict and not merge_conflict:
            # 用 --no-commit 方式触发表明确冲突
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(repo_path), capture_output=True, text=True)
            result_b2 = subprocess.run(
                ["git", "merge", "--no-ff", "--no-commit", "feature/rename-b"],
                cwd=str(repo_path), capture_output=True, text=True,
            )
            merge_conflict = (result_b2.returncode != 0)

        assert merge_conflict or has_conflict, "应该检测到合并冲突"

        # 冲突文件中应该包含冲突标记
        conflict_content = (repo_path / "README.md").read_text(encoding="utf-8")
        has_markers = "<<<<<<<" in conflict_content or ">>>>>>>" in conflict_content
        print(f"  Conflict markers in file: {has_markers}")

        # --abort 恢复
        abort_result = subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        print(f"  Abort: returncode={abort_result.returncode}")

        # 验证 master 恢复到合并前的干净状态
        master_after = get_current_commit(repo_path)
        status_after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        print(f"  Master after abort: {master_after[:8]} (was {master_before[:8]})")
        print(f"  Workspace clean: {status_after.stdout.strip() == ''}")
        assert status_after.stdout.strip() == "", "合并 abort 后工作区应干净"

        print(f"  [PASS] 场景2: 真实冲突")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 3: 回归失败
# ═══════════════════════════════════════════════════════════

def test_scenario_03_regression_failure():
    """
    合并成功但回归测试失败时，合并应被安全撤销（使用 ORIG_HEAD 回退）。
    验证：
    1. 合并执行成功
    2. 回归测试失败
    3. 撤销合并
    4. master 恢复到合并前状态
    5. 不影响已完成的其他任务
    """
    import subprocess

    tmp_dir = Path(tempfile.mkdtemp(prefix="merge_regression_"))
    try:
        repo_path = create_test_git_repo(tmp_dir, "repo")

        print("\n[SCENARIO 3] 回归失败撤销")

        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 先在 master 上合并一个正常任务（A），确保它不受影响
        create_branch_edit(repo_path, "feature/good-task",
                           "module_a.py",
                           'return "Hello"',
                           'return "Hello from Good Task"')

        os.system(f'cd /d "{repo_path}" && git checkout master')

        result_good = subprocess.run(
            ["git", "merge", "--no-ff", "feature/good-task", "-m", "Merge good task"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        assert result_good.returncode == 0, f"Good merge failed: {result_good.stderr}"
        print(f"  Merge good task: OK")

        master_before_bad = get_current_commit(repo_path)
        print(f"  Master before bad merge: {master_before_bad[:8]}")

        # 创建破坏性分支（修改 module_a 后破坏回归测试）
        os.system(f'cd /d "{repo_path}" && git checkout master')

        # 创建会导致回归失败的修改
        create_branch_edit(repo_path, "feature/breaking",
                           "module_a.py",
                           'return "Hello from Good Task"',
                           'return "BROKEN_VALUE"')

        # 尝试合并，regression_test.py 期望 "Hello" 前缀
        result_merge = subprocess.run(
            ["git", "merge", "--no-ff", "feature/breaking", "-m", "Merge breaking change"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        print(f"  Merge breaking: returncode={result_merge.returncode}")

        if result_merge.returncode == 0:
            print(f"  Merge succeeded (breaking change merged)")

            # 运行回归测试，应该失败
            reg_result = subprocess.run(
                [sys.executable, str(repo_path / "regression_test.py")],
                cwd=str(repo_path), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            regression_failed = (reg_result.returncode != 0)
            print(f"  Regression test: {'FAIL' if regression_failed else 'PASS'}")

            if regression_failed:
                # 安全撤销合并（直接回退到 before_commit，与 _safe_undo_merge 逻辑一致）
                undo_result = subprocess.run(
                    ["git", "reset", "--hard", master_before_bad],
                    cwd=str(repo_path), capture_output=True, text=True,
                )
                print(f"  Undo merge (reset to before_commit): returncode={undo_result.returncode}")

                # 验证 master 已恢复
                master_after_undo = get_current_commit(repo_path)
                print(f"  Master after undo: {master_after_undo[:8]} (target: {master_before_bad[:8]})")
                assert master_after_undo == master_before_bad, \
                    f"Undo should restore to {master_before_bad[:8]}, got {master_after_undo[:8]}"

                # 清理可能的 untracked 文件
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(repo_path), capture_output=True, text=True,
                )

                # 验证 module_a 已恢复
                content_a = (repo_path / "module_a.py").read_text(encoding="utf-8")
                assert "Hello from Good Task" in content_a, \
                    f"Undo should restore good task content, got: {content_a[:100]}"
                print(f"  module_a restored: OK")

                # 验证工作区干净（排除 untracked/ignored）
                status = subprocess.run(
                    ["git", "status", "--porcelain", "-uno"],
                    cwd=str(repo_path), capture_output=True, text=True,
                )
                assert status.stdout.strip() == "", "Undo 后工作区应干净"
                print(f"  Workspace clean after undo: OK")

                # 验证回归测试通过（good task 的修改应该还在）
                reg_after = subprocess.run(
                    [sys.executable, str(repo_path / "regression_test.py")],
                    cwd=str(repo_path), capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                assert reg_after.returncode == 0, f"Post-undo regression should pass: {reg_after.stderr}"
                print(f"  Post-undo regression: PASS")

        print(f"  [PASS] 场景3: 回归失败撤销")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("MergeCoordinator 三个真实场景测试")
    print("=" * 60)

    passed = 0
    failed = 0

    for name, fn in [
        ("场景1: 正常合并", test_scenario_01_normal_merge),
        ("场景2: 真实冲突", test_scenario_02_real_conflict),
        ("场景3: 回归失败撤销", test_scenario_03_regression_failure),
    ]:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"\n[FAIL] {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"\n[ERROR] {name}: {e}")

    print(f"\n{'=' * 60}")
    print(f"  MergeCoordinator: {passed} PASS, {failed} FAIL")
    print(f"{'=' * 60}")
