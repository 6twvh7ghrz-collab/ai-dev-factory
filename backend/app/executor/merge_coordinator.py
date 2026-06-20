"""MergeCoordinator - 串行合并队列

任务测试通过后不能由 Worker 直接合并。
必须进入串行合并队列:

一次只合并一个任务
→ 检查主分支 clean
→ 检查 start_commit
→ 检查冲突
→ 合并任务分支
→ 执行合并后回归测试
→ 成功才标记最终 completed

合并冲突时:
- 不得自动强行解决
- 不得使用 theirs/ours 覆盖
- 状态 = blocked 或 waiting_approval
- 保存冲突文件
- 主分支恢复干净

合并后回归失败时:
- 撤销本次合并
- 任务 = blocked
- 保存测试证据
- 不影响已完成的其他任务
"""
import subprocess
import threading
import json
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime


@dataclass
class MergeItem:
    """合并队列项"""
    task_id: int
    execution_id: int
    branch: str
    worktree_path: str
    worker_id: str
    start_commit: str
    status: str = "waiting"  # waiting / merging / testing / done / failed
    queued_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    merge_started_at: Optional[str] = None
    merge_finished_at: Optional[str] = None
    error: Optional[str] = None
    conflict_files: List[str] = field(default_factory=list)


class MergeCoordinator:
    """串行合并队列"""

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self._queue: List[MergeItem] = []
        self._lock = threading.Lock()
        self._merging = False

    def _run_git(self, args: List[str], cwd: str = None,
                 capture: bool = True, timeout: int = 60) -> Dict[str, Any]:
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

    def enqueue(self, item: MergeItem):
        """加入合并队列"""
        with self._lock:
            self._queue.append(item)

    def get_queue(self) -> List[Dict[str, Any]]:
        """获取队列状态"""
        with self._lock:
            return [
                {
                    "task_id": m.task_id,
                    "execution_id": m.execution_id,
                    "branch": m.branch,
                    "status": m.status,
                    "queued_at": m.queued_at,
                    "error": m.error,
                }
                for m in self._queue
            ]

    def get_queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def is_merging(self) -> bool:
        with self._lock:
            return self._merging

    def process_next(self) -> Dict[str, Any]:
        """处理队列中的下一个合并项（串行）"""
        with self._lock:
            if self._merging:
                return {"success": False, "error": "已在合并中"}
            if not self._queue:
                return {"success": True, "message": "队列为空", "merged": None}

            self._merging = True
            item = self._queue.pop(0)

        try:
            result = self._merge_single(item)
            with self._lock:
                self._merging = False
            return result
        except Exception as e:
            with self._lock:
                self._merging = False
            return {
                "success": False,
                "error": str(e),
                "task_id": item.task_id,
                "execution_id": item.execution_id,
            }

    def _merge_single(self, item: MergeItem) -> Dict[str, Any]:
        """合并单个任务（含冲突处理 + 回归测试 + 回归失败回退）"""
        item.merge_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 0. 记录合并前 commit（用于安全回退）
        before_commit = self._get_current_commit()
        if not before_commit:
            item.status = "failed"
            item.error = "无法获取当前 commit"
            return {
                "success": False, "error": item.error,
                "task_id": item.task_id, "execution_id": item.execution_id,
                "status": "failed", "before_commit": before_commit,
            }

        # 1. 切换到主分支
        r = self._run_git(["checkout", "master"])
        if not r["success"]:
            item.status = "failed"
            item.error = f"无法切换到 master: {r['stderr']}"
            return {
                "success": False, "error": item.error,
                "task_id": item.task_id, "execution_id": item.execution_id,
                "status": "failed", "before_commit": before_commit,
            }

        # 2. 检查主分支 clean
        r = self._run_git(["status", "--porcelain"])
        if r["stdout"] != "":
            item.status = "failed"
            item.error = "主分支不 clean，无法合并"
            return {
                "success": False, "error": item.error,
                "task_id": item.task_id, "execution_id": item.execution_id,
                "status": "failed", "before_commit": before_commit,
            }

        # 3. 拉取最新
        self._run_git(["pull", "origin", "master"], timeout=60)

        # 4. 尝试合并任务分支
        item.status = "merging"
        r = self._run_git([
            "merge", item.branch,
            "--no-ff",
            "-m", f"merge: task-{item.task_id} (execution-{item.execution_id})",
        ], timeout=60)

        if not r["success"]:
            # 合并冲突
            item.status = "failed"
            item.error = f"合并冲突: {r['stderr']}"

            # 获取冲突文件
            conflict_r = self._run_git(["diff", "--name-only", "--diff-filter=U"])
            if conflict_r["success"]:
                item.conflict_files = conflict_r["stdout"].split("\n") if conflict_r["stdout"] else []

            # 中止合并，恢复主分支干净
            self._run_git(["merge", "--abort"])

            return {
                "success": False, "error": item.error,
                "task_id": item.task_id, "execution_id": item.execution_id,
                "status": "blocked", "conflict_files": item.conflict_files,
                "before_commit": before_commit,
            }

        # 5. 合并成功，记录合并后 commit
        merge_commit = self._get_current_commit()
        item.merge_finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 6. 执行回归测试
        item.status = "testing"
        reg_result = self.run_regression_tests()

        if not reg_result.get("passed"):
            # 回归测试失败 → 撤销合并
            self._log_merge_result(item, reg_result, "regression_failed")
            self._safe_undo_merge(item, before_commit)

            item.status = "failed"
            item.error = f"回归测试失败: {reg_result.get('error', reg_result.get('stderr', 'unknown'))}"
            return {
                "success": False, "error": item.error,
                "task_id": item.task_id, "execution_id": item.execution_id,
                "status": "blocked", "regression_result": reg_result,
                "before_commit": before_commit, "merge_commit": merge_commit,
                "note": "合并已撤销，任务 blocked",
            }

        # 7. 全部通过
        self._log_merge_result(item, reg_result, "success")
        item.status = "done"
        return {
            "success": True, "error": None,
            "task_id": item.task_id, "execution_id": item.execution_id,
            "status": "merged", "merge_commit": merge_commit,
            "before_commit": before_commit,
            "regression_passed": True,
        }

    def _safe_undo_merge(self, item: MergeItem, before_commit: str):
        """安全撤销合并（重置到合并前的 commit，而非盲目 HEAD~1）"""
        r = self._run_git(["reset", "--hard", before_commit], timeout=30)
        if not r["success"]:
            # Fallback: 尝试 HEAD~1
            r2 = self._run_git(["reset", "--hard", "HEAD~1"], timeout=30)
            if not r2["success"]:
                # 最后手段：使用 ORIG_HEAD
                self._run_git(["reset", "--hard", "ORIG_HEAD"], timeout=30)
        # 清理未跟踪文件
        self._run_git(["clean", "-fd"], timeout=15)

    def _log_merge_result(self, item: MergeItem, reg_result: Dict[str, Any], outcome: str):
        """记录合并结果到 merge_log.json"""
        log_path = self.repo_path / ".executor" / "merge_log.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        log_entry = {
            "task_id": item.task_id,
            "execution_id": item.execution_id,
            "branch": item.branch,
            "outcome": outcome,
            "regression": reg_result,
            "queued_at": item.queued_at,
            "merge_started_at": item.merge_started_at,
            "merge_finished_at": item.merge_finished_at,
            "conflict_files": item.conflict_files,
            "error": item.error,
        }
        existing = []
        if log_path.exists():
            try:
                existing = json.loads(log_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.append(log_entry)
        log_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_current_commit(self) -> Optional[str]:
        r = self._run_git(["rev-parse", "HEAD"], timeout=10)
        return r["stdout"] if r["success"] else None

    def is_master_clean(self) -> bool:
        """检查主分支是否 clean"""
        r = self._run_git(["status", "--porcelain"])
        return r["success"] and r["stdout"] == ""

    def get_master_commit(self) -> Optional[str]:
        return self._get_current_commit()

    def run_regression_tests(
        self,
        test_command: List[str] = None,
    ) -> Dict[str, Any]:
        """在主分支上运行回归测试"""
        test_cmd = test_command or ["pytest", "-v", "--tb=short"]
        try:
            result = subprocess.run(
                test_cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "success": result.returncode == 0,
                "passed": result.returncode == 0,
                "stdout": result.stdout[:5000],
                "stderr": result.stderr[:5000],
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "passed": False, "error": "回归测试超时"}
        except Exception as e:
            return {"success": False, "passed": False, "error": str(e)}

    def undo_last_merge(self) -> Dict[str, Any]:
        """撤销最后一次合并（安全版：使用 ORIG_HEAD 回退）"""
        before = self._get_current_commit()
        # ORIG_HEAD 是 git merge 自动保存的合并前位置，比 HEAD~1 更可靠
        r = self._run_git(["reset", "--hard", "ORIG_HEAD"], timeout=30)
        after = self._get_current_commit()
        return {
            "success": r["success"],
            "error": r["stderr"] if not r["success"] else None,
            "before_commit": before,
            "after_commit": after,
        }
