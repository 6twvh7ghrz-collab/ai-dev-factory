# V2.0-A 神经中枢架构设计

> **状态**: 冻结  
> **版本**: V2.0-A-R2  
> **日期**: 2026-06-19  
> **修订**: MVP 并发语义最终修正（1+1+1，并发=1，Reviewer/Supervisor 不计入执行并发）  
> **分支**: v2/supervisor-mvp  

---

## 1. 概述

### 1.1 设计目标

将 V1.x 的单体 `LoopController → TaskWorker` 串行执行模型，重构为 **Supervisor 编配多 Agent** 的神经中枢架构。

| V1.x 现状 | V2.0-B MVP 目标 | V2.1+ 后续 |
|-----------|-----------------|-----------|
| 单 Worker 串行循环 | Supervisor + 1 Worker + 1 Reviewer | 多 Worker 并行 |
| LoopController 大而全 | Supervisor 编配 + Agent 原子能力 | — |
| 紧耦合 Git/Test/Safety | Agent 按能力分工 | 更多 Agent 类型 |
| 无任务分派协议 | 标准化 Agent Protocol | 流式协议 |
| 隐式状态转换 | 显式 14 状态机驱动 | Saga 事务 |
| 硬编码 DeepSeek | 可插拔模型适配器 | 多模型路由 |

### 1.2 核心原则

1. **Supervisor 不执行任务**：只做编排、决策、状态管理
2. **Agent 原子化**：每个 Agent 只负责一种能力（写代码/跑测试/Git操作/安全检查）
3. **协议驱动**：Agent 间通过统一协议通信，不直接调用
4. **状态机显式化**：所有对象（Project/Task/Run/Agent）有明确状态和转换规则
5. **向后兼容**：V1 API 契约保持兼容，内部升级对前端透明

---

## 2. 系统分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend (React)                       │
│  Dashboard / Project Detail / Task Board / Executor Console │
├─────────────────────────────────────────────────────────────┤
│                    API Gateway (FastAPI)                   │
│  REST Endpoints → Validated → Routed to Supervisor/Planner │
├─────────────────────────────────────────────────────────────┤
│                     SUPERVISOR LAYER                        │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Orchestrator │  │ StateMachine │  │ DecisionEngine   │  │
│  │ (主循环)      │  │ (状态管理器)  │  │ (决策引擎)        │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │            │
│         └──────────┬──────┴────────────────────┘            │
│                    │  Message Bus                           │
│                    ▼                                         │
├─────────────────────────────────────────────────────────────┤
│                     AGENT LAYER                             │
│  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌───────────────┐  │
│  │CodeGen   │ │TestRunner│ │GitAgent │ │SafetyInspector│  │
│  │(代码生成) │ │(测试执行) │ │(版本控制)│ │(安全检查)      │  │
│  └──────────┘ └──────────┘ └─────────┘ └───────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌───────────────────────────┐  │
│  │CmdAgent  │ │MergeAgent│ │RecoveryAgent(自修复)       │  │
│  │(命令执行) │ │(代码合并) │ │                           │  │
│  └──────────┘ └──────────┘ └───────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                    DATA LAYER                               │
│  SQLite DB  │  File System  │  Git Repositories            │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 职责边界

| 层 | 职责 | 禁止 |
|----|------|------|
| **Supervisor** | 编排工作流、状态机驱动、资源仲裁、审批控制 | 不写代码、不运行命令、不操作 Git |
| **Agent** | 执行原子能力：生成代码、运行测试、提交变更 | 不做跨 Agent 编排决策、不修改状态机 |
| **Message Bus** | 传递消息、保证投递、记录审计日志 | 不修改消息体、不做业务判断 |
| **API Gateway** | 请求验证、路由、权限检查 | 不包含业务逻辑 |

---

## 3. Supervisor 核心组件

### 3.1 Orchestrator（编排器）

继承 V1 `LoopController` 的循环概念，但将内部逻辑拆分为独立组件：

```python
# V2 概念代码
class SupervisorOrchestrator:
    """神经中枢主循环"""
    state_machine: ProjectStateMachine
    decision_engine: DecisionEngine
    message_bus: MessageBus
    agent_registry: AgentRegistry

    async def run_loop(self, project_id: int):
        while not self.should_terminate():
            state = self.state_machine.get(project_id)
            decision = self.decision_engine.evaluate(state)
            if decision.type == "DISPATCH_TASKS":
                await self._dispatch_to_agents(decision.tasks)
            elif decision.type == "WAIT":
                await self._poll_agents(decision.agent_ids)
            elif decision.type == "COMPLETE":
                self.state_machine.transition(project_id, "completed")
                break
```

### 3.2 StateMachine（状态机）

详见 [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)

核心职责：
- 管理 Project / Task / AgentRun 三层次状态
- 提供原子状态转换接口
- 记录状态变更审计日志
- 防止无效转换（乐观锁/版本号）

### 3.3 DecisionEngine（决策引擎）

继承 V1 `StartDecisionService` 的决策能力，扩展为运行时的持续决策：

| V1 Decision | V2 Decision | 说明 |
|-------------|-------------|------|
| EXECUTE_READY_TASKS | DISPATCH_TASKS | 分派到 Agent 池 |
| PLAN_EXISTING_TASKS | REQUEST_PLANNING | 调用 PlannerAgent |
| WAIT_DEPENDENCIES | HOLD | 等待依赖满足 |
| REQUEST_APPROVAL | GATE_APPROVAL | 审批门禁 |
| 新增 | AUTO_REPAIR | 自修复 |
| 新增 | ESCALATE | 升级人工 |

### 3.4 MessageBus（消息总线）

Agent 间通信的唯一通道：
- 基于内存队列 + 数据库持久化
- 支持 point-to-point（点对点）和 broadcast（广播）
- 消息格式遵循 [Agent Protocol](./V2_AGENT_PROTOCOL.md)

---

## 4. Agent 定义

### 4.1 Agent 能力矩阵

| Agent | 能力 | 输入 | 输出 | 幂等 | 超时 |
|-------|------|------|------|------|------|
| **CodeGenAgent** | 生成/修改代码 | TaskPrompt + 上下文 | 代码变更 + diff | 是 | 300s |
| **TestRunnerAgent** | 运行测试套件 | 测试命令 + 文件列表 | TestResult (pass/fail/output) | 否 | 120s |
| **GitAgent** | Git 操作 | (clone/commit/branch/merge) 命令 | GitResult | 否 | 60s |
| **SafetyInspectorAgent** | 安全检查 | 变更文件列表 + diff | SafetyReport (pass/block) | 是 | 30s |
| **CmdAgent** | 执行系统命令 | Command + cwd + env | CommandResult | 否 | 300s |
| **MergeAgent** | 代码合并 | 分支名 + 合并策略 | MergeResult | 否 | 120s |
| **RecoveryAgent** | 自修复 | 失败任务 + 错误上下文 | RepairAttempt | 否 | 600s |

### 4.2 Agent 生命周期

```
REGISTERED → IDLE → DISPATCHED → RUNNING → COMPLETED/FAILED/TIMEOUT
                    ↑_______________↓ (retry)
```

### 4.3 Agent 注册与发现

```python
class AgentRegistry:
    """Agent 能力注册中心"""
    agents: Dict[str, AgentCapability]

    def register(self, agent_id: str, capability: AgentCapability):
        """注册 Agent 能力"""
    
    def find_capable(self, task_type: str) -> List[str]:
        """按任务类型查找可用 Agent"""
    
    def heartbeat(self, agent_id: str) -> bool:
        """Agent 心跳上报"""
```

---

## 5. 数据流

### 5.1 任务执行主流程

```
User/API → [Start Decision] → [Supervisor Orchestrator]
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              StateMachine   DecisionEngine   MessageBus
                    │              │              │
                    │         [DISPATCH]          │
                    │              │              │
                    │    ┌─────────┴─────────┐    │
                    │    ▼                   ▼    │
                    │ CodeGenAgent    TestRunnerAgent
                    │    │                   │    │
                    │    └────┬──────────────┘    │
                    │         ▼                   │
                    │    [Collect Results]        │
                    │         │                   │
                    │         ▼                   │
                    │    [State Transition]       │
                    │         │                   │
                    └─────────┴───────────────────┘
                                    │
                              [Next Iteration] / [Done]
```

### 5.2 V2.0-B 执行示意（单 Worker 串行）

```
                         Supervisor
                             │
                             │ DISPATCH
                             ▼
                      ┌──────────────┐
                      │    Worker    │  ← 唯一执行 Worker
                      │ claim → run  │
                      │ → submit     │
                      └──────┬───────┘
                             │ RESULT_SUBMITTED
                             ▼
                      ┌──────────────┐
                      │   Reviewer   │  ← 审查
                      │ PASS/REWORK/ │
                      │ BLOCKED/     │
                      │ NEED_USER    │
                      └──────┬───────┘
                             │ VERIFIED
                             ▼
                      ┌──────────────┐
                      │   Next Task  │
                      │   (依赖满足)  │
                      └──────────────┘
```

> **注**：多 Worker 并行属于 V2.1+，不属于 V2.0-B。

---

## 6. 与 V1 的关键差异

| 维度 | V1.x | V2.0 |
|------|------|------|
| 执行模型 | 单 Worker 串行 | 1 Supervisor + 1 Worker + 1 Reviewer 串行 |
| 循环控制 | LoopController 大单体 | Supervisor Orchestrator 编配 |
| 状态管理 | 隐式状态（字段混杂） | 显式状态机（三层次） |
| Agent 通信 | 直接函数调用 | MessageBus + Protocol |
| 任务分派 | TaskScheduler 直接领取 | 能力匹配 + 负载均衡 |
| 自修复 | TaskWorker 硬编码 2 次 | RecoveryAgent 独立策略 |
| 审批 | 内嵌在 LoopController | 独立 GATE 节点 |
| 可观测性 | 日志分散 | 统一 Trace/Span 体系 |

---

## 7. 技术选型

| 组件 | 选型 | 原因 |
|------|------|------|
| Web 框架 | FastAPI (不变) | 现有基础设施 |
| 数据库 | SQLite (不变) | 单机部署，零配置 |
| 异步 | asyncio + threading | 兼容 V1 同步代码 |
| Agent 通信 | 内存队列 + DB 持久化 | 单进程部署，无需消息队列中间件 |
| 模型适配 | ModelAdapter 接口 | 抽象层，支持切换 |
| Git 操作 | gitpython / subprocess | 现有 GitManager 封装 |

---

## 8. 目录结构规划

```
backend/app/
├── supervisor/                    # [NEW] Supervisor 层
│   ├── __init__.py
│   ├── orchestrator.py            # 编排器主循环
│   ├── state_machine.py           # 状态机
│   ├── decision_engine.py         # 决策引擎
│   ├── message_bus.py             # 消息总线
│   └── agent_registry.py          # Agent 注册中心
├── agents/                        # [NEW] Agent 层
│   ├── __init__.py
│   ├── base.py                    # Agent 基类
│   ├── code_gen_agent.py          # 代码生成 Agent
│   ├── test_runner_agent.py       # 测试运行 Agent
│   ├── git_agent.py               # Git 操作 Agent
│   ├── safety_inspector_agent.py  # 安全检查 Agent
│   ├── cmd_agent.py               # 命令执行 Agent
│   ├── merge_agent.py             # 合并 Agent
│   └── recovery_agent.py          # 自修复 Agent
├── executor/                      # [保留] V1 兼容层
├── planner/                       # [保留] 规划层
├── api/                           # [扩展] API 层
├── models/                        # [扩展] 数据模型
└── core/                          # [保留] 核心配置
```

---

## 9. 架构约束

### 9.1 必须遵守

- Supervisor 与 Agent 只能通过 MessageBus 通信
- 所有状态变更必须通过 StateMachine.transition()
- Agent 不直接访问其他 Agent 的内部状态
- 审批门禁在执行前必须通过

### 9.2 MVP 范围

- **单 Supervisor + 单 Worker + 单 Reviewer**（V2.0-B MVP，并发固定为 1）
- **仅 Agent Protocol v1.0 消息格式**
- **不支持 Agent 热加载**（启动时注册）
- **多 Worker 并行属于 V2.1+**

---

## 10. 附录

### 10.1 术语表

| 术语 | 定义 |
|------|------|
| Supervisor | 神经中枢，负责编配 Agent、管理状态机、做决策 |
| Agent | 执行原子能力的独立单元 |
| MessageBus | Agent 间消息传递的唯一通道 |
| StateMachine | 明确的状态和转换规则集合 |
| DecisionEngine | 基于状态和上下文的决策器 |
| AgentCapability | Agent 的能力描述（类型、输入输出、超时等） |
| TaskDispatch | 将任务分派给匹配能力的 Agent |
| GATE | 审批门禁节点 |

### 10.2 参考

- V1 架构文档（现有代码）
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
