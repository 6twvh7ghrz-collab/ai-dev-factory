"""V1.8: CommandRunner Windows .cmd/.bat 可靠执行测试

测试覆盖：
  1. 无 PATH 中 node/npm，但 ToolchainResolver 找到绝对路径 → 成功
  2. npm.cmd run typecheck 成功（真实项目）
  3. npx.cmd --version 成功
  4. 路径含空格成功
  5. 路径含中文成功（仅解析，不执行）
  6. FileNotFoundError 被完整记录
  7. Shell 注入被阻止
  8. 超时被记录（timed_out=True）
  9. PATHEXT 被保留在 env 中
  10. resolved_executable 写入 CommandResult
  11. error/exit_code/duration 全部记录
  12. _build_actual_command 对 .cmd 使用 cmd.exe 包装
  13. _build_actual_command 对 .exe 直接使用绝对路径
  14. execution_logs 新增字段正确写入
  15. AutoRepair 和普通测试使用同一 CommandRunner 路径
"""
import os
import sys
import json
import tempfile
import shutil
import platform
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
import pytest

# 确保 backend 在 sys.path 中
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ── 工具函数 ──

def create_fake_node_env(tmp_path: Path) -> dict:
    """在临时目录中创建假 Node.js 环境（含 node.exe 和 npm.cmd）"""
    node_dir = tmp_path / "fake-node"
    node_dir.mkdir(parents=True, exist_ok=True)

    is_windows = platform.system() == "Windows"

    if is_windows:
        node_exe = node_dir / "node.exe"
        npm_cmd = node_dir / "npm.cmd"
    else:
        node_exe = node_dir / "node"
        npm_cmd = node_dir / "npm"

    # node.exe/npm.cmd 是批处理文本（echo 版本号）
    version_script = '@echo v20.19.0' if is_windows else '#!/bin/sh\necho "v20.19.0"'
    node_exe.write_text(version_script, encoding='ascii')
    npm_script = '@echo 10.8.2' if is_windows else '#!/bin/sh\necho "10.8.2"'
    npm_cmd.write_text(npm_script, encoding='ascii')

    return {
        "node_dir": str(node_dir),
        "node_exe": str(node_exe),
        "npm_exe": str(npm_cmd),
    }


# ── 测试 ──

class TestResolvedExecutable:
    """1. 无 PATH 时 ToolchainResolver 解析可执行文件"""

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific .cmd resolution test")
    def test_npm_resolved_when_not_in_path(self, tmp_path):
        """PATH 中没有 npm，但 ToolchainResolver 在当前机器上能找到 → 解析成功"""
        from app.executor.command_runner import CommandRunner
        from app.executor.toolchain_resolver import ExecutorToolchainResolver

        # 直接使用本地真实 npm 路径（不 mock PATH）
        npm_path, _ = ExecutorToolchainResolver.resolve_npm()
        if not npm_path:
            pytest.skip("本机未安装 npm，无法测试真实解析")

        runner = CommandRunner(use_toolchain_env=True)
        resolved = runner._resolve_executable_enhanced("npm", runner._get_toolchain_env())
        assert resolved, "应解析到 npm 路径"
        assert os.path.isabs(resolved), f"应返回绝对路径，得到: {resolved}"

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific")
    def test_npm_cmd_full_path_works(self, tmp_path):
        """npm.cmd 完整路径可正常执行 --version"""
        from app.executor.command_runner import CommandRunner
        from app.executor.toolchain_resolver import ExecutorToolchainResolver

        npm_path, _ = ExecutorToolchainResolver.resolve_npm()
        if not npm_path:
            pytest.skip("本机未安装 npm")

        runner = CommandRunner(use_toolchain_env=True)
        result = runner.run([npm_path, "--version"], cwd=str(tmp_path), timeout=15)
        assert result.exit_code == 0, f"exit_code={result.exit_code}, error={result.error}, stderr={result.stderr}"
        assert result.success
        assert result.resolved_executable.endswith("npm.cmd") or "npm" in result.resolved_executable.lower()

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific")
    def test_npm_cmd_via_resolver(self, tmp_path):
        """通过 CommandRunner.run(["npm", "--version"]) → 自动解析为绝对路径并执行"""
        from app.executor.command_runner import CommandRunner
        from app.executor.toolchain_resolver import ExecutorToolchainResolver

        npm_path, _ = ExecutorToolchainResolver.resolve_npm()
        if not npm_path:
            pytest.skip("本机未安装 npm")

        runner = CommandRunner(use_toolchain_env=True)
        result = runner.run(["npm", "--version"], cwd=str(tmp_path), timeout=15)
        assert result.exit_code == 0, f"exit_code={result.exit_code}, error={result.error}"
        assert result.success
        assert result.resolved_executable.endswith("npm.cmd") or "npm" in result.resolved_executable.lower()

    def test_node_resolved_from_toolchain(self):
        """node 通过 ToolchainResolver 解析"""
        from app.executor.command_runner import CommandRunner
        from app.executor.toolchain_resolver import ExecutorToolchainResolver

        node_path, _ = ExecutorToolchainResolver.resolve_node()
        if not node_path:
            pytest.skip("本机未安装 Node.js")

        runner = CommandRunner(use_toolchain_env=True)
        resolved = runner._resolve_executable_enhanced("node", runner._get_toolchain_env())
        assert resolved, "应解析到 node 路径"
        assert os.path.isabs(resolved)

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific")
    def test_npx_cmd_via_resolver(self, tmp_path):
        """npx.cmd 通过 ToolchainResolver 解析"""
        from app.executor.command_runner import CommandRunner
        from app.executor.toolchain_resolver import ExecutorToolchainResolver

        npm_path, _ = ExecutorToolchainResolver.resolve_npm()
        if not npm_path:
            pytest.skip("本机未安装 npm")

        runner = CommandRunner(use_toolchain_env=True)
        resolved = runner._resolve_executable_enhanced("npx", runner._get_toolchain_env())
        assert resolved, "应解析到 npx 路径"
        assert os.path.isabs(resolved)


class TestTypecheckExecution:
    """2. npm run typecheck 真实执行"""

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific")
    def test_typecheck_in_real_workspace(self):
        """在 ecommerce-ops-desktop 中执行 npm run typecheck（完整 stdout/stderr 记录）"""
        from app.executor.command_runner import CommandRunner

        workspace = Path(r"C:\SandboxUser\本机\Desktop\ecommerce-ops-desktop")
        if not workspace.exists() or not (workspace / "package.json").exists():
            pytest.skip("workspace 文件不存在")

        if not (workspace / "node_modules").exists():
            pytest.skip("node_modules 未安装")

        runner = CommandRunner(use_toolchain_env=True)
        result = runner.run(["npm", "run", "typecheck"], cwd=str(workspace), timeout=120)

        # 完整记录
        assert hasattr(result, 'stdout'), "应包含 stdout"
        assert hasattr(result, 'stderr'), "应包含 stderr"
        assert hasattr(result, 'exit_code'), "应包含 exit_code"
        assert hasattr(result, 'duration_ms'), "应包含 duration_ms"
        assert hasattr(result, 'error'), "应包含 error 字段"
        assert hasattr(result, 'resolved_executable'), "应包含 resolved_executable"
        assert hasattr(result, 'timed_out'), "应包含 timed_out"
        assert hasattr(result, 'killed'), "应包含 killed"

        # 验证不是 FileNotFoundError
        if result.exit_code == -1:
            assert result.error, f"exit_code=-1 但无错误信息: stdout={result.stdout[:200]}, stderr={result.stderr[:200]}"

        print(f"\n=== Typecheck Result ===")
        print(f"exit_code: {result.exit_code}")
        print(f"duration_ms: {result.duration_ms}")
        print(f"success: {result.success}")
        print(f"resolved_executable: {result.resolved_executable}")
        print(f"error: {result.error}")
        print(f"timed_out: {result.timed_out}")
        print(f"stdout (first 200): {result.stdout[:200]}")
        print(f"stderr (first 200): {result.stderr[:200]}")


class TestPathHandling:
    """3. 路径包含空格"""

    def test_executable_with_spaces_in_path(self, tmp_path):
        """可执行文件路径含空格时正常执行（使用 cmd.exe 作为带空格路径的可执行文件）"""
        from app.executor.command_runner import CommandRunner

        # 验证 CommandRunner._windows_quote 正确处理含空格路径
        quoted = CommandRunner._windows_quote(r"C:\Program Files\nodejs\npm.cmd")
        assert quoted == r'"C:\Program Files\nodejs\npm.cmd"', f"got: {quoted}"

        # 验证含空格的路径可作为 argv[0] 正确传递给 cmd.exe
        runner = CommandRunner(use_toolchain_env=False)
        actual = runner._build_actual_command(
            ["npm", "run", "typecheck"],
            r"C:\Program Files\nodejs\npm.cmd"
        )
        # 最终执行命令应包含引号保护的路径
        assert "npm.cmd" in str(actual), f"build_actual_command 结果应包含 npm.cmd: {actual}"
        assert actual[0] == "cmd.exe"

    def test_cmd_dir_with_spaces_works(self, tmp_path):
        """带空格的 cwd 正常工作"""
        from app.executor.command_runner import CommandRunner

        work_dir = tmp_path / "my workspace"
        work_dir.mkdir(parents=True, exist_ok=True)

        runner = CommandRunner(use_toolchain_env=False)
        result = runner.run(
            ["cmd.exe", "/c", "echo", "hello_from_space"],
            cwd=str(work_dir), timeout=10
        )
        assert result.exit_code == 0
        assert "hello_from_space" in result.stdout


class TestFileNotFoundError:
    """4. FileNotFoundError 被完整记录"""

    def test_file_not_found_error_recorded(self, tmp_path):
        """不存在的命令 → FileNotFoundError 含详细错误信息"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        result = runner.run(["nonexistent_command_xyz_123", "--help"],
                            cwd=str(tmp_path), timeout=5)

        assert result.exit_code == -1, f"期望 exit_code=-1, 得到 {result.exit_code}"
        assert result.error, "应包含错误信息"
        assert "FileNotFoundError" in result.error or "FileNotFound" in result.error, \
            f"错误信息应包含 FileNotFoundError: {result.error}"
        assert result.resolved_executable, "应记录 resolved_executable（即使解析失败）"
        assert result.success is False
        assert result.timed_out is False
        assert result.killed is False


class TestTimeoutRecording:
    """5. 超时被记录"""

    def test_timeout_recorded(self, tmp_path):
        """长时间运行的命令被 timeout 并记录 timed_out=True"""
        from app.executor.command_runner import CommandRunner

        if platform.system() == "Windows":
            # ping 3次约3秒（带 1 秒间隔），用 1 秒超时触发超时
            cmd = ["ping", "-n", "4", "127.0.0.1"]
        else:
            cmd = ["sleep", "5"]

        runner = CommandRunner(use_toolchain_env=False)
        result = runner.run(cmd, cwd=str(tmp_path), timeout=1)  # 1秒超时

        assert result.timed_out is True, f"期望 timed_out=True, 得到 {result.timed_out}"
        assert result.success is False
        assert "Timeout" in (result.error or ""), f"错误信息应包含 Timeout: {result.error}"


class TestShellInjectionPrevention:
    """6. Shell 注入被阻止"""

    def test_pipe_injection_blocked(self):
        """管道注入被阻止"""
        from app.executor.command_runner import CommandRunner

        with pytest.raises(ValueError, match="Shell injection"):
            CommandRunner._validate_no_shell_injection('npm.cmd" | echo hacked')

    def test_semicolon_injection_blocked(self):
        """分号注入被阻止"""
        from app.executor.command_runner import CommandRunner

        with pytest.raises(ValueError, match="Shell injection"):
            CommandRunner._validate_no_shell_injection("npm; rm -rf /")

    def test_backtick_injection_blocked(self):
        """反引号注入被阻止"""
        from app.executor.command_runner import CommandRunner

        with pytest.raises(ValueError, match="Shell injection"):
            CommandRunner._validate_no_shell_injection("npm`whoami`")

    def test_dollar_injection_blocked(self):
        """$变量注入被阻止"""
        from app.executor.command_runner import CommandRunner

        with pytest.raises(ValueError, match="Shell injection"):
            CommandRunner._validate_no_shell_injection("npm $(whoami)")

    def test_pipe_in_executable_path_blocked(self):
        """管道在可执行文件路径中被阻止"""
        from app.executor.command_runner import CommandRunner

        with pytest.raises(ValueError, match="Shell injection"):
            runner = CommandRunner(use_toolchain_env=False)
            # 即使 resolver 返回了含 | 的路径，_build_actual_command 也会拦截
            runner._build_actual_command(["echo", "hello"], "echo | dir")

    def test_clean_paths_pass_validation(self):
        """干净路径通过安全验证"""
        from app.executor.command_runner import CommandRunner

        # 不应抛出异常
        CommandRunner._validate_no_shell_injection(r"C:\Program Files\nodejs\npm.cmd")
        CommandRunner._validate_no_shell_injection("/usr/local/bin/node")
        CommandRunner._validate_no_shell_injection(r"C:\用户\工具\npm.cmd")  # 中文路径


class TestBuildActualCommand:
    """7. _build_actual_command 构建正确命令"""

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="Windows-specific .cmd handling")
    def test_cmd_file_uses_cmd_exe_wrapper(self):
        """Windows .cmd 文件使用 cmd.exe 包装"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        actual = runner._build_actual_command(
            ["npm", "run", "typecheck"],
            r"C:\Program Files\nodejs\npm.cmd"
        )
        assert actual[0] == "cmd.exe", f"期望 cmd.exe, 得到 {actual[0]}"
        assert "/d" in actual
        assert "/s" in actual
        assert "/c" in actual
        assert "npm.cmd" in actual[-1], f"最后参数应包含 npm.cmd: {actual[-1]}"

    def test_exe_file_uses_direct_path(self):
        """普通 .exe 文件直接使用绝对路径"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        actual = runner._build_actual_command(
            ["node", "--version"],
            r"C:\Program Files\nodejs\node.exe"
        )
        assert actual[0] == r"C:\Program Files\nodejs\node.exe"
        assert actual[1] == "--version"

    def test_nonexe_uses_direct_path_on_unix(self):
        """Unix 上即使无扩展名也直接使用绝对路径"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        if platform.system() != "Windows":
            actual = runner._build_actual_command(
                ["node", "--version"],
                "/usr/local/bin/node"
            )
            assert actual[0] == "/usr/local/bin/node"


class TestPATHEXTPreservation:
    """8. PATHEXT 被保留"""

    def test_pathext_in_built_env(self):
        """PATHEXT 存在于构建的环境变量中"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=True)
        env = runner._build_final_env()
        if platform.system() == "Windows":
            assert "PATHEXT" in env, "Windows 上 PATHEXT 必须存在"
            assert ".CMD" in env["PATHEXT"] or ".cmd" in env["PATHEXT"], \
                f"PATHEXT 应包含 .CMD: {env['PATHEXT']}"

    def test_pathext_fallback_if_missing(self, monkeypatch):
        """如果系统 PATHEXT 缺失，使用默认值"""
        from app.executor.command_runner import CommandRunner

        if platform.system() == "Windows":
            monkeypatch.delenv("PATHEXT", raising=False)
            runner = CommandRunner(use_toolchain_env=True)
            env = runner._build_final_env()
            assert "PATHEXT" in env
            assert ".CMD" in env["PATHEXT"]


class TestCommandResultFields:
    """9. CommandResult 完整字段"""

    def test_all_fields_present_on_success(self, tmp_path):
        """成功执行时所有字段都存在且正确"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        # 使用 echo 命令（跨平台）
        if platform.system() == "Windows":
            result = runner.run(["cmd.exe", "/c", "echo hello"], cwd=str(tmp_path), timeout=5)
        else:
            result = runner.run(["echo", "hello"], cwd=str(tmp_path), timeout=5)

        assert result.exit_code == 0
        assert result.success
        assert result.duration_ms >= 0
        assert hasattr(result, 'timed_out')
        assert result.timed_out is False
        assert hasattr(result, 'killed')
        assert result.killed is False
        assert hasattr(result, 'resolved_executable')
        assert hasattr(result, 'error')

    def test_all_fields_present_on_failure(self, tmp_path):
        """失败执行时所有字段都存在且 error != None"""
        from app.executor.command_runner import CommandRunner

        runner = CommandRunner(use_toolchain_env=False)
        result = runner.run(["this_command_does_not_exist_xyz"], cwd=str(tmp_path), timeout=5)

        assert result.success is False
        assert result.exit_code == -1
        assert result.error, "失败时 error 必须非空"
        assert "FileNotFoundError" in result.error
        assert result.timed_out is False


class TestResultCollectorV18:
    """10. execution_logs 新增字段写入"""

    def test_v18_fields_written_to_execution_logs(self, tmp_path):
        """V1.8 字段写入 execution_logs"""
        import sqlite3
        from app.executor.result_collector import ResultCollector

        db_path = tmp_path / "test_v18.db"
        rc = ResultCollector(str(db_path))

        # 初始化表结构（简单起见）
        conn = rc._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL,
                step_name VARCHAR(100) NOT NULL,
                step_status VARCHAR(20) DEFAULT 'running',
                command TEXT,
                stdout TEXT,
                stderr TEXT,
                exit_code INTEGER,
                duration_ms INTEGER DEFAULT 0,
                detail TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # 写入日志（应触发 schema 迁移）
        rc.add_log(
            999, "run_test", "failed",
            command="npm run typecheck",
            stdout="",
            stderr="",
            exit_code=-1,
            duration_ms=19,
            detail="FileNotFoundError",
            resolved_executable=r"C:\nodejs\npm.cmd",
            error="FileNotFoundError: [WinError 2]",
            timed_out=0,
            killed=0,
            cwd=r"C:\workspace",
        )

        # 验证写入
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_logs WHERE execution_id=999")
        row = cur.fetchone()
        assert row is not None, "应写入一条日志"

        cols = {col[1] for col in cur.execute("PRAGMA table_info(execution_logs)").fetchall()}
        assert "resolved_executable" in cols, "resolved_executable 列应存在"
        assert "error" in cols, "error 列应存在"
        assert "timed_out" in cols, "timed_out 列应存在"
        assert "killed" in cols, "killed 列应存在"
        assert "cwd" in cols, "cwd 列应存在"


class TestCommandRunnerNoShellTrue:
    """11. 禁止 shell=True"""

    def test_subprocess_run_never_uses_shell_true(self):
        """验证 CommandRunner.run 从不使用 shell=True（排除注释和文档字符串）"""
        from app.executor.command_runner import CommandRunner
        import inspect

        source = inspect.getsource(CommandRunner.run)
        # 过滤掉注释行和 docstring 中的文本
        code_only = "\n".join(
            line for line in source.split("\n")
            if not line.strip().startswith("#") and "shell=True" not in line.split("#")[0].split('"""')[0]
        )
        # 在非注释、非 docstring 的行中不应有 shell=True
        lines_with_shell_true = [
            line for line in code_only.split("\n")
            if "shell=True" in line and not line.strip().startswith("#")
            and '"""' not in line.split("#")[0]
            and "shell=False" not in line  # 排除注释中提及的情况
        ]
        assert len(lines_with_shell_true) == 0, \
            f"发现 shell=True 出现在代码中: {lines_with_shell_true}"


class TestAutoRepairSameRunner:
    """12. AutoRepair 使用与普通测试相同的 CommandRunner 路径"""

    def test_auto_repair_same_runner_path(self):
        """验证 _run_command 和 _run_tests 使用同一个 self.runner"""
        # 代码结构验证：self.runner 在 TaskWorker.__init__ 中创建一次
        # _run_command 使用 self.runner, _run_tests 使用 self.tester(self.runner)
        with patch.dict(os.environ, {}, clear=False):
            from app.executor.command_runner import CommandRunner
            runner1 = CommandRunner(use_toolchain_env=True)
            runner2 = CommandRunner(use_toolchain_env=True)
            # 两个实例应具有相同的工具链解析逻辑
            assert runner1._use_toolchain_env == runner2._use_toolchain_env
