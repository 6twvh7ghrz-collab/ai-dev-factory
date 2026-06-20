# V2.0-A-R2 MVP 验收标准

> **状态**: 冻结  
> **版本**: V2.0-A-R2  
> **日期**: 2026-06-19  
> **修订**: 统一最终实体名、移除旧表名残留、MVP 并发语义最终修正  

---

## 1. 概述

定义 V2.0-A MVP 的最低交付标准。所有标准必须在 Phase 5（测试 & 验证）通过后，才能进入 Phase 6（上线）。

### 1.1 MVP 范围界定

| 维度 | 包含 | 不包含 (V2.1+) |
|------|------|---------------|
| 执行模型 | 1 Supervisor + 1 Worker + 1 Reviewer 串行 | 多 Worker 并行 |
| 并发度 | 仅 1 个执行 Worker（并发数固定为 1） | 动态扩缩容 |
| 通信 | MessageBus（内存+DB） | 外部消息队列 |
| Worker 类型 | CodeGen, TestRunner, Git, Safety, Cmd | MergeAgent, RecoveryAgent 完整版 |
| 状态机 | 14 状态 Task + 9 状态 Project | Saga / 复杂条件转换 |
| 审批 | V1 审批沿用 + Reviewer 审查 | 多级审批 |
| API | V1 兼容 + V2 8 端点 | WebSocket 实时推送 |
| 实体 | 10 张新表 + SQL 草案 | 数据库中间件 |

---

## 2. 功能验收标准

### 2.1 AC-F01：Supervisor 编排正确性

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F01.1 | Supervisor 能启动一个 project 的自动化构建 | `POST /api/executor/start?project_id=N` → supervisor_run 创建 |
| AC-F01.2 | Supervisor 扫描到 ready 任务后通过 MessageBus 分派 | agent_messages 表有 TASK_DISPATCH 记录 |
| AC-F01.3 | 无依赖任务并行分派给不同 Agent | 同一秒内 3 个 TASK_DISPATCH 消息 |
| AC-F01.4 | 有依赖的任务在前驱完成后才分派 | 分派时间 ≥ 前驱 TASK_RESULT 时间 |
| AC-F01.5 | 全部任务完成后 Supervisor 写回 supervisor_run.completed | status='completed', finished_at 有值 |
| AC-F01.6 | 有阻塞时 Supervisor 返回 blocked | status='blocked', decision='HOLD' |

### 2.2 AC-F02：Worker 行为正确性

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F02.1 | Worker 注册后出现在 agent_workers 表 | `POST /api/v2/workers/register` → 201（旧名 agent_registrations → agent_workers） |
| AC-F02.2 | Worker 收到 TASK_DISPATCH 后发送 TASK_ACK | 30s 内 agent_messages 有 ACK |
| AC-F02.3 | Worker 执行完成后 submit TASK_RESULT | 包含完整的 files_modified, tests, evidence_refs |
| AC-F02.4 | Worker 每 10s 发送 heartbeat（写入 agent_heartbeats 表） | 连续 3 次间隔 ≤ 15s |
| AC-F02.5 | Worker 超时 3 次心跳 → 标记 unresponsive | consecutive_errors≥3, status='unresponsive' |
| AC-F02.6 | Worker 断连后任务回收 → 创建 task_handoff → 重新 QUEUED | task_handoffs 表有记录（旧名 task_dispatch_records → task_assignments） |

### 2.3 AC-F03：CodeGenAgent 执行质量

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F03.1 | CodeGenAgent 按 codex_prompt 生成代码 | diff 符合预期修改 |
| AC-F03.2 | 生成代码不超过 files_to_modify 范围 | Safety 检查通过 |
| AC-F03.3 | 模型调用失败后重试 1 次 | model_calls ≤ 2 |
| AC-F03.4 | 生成代码后写入正确文件路径 | 文件存在且可读 |

### 2.4 AC-F04：TestRunnerAgent 执行质量

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F04.1 | TestRunnerAgent 执行 test_steps 中的命令 | 命令输出写入 stdout |
| AC-F04.2 | 测试通过返回 {"passed":N, "failed":0} | TASK_RESULT.status='completed' |
| AC-F04.3 | 测试失败返回失败详情 | TASK_RESULT 含 error |
| AC-F04.4 | 命令执行超时（120s）返回 timeout | status='timeout' |

### 2.5 AC-F05：GitAgent 行为

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F05.1 | GitAgent 能创建 worktree 分支 | 分支名格式 task/{task_id}-{slug} |
| AC-F05.2 | GitAgent 能 commit 变更 | commit message 含 task_id |
| AC-F05.3 | GitAgent commit 前验证 workspace clean | 未跟踪文件不提交 |

### 2.6 AC-F06：SafetyInspectorAgent

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-F06.1 | 检查修改文件不超出 files_to_modify 范围 | 超出范围 → block |
| AC-F06.2 | 检查不包含系统路径 | 系统路径 → block |
| AC-F06.3 | 检查通过返回 pass | TASK_RESULT 可继续 |

---

## 3. 状态机验收标准

### 3.1 AC-S01：合法转换

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-S01.1 | 所有合法转换执行成功 | task_events 记录完整 |
| AC-S01.2 | project: draft→analyzing→planning→ready_to_build→building→completed | 完整流程通过 |
| AC-S01.3 | task: draft→needs_planning→ready→dispatched→executing→completed | 完整流程通过 |
| AC-S01.4 | task: executing→failed→repairing→completed | 修复流程通过 |
| AC-S01.5 | task: executing→failed→repairing→failed→escalated | 修复耗尽升级 |

### 3.2 AC-S02：非法转换拦截

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-S02.1 | completed→executing 被拒绝 | 返回 INVALID_STATE_TRANSITION |
| AC-S02.2 | cancelled→any 被拒绝 | 返回 INVALID_STATE_TRANSITION |
| AC-S02.3 | 并发冲突返回版本错误 | STATE_VERSION_CONFLICT |
| AC-S02.4 | 每次拒绝写入 task_events（to_state 为空） | events 记录尝试 |

---

## 4. API 兼容性验收标准

### 4.1 AC-A01：V1 端点兼容

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-A01.1 | `POST /api/executor/start` 返回 200 | HTTP 状态码 |
| AC-A01.2 | `GET /api/executor/status` 返回正确字段 | 含 running, status, current_step |
| AC-A01.3 | `POST /api/executor/pause/resume/stop` 正常 | HTTP 200 |
| AC-A01.4 | `GET /api/executor/queue` 返回队列 | 含 runnable_tasks |
| AC-A01.5 | `GET /api/executor/start-decision` 返回决策 | 决策类型正确 |
| AC-A01.6 | `POST /api/planner/*` 系列正常工作 | HTTP 200 |

### 4.2 AC-A02：V2 端点

| ID | 验收标准 |
|----|---------|
| AC-A02.1 | `GET /api/v2/supervisor/runs` 返回运行列表 |
| AC-A02.2 | `GET /api/v2/supervisor/status/{project_id}` 返回完整状态 |
| AC-A02.3 | `GET /api/v2/agents` 返回 Agent 列表 |
| AC-A02.4 | `GET /api/v2/agents/{id}/runs` 返回执行历史 |
| AC-A02.5 | `GET /api/v2/transitions` 返回状态变更日志 |
| AC-A02.6 | `GET /api/v2/messages` 返回消息历史 |
| AC-A02.7 | `GET /api/version` 返回 v2 版本信息 |

---

## 5. 数据完整性验收标准

### 5.1 AC-D01：Migration

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-D01.1 | 10 张新表 + 2 张保留表创建成功 | SQL .schema 验证 |
| AC-D01.2 | V1 表新增列存在且可为 NULL | SHOW COLUMNS |
| AC-D01.3 | V1 数据迁移后状态映射正确 | 抽查 10% 记录 |
| AC-D01.4 | V1 代码仍能读写旧字段 | 简单 CRUD 测试 |

### 5.2 AC-D02：数据一致性

| ID | 验收标准 |
|----|---------|
| AC-D02.1 | supervisor_run.task_completed = 实际 completed 任务数 |
| AC-D02.2 | agent_workers.tasks_completed = COUNT(task_results WHERE result_status IN ('verified','submitted')) |
| AC-D02.3 | task_assignments 与 task_results 一一对应 |
| AC-D02.4 | task_events 无孤立记录（task_id 指向存在的记录） |
| AC-D02.5 | agent_messages 无未投递的 TASK_DISPATCH（status≠pending 或已有 delivery_attempts） |

---

## 6. 文档完整性验收标准（V2.0-A-R 新增）

### 6.1 AC-DOC：设计文档补全验证

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-DOC01 | 10 个实体全部有 SQL 草案 | 检查 V2_DATA_MODEL.md 中 10 张表均有 CREATE TABLE |
| AC-DOC02 | 8 个端点全部有完整契约 | 检查 V2_API_CONTRACT.md 中 8 个端点有请求/响应/错误码 |
| AC-DOC03 | Task Packet 字段完整（14 字段） | 检查 V2_AGENT_PROTOCOL.md 中 Task Packet 含 current_stage, completed_steps, remaining_steps, forbidden_actions, allowed_files, allowed_task_ids, git_head, last_error, test_commands, success_criteria, evidence_required, handoff_context, budget_state, idempotency_key |
| AC-DOC04 | Result Packet 字段完整（12 字段） | 检查 V2_AGENT_PROTOCOL.md 中 Result Packet 含 execution_id, result_status, files_modified, tests, git_commit, manual_actions, errors, evidence_refs, handoff_requested, remaining_steps, worker_id, submitted_at |
| AC-DOC05 | Worker 无法直接 VERIFIED | 检查 V2_STATE_MACHINE.md：Worker 只能提交 RESULT_SUBMITTED |
| AC-DOC06 | claim 幂等 | 检查 V2_API_CONTRACT.md：POST /api/v2/tasks/{id}/claim 支持 Idempotency-Key |
| AC-DOC07 | submit 幂等 | 检查 V2_API_CONTRACT.md：POST /api/v2/tasks/{id}/submit 支持 Idempotency-Key |
| AC-DOC08 | heartbeat 不能续租他人 lease | 检查 V2_API_CONTRACT.md：heartbeat 端点返回 LEASE_CONFLICT 错误 |
| AC-DOC09 | handoff 保留完整上下文 | 检查 V2_DATA_MODEL.md：task_handoffs 表含 completed_steps_json, remaining_steps_json, file_snapshot_json, environment_json |
| AC-DOC10 | review 缺少证据时拒绝 VERIFIED | 检查 V2_API_CONTRACT.md：review 端点返回 EVIDENCE_INCOMPLETE |
| AC-DOC11 | task_events 为 append-only | 检查 V2_DATA_MODEL.md：task_events 表注明 append-only 策略 |
| AC-DOC12 | V1.8 行为不受影响 | 检查 V2_API_CONTRACT.md：V1 端点保留清单 + 兼容性承诺 |

---

## 7. 性能验收标准

### 6.1 AC-P01：性能指标

| ID | 指标 | 阈值 | 测量方法 |
|----|------|------|---------|
| AC-P01.1 | 任务分派延迟 | < 5s | TASK_DISPATCH 到 TASK_ACK 时间差 |
| AC-P01.2 | 消息投递延迟 | < 1s | MessageBus.send 到数据库写入时间 |
| AC-P01.3 | 状态转换延迟 | < 100ms | StateMachine.transition 执行时间 |
| AC-P01.4 | 心跳检查周期 | 10s ± 2s | 定时器精度 |

### 7.2 AC-P02：串行效率（V2.0-B 不做并行效率）

| ID | 指标 | 阈值 |
|----|------|------|
| AC-P02.1 | 单 Worker 串行 vs V1 串行执行时间 | 无明显退化 (≤ 1.2x) |
| AC-P02.2 | Worker 空闲率（等待 review/submit overhead） | < 10% |
| AC-P02.3 | 任务 claim 到 submit 端到端延迟 | 同复杂度任务与 V1 持平 |

> **注**：V2.0-B 不做多 Worker 并行，并行效率指标属于 V2.1+。

---

## 8. 可靠性验收标准

### 8.1 AC-R01：容错

| ID | 验收标准 | 验证方法 |
|----|---------|---------|
| AC-R01.1 | Agent 崩溃后任务自动回收 | 强制 kill Agent 进程 |
| AC-R01.2 | Supervisor 重启后恢复中的 run | 重启服务，检查状态 |
| AC-R01.3 | 数据库文件锁冲突自动重试 | 模拟并发写入 |
| AC-R01.4 | 消息重复投递不导致重复执行 | message_id 去重 |

### 7.2 AC-R02：可观测性

| ID | 验收标准 |
|----|---------|
| AC-R02.1 | task_events 记录所有任务事件与状态变更 |
| AC-R02.2 | agent_messages 记录所有消息 |
| AC-R02.3 | task_results 记录每次任务执行结果 |
| AC-R02.4 | supervisor_runs 记录每次运行统计 |
| AC-R02.5 | task_assignments 记录分派时间线 |

---

## 8. 切换与回滚验收

### 8.1 AC-SW01：配置切换

| ID | 验收标准 |
|----|---------|
| AC-SW01.1 | `V2_ENABLED=False` → 所有行为同 V1 |
| AC-SW01.2 | `V2_ENABLED=True` → 走 Supervisor 路径 |
| AC-SW01.3 | 运行时切换不影响已完成的 run |
| AC-SW01.4 | 前端无需感知切换 |

### 8.2 AC-SW02：回滚

| ID | 验收标准 |
|----|---------|
| AC-SW02.1 | 回滚后 V1 端点全部正常 |
| AC-SW02.2 | 回滚后新表可安全删除 |
| AC-SW02.3 | 回滚时间 < 5 分钟 |

---

## 9. 安全验收标准

### 9.1 AC-SEC01：安全

| ID | 验收标准 |
|----|---------|
| AC-SEC01.1 | Agent 不直接操作数据库（通过 API 层） |
| AC-SEC01.2 | 消息体中不含 API Key |
| AC-SEC01.3 | SafetyInspectorAgent 阻止系统路径写入 |
| AC-SEC01.4 | supervisor_run 权限检查 |
| AC-SEC01.5 | 审批门禁在 Agent 执行前验证 |

---

## 10. 验收清单汇总

### 10.1 通过条件

- 所有功能标准（AC-F01~F06）通过
- 所有状态机标准（AC-S01~S02）通过
- 所有 API 兼容标准（AC-A01~A02）通过
- 所有数据完整性标准（AC-D01~D02）通过
- 所有文档完整性标准（AC-DOC01~DOC12）通过 【V2.0-A-R 新增】
- 所有性能标准（AC-P01~P02）通过
- 所有可靠性标准（AC-R01~R02）通过
- 所有切换/回滚标准（AC-SW01~SW02）通过
- 所有安全标准（AC-SEC01）通过

**总计：约 62 项验收标准（含 V2.0-A-R 新增 12 项）**

### 10.2 失败处理

- 任何 AC 失败需修复后重新验证
- 阻塞级 Bug（is_blocking=yes）必须修复
- 非阻塞 Bug 可记录为 Known Issues

---

## 11. 附录

### 11.1 测试项目

建议使用以下项目作为验收测试用例：

1. **sanbox-project-1**: 简单 CRUD 项目（5-8 任务）
2. **sanbox-project-2**: 含依赖的复杂项目（10-15 任务）
3. **sanbox-project-3**: 含 Bug 修复的迭代项目

### 11.2 验收流程

```
1. 准备测试环境 → 运行 migration
2. 运行 V1 回归测试 → 全部通过
3. 启用 V2_ENABLED=True
4. 依次执行 3 个测试项目
5. 人工检查关键指标
6. 运行自动化验收脚本
7. 签署验收报告
```

### 12.3 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
- [V2_API_CONTRACT.md](./V2_API_CONTRACT.md)
- [V2_MIGRATION_PLAN.md](./V2_MIGRATION_PLAN.md)
