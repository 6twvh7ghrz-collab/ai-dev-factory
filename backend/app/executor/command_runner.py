"""命令执行器 - 封装 subprocess 调用，记录 stdout/stderr/exit_code/duration

V1.8: execute resolved Windows command shims reliably
  - 将 argv[0] 替换为 ToolchainResolver 解析到的绝对路径
  - Windows .cmd/.bat 通过 cmd.exe /d /s /c 包装执行
  - 禁止 shell=True，防止命令注入
  - CommandResult 新增 timed_out / killed 字段
"""
import subprocess
import time
import os
import platform as _platform
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from pathlib import Path


# ── Windows 可执行文件扩展名 ──
_WINDOWS_SCRIPT_EXTS = frozenset({".cmd", ".bat"})
_WINDOWS_EXECUTABLE_EXTS = frozenset({".exe", ".com"})

# ── 禁止的 shell 元字符（防止 shell 注入）──
_SHELL_INJECTION_CHARS = frozenset({"|", "&", ";", "$", "`", "(", ")", "{", "}", "<", ">", "\n"})


@dataclass
class CommandResult:
    """命令执行结果"""
    command: List[str]
    cwd: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    success: bool
    error: Optional[str] = None
    resolved_executable: str = ""   # 实际解析到的可执行文件路径
    timed_out: bool = False          # V1.8: 是否超时
    killed: bool = False             # V1.8: 是否被终止


class CommandRunner:
    """安全的命令执行器（V1.8: 可靠执行 Windows .cmd/.bat 命令）

    核心原则：
      1. 优先通过 ToolchainResolver 解析可执行文件绝对路径
      2. 将 argv[0] 替换为解析到的绝对路径
      3. Windows .cmd/.bat 通过 cmd.exe /d /s /c 包装（不使用 shell=True）
      4. 禁止 shell=True 及任何形式的命令注入
      5. AutoRepair 和普通测试使用同一 CommandRunner 路径
    """

    def __init__(self, use_toolchain_env: bool = True):
        """
        Args:
            use_toolchain_env: 是否自动注入工具链环境（Node.js/npm PATH）
        """
        self._use_toolchain_env = use_toolchain_env
        self._cached_toolchain_env: Optional[Dict[str, str]] = None

    def _get_toolchain_env(self) -> Dict[str, str]:
        """获取注入工具链的环境变量（延迟加载+缓存）"""
        if self._cached_toolchain_env is None:
            try:
                from .toolchain_resolver import ExecutorToolchainResolver
                self._cached_toolchain_env = ExecutorToolchainResolver.build_subprocess_env()
            except ImportError:
                self._cached_toolchain_env = os.environ.copy()
        return self._cached_toolchain_env

    def run(self, command: List[str], cwd: str = None,
            timeout: int = 120, env: dict = None) -> CommandResult:
        """
        执行命令并返回完整结果

        执行流程（V1.8）：
          1. 通过 ToolchainResolver 解析可执行文件绝对路径
          2. 将 argv[0] 替换为解析到的绝对路径
          3. 构建最终环境变量（包含工具链 PATH 注入）
          4. 如果是 Windows .cmd/.bat → 使用 cmd.exe /d /s /c 包装
          5. subprocess.run（capture_output=True, text=True, shell=False 强制）
          6. 完整保存 stdout/stderr/exit_code/duration/error

        Args:
            command: 命令和参数列表
            cwd: 工作目录
            timeout: 超时秒数
            env: 环境变量（合并到当前环境；如果为 None 且 use_toolchain_env=True，
                 则自动注入工具链 PATH）
        """
        cmd_str = [str(c) for c in command]
        start = time.time()

        # ── V1.8: 构建最终环境变量 ──
        final_env = self._build_final_env(env)

        # ── V1.8: 使用增强解析器找到真正的可执行文件 ──
        resolved_executable = self._resolve_executable_enhanced(cmd_str[0], final_env)

        # ── V1.8: 构建实际执行命令（替换 argv[0]）──
        actual_command = self._build_actual_command(cmd_str, resolved_executable)

        cwd_str = str(cwd) if cwd else "."

        try:
            result = subprocess.run(
                actual_command,
                cwd=cwd_str,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=final_env,
                encoding='utf-8',
                errors='replace',
                shell=False,  # V1.8: 强制禁止 shell=True
            )
            duration_ms = int((time.time() - start) * 1000)

            return CommandResult(
                command=cmd_str,       # 原始逻辑命令
                cwd=cwd_str,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.returncode,
                duration_ms=duration_ms,
                success=result.returncode == 0,
                resolved_executable=resolved_executable,
                timed_out=False,
                killed=False,
            )

        except subprocess.TimeoutExpired as e:
            duration_ms = int((time.time() - start) * 1000)
            return CommandResult(
                command=cmd_str,
                cwd=cwd_str,
                stdout=(e.stdout or "") if hasattr(e, 'stdout') else "",
                stderr=(e.stderr or "") if hasattr(e, 'stderr') else "",
                exit_code=-1,
                duration_ms=duration_ms,
                success=False,
                error=f"Timeout after {timeout}s",
                resolved_executable=resolved_executable,
                timed_out=True,
                killed=False,
            )

        except FileNotFoundError as e:
            # V1.8: 专门捕获 FileNotFoundError 以完整记录
            duration_ms = int((time.time() - start) * 1000)
            return CommandResult(
                command=cmd_str,
                cwd=cwd_str,
                stdout="",
                stderr="",
                exit_code=-1,
                duration_ms=duration_ms,
                success=False,
                error=f"FileNotFoundError: {e} (resolved={resolved_executable})",
                resolved_executable=resolved_executable,
                timed_out=False,
                killed=False,
            )

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return CommandResult(
                command=cmd_str,
                cwd=cwd_str,
                stdout="",
                stderr="",
                exit_code=-1,
                duration_ms=duration_ms,
                success=False,
                error=f"{type(e).__name__}: {e}",
                resolved_executable=resolved_executable,
                timed_out=False,
                killed=False,
            )

    # ── V1.8: 增强版可执行文件解析 ──

    def _resolve_executable_enhanced(self, executable: str, env: dict) -> str:
        """增强版可执行文件解析：使用 ToolchainResolver + shutil.which 双重保障

        解析优先级：
          1. 已知工具：npm/npx/node → 使用 ExecutorToolchainResolver
          2. shutil.which 在注入后的 PATH 中搜索
          3. 回退到原始参数
        """
        # 1. 对 npm/npx 等常见 Node.js 工具，使用 ToolchainResolver
        _base = os.path.basename(executable).lower().rstrip('.exe').rstrip('.cmd')
        if _base in ("npm", "npx", "node"):
            try:
                from .toolchain_resolver import ExecutorToolchainResolver
                if _base == "npm":
                    npm_path, _ = ExecutorToolchainResolver.resolve_npm()
                    if npm_path:
                        return npm_path
                elif _base == "npx":
                    # npx 通常与 npm 在同一目录
                    npm_path, _ = ExecutorToolchainResolver.resolve_npm()
                    if npm_path:
                        npx_dir = str(Path(npm_path).parent)
                        npx_candidate = os.path.join(npx_dir, "npx.cmd" if _platform.system() == "Windows" else "npx")
                        if os.path.isfile(npx_candidate):
                            return npx_candidate
                elif _base == "node":
                    node_path, _ = ExecutorToolchainResolver.resolve_node()
                    if node_path:
                        return node_path
            except ImportError:
                pass

        # 2. shutil.which 在注入后的 PATH 中搜索
        import shutil
        path_val = env.get("PATH") if env else None
        resolved = shutil.which(executable, path=path_val)
        if resolved:
            return resolved

        # 3. Windows: 尝试在注入 PATH 中搜索带扩展名的变体
        if _platform.system() == "Windows":
            for ext in (".cmd", ".exe"):
                candidate = shutil.which(executable + ext, path=path_val)
                if candidate:
                    return candidate

        # 4. 回退：保持原始参数（后续可能触发 FileNotFoundError）
        return executable

    def _build_actual_command(self, original_cmd: List[str],
                               resolved_executable: str) -> List[str]:
        """V1.8: 构建实际执行命令

        - 将 argv[0] 替换为 resolved_executable 绝对路径
        - Windows .cmd/.bat → 使用 cmd.exe /d /s /c 包装
        - 普通 .exe → 直接使用绝对路径
        - 禁止注入 shell 元字符
        """
        exe = resolved_executable

        # 安全检查：验证解析路径不包含 shell 注入字符
        self._validate_no_shell_injection(exe)

        if _platform.system() == "Windows":
            ext = os.path.splitext(exe)[1].lower()
            if ext in _WINDOWS_SCRIPT_EXTS:
                # Windows .cmd/.bat: 使用 cmd.exe /d /s /c 包装
                # /d 禁止 AutoRun, /s 处理引号, /c 执行后退出
                # 引用所有参数防止空格/特殊字符问题
                quoted_args = [self._windows_quote(exe)] + [
                    self._windows_quote(arg) for arg in original_cmd[1:]
                ]
                script_line = " ".join(quoted_args)
                return ["cmd.exe", "/d", "/s", "/c", script_line]

        # 非脚本可执行文件：直接替换 argv[0] 为绝对路径
        return [exe] + original_cmd[1:]

    @staticmethod
    def _windows_quote(arg: str) -> str:
        """Windows 命令行安全引用（处理含空格/特殊字符的参数）"""
        if not arg:
            return '""'
        if '"' in arg:
            # 将内部的双引号转义
            escaped = arg.replace('"', '\\"')
            return f'"{escaped}"'
        if ' ' in arg or '\t' in arg:
            return f'"{arg}"'
        return arg

    @staticmethod
    def _validate_no_shell_injection(path_str: str) -> None:
        """V1.8: 验证路径不包含 shell 注入字符

        Raises:
            ValueError: 检测到潜在 shell 注入
        """
        for ch in path_str:
            if ch in _SHELL_INJECTION_CHARS:
                raise ValueError(
                    f"Shell injection detected in executable path: {path_str!r} "
                    f"(char: {ch!r})"
                )

    # ── 回退解析器（兼容旧接口）──

    def _resolve_executable(self, executable: str, env: dict = None) -> str:
        """解析可执行文件的实际路径（用于日志记录，兼容旧接口）"""
        import shutil
        final_env = self._build_final_env(env)
        resolved = shutil.which(executable, path=final_env.get("PATH") if final_env else None)
        return resolved or executable

    def _build_final_env(self, user_env: dict = None) -> dict:
        """构建最终环境变量：合并工具链环境 + 用户环境"""
        if self._use_toolchain_env:
            base_env = self._get_toolchain_env()
        else:
            base_env = os.environ.copy()

        if user_env:
            base_env.update(user_env)

        # V1.8: 确保 PATHEXT 继承现有值，Windows 依赖它识别 .cmd/.bat
        if _platform.system() == "Windows" and "PATHEXT" not in base_env:
            base_env["PATHEXT"] = os.environ.get(
                "PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL"
            )

        return base_env

    @staticmethod
    def run_shell(script: str, cwd: str = None,
                  timeout: int = 120) -> CommandResult:
        """执行 shell 脚本（Windows: cmd /c, 使用 CommandRunner 统一路径）"""
        runner = CommandRunner()
        if _platform.system() == "Windows":
            return runner.run(["cmd.exe", "/c", script], cwd=cwd, timeout=timeout)
        else:
            return runner.run(["bash", "-c", script], cwd=cwd, timeout=timeout)
