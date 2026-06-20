# V2.0-A-R 状态机设计

> **状态**: 冻结  
> **版本**: V2.0-A-R  
> **日期**: 2026-06-19  
> **修订**: 补全 14 状态任务状态机，每状态含进入/离开条件、超时处理、失败补偿

---

## 1. 概述

V2.0 引入三层次状态机：**Project**（项目）、**Task**（任务，14 状态）和 **AgentRun**（Agent 运行）。核心原则：

- 所有状态转换通过 `StateMachine.transition()` 原子操作
- 乐观锁（state_version 字段）防止并发冲突
- 每次转换写入 `task_events`（append-only）
- 非法转换返回 `INVALID_STATE_TRANSITION`

---

## 2. Task 状态机（14 状态）

### 2.1 完整状态图

```
                                  ┌──────────────┐
                                  │    DRAFT     │  ← Planner 写入
                                  └──────┬───────┘
                                         │ plan_complete
                                         ▼
                                  ┌──────────────┐
                                  │   PLANNED    │  ← Planner 写入
                                  └──────┬───────┘
                                         │ approve
                                         ▼
                                  ┌──────────────┐
                                  │   APPROVED   │  ← 用户审批通过
                                  └──────┬───────┘
                                         │ enqueue
                                         ▼
                                  ┌──────────────┐
                                  │   QUEUED     │  ← Supervisor 写入，等待 claim
                                  └──────┬───────┘
                                         │ claim (Worker)
                                         ▼
                                  ┌──────────────┐
                           ┌──────│   CLAIMED    │──────┐
                           │      └──────┬───────┘      │
                           │             │ start         │ timeout
                           │             ▼               ▼
                           │      ┌──────────────┐ ┌──────────┐
                           │      │   RUNNING    │ │  QUEUED  │ (lease 过期回收)
                           │      └──────┬───────┘ └──────────┘
                           │             │
                           │             │ submit (Worker)
                           │             ▼
                           │      ┌──────────────────┐
                           │      │ RESULT_SUBMITTED │  ← Worker 只能提交到此
                           │      └──────┬───────────┘
                           │             │
                           │    ┌────────┼────────┐
                           │    ▼        ▼        ▼
                           │ ┌──────┐ ┌──────┐ ┌──────────┐
                           │ │VERIFIED│ │REWORK│ │ BLOCKED  │
                           │ └──┬───┘ └──┬───┘ └────┬─────┘
                           │    │        │          │
                           │    │ ◄──────┘          │ unblock
                           │    │ rework_complete   │
                           │    │                   ▼
                           │    │            ┌──────────────┐
                           │    │            │   QUEUED     │
                           │    │ (重新 claim) └──────────────┘
                           │    │
                           │    │ (终态)
                           │    ▼
                           │
                           │      ┌──────────┐
                           ├──────│  FAILED  │  ← 终态（不可修复）
                           │      └──────────┘
                           │
                           │      ┌──────────┐
                           ├──────│ CANCELLED│  ← 终态
                           │      └──────────┘
                           │
                           │      ┌──────────┐
                           └──────│NEED_USER │  ← Reviewer 写入，等待用户决策
                                  └────┬─────┘
                                       │ user_decides
                                       ▼
                                  (QUEUED / BLOCKED / CANCELLED)
```

### 2.2 14 状态详细定义

| # | 状态 | 写入者 | 允许的前置状态 | 进入条件 | 离开条件 | 超时处理 | 失败补偿 | task_events 事件 | 需用户审批 |
|---|------|--------|---------------|---------|---------|---------|---------|-----------------|-----------|
| 1 | **DRAFT** | Planner | (初始) | 任务被 Planner 生成 | 字段补全完成 → PLANNED | 无（Planner 阶段无超时） | 回退到 DRAFT | task:created | 否 |
| 2 | **PLANNED** | Planner | DRAFT | 任务字段补全（codex_prompt, files_to_modify 等） | 审批通过 → APPROVED | 72h 未审批 → 提醒用户 | 重新规划 | task:planned | 否 |
| 3 | **APPROVED** | User | PLANNED | 用户审批通过 | 项目进入 building → QUEUED | 无 | 撤销审批 → PLANNED | task:approved | ✅ 是 |
| 4 | **QUEUED** | Supervisor | APPROVED, CLAIMED(RECLAIM), BLOCKED(UNBLOCK), NEED_USER(RESOLVE) | 依赖全部 VERIFIED | Worker claim → CLAIMED | 24h 未被 claim → 保持 QUEUED | 重新入队 | task:queued | 否 |
| 5 | **CLAIMED** | Worker | QUEUED | Worker 成功 claim（lease 获取） | Worker 开始执行 → RUNNING | 30s 未 start → lease 过期回收 → QUEUED | 任务回收，新 Worker 可 claim | task:claimed | 否 |
| 6 | **RUNNING** | Worker (implicit via heartbeat) | CLAIMED | Worker 上报 heartbeat 并开始执行 | submit → RESULT_SUBMITTED / heartbeat 超时 → QUEUED | 连续 3 次心跳超时 → QUEUED | 任务回收，保留 handoff 上下文 | task:running | 否 |
| 7 | **RESULT_SUBMITTED** | Worker (submit) | CLAIMED, RUNNING | Worker 调用 submit API 成功 | Review 完成 → VERIFIED/REWORK/BLOCKED | 1h 未被 review → 提醒 Reviewer | 不补偿（等待 review） | task:submitted | 否 |
| 8 | **REVIEWING** | Reviewer (implicit) | RESULT_SUBMITTED | Reviewer 开始审查 | 决策完成 → VERIFIED/REWORK/BLOCKED/NEED_USER | 30min 无决策 → 提醒 Reviewer | 重新排队审查 | task:reviewing | 否 |
| 9 | **VERIFIED** | Reviewer | RESULT_SUBMITTED, REVIEWING | Reviewer PASS 决策 + 证据完整 | (终态，依赖者 → QUEUED) | 无 | 可被依赖覆盖 → 返回 REWORK | task:verified | ✅ 是 (PASS) |
| 10 | **REWORK** | Reviewer | RESULT_SUBMITTED, REVIEWING | Reviewer REWORK 决策 + rework_steps | rework_complete → QUEUED | rework_deadline 到达 → FAILED | 增加 rework_attempt 计数 | task:rework | ✅ 是 (REWORK) |
| 11 | **BLOCKED** | Reviewer | RESULT_SUBMITTED, REVIEWING | Reviewer BLOCKED 决策 | unblock_condition 满足 → QUEUED | blocked_until 到达 → 检查条件 | 不补偿（等待条件满足） | task:blocked | ✅ 是 (BLOCKED) |
| 12 | **NEED_USER** | Reviewer | RESULT_SUBMITTED, REVIEWING | Reviewer NEED_USER 决策 | 用户决策 → QUEUED/BLOCKED/CANCELLED | 72h 无响应 → 再次提醒 | 不补偿 | task:need_user | ✅ 是 (NEED_USER) |
| 13 | **FAILED** | Supervisor, Reviewer | RUNNING, REWORK, CLAIMED | 不可恢复错误或 rework 耗尽 | (终态) | 无 | 人工介入或项目级处理 | task:failed | 否 |
| 14 | **CANCELLED** | User | DRAFT, PLANNED, APPROVED, QUEUED, BLOCKED, NEED_USER | 用户取消任务 | (终态，不可恢复) | 无 | 从终态不可恢复 | task:cancelled | ✅ 是 |

### 2.3 强制规则

1. **Worker 只能提交 RESULT_SUBMITTED**，不能直接写 `VERIFIED` 或任何 `completed` 类状态
2. **Reviewer 负责 VERIFIED / REWORK / BLOCKED / NEED_USER**
3. **高风险操作必须由用户审批**（APPROVED 前的所有状态变更）
4. **所有状态转换通过服务层和乐观锁**（state_version 比对）
5. **非法转换返回 `INVALID_STATE_TRANSITION`**

### 2.4 状态转换 Python 定义

```python
VALID_TRANSITIONS = {
    "draft":             ["planned", "cancelled"],
    "planned":           ["approved", "draft", "cancelled"],
    "approved":          ["queued", "planned", "cancelled"],
    "queued":            ["claimed", "cancelled"],
    "claimed":           ["running", "queued", "failed", "cancelled"],
    "running":           ["result_submitted", "queued", "failed"],
    "result_submitted":  ["reviewing", "verified", "rework", "blocked", "need_user"],
    "reviewing":         ["verified", "rework", "blocked", "need_user"],
    "verified":          [],                                                    # 终态
    "rework":            ["queued", "failed"],
    "blocked":           ["queued", "cancelled"],
    "need_user":         ["queued", "blocked", "cancelled"],
    "failed":            [],                                                    # 终态
    "cancelled":         [],                                                    # 终态
}
```

### 2.5 超时与补偿总览

| 状态 | 超时时间 | 超时触发 | 补偿操作 |
|------|---------|---------|---------|
| CLAIMED | 30s | 未转为 RUNNING | lease 过期 → 自动回收 → QUEUED |
| RUNNING | 3×10s | 连续 3 次心跳丢失 | 标记 UNRESPONSIVE → 创建 task_handoff → QUEUED |
| RESULT_SUBMITTED | 1h | 未被 review | 提醒 Reviewer（不自动变更状态） |
| REVIEWING | 30min | 未完成审查 | 提醒 Reviewer |
| NEED_USER | 72h | 用户未响应 | 再次提醒用户 |
| REWORK | rework_deadline | 返工超时 | → FAILED |

---

## 3. Project 状态机

### 3.1 状态定义

```
DRAFT → ANALYZING → PLANNING → APPROVED → BUILDING → COMPLETED
                              ↓           ↓
                          CANCELLED    PAUSED / BLOCKED
```

| # | 状态 | 写入者 | 说明 |
|---|------|--------|------|
| 1 | DRAFT | User | 初始创建 |
| 2 | ANALYZING | Planner | AI 需求分析中 |
| 3 | PLANNING | Planner | 模块/任务生成中 |
| 4 | READY_TO_BUILD | Planner | 规划完成，至少 1 个 APPROVED 任务 |
| 5 | BUILDING | Supervisor | Supervisor 运行中，任务被 CLAIMED/RUNNING |
| 6 | PAUSED | User | 用户暂停 |
| 7 | BLOCKED | Supervisor | 所有任务 BLOCKED 或 FAILED |
| 8 | COMPLETED | Supervisor | 所有任务 VERIFIED |
| 9 | FIXING | Supervisor | Bug 修复中 |
| 10 | CANCELLED | User | 项目取消（终态） |

### 3.2 状态转换表

| 从 | 到 | 触发 |
|----|----|------|
| DRAFT | ANALYZING | 用户发起 AI 分析 |
| ANALYZING | PLANNING | 分析完成 |
| PLANNING | READY_TO_BUILD | 规划完成 + 审批通过 |
| READY_TO_BUILD | BUILDING | 用户启动构建 |
| BUILDING | PAUSED | 用户暂停 |
| BUILDING | BLOCKED | 全部任务阻塞 |
| BUILDING | COMPLETED | 全部任务 VERIFIED |
| BUILDING | FIXING | Bug 检测到 |
| PAUSED | BUILDING | 用户恢复 |
| BLOCKED | BUILDING | 依赖满足 |
| FIXING | BUILDING | 修复完成 |
| ANY | CANCELLED | 用户取消 |

---

## 4. AgentRun 状态机

### 4.1 状态定义

```
REGISTERED → IDLE → CLAIMED → RUNNING → COMPLETED/FAILED/TIMEOUT
                 ↑                            ↓
                 └──────── UNRESPONSIVE ←──────┘ (recovery)
```

### 4.2 Worker 状态

| 状态 | 说明 |
|------|------|
| registered | Worker 已注册，等待 Supervisor 激活 |
| idle | 空闲，可接受新任务 |
| claimed | 已领取任务，等待执行开始 |
| running | 执行中 |
| unresponsive | 心跳超时 3 次 |
| disconnected | Worker 主动断开 |

---

## 5. 状态机实现

### 5.1 原子转换接口

```python
class StateMachine:
    """三层次状态机"""

    def transition(self, obj_type: str, obj_id: int,
                   to_state: str, reason: str = "",
                   operator_type: str = "system",
                   operator_id: str = "",
                   metadata: dict = None,
                   idempotency_key: str = None) -> TransitionResult:
        """
        原子状态转换。

        步骤:
        1. 读取当前 status + state_version
        2. 验证转换合法性 (VALID_TRANSITIONS[from_state] 包含 to_state)
        3. 验证 operator 权限（Worker 不能直接写 VERIFIED 等）
        4. UPDATE SET status=?, state_version=state_version+1
           WHERE id=? AND state_version=?
        5. rowcount=0 → STATE_VERSION_CONFLICT
        6. INSERT INTO task_events (append-only)
        7. 返回 TransitionResult
        """
```

### 5.2 权限校验

| Operator | 可写入的状态 |
|----------|------------|
| Planner | DRAFT, PLANNED |
| User | APPROVED, CANCELLED |
| Supervisor | QUEUED, FAILED, BLOCKED(系统级) |
| Worker | CLAIMED (via claim), RUNNING (via heartbeat), RESULT_SUBMITTED (via submit) |
| Reviewer | VERIFIED, REWORK, BLOCKED, NEED_USER |

### 5.3 Worker 权限强制规则

```python
WORKER_ALLOWED_TARGETS = {"claimed", "running", "result_submitted"}

def validate_worker_transition(from_state: str, to_state: str) -> bool:
    if to_state not in WORKER_ALLOWED_TARGETS:
        raise InvalidStateTransition(
            f"Worker cannot write '{to_state}'. "
            f"Workers may only submit RESULT_SUBMITTED. "
            f"VERIFIED is reserved for Reviewer."
        )
```

### 5.4 事件钩子

| 事件 | 触发时机 | 监听者 |
|------|---------|--------|
| task:queued | 任务入队 | Supervisor → 检查可分配任务 |
| task:claimed | Worker 领取 | Supervisor → 启动超时监察 |
| task:submitted | Worker 提交 | Supervisor → 触发 Reviewer |
| task:verified | 审查通过 | Supervisor → 激活依赖任务 |
| task:failed | 任务失败 | Supervisor → 决定修复/升级 |
| task:handoff | 任务交接 | Supervisor → 重新分派 |
| agent:unresponsive | Worker 无响应 | Supervisor → 回收任务 |

---

## 6. V1 → V2 任务状态映射

| V1 status | V2 status | 说明 |
|-----------|-----------|------|
| pending (readiness_status=ready) | queued | 直接对应 |
| pending (readiness_status=needs_planning) | draft | 待补全 |
| executing | running / claimed | 拆分确认阶段 |
| completed | verified | V1 completed = V2 verified |
| waiting_test | running | 合并到 running |
| test_failed | failed | 对应 |
| paused / cancelled | cancelled | 合并 |
| (无) | result_submitted | V2 新增 |
| (无) | reviewing | V2 新增 |
| (无) | rework | V2 新增 |
| (无) | blocked | V2 新增 |
| (无) | need_user | V2 新增 |

---

## 7. 附录

### 7.1 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
- [V2_API_CONTRACT.md](./V2_API_CONTRACT.md)
