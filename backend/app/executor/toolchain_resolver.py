"""ExecutorToolchainResolver - 正式工具链解析器

职责：
  resolve_node()          - 解析 Node.js 可执行文件路径
  resolve_npm()           - 解析 npm 可执行文件路径
  build_subprocess_env()  - 构建注入工具链的子进程环境变量
  validate_node_toolchain() - 完整工具链可用性验证

解析优先级：
  1. 项目或 Executor 明确配置的 Node 路径 (EXECUTOR_NODE_HOME)
  2. 当前 PATH 中的 node/npm
  3. Windows 标准安装目录
  4. 已确认稳定的内部工具链目录

Windows 特殊处理：
  - npm 是 .cmd 文件，必须通过完整路径调用或通过 shell=True 但受控
  - node.exe 是 .exe 文件
  - npx 是 .cmd 文件
"""
import os
import sys
import shutil
import platform
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from subprocess import run as subprocess_run, PIPE


@dataclass
class ToolchainStatus:
    """工具链状态"""
    available: bool
    node_executable: str = ""
    npm_executable: str = ""
    node_version: str = ""
    npm_version: str = ""
    node_home: str = ""
    resolution_method: str = ""  # config / path / standard_install / custom_dir / not_found
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    path_summary: str = ""  # PATH 摘要（不含敏感信息）


# ── Windows 标准 Node.js 安装目录 ──
_WINDOWS_STANDARD_NODE_DIRS = [
    Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "nodejs",
    Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "nodejs",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
]

# ── 已知稳定的内部工具链目录（不包含 *.installing.* 等临时目录） ──
_WINDOWS_STABLE_NODE_CANDIDATES = [
    # 用户本地 Programs 目录（标准安装位置）
    lambda: Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
    # AppData Roaming npm-global
    lambda: Path(os.environ.get("APPDATA", "")) / "npm",
]


def _is_stable_node_directory(path: Path) -> bool:
    """检查路径是否为稳定 Node 安装目录（排除临时/缓存目录）"""
    path_str = str(path).lower()
    # 排除临时安装目录
    unstable_patterns = [
        ".installing.",
        "temp",
        "tmp",
        "cache",
        "download",
    ]
    for pattern in unstable_patterns:
        if pattern in path_str:
            return False
    return True


def _run_version_check(executable: str) -> str:
    """运行 --version 获取版本号"""
    try:
        result = subprocess_run(
            [executable, "--version"],
            capture_output=True, text=True, timeout=10,
            encoding='utf-8', errors='replace',
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0][:100]
        # 某些工具将版本输出到 stderr
        if result.stderr.strip() and "version" in result.stderr.lower():
            return result.stderr.strip().split("\n")[0][:100]
    except Exception:
        pass
    return ""


class ExecutorToolchainResolver:
    """正式工具链解析器 - 确保 Executor 子进程能找到 Node.js/npm"""

    @staticmethod
    def resolve_node() -> Tuple[str, str]:
        """解析 Node.js 可执行文件路径。

        Returns:
            (node_path, resolution_method)
        """
        is_windows = platform.system() == "Windows"

        # 优先级 1: EXECUTOR_NODE_HOME 环境变量
        node_home = os.environ.get("EXECUTOR_NODE_HOME", "")
        if node_home:
            node_home_path = Path(node_home)
            if is_windows:
                candidates = [
                    node_home_path / "node.exe",
                    node_home_path / "node",
                ]
            else:
                candidates = [
                    node_home_path / "bin" / "node",
                    node_home_path / "node",
                ]
            for candidate in candidates:
                if candidate.exists() and candidate.is_file():
                    return (str(candidate), "config")

        # 优先级 2: 当前 PATH
        node_path = shutil.which("node") or shutil.which("node.exe")
        if node_path:
            return (node_path, "path")

        # 优先级 3: Windows 标准安装目录
        if is_windows:
            for std_dir in _WINDOWS_STANDARD_NODE_DIRS:
                if std_dir.exists():
                    node_exe = std_dir / "node.exe"
                    if node_exe.exists() and node_exe.is_file():
                        return (str(node_exe), "standard_install")

        # 优先级 4: 已知稳定目录
        for candidate_fn in _WINDOWS_STABLE_NODE_CANDIDATES:
            try:
                candidate = candidate_fn()
                if candidate.exists():
                    node_exe = candidate / "node.exe" if is_windows else candidate / "bin" / "node"
                    if node_exe.exists() and node_exe.is_file():
                        if _is_stable_node_directory(candidate):
                            return (str(node_exe), "custom_dir")
            except Exception:
                continue

        return ("", "not_found")

    @staticmethod
    def resolve_npm(node_home: str = "") -> Tuple[str, str]:
        """解析 npm 可执行文件路径。

        Windows 上 npm 是 .cmd 文件，必须特殊处理。

        Args:
            node_home: Node.js 安装目录（如果已知）

        Returns:
            (npm_path, resolution_method)
        """
        is_windows = platform.system() == "Windows"

        # 如果知道 node_home，直接定位
        if node_home:
            node_home_path = Path(node_home)
            if is_windows:
                candidates = [
                    node_home_path / "npm.cmd",
                    node_home_path / "npm",
                ]
            else:
                candidates = [
                    node_home_path / "bin" / "npm",
                    node_home_path / "npm",
                ]
            for candidate in candidates:
                if candidate.exists() and candidate.is_file():
                    return (str(candidate), "config")

        # 从 PATH 查找
        npm_path = shutil.which("npm") or shutil.which("npm.cmd")
        if npm_path:
            return (npm_path, "path")

        # Windows 标准安装目录
        if is_windows:
            for std_dir in _WINDOWS_STANDARD_NODE_DIRS:
                if std_dir.exists():
                    for name in ["npm.cmd", "npm"]:
                        npm_exe = std_dir / name
                        if npm_exe.exists() and npm_exe.is_file():
                            return (str(npm_exe), "standard_install")

        # 已知稳定目录
        for candidate_fn in _WINDOWS_STABLE_NODE_CANDIDATES:
            try:
                candidate = candidate_fn()
                if candidate.exists():
                    for name in ["npm.cmd", "npm"]:
                        npm_exe = candidate / name
                        if npm_exe.exists() and npm_exe.is_file():
                            if _is_stable_node_directory(candidate):
                                return (str(npm_exe), "custom_dir")
            except Exception:
                continue

        return ("", "not_found")

    @staticmethod
    def build_subprocess_env() -> Dict[str, str]:
        """构建注入工具链后的子进程环境变量。

        Returns:
            完整的环境变量字典（继承当前进程环境 + 注入工具链 PATH）
        """
        env = os.environ.copy()

        # 解析 Node 路径
        node_path, node_method = ExecutorToolchainResolver.resolve_node()
        if node_path and node_method != "not_found":
            node_dir = str(Path(node_path).parent)
            # 将 Node 目录前置到 PATH
            existing_path = env.get("PATH", "")
            if node_dir not in existing_path:
                env["PATH"] = node_dir + os.pathsep + existing_path

        # 设置 EXECUTOR_NODE_HOME 为后续子进程继承
        if node_path and node_method != "not_found":
            node_home = str(Path(node_path).parent)
            env["EXECUTOR_NODE_HOME"] = node_home

        return env

    @staticmethod
    def validate_node_toolchain(workspace: str = None) -> ToolchainStatus:
        """完整验证 Node.js 工具链可用性。

        检查项：
          1. node 可执行文件存在
          2. npm 可执行文件存在
          3. node --version 成功
          4. npm --version 成功
          5. 如提供 workspace，检查 package.json 存在

        Args:
            workspace: 项目工作目录（可选，用于检查 package.json）

        Returns:
            ToolchainStatus
        """
        errors = []
        warnings = []

        # Step 1: 解析 node
        node_path, node_method = ExecutorToolchainResolver.resolve_node()
        if not node_path:
            errors.append("Node.js 可执行文件未找到")
            return ToolchainStatus(
                available=False,
                resolution_method="not_found",
                errors=errors,
                path_summary=ExecutorToolchainResolver._build_path_summary(),
            )

        node_home = str(Path(node_path).parent)

        # Step 2: 解析 npm
        npm_path, npm_method = ExecutorToolchainResolver.resolve_npm(node_home)
        if not npm_path:
            errors.append("npm 可执行文件未找到（Node 已找到但 npm 不在预期位置）")

        # Step 3: node --version
        node_version = _run_version_check(node_path)
        if not node_version:
            errors.append(f"node --version 失败: {node_path}")

        # Step 4: npm --version
        npm_version = ""
        if npm_path:
            npm_version = _run_version_check(npm_path)
            if not npm_version:
                errors.append(f"npm --version 失败: {npm_path}")

        # Step 5: 检查 package.json（可选）
        if workspace:
            pkg_json = Path(workspace) / "package.json"
            if not pkg_json.exists():
                warnings.append(f"workspace 中未找到 package.json: {workspace}")
            else:
                # 检查必要 scripts
                try:
                    import json
                    pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                    scripts = pkg.get("scripts", {})
                    required_scripts = ["typecheck", "build", "test"]
                    missing = [s for s in required_scripts if s not in scripts]
                    if missing:
                        warnings.append(f"package.json 缺少 scripts: {missing}")
                except Exception as e:
                    warnings.append(f"package.json 解析失败: {e}")

        # 构建 PATH 摘要
        path_summary = ExecutorToolchainResolver._build_path_summary()

        available = len(errors) == 0

        return ToolchainStatus(
            available=available,
            node_executable=node_path,
            npm_executable=npm_path,
            node_version=node_version,
            npm_version=npm_version,
            node_home=node_home,
            resolution_method=node_method,
            errors=errors,
            warnings=warnings,
            path_summary=path_summary,
        )

    @staticmethod
    def _build_path_summary() -> str:
        """构建 PATH 环境变量摘要（不含敏感信息）"""
        path_value = os.environ.get("PATH", "")
        if not path_value:
            return "PATH is empty"

        entries = path_value.split(os.pathsep)
        # 只显示前 10 个和后 5 个条目
        if len(entries) <= 15:
            return f"PATH ({len(entries)} entries): " + " | ".join(entries)
        else:
            first = entries[:10]
            last = entries[-5:]
            return (f"PATH ({len(entries)} entries): "
                    + " | ".join(first)
                    + " | ... | "
                    + " | ".join(last))

    @staticmethod
    def get_node_home_from_executable(node_path: str) -> str:
        """从 node 可执行文件路径推导 NODE_HOME"""
        return str(Path(node_path).parent)


# ── 便捷函数 ──

def resolve_node() -> str:
    """便捷函数：解析 Node.js 路径"""
    path, _ = ExecutorToolchainResolver.resolve_node()
    return path


def resolve_npm(node_home: str = "") -> str:
    """便捷函数：解析 npm 路径"""
    path, _ = ExecutorToolchainResolver.resolve_npm(node_home)
    return path


def build_subprocess_env() -> Dict[str, str]:
    """便捷函数：构建子进程环境变量"""
    return ExecutorToolchainResolver.build_subprocess_env()


def validate_node_toolchain(workspace: str = None) -> ToolchainStatus:
    """便捷函数：验证 Node 工具链"""
    return ExecutorToolchainResolver.validate_node_toolchain(workspace)


# ── 快速预检函数 ──

def precheck_node_toolchain(workspace: str = None) -> ToolchainStatus:
    """
    快速预检 Node 工具链（用于 Executor 启动前检查）。
    
    如果工具链不可用，返回 NODE_TOOLCHAIN_NOT_AVAILABLE 状态。
    此函数应在 AI 代码生成之前调用，避免生成完代码才发现 npm 不存在。
    """
    return ExecutorToolchainResolver.validate_node_toolchain(workspace)
