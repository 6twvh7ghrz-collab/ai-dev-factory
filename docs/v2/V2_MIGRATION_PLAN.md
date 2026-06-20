# V2.0-A-R2 迁移计划

> **状态**: 冻结  
> **版本**: V2.0-A-R2  
> **日期**: 2026-06-19  
> **修订**: 统一使用最终 10 实体名称、移除旧表名残留、移除"13天"固定承诺  

---

## 1. 概述

从 V1.x 单体 Executor 架构迁移到 V2.0 Supervisor + Multi-Agent 架构的完整计划。

### 1.1 迁移原则

1. **渐进式升级**：V1 和 V2 可共存，通过配置开关控制
2. **零停机迁移**：升级过程中已有 run 不受影响
3. **数据向后兼容**：V1 表结构不变，V2 新增列有默认值
4. **可回滚**：任何阶段可一键回退到 V1 行为
5. **前端无感**：API 契约兼容，前端无需改动

### 1.2 迁移开关

```python
# backend/app/core/config.py
class Settings:
    V2_ENABLED: bool = False          # 主开关
    V2_SUPERVISOR_MODE: bool = False  # Supervisor 模式
    V2_AGENT_CONCURRENCY: int = 1     # Agent 执行并发数（V2.0-B 固定为1）
    V2_MESSAGE_BUS: str = "memory"    # memory / sqlite
```

---

## 2. 迁移阶段

### Phase 0：基础准备（1天）

**目标**：创建数据库迁移脚本，不修改任何运行时代码。

| 任务 | 描述 | 回滚 |
|------|------|------|
| P0-1 | 创建 `006_v2_supervisor_tables.py` 迁移脚本 | 删除新表 |
| P0-2 | 创建 `007_v2_extend_projects.py` 迁移脚本 | 删除新列 |
| P0-3 | 创建 `008_v2_extend_tasks.py` 迁移脚本 | 删除新列 |
| P0-4 | 运行迁移，验证新表和列存在 | DROP TABLE |
| P0-5 | 确认 V1 端点仍正常工作 | — |

**验证标准**：
- 新增 10 张表（agent_workers, agent_capabilities, task_assignments, task_handoffs, task_results, review_decisions, execution_artifacts, agent_heartbeats, task_events, sandbox_profiles）+ 2 张保留表（supervisor_runs, agent_messages）
- projects / development_tasks 新增列存在但可为 NULL
- 所有 V1 API 端点返回状态码 200

> **兼容说明**：旧名 agent_registrations → agent_workers、task_dispatch_records → task_assignments、agent_runs → task_results、transition_logs (task部分) → task_events

---

### Phase 1：核心环路（2天）

**目标**：实现 Supervisor Orchestrator + StateMachine，V1 API 内部走新路径。

| 任务 | 描述 | 风险 |
|------|------|------|
| P1-1 | 实现 `supervisor/orchestrator.py` | 中 |
| P1-2 | 实现 `supervisor/state_machine.py` | 中 |
| P1-3 | 实现 `supervisor/decision_engine.py` | 低 |
| P1-4 | 适配 `POST /api/executor/start` 走 Supervisor | 高 |
| P1-5 | 适配 `GET /api/executor/status` 返回扩展字段 | 低 |
| P1-6 | 实现 `task_events`（append-only 任务事件）写入 | 低 |

**验证标准**：
- `V2_ENABLED=True` 时 start 创建 supervisor_run
- `V2_ENABLED=False` 时 start 走原 LoopController
- 状态变更写入 task_events（旧名 transition_logs → task_events）

---

### Phase 2：MessageBus + Agent基础（2天）

**目标**：实现消息总线和 Agent 基类。

| 任务 | 描述 | 风险 |
|------|------|------|
| P2-1 | 实现 `supervisor/message_bus.py` | 中 |
| P2-2 | 实现 `supervisor/agent_registry.py` | 低 |
| P2-3 | 实现 `agents/base.py`（Agent 基类） | 低 |
| P2-4 | 实现 Agent 注册/心跳机制 | 中 |
| P2-5 | 实现 `POST /api/v2/agents/*` 端点 | 低 |

**验证标准**：
- Agent 可注册并上报心跳
- MessageBus 能投递消息并持久化到 agent_messages 表
- 心跳超时 → Agent 标记 unresponsive

---

### Phase 3：Agent 迁移（3天）

**目标**：将 V1 TaskWorker 内部能力拆分为独立 Agent。

| 任务 | 描述 | 来源 |
|------|------|------|
| P3-1 | 实现 `agents/code_gen_agent.py` | ModelAdapter |
| P3-2 | 实现 `agents/test_runner_agent.py` | TestRunner |
| P3-3 | 实现 `agents/git_agent.py` | GitManager |
| P3-4 | 实现 `agents/safety_inspector_agent.py` | SafetyGuard |
| P3-5 | 实现 `agents/cmd_agent.py` | CommandRunner |
| P3-6 | 实现 `agents/recovery_agent.py` | Repair 逻辑 |

**Agent 与 V1 组件的对应关系**：

```
V1 TaskWorker.execute()
  ├── git.checkpoint()        → GitAgent.prepare_workspace()
  ├── toolchain.validate()    → CmdAgent.validate_toolchain()
  ├── model.generate()        → CodeGenAgent.generate()
  ├── safety.check()          → SafetyInspectorAgent.inspect()
  ├── runner.run_tests()      → TestRunnerAgent.run()
  ├── git.commit()            → GitAgent.commit_changes()
  └── repair loop             → RecoveryAgent.repair()
```

**验证标准**：
- 每个 Agent 可独立接收 TASK_DISPATCH 消息并执行
- Agent 输出 TASK_RESULT 格式正确
- V1 run-one 仍可通过 Agent 路径执行

---

### Phase 4：编排集成（2天）

**目标**：Supervisor 通过 MessageBus 编排多 Agent 并行执行。

| 任务 | 描述 | 风险 |
|------|------|------|
| P4-1 | 实现依赖分析 + 任务分组（无依赖任务可并行） | 高 |
| P4-2 | 实现串行分派逻辑（单 Worker，并发=1） | 高 |
| P4-3 | 实现 Agent 负载均衡 | 低 |
| P4-4 | 实现任务失败回收 + 重新分派 | 中 |
| P4-5 | 实现 RecoveryAgent 自修复编排 | 中 |

**验证标准**：
- Worker 顺序获取任务、执行、提交、由 Reviewer 审查（单 Worker 串行）
- Worker 失败后任务回收并重新分派（同一 Worker 或下一可用 Worker）
- 测试修复流程 max_repairs=2 正确执行
- 依赖任务在前驱 Verified 后自动进入 QUEUED

---

### Phase 5：测试 & 验证

进入标准：Phase 4 所有验证标准通过。

| 任务 | 描述 |
|------|------|
| P5-1 | V1 回归测试（全部 V1 端点 + 执行流程） |
| P5-2 | V2 集成测试（1 Supervisor + 1 Worker + 1 Reviewer 完整流程） |
| P5-3 | Agent 单元测试（每个 Agent 独立） |
| P5-4 | 串行执行测试（单 Worker 顺序执行多任务） |
| P5-5 | 错误恢复测试（Agent 崩溃恢复） |
| P5-6 | 状态机测试（合法/非法转换） |
| P5-7 | 性能对比测试（V1 vs V2 执行时间） |

退出标准：所有 AC 通过 → 进入 Phase 6

---

### Phase 6：上线 & 监控（1天）

| 任务 | 描述 |
|------|------|
| P6-1 | 配置 `V2_ENABLED=True` |
| P6-2 | 部署到沙箱环境 |
| P6-3 | 运行一个完整项目验证 |
| P6-4 | 监控 24h 无异常 |
| P6-5 | 切到正式环境 |

---

## 3. 回滚方案

### 3.1 快速回滚（< 5分钟）

```bash
# 方式 1：配置回滚
# 设置 V2_ENABLED=False，重启服务
# 所有 V1 API 走原 LoopController 路径

# 方式 2：数据库回滚
python -m app.migrations.rollback_v2
# 删除新表 + 恢复列
```

### 3.2 回滚检查清单

| 检查项 | 预期 |
|--------|------|
| `POST /api/executor/start` | 走 LoopController，不创建 supervisor_run |
| `GET /api/executor/status` | 不返回 agents 字段 |
| Agent 线程全部退出 | 无残留 |
| V1 任务执行正常 | 同回滚前 |

---

## 4. 风险与缓解

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| V1 API 内部行为不一致 | 高 | 每次 release 前运行 V1 回归测试 |
| Agent 间消息丢失 | 中 | 消息持久化 + message_id 去重 + 重试 |
| 并行执行导致文件冲突 | 中 | ResourceLockManager + 按文件分组 |
| 状态机版本冲突 | 中 | 乐观锁 + version 字段 |
| 性能回退 | 低 | V1/V2 对比测试，切换开关 |
| Agent 死锁 | 低 | 超时 + heartbeat 检测 |

---

## 5. 时间线

```
Week 1:
  Day 1: Phase 0 (基础准备)
  Day 2-3: Phase 1 (核心环路)
  Day 4-5: Phase 2 (MessageBus + Agent基础)

Week 2:
  Day 1-3: Phase 3 (Agent 迁移)
  Day 4-5: Phase 4 (编排集成)

Week 3:
  Day 1-2: Phase 5 (测试 & 验证)
  Day 3: Phase 6 (上线 & 监控)
  Day 4-5: Buffer / 修复

总计: 约 13 个工作日（各阶段可并行，实际按团队速度调整）
```

---

## 6. 数据迁移脚本示例

### 6.1 V1 → V2 项目状态迁移

```python
# migrations/009_v2_data_migration.py

def migrate_project_status(conn):
    """将 V1 项目状态映射到 V2"""
    rows = conn.execute(
        "SELECT id, status FROM projects WHERE state_version IS NULL"
    ).fetchall()

    for row in rows:
        v2_status = {
            "draft": "draft",
            "analyzing": "analyzing",
            "generated": "planning",
            "developing": "building",
            "testing": "testing",
            "completed": "completed",
            "paused": "paused",
        }.get(row["status"], row["status"])

        conn.execute(
            "UPDATE projects SET status=?, state_version=1 WHERE id=?",
            (v2_status, row["id"])
        )

def migrate_task_status(conn):
    """将 V1 任务状态映射到 V2"""
    rows = conn.execute(
        """SELECT id, status, readiness_status
           FROM development_tasks WHERE state_version IS NULL"""
    ).fetchall()

    for row in rows:
        if row["status"] == "pending":
            if row["readiness_status"] == "ready":
                v2_status = "ready"
            elif row["readiness_status"] == "needs_planning":
                v2_status = "needs_planning"
            else:
                v2_status = "draft"
        else:
            v2_status = {
                "executing": "executing",
                "completed": "completed",
                "waiting_test": "executing",
                "test_failed": "failed",
                "paused": "cancelled",
                "cancelled": "cancelled",
            }.get(row["status"], row["status"])

        conn.execute(
            "UPDATE development_tasks SET status=?, state_version=1 WHERE id=?",
            (v2_status, row["id"])
        )
```

---

## 7. 附录

### 7.1 迁移命令参考

```bash
# 运行迁移
python -m app.migrate up

# 查看迁移状态
python -m app.migrate status

# 回滚到指定版本
python -m app.migrate down 005

# 验证迁移
python -m app.migrate verify
```

### 7.2 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
- [V2_API_CONTRACT.md](./V2_API_CONTRACT.md)
