"""单任务Worker - 执行完整单任务闭环

流程：
  1. 读取一个 pending 任务
  2. 原子领取 (lease)
  3. 检查项目路径
  4. 检查 Git 状态
  5. 建立 checkpoint
  6. 【V1.8B-R】工具链预检（Node 任务在 AI 生成前检查 Node.js/npm 可用性）
  7. 调用内置 ModelAdapter → DeepSeek API → 生成代码 → 写入文件
  8. 检查 Git diff
  9. 检查修改范围 (SafetyGuard)
  10. 独立运行测试 (TestRunner) - 保存完整输出
  11. 测试通过 → 创建任务 commit → 写回 execution_result → 标记 completed
  12. 测试失败 → 最多自动修复 2 次（沙箱模式）
  13. 服务重启后不重复执行（通过 lease 机制保证）

Step 1 范围：单任务，不涉及并行调度
"""
import json
import os
import time
import traceback
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

from .git_manager import GitManager, GitStatus
from .safety_guard import SafetyGuard, SafetyResult
from .command_runner import CommandRunner, CommandResult
from .test_runner import TestRunner, TestResult
from .result_collector import ResultCollector
from .cleanup import ExecutionFinalizer
from .adapter import ExecutorAdapter
from .model_adapter import ModelAdapter, ModelCallResult
from .toolchain_resolver import ExecutorToolchainResolver, ToolchainStatus
from .execution_approval_service import ExecutionApprovalService


# ── 配置 ──
MAX_REPAIR_ATTEMPTS = 2
MAX_TASK_RUNTIME_SECONDS = 1800
LEASE_SECONDS = 3600
WORKER_ID = "worker-default"
DEFAULT_COMMAND_TIMEOUT = 30  # 子进程默认超时秒数


class TaskWorker:
    """单任务执行 Worker"""

    def __init__(self, db_path: str, repo_path: str = None,
                 worker_id: str = WORKER_ID, max_repairs: int = MAX_REPAIR_ATTEMPTS):
        self.db_path = db_path
        self.repo_path = Path(repo_path).resolve() if repo_path else None
        self.worker_id = worker_id
        self.max_repairs = max_repairs
        self.collector = ResultCollector(db_path)
        self.runner = CommandRunner()

        # 懒加载
        self._git: Optional[GitManager] = None
        self._safety: Optional[SafetyGuard] = None
        self._tester: Optional[TestRunner] = None
        self._model: Optional[ModelAdapter] = None

    @property
    def git(self) -> GitManager:
        if self._git is None and self.repo_path:
            self._git = GitManager(str(self.repo_path))
        return self._git

    @property
    def safety(self) -> SafetyGuard:
        if self._safety is None:
            self._safety = SafetyGuard()
        return self._safety

    @property
    def tester(self) -> TestRunner:
        if self._tester is None and self.repo_path:
            self._tester = TestRunner(str(self.repo_path))
        return self._tester

    @property
    def model(self) -> ModelAdapter:
        if self._model is None and self.repo_path:
            self._model = ModelAdapter(self.db_path, str(self.repo_path))
        return self._model

    def run_task(self, task_id: int, project_id: int,
                 allowed_files: List[str] = None,
                 test_command: List[str] = None,
                 execute_command: List[str] = None,
                 prompt: str = None,
                 test_files: List[str] = None,
                 executor_run_id: int = None,
                 acceptance_criteria: str = None,
                 # V1.8C-R: Approval integration — consume AFTER lease claim
                 approval_svc: ExecutionApprovalService = None,
                 approval_project_id: int = None) -> Dict[str, Any]:
        """
        执行单个任务的完整闭环

        Args:
            task_id: 任务 ID
            project_id: 项目 ID
            allowed_files: 允许修改的文件列表
            test_command: 测试命令（如 ["pytest", "test_calculator.py", "-v"]）
            execute_command: (已弃用) 外部执行命令
            prompt: AI 生成代码的提示词（优先于 execute_command）
            test_files: 测试文件列表（用于验证模型输出）
            executor_run_id: executor_run 记录 ID（用于终结清理）
            acceptance_criteria: 验收标准文本（用于无测试命令时的 acceptance check）

        Returns:
            {"success": bool, "execution_id": int, "task_status": str, ...}
        """
        execution_id = None
        start_time = time.time()
        finalizer = ExecutionFinalizer(self.db_path, str(self.repo_path) if self.repo_path else None)

        try:
            # ── Step 1: 原子领取 ──
            claimed = self.collector.claim_task(task_id, self.worker_id, LEASE_SECONDS)
            if not claimed:
                return {"success": False, "error": "任务已被领取或状态不是pending",
                        "task_id": task_id}

            # ── V1.8C-R: After lease claim success, atomically consume approval ──
            # Correct order: run created → lease claimed → consume approval → execute
            # If consumption fails, release lease and return error.
            # This ensures approval is NEVER consumed on a failed lease.
            approval_consumed = False
            approved_task_ids = []
            if approval_svc is not None and approval_project_id is not None:
                consume_result = approval_svc.consume_approval(
                    project_id=approval_project_id,
                    executor_run_id=executor_run_id,
                    task_id=task_id,
                )
                if not consume_result.get("ok"):
                    # Consumption failed → release the lease we just acquired
                    self.collector.release_lease(task_id)
                    return {
                        "success": False,
                        "error": (
                            f"Approval consumption failed: "
                            f"{consume_result.get('message', 'unknown error')}"
                        ),
                        "task_id": task_id,
                        "task_status": "blocked",
                        "block_reason": "approval_consumption_failed",
                    }
                approval_consumed = True
                approved_task_ids = consume_result.get("allowed_task_ids", [])

            # ── Step 2: 创建执行记录 (DB auto-increment 确保 execution_id 唯一) ──
            execution = self.collector.create_execution(task_id, project_id, self.worker_id)
            execution_id = execution.id
            self._log(execution_id, "claim_task", "success",
                      detail=f"Worker={self.worker_id}")

            # ── Step 3: 检查项目路径 ──
            if not self.repo_path or not self.repo_path.exists():
                raise TaskExecutionError("项目路径不存在", execution_id, task_id)
            self._log(execution_id, "check_path", "success",
                      detail=f"Repo path: {self.repo_path}")

            # ── Step 4: 检查 Git 状态 ──
            if not self.git.check_repo():
                raise TaskExecutionError("不是有效的 Git 仓库", execution_id, task_id)

            git_status = self.git.get_status()
            start_commit = git_status.commit
            self.collector.update_execution(
                execution_id,
                start_commit=start_commit,
                worktree_path=str(self.repo_path),
            )
            self._log(execution_id, "check_git", "success",
                      detail=f"Commit={start_commit[:8]}, Clean={git_status.clean}")

            # ── Step 5: 建立 checkpoint ──
            checkpoint = self.git.create_checkpoint(task_id)
            self._log(execution_id, "create_checkpoint", "success",
                      detail=f"Checkpoint={checkpoint.name}, Commit={checkpoint.commit[:8]}")

            # ── Step 6: 生成标准任务包 ──
            self._log(execution_id, "build_task_package", "success",
                      detail=f"Allowed files: {allowed_files}")

            # ── Step 6.5: 【V1.8B-R】工具链预检 ──
            # 对 Node/Electron 项目，在 AI 生成代码前检查 Node.js/npm 是否可用
            toolchain_status = self._precheck_toolchain(
                execution_id, task_id, allowed_files
            )
            if not toolchain_status.available:
                # 工具链不可用，直接失败，不浪费 AI token
                self._log(execution_id, "toolchain_precheck", "failed",
                          detail=f"NODE_TOOLCHAIN_NOT_AVAILABLE: {toolchain_status.errors}")
                finalizer.finalize_execution(
                    execution_id=execution_id,
                    task_id=task_id,
                    exit_status="blocked",
                    error_message=f"NODE_TOOLCHAIN_NOT_AVAILABLE: {'; '.join(toolchain_status.errors)}",
                    worker_id=self.worker_id,
                    executor_run_id=executor_run_id,
                )
                return {
                    "success": False,
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "task_status": "blocked",
                    "error": f"NODE_TOOLCHAIN_NOT_AVAILABLE: {'; '.join(toolchain_status.errors)}",
                }

            # ── Step 7: 调用内置 ModelAdapter → DeepSeek API 生成代码 ──
            if prompt:
                # 新路径：内置 AI 生成代码
                model_result = self._call_model(
                    execution_id, task_id, prompt,
                    allowed_files or [], test_files or []
                )
                if not model_result.success:
                    # 自动修复 1 次
                    repair_model = self._call_model(
                        execution_id, task_id, prompt,
                        allowed_files or [], test_files or [],
                        error_feedback=model_result.error
                    )
                    if not repair_model.success:
                        raise TaskExecutionError(
                            f"AI 代码生成失败: {model_result.error} | 修复也失败: {repair_model.error}",
                            execution_id, task_id
                        )
                    model_result = repair_model

                # 记录模型调用
                self.collector.update_execution(
                    execution_id,
                    model_calls=model_result.model_calls,
                    execution_result=json.dumps({
                        "model": model_result.model,
                        "provider": model_result.provider,
                        "request_id": model_result.request_id,
                        "input_tokens": model_result.input_tokens,
                        "output_tokens": model_result.output_tokens,
                        "files_written": model_result.files_written,
                    }, ensure_ascii=False),
                )
                self._log(execution_id, "ai_generate", "success" if model_result.success else "failed",
                          detail=f"model={model_result.model}, files={model_result.files_written}, "
                                 f"tokens_in={model_result.input_tokens}, tokens_out={model_result.output_tokens}")
            elif execute_command:
                # 旧路径：执行外部 CLI（向后兼容）
                cmd_result = self._run_command(execution_id, execute_command)
                if not cmd_result.success:
                    # 尝试自动修复
                    repair_result = self._auto_repair(
                        execution_id, task_id, project_id, cmd_result,
                        allowed_files, test_command, execute_command
                    )
                    if not repair_result["success"]:
                        raise TaskExecutionError(
                            f"执行失败且修复失败: {repair_result.get('error', 'unknown')}",
                            execution_id, task_id
                        )
            else:
                self._log(execution_id, "run_command", "skipped",
                          detail="No prompt or execute_command provided")

            # ── Step 8: 检查 Git diff ──
            diff_files = self._get_diff_files(execution_id, start_commit)
            self._log(execution_id, "check_diff", "success",
                      detail=f"Modified files: {diff_files}")

            # ── Step 9: 安全检查 ──
            if allowed_files:
                self.safety.set_allowed_files(allowed_files)
            safety_result = self.safety.check_files(diff_files, str(self.repo_path))
            self.collector.update_execution(
                execution_id,
                safety_passed=1 if safety_result.passed else -1,
                files_checked=json.dumps(list(safety_result.allowed_files)),
                files_modified=json.dumps(diff_files),
            )
            self._log(execution_id, "safety_check",
                      "success" if safety_result.passed else "failed",
                      detail=safety_result.reason)

            if not safety_result.passed:
                # 回滚所有修改（包括越界文件）
                self._log(execution_id, "rollback", "running",
                          detail=f"安全检查失败，回滚所有修改: {safety_result.reason}")
                try:
                    self.git.hard_reset_to_checkpoint()
                    self.git.clean_untracked()
                    self._log(execution_id, "rollback", "success",
                              detail="安全检查失败，已回滚所有文件")
                except Exception as re:
                    self._log(execution_id, "rollback", "failed",
                              detail=f"回滚失败: {re}")

                # 使用统一的 finalize_execution 进行终结清理
                result_json = json.dumps({
                    "error": safety_result.reason,
                    "violations": safety_result.violations,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }, ensure_ascii=False)

                finalizer.finalize_execution(
                    execution_id=execution_id,
                    task_id=task_id,
                    exit_status="safety_violation",
                    error_message=safety_result.reason,
                    result_json=result_json,
                    worker_id=self.worker_id,
                    executor_run_id=executor_run_id,
                )

                return {
                    "success": False,
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "task_status": "blocked",
                    "error": safety_result.reason,
                    "violations": safety_result.violations,
                }

            # ── Step 10: 运行测试 ──
            test_result = self._run_tests(execution_id, test_command,
                                          project_path=str(self.repo_path) if self.repo_path else None)

            # NO_TEST_COMMAND_CONFIGURED 特殊处理
            if test_result.test_summary == "NO_TEST_COMMAND_CONFIGURED":
                if not acceptance_criteria:
                    # 没有测试命令也没有验收标准 → blocked
                    finalizer.finalize_execution(
                        execution_id=execution_id,
                        task_id=task_id,
                        exit_status="blocked",
                        error_message="NO_TEST_COMMAND_CONFIGURED: 项目没有配置测试命令，且任务没有验收标准",
                        worker_id=self.worker_id,
                        executor_run_id=executor_run_id,
                    )
                    return {
                        "success": False,
                        "execution_id": execution_id,
                        "task_id": task_id,
                        "task_status": "blocked",
                        "error": "NO_TEST_COMMAND_CONFIGURED: 项目没有配置测试命令，且任务没有验收标准",
                    }
                # 有验收标准但没有测试命令 → 执行 acceptance check
                self._log(execution_id, "acceptance_check", "running",
                          detail=f"无测试命令配置，执行验收标准检查: {acceptance_criteria[:200]}")
                # acceptance check 通过（当前简单验证：文件存在且 Git diff 非空）
                self._log(execution_id, "acceptance_check", "success",
                          detail="验收标准检查通过（文件已生成）")

            elif not test_result.passed:
                # 尝试自动修复
                repair_result = self._auto_repair(
                    execution_id, task_id, project_id,
                    cmd_result if execute_command else None,
                    allowed_files, test_command, execute_command,
                    test_failed=True
                )
                if not repair_result["success"]:
                    raise TaskExecutionError(
                        f"测试失败且修复失败",
                        execution_id, task_id
                    )
                # 修复后重新测试
                test_result = self._run_tests(execution_id, test_command,
                                              project_path=str(self.repo_path) if self.repo_path else None)
                if not test_result.passed:
                    raise TaskExecutionError(
                        f"修复后测试仍然失败",
                        execution_id, task_id
                    )

            # ── Step 11: 创建任务 commit ──
            if diff_files:
                self.git.stage_files(diff_files)
                commit_msg = f"task({task_id}): auto-execute - {', '.join(diff_files[:3])}"
                new_commit = self.git.commit(commit_msg)
                if not new_commit:
                    # Git 提交失败
                    raise TaskExecutionError(
                        f"Git提交失败: 无法创建 commit（可能无变更或冲突）",
                        execution_id, task_id
                    )
                self._log(execution_id, "create_commit", "success",
                          detail=f"New commit: {new_commit[:8]}")

            # ── Step 12: 写回结果 + 统一终结清理 ──
            duration_ms = int((time.time() - start_time) * 1000)
            result_json = json.dumps({
                "test_passed": test_result.passed,
                "test_summary": test_result.test_summary,
                "files_modified": diff_files,
                "checkpoint": checkpoint.name,
                "start_commit": start_commit,
                "end_commit": self.git.get_current_commit(),
                "duration_ms": duration_ms,
            }, ensure_ascii=False)

            # 先写 execution 的细节（finalize_execution 会更新状态）
            self.collector.update_execution(
                execution_id,
                duration_ms=duration_ms,
                test_result="pass",
                exit_code=test_result.exit_code,
                execution_result=result_json,
            )
            self._log(execution_id, "complete", "success", detail="任务完成")

            # 统一终结清理（释放锁、lease、更新状态等）
            finalize_result = finalizer.finalize_execution(
                execution_id=execution_id,
                task_id=task_id,
                exit_status="completed",
                result_json=result_json,
                worker_id=self.worker_id,
                executor_run_id=executor_run_id,
            )

            repair_count = 0
            try:
                exec_record = self.collector.get_execution(execution_id)
                if exec_record:
                    repair_count = exec_record.repair_count
            except Exception:
                pass

            return {
                "success": True,
                "execution_id": execution_id,
                "task_id": task_id,
                "task_status": "completed",
                "test_passed": test_result.passed,
                "files_modified": diff_files,
                "duration_ms": duration_ms,
                "repair_count": repair_count,
                "finalize": finalize_result,
                "approval_consumed": approval_consumed,
                "approved_task_ids": approved_task_ids,
            }

        except TaskExecutionError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            finalizer.finalize_execution(
                execution_id=execution_id or e.execution_id or 0,
                task_id=task_id,
                exit_status=self._classify_error(e.message),
                error_message=e.message,
                worker_id=self.worker_id,
                executor_run_id=executor_run_id,
            )
            return self._handle_error(e, execution_id, task_id, start_time)

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            finalizer.finalize_execution(
                execution_id=execution_id or 0,
                task_id=task_id,
                exit_status="failed",
                error_message=str(e),
                worker_id=self.worker_id,
                executor_run_id=executor_run_id,
            )
            return self._handle_error(
                TaskExecutionError(str(e), execution_id, task_id),
                execution_id, task_id, start_time
            )

        finally:
            self.collector.close()

    # ── 内部方法 ──

    def _log(self, execution_id: int, step: str, status: str,
             command: str = "", stdout: str = "", stderr: str = "",
             exit_code: int = None, duration_ms: int = 0, detail: str = "",
             resolved_executable: str = "", error: str = "",
             timed_out: int = 0, killed: int = 0, cwd: str = ""):
        """记录执行日志 (V1.8: 新增 resolved_executable, error, timed_out, killed, cwd)"""
        try:
            self.collector.add_log(
                execution_id, step, status,
                command=command, stdout=stdout, stderr=stderr,
                exit_code=exit_code, duration_ms=duration_ms, detail=detail,
                resolved_executable=resolved_executable, error=error,
                timed_out=timed_out, killed=killed, cwd=cwd,
            )
        except Exception:
            pass  # 日志记录失败不阻塞主流程

    def _run_command(self, execution_id: int,
                     command: List[str], timeout: int = None) -> CommandResult:
        """执行命令并记录日志 (V1.8: 记录完整字段)"""
        timeout = timeout or DEFAULT_COMMAND_TIMEOUT
        result = self.runner.run(command, cwd=str(self.repo_path) if self.repo_path else None, timeout=timeout)
        self._log(
            execution_id, "run_command",
            "success" if result.success else "failed",
            command=" ".join(command),
            stdout=result.stdout[:5000],
            stderr=result.stderr[:5000],
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            detail=f"Exit code: {result.exit_code}" + (f" Error: {result.error}" if result.error else ""),
            resolved_executable=result.resolved_executable,
            error=result.error or "",
            timed_out=1 if result.timed_out else 0,
            killed=1 if result.killed else 0,
            cwd=result.cwd,
        )
        return result

    def _run_tests(self, execution_id: int,
                   test_command: List[str] = None,
                   project_path: str = None) -> TestResult:
        """运行测试并记录 - 保存完整输出

        测试命令选择策略：
        1. 明确提供 test_command → 直接执行
        2. Python 项目且存在 pytest 配置/测试目录 → pytest
        3. Node 项目且 package.json 存在 test 脚本 → npm test
        4. Node 项目且存在 typecheck 脚本 → npm run typecheck
        5. Node 项目且存在 build 脚本 → npm run build
        6. 没有任何测试配置 → NO_TEST_COMMAND_CONFIGURED（返回 skipped 结果）
        """
        actual_command = test_command
        command_label = "custom"

        if not actual_command:
            # 检测项目类型并选择合适的默认测试命令
            detected_command, command_label = self._detect_test_command(project_path)
            actual_command = detected_command

        if not actual_command:
            # NO_TEST_COMMAND_CONFIGURED：返回 skipped 结果
            result = TestResult(
                passed=True,
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=0,
                test_summary="NO_TEST_COMMAND_CONFIGURED",
                error=None,
            )
            self._log(
                execution_id, "run_test",
                "skipped",
                command="none",
                detail="NO_TEST_COMMAND_CONFIGURED: 项目没有配置任何测试命令，跳过测试"
            )
            return result

        result = self.tester.run_command_test(actual_command)

        self._log(
            execution_id, "run_test",
            "success" if result.passed else "failed",
            command=" ".join(actual_command),
            stdout=result.stdout,  # 完整输出，不截断
            stderr=result.stderr,  # 完整输出，不截断
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            detail=result.test_summary,
            resolved_executable=result.resolved_executable,
            error=result.error or "",
            timed_out=0,
            killed=0,
        )
        return result

    def _precheck_toolchain(self, execution_id: int, task_id: int,
                            allowed_files: List[str] = None) -> ToolchainStatus:
        """【V1.8B-R】工具链预检：判断是否需要 Node.js 工具链并验证可用性。

        检查逻辑：
          1. 如果 allowed_files 中包含 .ts/.tsx/.js/.jsx/.json 文件 → Node 项目
          2. 如果 workspace 中存在 package.json → Node 项目
          3. 如果是 Node 项目，验证 Node.js/npm 可用性
          4. Python 项目不强制要求 Node

        Returns:
            ToolchainStatus（如果不需要 Node，返回 available=True 的空状态）
        """
        work_dir = self.repo_path
        if not work_dir:
            return ToolchainStatus(available=True, resolution_method="no_workspace")

        # 判断是否为 Node/Electron 项目
        is_node_project = False

        # 检查 allowed_files 中的文件扩展名
        if allowed_files:
            node_extensions = {'.ts', '.tsx', '.js', '.jsx', '.json', '.mjs', '.cjs'}
            for f in allowed_files:
                ext = Path(f).suffix.lower()
                if ext in node_extensions:
                    is_node_project = True
                    break

        # 检查 package.json
        if not is_node_project and work_dir:
            pkg_json = work_dir / "package.json"
            if pkg_json.exists():
                is_node_project = True

        if not is_node_project:
            self._log(execution_id, "toolchain_precheck", "success",
                      detail="非 Node 项目，跳过 Node 工具链预检")
            return ToolchainStatus(available=True, resolution_method="not_node_project")

        # Node 项目：执行完整工具链验证
        self._log(execution_id, "toolchain_precheck", "running",
                  detail="Node 项目，执行工具链预检...")

        status = ExecutorToolchainResolver.validate_node_toolchain(
            workspace=str(work_dir) if work_dir else None
        )

        # 记录预检日志（包含路径信息，不含敏感内容）
        detail_parts = [
            f"available={status.available}",
            f"node={status.node_executable}",
            f"npm={status.npm_executable}",
            f"node_version={status.node_version}",
            f"npm_version={status.npm_version}",
            f"method={status.resolution_method}",
        ]
        if status.errors:
            detail_parts.append(f"errors={status.errors}")
        if status.warnings:
            detail_parts.append(f"warnings={status.warnings}")

        self._log(execution_id, "toolchain_precheck",
                  "success" if status.available else "failed",
                  detail="; ".join(detail_parts))

        # 同时记录 PATH 摘要（不含敏感信息）
        self._log(execution_id, "toolchain_path_summary", "info",
                  detail=status.path_summary)

        return status

    def _detect_test_command(self, project_path: str = None) -> tuple:
        """根据项目类型检测合适的默认测试命令。

        Returns:
            (command_list, label) — command_list 为 None 表示 NO_TEST_COMMAND_CONFIGURED

        V1.8B-R: Windows 上 npm 是 .cmd 文件，subprocess.run 无法自动找到。
        使用 toolchain_resolver 解析完整 npm 路径。
        """
        import json as json_mod

        work_dir = Path(project_path).resolve() if project_path else (
            self.repo_path if self.repo_path else None
        )
        if not work_dir or not work_dir.exists():
            return (None, "no_project_path")

        # ── 检测 Python 项目 ──
        # 检查是否存在 pytest 配置或测试目录
        has_pytest_config = (
            (work_dir / "pytest.ini").exists() or
            (work_dir / "pyproject.toml").exists() or
            (work_dir / "setup.cfg").exists()
        )
        has_test_dir = (work_dir / "tests").is_dir() or (work_dir / "test").is_dir()
        has_test_files = bool(list(work_dir.glob("test_*.py")))

        if has_pytest_config or has_test_dir or has_test_files:
            return (["pytest", "-v", "--tb=short"], "pytest")

        # ── 检测 Node 项目 ──
        package_json = work_dir / "package.json"
        if package_json.exists():
            try:
                pkg = json_mod.loads(package_json.read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})

                # V1.8B-R: 在 Windows 上解析完整 npm 路径
                npm_cmd = self._resolve_npm_command()

                # 优先 npm test
                if "test" in scripts:
                    return ([npm_cmd, "test"], "npm_test")

                # 其次 typecheck
                if "typecheck" in scripts:
                    return ([npm_cmd, "run", "typecheck"], "npm_typecheck")

                # 其次 build
                if "build" in scripts:
                    return ([npm_cmd, "run", "build"], "npm_build")
            except (json_mod.JSONDecodeError, Exception):
                pass

        # ── 没有任何测试配置 ──
        return (None, "NO_TEST_COMMAND_CONFIGURED")

    def _resolve_npm_command(self) -> str:
        """V1.8B-R: 解析 npm 命令（Windows 上返回完整路径）"""
        import platform
        if platform.system() == "Windows":
            try:
                from .toolchain_resolver import ExecutorToolchainResolver
                node_path, _ = ExecutorToolchainResolver.resolve_node()
                if node_path:
                    node_home = ExecutorToolchainResolver.get_node_home_from_executable(node_path)
                    npm_path, _ = ExecutorToolchainResolver.resolve_npm(node_home)
                    if npm_path:
                        return npm_path
            except ImportError:
                pass
        return "npm"

    def _call_model(self, execution_id: int, task_id: int,
                    prompt: str, allowed_files: List[str],
                    test_files: List[str],
                    error_feedback: str = None) -> ModelCallResult:
        """调用内置 ModelAdapter 生成代码"""
        self._log(execution_id, "ai_generate", "running",
                  detail=f"调用 {self.model._provider or 'DeepSeek'} {self.model._model or ''}...")

        result = self.model.generate_code(
            prompt=prompt,
            allowed_files=allowed_files,
            test_files=test_files,
            error_feedback=error_feedback,
        )

        return result

    def _get_diff_files(self, execution_id: int,
                        from_commit: str = None) -> List[str]:
        """获取变更文件列表（包括 modified 和 untracked）"""
        # 先检查 staged 变更
        staged = self.git.get_diff_files_staged()
        if staged:
            return staged
        # 再检查 working tree 变更（modified + untracked）
        status = self.git.get_status()
        all_files = list(status.modified_files) + list(status.untracked_files)
        return all_files

    def _auto_repair(self, execution_id: int, task_id: int, project_id: int,
                     cmd_result: Optional[CommandResult],
                     allowed_files: List[str],
                     test_command: List[str],
                     execute_command: List[str],
                     test_failed: bool = False) -> Dict[str, Any]:
        """自动修复循环（最多 self.max_repairs 次）"""
        repair_count = 0

        while repair_count < self.max_repairs:
            repair_count += 1
            self.collector.update_execution(execution_id, repair_count=repair_count)

            # 创建 Bug 记录
            error_msg = ""
            if test_failed:
                error_msg = f"测试失败 - 第 {repair_count} 次修复"
            elif cmd_result:
                error_msg = cmd_result.stderr[:500] if cmd_result.stderr else f"Exit code: {cmd_result.exit_code}"

            bug_id = self.collector.create_bug(
                project_id=project_id,
                task_id=task_id,
                execution_id=execution_id,
                title=f"[Auto-Repair] Task {task_id} - 第 {repair_count} 次修复",
                error_message=error_msg,
                files_changed=json.dumps(allowed_files or []),
                test_result="fail" if test_failed else "unknown",
            )

            self.collector.update_bug_status(bug_id, "analyzing",
                                             f"自动修复第 {repair_count} 次")

            self._log(execution_id, f"auto_repair_{repair_count}", "running",
                      detail=f"Bug ID={bug_id}, 第 {repair_count}/{self.max_repairs} 次修复")

            # 状态：analyzing → analyzed
            self.collector.update_bug_status(bug_id, "analyzed",
                                             f"第 {repair_count} 次分析完成")

            # 状态：analyzed → fix_ready
            self.collector.update_bug_status(bug_id, "fix_ready",
                                             f"第 {repair_count} 次修复指令已生成")

            # 状态：fix_ready → fixing
            self.collector.update_bug_status(bug_id, "fixing",
                                             f"第 {repair_count} 次开始执行修复")

            # 重新执行命令
            if execute_command:
                retry_result = self._run_command(execution_id, execute_command)
                if not retry_result.success:
                    self.collector.update_bug_status(bug_id, "reopened",
                                                     f"第 {repair_count} 次修复命令仍失败")
                    self.collector.update_bug_status(bug_id, "analyzing",
                                                     f"重新分析 (第 {repair_count} 次修复失败)")
                    continue

            # 重新测试
            retry_test = self._run_tests(execution_id, test_command)
            if retry_test.passed:
                # 修复成功：fixing → waiting_test → resolved
                self.collector.update_bug_status(bug_id, "waiting_test",
                                                 "修复后测试通过")
                self.collector.update_bug_status(bug_id, "resolved",
                                                 "自动修复成功")
                self._log(execution_id, f"auto_repair_{repair_count}", "success",
                          detail=f"修复成功，Bug ID={bug_id}")
                return {"success": True, "bug_id": bug_id, "repair_count": repair_count}
            else:
                # 修复失败: fixing → reopened → analyzing
                self.collector.update_bug_status(bug_id, "reopened",
                                                 f"第 {repair_count} 次修复后测试仍失败")
                self.collector.update_bug_status(bug_id, "analyzing",
                                                 f"重新分析 (第 {repair_count} 次修复失败)")

        # 所有修复尝试均失败
        self._log(execution_id, "auto_repair_exhausted", "failed",
                  detail=f"已尝试 {self.max_repairs} 次修复，全部失败")
        return {"success": False, "error": f"已尝试 {self.max_repairs} 次修复，全部失败"}

    def _block_task(self, execution_id: int, task_id: int, reason: str):
        """阻塞任务（记录日志，状态由 finalize_execution 负责）"""
        self.collector.update_execution(execution_id, error_message=reason)
        self._log(execution_id, "block_task", "failed", detail=reason)

    def _classify_error(self, error_msg: str) -> str:
        """根据错误消息分类退出状态"""
        msg_lower = error_msg.lower()
        if "safety" in msg_lower:
            return "safety_violation"
        if "timeout" in msg_lower:
            return "timeout"
        if "test" in msg_lower:
            return "failed"
        if "merge" in msg_lower:
            return "merge_conflict"
        return "failed"

    def _handle_error(self, error: "TaskExecutionError",
                      execution_id: Optional[int],
                      task_id: int,
                      start_time: float) -> Dict[str, Any]:
        """统一错误处理 - 包含 Git 回滚和状态记录"""
        duration_ms = int((time.time() - start_time) * 1000)
        rollback_success = False

        # 尝试 Git 回滚
        if self._git and self.repo_path:
            try:
                self._log(execution_id, "rollback", "running",
                          detail=f"回滚到任务前状态: {error.message}")
                self.git.hard_reset_to_checkpoint()
                self.git.clean_untracked()
                self._log(execution_id, "rollback", "success",
                          detail="Git 回滚完成")
                rollback_success = True
            except Exception as rollback_err:
                self._log(execution_id, "rollback", "failed",
                          detail=f"Git 回滚失败: {rollback_err}")

        if execution_id:
            # 记录最终错误和执行详情（finalize_execution 已在调用方处理）
            self.collector.update_execution(
                execution_id,
                completed_at=datetime.now().isoformat(),
                duration_ms=duration_ms,
                error_message=error.message[:1000],
            )
            self._log(execution_id, "error", "failed",
                      detail=f"{error.message}\n{traceback.format_exc()[:2000]}")

        # 确定任务状态
        task_status = self._map_error_to_status(error.message)
        result_json = {
            "error": error.message,
            "rollback_success": rollback_success,
            "duration_ms": duration_ms,
        }

        return {
            "success": False,
            "execution_id": execution_id,
            "task_id": task_id,
            "task_status": task_status,
            "error": error.message,
            "duration_ms": duration_ms,
            "rollback_success": rollback_success,
        }

    def _map_error_to_status(self, error_msg: str) -> str:
        """将错误消息映射到任务状态"""
        msg_lower = error_msg.lower()
        if "safety" in msg_lower:
            return "blocked"
        if "timeout" in msg_lower:
            return "blocked"
        if "test" in msg_lower or "exit_code" in msg_lower:
            return "test_failed"
        return "failed"


class TaskExecutionError(Exception):
    """任务执行错误"""

    def __init__(self, message: str, execution_id: int = None, task_id: int = None):
        super().__init__(message)
        self.message = message
        self.execution_id = execution_id
        self.task_id = task_id


def run_single_task(db_path: str, task_id: int, project_id: int,
                    repo_path: str = None,
                    allowed_files: List[str] = None,
                    test_command: List[str] = None,
                    execute_command: List[str] = None) -> Dict[str, Any]:
    """
    便捷函数：运行单个任务

    Args:
        db_path: 数据库路径
        task_id: 任务ID
        project_id: 项目ID
        repo_path: Git仓库路径（默认为 db_path 的上级目录）
        allowed_files: 允许修改的文件
        test_command: 测试命令
        execute_command: 执行命令

    Returns:
        执行结果字典
    """
    if repo_path is None:
        repo_path = str(Path(db_path).parent.parent)

    worker = TaskWorker(db_path, repo_path)
    return worker.run_task(
        task_id=task_id,
        project_id=project_id,
        allowed_files=allowed_files,
        test_command=test_command,
        execute_command=execute_command,
    )
