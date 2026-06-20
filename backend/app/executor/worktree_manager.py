"""WorktreeManager - Git Worktree 管理

每个任务创建独立 Worktree：
- 分支: executor/task-{task_id}-{execution_id}
- Worktree: .executor/worktrees/task-{task_id}-{execution_id}
- Checkpoint: checkpoint/task-{task_id}-{execution_id}

职责：
- 创建独立分支和 Worktree
- 清理 Worktree
- 检查隔离性
"""
import os
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime


class WorktreeManager:
    """Git Worktree 管理器"""

    WORKTREE_BASE = ".executor/worktrees"
    BRANCH_PREFIX = "executor/task"

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.worktree_base = self.repo_path / self.WORKTREE_BASE

    def _run_git(self, args: List[str], cwd: str = None,
                 capture: bool = True, timeout: int = 30) -> Dict[str, Any]:
        """执行 git 命令"""
        cwd = cwd or str(self.repo_path)
        cmd = ["git"] + args
        try:
            result = subprocess.run(
                cmd, cwd=cwd,
                capture_output=capture,
                text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip() if capture else "",
                "stderr": result.stderr.strip() if capture else "",
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "Git command timed out",
                    "returncode": -1}
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e),
                    "returncode": -1}

    def create_worktree(
        self,
        task_id: int,
        execution_id: int,
        base_branch: str = "master",
    ) -> Dict[str, Any]:
        """为任务创建独立 Worktree

        返回:
            {"success": bool, "branch": str, "worktree_path": str,
             "checkpoint": str, "error": str}
        """
        branch = f"{self.BRANCH_PREFIX}-{task_id}-{execution_id}"
        worktree_dir = self.worktree_base / f"task-{task_id}-{execution_id}"
        checkpoint_name = f"checkpoint/task-{task_id}-{execution_id}"

        # 确保 worktree 基础目录存在
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # 检查 worktree 是否已存在
        if worktree_dir.exists():
            return {
                "success": False,
                "error": f"Worktree already exists: {worktree_dir}",
                "branch": branch,
                "worktree_path": str(worktree_dir),
                "checkpoint": checkpoint_name,
            }

        try:
            # 确保 base_branch 是最新的
            self._run_git(["fetch", "origin", base_branch], timeout=60)

            # 创建分支（基于 master）
            r = self._run_git(["branch", branch, base_branch])
            if not r["success"]:
                # 分支可能已存在，尝试删除重建
                self._run_git(["branch", "-D", branch])
                r = self._run_git(["branch", branch, base_branch])
                if not r["success"]:
                    return {
                        "success": False,
                        "error": f"Failed to create branch: {r['stderr']}",
                        "branch": branch,
                        "worktree_path": str(worktree_dir),
                        "checkpoint": checkpoint_name,
                    }

            # 创建 worktree
            r = self._run_git([
                "worktree", "add",
                str(worktree_dir),
                branch,
            ], timeout=60)

            if not r["success"]:
                # 清理分支
                self._run_git(["branch", "-D", branch])
                return {
                    "success": False,
                    "error": f"Failed to create worktree: {r['stderr']}",
                    "branch": branch,
                    "worktree_path": str(worktree_dir),
                    "checkpoint": checkpoint_name,
                }

            # 在 worktree 中创建 checkpoint tag
            r_checkpoint = self._run_git(
                ["tag", checkpoint_name, "HEAD"],
                cwd=str(worktree_dir),
            )

            return {
                "success": True,
                "error": None,
                "branch": branch,
                "worktree_path": str(worktree_dir),
                "checkpoint": checkpoint_name,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "branch": branch,
                "worktree_path": str(worktree_dir),
                "checkpoint": checkpoint_name,
            }

    def remove_worktree(
        self,
        task_id: int,
        execution_id: int,
        force: bool = False,
    ) -> Dict[str, Any]:
        """清理 Worktree（含元数据跟踪）

        步骤:
        1. 记录清理前 worktree 列表
        2. git worktree remove
        3. git branch -D
        4. 删除目录（如残留）
        5. git worktree prune
        6. 记录清理后 worktree 列表
        7. 写入清理元数据
        """
        branch = f"{self.BRANCH_PREFIX}-{task_id}-{execution_id}"
        worktree_dir = self.worktree_base / f"task-{task_id}-{execution_id}"

        errors = []
        cleanup_status = "pending"
        cleanup_error = ""
        before_list = []
        after_list = []

        try:
            # 0. 记录清理前状态
            before = self._run_git(["worktree", "list", "--porcelain"], timeout=15)
            before_list = [
                line for line in before.get("stdout", "").split("\n") if line.strip()
            ]

            # 1. 移除 worktree
            if worktree_dir.exists():
                r = self._run_git([
                    "worktree", "remove",
                    str(worktree_dir),
                ] + (["--force"] if force else []), timeout=30)

                if not r["success"] and not force:
                    errors.append(f"worktree remove: {r['stderr']}")

                # 2. 如果 worktree 目录仍然存在，强制删除
                if worktree_dir.exists() and force:
                    import shutil
                    try:
                        shutil.rmtree(str(worktree_dir), ignore_errors=True)
                    except Exception as e:
                        errors.append(f"shutil.rmtree: {e}")

            # 3. 删除分支
            r = self._run_git(["branch", "-D", branch], timeout=15)
            if not r["success"]:
                # 分支可能已不存在，非致命
                pass

            # 4. 清理 worktree 记录
            self._run_git(["worktree", "prune"], timeout=15)

            # 5. 记录清理后状态
            after = self._run_git(["worktree", "list", "--porcelain"], timeout=15)
            after_list = [
                line for line in after.get("stdout", "").split("\n") if line.strip()
            ]

            cleanup_status = "completed" if len(errors) == 0 else "partial"
        except Exception as e:
            cleanup_status = "failed"
            cleanup_error = str(e)
            errors.append(cleanup_error)

        # 6. 写入清理元数据
        self._write_cleanup_metadata(
            task_id, execution_id, branch, str(worktree_dir),
            cleanup_status, cleanup_error, errors,
            before_list, after_list,
        )

        return {
            "success": len(errors) == 0,
            "errors": errors,
            "branch": branch,
            "worktree_path": str(worktree_dir),
            "cleanup_status": cleanup_status,
            "cleanup_error": cleanup_error,
        }

    def _write_cleanup_metadata(
        self, task_id: int, execution_id: int,
        branch: str, worktree_path: str,
        status: str, error: str, errors: List[str],
        before_list: List[str], after_list: List[str],
    ):
        """写入 Worktree 清理元数据到日志文件"""
        import json
        metadata_path = self.repo_path / ".executor" / "worktree_cleanup_log.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "task_id": task_id,
            "execution_id": execution_id,
            "branch": branch,
            "worktree_path": worktree_path,
            "cleanup_status": status,
            "cleanup_error": error,
            "errors": errors,
            "before_count": len(before_list),
            "after_count": len(after_list),
            "timestamp": datetime.now().isoformat(),
        }

        existing = []
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.append(entry)
        # 只保留最近 50 条记录
        metadata_path.write_text(
            json.dumps(existing[-50:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_worktrees(self) -> List[Dict[str, Any]]:
        """列出所有 worktree"""
        r = self._run_git(["worktree", "list"], timeout=15)
        if not r["success"]:
            return []

        worktrees = []
        for line in r["stdout"].split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 1:
                path = parts[0]
                branch = ""
                commit = ""
                for i, p in enumerate(parts):
                    if p.startswith("[") and p.endswith("]"):
                        branch = p[1:-1]
                    if len(p) == 40 and all(c in "0123456789abcdef" for c in p):
                        commit = p
                worktrees.append({
                    "path": path,
                    "branch": branch,
                    "commit": commit,
                })
        return worktrees

    def cleanup_all_worktrees(self) -> Dict[str, Any]:
        """清理所有执行器创建的 worktree"""
        worktrees = self.list_worktrees()
        cleaned = []
        errors = []

        for wt in worktrees:
            path = wt["path"]
            branch = wt["branch"]
            # 只清理 executor/task- 开头的 worktree
            if "executor/task-" in branch and self.WORKTREE_BASE in path:
                result = self._run_git([
                    "worktree", "remove", path, "--force"
                ], timeout=30)
                if result["success"]:
                    cleaned.append(path)
                else:
                    errors.append(f"{path}: {result['stderr']}")

                # 删除分支
                self._run_git(["branch", "-D", branch], timeout=15)

        # 清理 worktree 记录
        self._run_git(["worktree", "prune"], timeout=15)

        return {
            "success": len(errors) == 0,
            "cleaned": cleaned,
            "errors": errors,
        }

    def get_current_commit(self, worktree_path: str = None) -> Optional[str]:
        """获取当前 HEAD commit"""
        cwd = worktree_path or str(self.repo_path)
        r = self._run_git(["rev-parse", "HEAD"], cwd=cwd, timeout=10)
        if r["success"]:
            return r["stdout"]
        return None

    def is_clean(self, worktree_path: str = None) -> bool:
        """检查 worktree 是否 clean"""
        cwd = worktree_path or str(self.repo_path)
        r = self._run_git(["status", "--porcelain"], cwd=cwd, timeout=10)
        if r["success"]:
            return r["stdout"] == ""
        return False

    def reset_to_checkpoint(
        self,
        task_id: int,
        execution_id: int,
        worktree_path: str = None,
    ) -> bool:
        """回滚到 checkpoint"""
        checkpoint_name = f"checkpoint/task-{task_id}-{execution_id}"
        cwd = worktree_path or str(self.repo_path)

        # 硬重置到 checkpoint
        r = self._run_git(["reset", "--hard", checkpoint_name], cwd=cwd, timeout=15)
        if not r["success"]:
            return False

        # 清理未跟踪文件
        self._run_git(["clean", "-fd"], cwd=cwd, timeout=15)
        return True
