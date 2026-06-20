"""Git 管理器 - 管理 Worktree、checkpoint、分支、合并

Step 1 范围：
  - 检查 Git 状态
  - 创建 checkpoint
  - 记录 start_commit
  - 检查 diff
  - 创建任务 commit
  - 不创建独立 Worktree（Step 1 在主工作区直接操作）
"""
import subprocess
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from pathlib import Path
from datetime import datetime


@dataclass
class GitStatus:
    """Git 状态快照"""
    clean: bool
    branch: str
    commit: str = ""
    modified_files: List[str] = field(default_factory=list)
    untracked_files: List[str] = field(default_factory=list)
    diff_summary: str = ""


@dataclass
class Checkpoint:
    """Git 检查点"""
    name: str
    commit: str
    created_at: str
    task_id: Optional[int] = None


class GitManager:
    """Git 仓库管理器"""

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _run_git(self, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """执行 git 命令"""
        return subprocess.run(
            ["git"] + args,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _git_output(self, args: List[str]) -> str:
        """执行 git 并返回 stdout"""
        result = self._run_git(args)
        return result.stdout.strip()

    def check_repo(self) -> bool:
        """验证是否为有效 Git 仓库"""
        result = self._run_git(["rev-parse", "--git-dir"])
        return result.returncode == 0

    def get_status(self) -> GitStatus:
        """获取当前 Git 状态"""
        # 分支
        branch = self._git_output(["rev-parse", "--abbrev-ref", "HEAD"])

        # 当前 commit
        commit = self._git_output(["rev-parse", "HEAD"])

        # 修改的文件
        modified = self._git_output(["diff", "--name-only"]).split("\n")
        modified = [f for f in modified if f]

        # 未跟踪文件
        untracked = self._git_output([
            "ls-files", "--others", "--exclude-standard"
        ]).split("\n")
        untracked = [f for f in untracked if f]

        # diff 摘要
        diff_summary = self._git_output(["diff", "--stat"])

        return GitStatus(
            clean=(len(modified) == 0 and len(untracked) == 0),
            branch=branch,
            commit=commit,
            modified_files=modified,
            untracked_files=untracked,
            diff_summary=diff_summary,
        )

    def get_current_commit(self) -> str:
        """获取当前 HEAD commit hash"""
        return self._git_output(["rev-parse", "HEAD"])

    def create_checkpoint(self, task_id: int) -> Checkpoint:
        """创建检查点：记录当前 commit 和时间戳"""
        commit = self.get_current_commit()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"task-{task_id}-{timestamp}"

        return Checkpoint(
            name=name,
            commit=commit,
            created_at=datetime.now().isoformat(),
            task_id=task_id,
        )

    def create_branch(self, branch_name: str) -> bool:
        """创建并切换到新分支"""
        result = self._run_git(["checkout", "-b", branch_name])
        return result.returncode == 0

    def switch_branch(self, branch_name: str) -> bool:
        """切换到已有分支"""
        result = self._run_git(["checkout", branch_name])
        return result.returncode == 0

    def get_diff(self, from_commit: str = None) -> str:
        """获取 diff（与指定 commit 或 HEAD~1 比较）"""
        if from_commit:
            return self._git_output(["diff", from_commit])
        return self._git_output(["diff", "HEAD~1"]) if self._git_output(["rev-list", "--count", "HEAD"]) != "1" else ""

    def get_diff_files(self, from_commit: str = None) -> List[str]:
        """获取变更文件列表"""
        if from_commit:
            files = self._git_output(["diff", "--name-only", from_commit])
        else:
            files = self._git_output(["diff", "--name-only", "HEAD~1"])
        return [f for f in files.split("\n") if f]

    def get_diff_files_staged(self) -> List[str]:
        """获取暂存区变更文件列表"""
        files = self._git_output(["diff", "--name-only", "--cached"])
        return [f for f in files.split("\n") if f]

    def stage_files(self, files: List[str]) -> bool:
        """暂存指定文件"""
        result = self._run_git(["add"] + files)
        return result.returncode == 0

    def commit(self, message: str) -> str:
        """创建 commit，返回新 commit hash"""
        result = self._run_git(["commit", "-m", message])
        if result.returncode == 0:
            return self.get_current_commit()
        # 检查是否 nothing to commit
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return self.get_current_commit()
        return ""

    def rollback_to(self, commit: str) -> bool:
        """回滚到指定 commit（hard reset）"""
        result = self._run_git(["reset", "--hard", commit])
        return result.returncode == 0

    def rollback_soft(self, commit: str) -> bool:
        """软回滚（保留工作区修改）"""
        result = self._run_git(["reset", "--soft", commit])
        return result.returncode == 0

    def stash(self) -> bool:
        """暂存工作区修改"""
        result = self._run_git(["stash"])
        return result.returncode == 0

    def stash_pop(self) -> bool:
        """恢复 stash"""
        result = self._run_git(["stash", "pop"])
        return result.returncode == 0

    def has_commits(self) -> bool:
        """检查是否有 commit"""
        result = self._run_git(["rev-list", "--count", "HEAD"])
        try:
            return int(result.stdout.strip()) > 0
        except ValueError:
            return False

    def hard_reset_to_checkpoint(self, checkpoint_commit: str = None) -> bool:
        """
        硬回滚到检查点状态
        - 如果指定了 checkpoint_commit，回滚到该 commit
        - 否则回滚到 HEAD（丢弃所有未提交修改）
        - 先 stash 再 reset --hard 再 clean
        """
        try:
            if checkpoint_commit:
                # 先尝试 stash 未跟踪文件
                self._run_git(["stash", "--include-untracked"])
                self._run_git(["reset", "--hard", checkpoint_commit])
            else:
                # 重置所有修改
                self._run_git(["checkout", "--", "."])
                # 删除未跟踪文件
                self._run_git(["clean", "-fd"])
            return True
        except Exception:
            return False

    def clean_untracked(self) -> bool:
        """清理未跟踪文件和目录（不包括 .gitignore 中的）"""
        try:
            self._run_git(["clean", "-fd"])
            return True
        except Exception:
            return False

    def stash_include_untracked(self) -> bool:
        """暂存所有修改（包括未跟踪文件）"""
        result = self._run_git(["stash", "--include-untracked"])
        return result.returncode == 0
