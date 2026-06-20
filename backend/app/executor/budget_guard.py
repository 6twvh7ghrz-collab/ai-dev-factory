"""BudgetGuard - 预算守卫

默认预算：
  MAX_TASKS_PER_RUN = 20
  MAX_RUNTIME_SECONDS = 7200
  MAX_CONSECUTIVE_FAILURES = 2
  MAX_REPAIR_ATTEMPTS_PER_TASK = 2
  MAX_CLI_CALLS_PER_TASK = 5
  IDLE_POLL_INTERVAL_SECONDS = 3
  HEARTBEAT_INTERVAL_SECONDS = 30
  HEARTBEAT_TIMEOUT_SECONDS = 120

预算超限 → status=paused, pause_reason=budget_exceeded
"""
import json
import time
from typing import Dict, Any


class BudgetGuard:
    """预算守卫"""

    # 默认预算
    MAX_TASKS_PER_RUN = 20
    MAX_RUNTIME_SECONDS = 7200  # 2 小时
    MAX_CONSECUTIVE_FAILURES = 5  # 提高容错（沙箱模式可能有 AI 重试）
    MAX_REPAIR_ATTEMPTS_PER_TASK = 2
    MAX_CLI_CALLS_PER_TASK = 5
    IDLE_POLL_INTERVAL_SECONDS = 3
    HEARTBEAT_INTERVAL_SECONDS = 30
    HEARTBEAT_TIMEOUT_SECONDS = 120

    def __init__(self, budget_overrides: Dict[str, Any] = None):
        """
        Args:
            budget_overrides: 预算覆盖字典，键对应类常量名
        """
        self._budget = {
            "max_tasks_per_run": self.MAX_TASKS_PER_RUN,
            "max_runtime_seconds": self.MAX_RUNTIME_SECONDS,
            "max_consecutive_failures": self.MAX_CONSECUTIVE_FAILURES,
            "max_repair_attempts_per_task": self.MAX_REPAIR_ATTEMPTS_PER_TASK,
            "max_cli_calls_per_task": self.MAX_CLI_CALLS_PER_TASK,
            "idle_poll_interval_seconds": self.IDLE_POLL_INTERVAL_SECONDS,
            "heartbeat_interval_seconds": self.HEARTBEAT_INTERVAL_SECONDS,
            "heartbeat_timeout_seconds": self.HEARTBEAT_TIMEOUT_SECONDS,
        }
        if budget_overrides:
            self._budget.update(budget_overrides)

    @property
    def max_tasks_per_run(self) -> int:
        return self._budget["max_tasks_per_run"]

    @property
    def max_runtime_seconds(self) -> int:
        return self._budget["max_runtime_seconds"]

    @property
    def max_consecutive_failures(self) -> int:
        return self._budget["max_consecutive_failures"]

    @property
    def max_repair_attempts_per_task(self) -> int:
        return self._budget["max_repair_attempts_per_task"]

    @property
    def max_cli_calls_per_task(self) -> int:
        return self._budget["max_cli_calls_per_task"]

    @property
    def idle_poll_interval_seconds(self) -> int:
        return self._budget["idle_poll_interval_seconds"]

    @property
    def heartbeat_interval_seconds(self) -> int:
        return self._budget["heartbeat_interval_seconds"]

    @property
    def heartbeat_timeout_seconds(self) -> int:
        return self._budget["heartbeat_timeout_seconds"]

    def check_run_budget(self, run: Dict[str, Any],
                         run_start_time: float) -> Dict[str, Any]:
        """
        检查 run 级预算。

        Returns:
            {"ok": bool, "reason": str}
        """
        tasks_completed = run.get("tasks_completed", 0) or 0
        tasks_total = run.get("tasks_total", 0) or 0
        tasks_failed = run.get("tasks_failed", 0) or 0
        tasks_blocked = run.get("tasks_blocked", 0) or 0

        # 1. 任务数量限制
        if tasks_total >= self.max_tasks_per_run:
            return {"ok": False, "reason": f"已达到最大任务数 {self.max_tasks_per_run}"}

        # 2. 运行时间限制
        elapsed = time.time() - run_start_time
        if elapsed >= self.max_runtime_seconds:
            return {"ok": False,
                    "reason": f"运行时间已达上限 {self.max_runtime_seconds}s"}

        # 3. 连续失败限制
        if tasks_failed >= self.max_consecutive_failures:
            return {"ok": False,
                    "reason": f"连续失败 {tasks_failed} 次，达到上限 {self.max_consecutive_failures}"}

        return {"ok": True, "reason": ""}

    def check_task_budget(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """
        检查任务级预算（由 TaskWorker 内部处理 repair_count 和 CLI 调用次数）。
        此处仅作兼容性返回。
        """
        return {"ok": True, "reason": ""}

    def to_dict(self) -> Dict[str, Any]:
        """导出预算配置"""
        return dict(self._budget)

    def to_json(self) -> str:
        """导出为 JSON 字符串"""
        return json.dumps(self._budget, ensure_ascii=False)
