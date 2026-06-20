# RESOURCE_LOCK_PIPELINE_NOT_INTEGRATED

**状态**: 已确认，待实施
**发现轮次**: V1.7D-PROBE
**确认轮次**: V1.7E

## 问题描述

当前 TaskWorker 未集成 ResourceLockManager。Executor 在 claim_task 和文件修改之间没有获取资源锁。

## 影响范围

- 在多 Worker 场景下，两个 Worker 可能同时修改同一个项目的文件
- 当前通过 `max_workers=1` 和单项目单任务执行规避此问题

## 当前限制条件

```text
max_workers = 1
单项目单任务执行
不得并发启动 Task 31
```

## 建议接入点

1. **claim_task 之后、文件修改之前申请锁**:
   ```python
   lock = resource_lock_manager.acquire(
       resource_scope="project",
       scope_key=f"project_{project_id}",
       resource_type="workspace_files",
       worker_id=worker_id,
       timeout_seconds=LEASE_SECONDS,
   )
   ```

2. **finalizer 或异常 finally 中释放锁**:
   ```python
   resource_lock_manager.release(lock_token, reason="task_completed")
   ```

## 后续任务

另立专门任务实施 ResourceLockManager 集成，不在 V1.7E 范围内。
