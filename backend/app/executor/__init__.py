"""执行器模块 - Step 3: 双Worker并行执行与独立自修复

组件：
  ExecutorAdapter        - 外部CLI适配器
  TaskWorker             - 单任务Worker
  GitManager             - Git Worktree管理
  SafetyGuard            - 安全检查
  CommandRunner          - 命令执行器
  TestRunner             - 独立测试运行器
  ResultCollector        - 结果收集器
  RunStore               - executor_runs原子操作层
  LoopController         - 自动循环控制器
  TaskScheduler          - 任务调度器
  RecoveryManager        - 重启恢复管理器
  BudgetGuard            - 预算守卫

Step 3 新增:
  ResourceLockManager    - 资源锁管理器
  ParallelScheduler      - 并行任务调度器
  WorkerPool             - 双Worker线程池
  WorktreeManager        - Git Worktree管理
  MergeCoordinator       - 串行合并队列
  ParallelRecoveryManager - 双Worker重启恢复

Step 3 修复:
  ExecutionFinalizer     - 统一终结清理器
  finalize_execution     - 便捷终结函数
"""
from .adapter import ExecutorAdapter, detect_available_adapters
from .git_manager import GitManager
from .safety_guard import SafetyGuard
from .command_runner import CommandRunner
from .test_runner import TestRunner
from .task_worker import TaskWorker, run_single_task
from .result_collector import ResultCollector
from .run_store import RunStore
from .loop_controller import LoopController
from .task_scheduler import TaskScheduler
from .recovery_manager import RecoveryManager
from .budget_guard import BudgetGuard

# Step 3 新增
from .resource_lock_manager import ResourceLockManager
from .parallel_scheduler import ParallelScheduler, TaskGroup
from .worker_pool import WorkerPool, WorkerContext
from .worktree_manager import WorktreeManager
from .merge_coordinator import MergeCoordinator, MergeItem
from .parallel_recovery_manager import ParallelRecoveryManager

# Step 3 修复: 统一终结清理
from .cleanup import ExecutionFinalizer, finalize_execution

# V1.8C: 项目执行审批
from .execution_approval_service import (
    ExecutionApprovalService,
    get_execution_approval_service,
    has_valid_execution_approval,
)

__all__ = [
    "ExecutorAdapter",
    "detect_available_adapters",
    "GitManager",
    "SafetyGuard",
    "CommandRunner",
    "TestRunner",
    "TaskWorker",
    "run_single_task",
    "ResultCollector",
    "RunStore",
    "LoopController",
    "TaskScheduler",
    "RecoveryManager",
    "BudgetGuard",
    # Step 3
    "ResourceLockManager",
    "ParallelScheduler",
    "TaskGroup",
    "WorkerPool",
    "WorkerContext",
    "WorktreeManager",
    "MergeCoordinator",
    "MergeItem",
    "ParallelRecoveryManager",
    # Step 3 修复
    "ExecutionFinalizer",
    "finalize_execution",
    # V1.8C
    "ExecutionApprovalService",
    "get_execution_approval_service",
    "has_valid_execution_approval",
]
