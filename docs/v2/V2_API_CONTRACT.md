# V2.0-A-R API 契约

> **状态**: 冻结  
> **版本**: V2.0-A-R  
> **日期**: 2026-06-19  
> **修订**: 补全 8 个核心端点，每个包含完整请求/响应/错误码

---

## 1. 概述

定义 V2.0 对外 API 契约。V1 所有端点保持兼容，V2 新增 Worker/Task/Review/Handoff/Event 端点。前端无需修改即可继续使用。

### 1.1 兼容性承诺

| 承诺 | 说明 |
|------|------|
| ✅ V1 端点路径不变 | `/api/executor/start` 等仍可用 |
| ✅ V1 端点请求参数不变 | JSON body / query params 兼容 |
| ✅ V1 端点响应格式不变 | `{ok, data, message, error}` |
| ⚠️ 内部行为变化 | start 现在走 Supervisor 编排 |
| ➕ V2 新增 8 个端点 | `/api/v2/workers/*`, `/api/v2/tasks/*`, `/api/v2/events` |

---

## 2. 8 个端点覆盖表

| # | 端点 | 调用方 | 用途 |
|---|------|--------|------|
| 1 | `POST /api/v2/workers/register` | Worker | Worker 注册与能力声明 |
| 2 | `POST /api/v2/tasks/{id}/claim` | Worker | 领取任务（lease） |
| 3 | `POST /api/v2/tasks/{id}/heartbeat` | Worker | 任务心跳上报 |
| 4 | `POST /api/v2/tasks/{id}/submit` | Worker | 提交执行结果 |
| 5 | `POST /api/v2/tasks/{id}/review` | Reviewer | 审查任务结果 |
| 6 | `POST /api/v2/tasks/{id}/handoff` | Worker/Supervisor | 任务交接 |
| 7 | `GET /api/v2/tasks/{id}/context` | Worker | 获取任务上下文 |
| 8 | `GET /api/v2/events` | 任意 | 查询事件日志 |

---

## 3. V1 端点保留（不变）

| 端点 | 说明 |
|------|------|
| `POST /api/executor/start` | 启动 Supervisor |
| `POST /api/executor/pause` | 暂停 |
| `POST /api/executor/resume` | 恢复 |
| `POST /api/executor/stop` | 停止 |
| `GET /api/executor/status` | 状态查询 |
| `GET /api/executor/queue` | 队列状态 |
| `GET /api/executor/start-decision` | 启动决策 |
| `GET /api/executor/executions` | 执行记录 |
| `POST /api/executor/run-one` | 单任务执行 |
| `POST /api/planner/preview` | 规划预览 |
| `POST /api/planner/approve` | 规划审批 |
| `POST /api/executor/approve` | 执行审批 |

---

## 4. V2 新增 8 端点详细契约

### 4.1 `POST /api/v2/workers/register`

**调用方**: Worker  
**状态前置**: Worker 未注册（`agent_workers` 表中无此 worker_id）  
**超时**: 10s  

#### 请求

```json
{
    "worker_id": "worker-code-gen-1",
    "agent_type": "code_gen",
    "display_name": "Code Generator #1",
    "version": "2.0.0",
    "host": "localhost",
    "capabilities": [
        {
            "capability_type": "code_gen",
            "language": "python",
            "framework": "fastapi",
            "max_files": 10,
            "supports_repair": true,
            "timeout_seconds": 300
        },
        {
            "capability_type": "code_gen",
            "language": "javascript",
            "framework": "react",
            "max_files": 15,
            "supports_repair": true,
            "timeout_seconds": 300
        }
    ],
    "idempotency_key": "reg-550e8400-e29b-41d4-a716-446655440000"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| worker_id | ✅ | Worker 唯一标识 |
| agent_type | ✅ | code_gen / test_runner / git / safety / cmd / merge / recovery |
| display_name | ✅ | 可读名称 |
| version | ✅ | Worker 版本 |
| capabilities | ✅ | 能力数组，至少 1 项 |
| capabilities[].capability_type | ✅ | 能力类型 |
| capabilities[].language | ✅ | 编程语言 |
| idempotency_key | ❌ | 客户端幂等键 |

#### 成功响应 (201)

```json
{
    "ok": true,
    "data": {
        "worker_id": "worker-code-gen-1",
        "status": "registered",
        "capabilities_count": 2,
        "registered_at": "2026-06-19T12:00:00Z"
    }
}
```

#### 重复请求（相同 idempotency_key）

```json
{
    "ok": true,
    "data": {
        "worker_id": "worker-code-gen-1",
        "status": "registered",
        "message": "Worker already registered (idempotent)"
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 409 | IDEMPOTENCY_CONFLICT | 相同 worker_id 但不同 capabilities（已注册不同配置） |
| 400 | WORKER_ALREADY_REGISTERED | worker_id 已存在且无幂等键匹配 |
| 422 | INVALID_CAPABILITY | capabilities 格式无效 |

**审计事件**: `worker.registered` → `agent_heartbeats` 首条记录

---

### 4.2 `POST /api/v2/tasks/{id}/claim`

**调用方**: Worker  
**状态前置**: task.status = `queued`（任务已入队但未被领取）  
**超时**: 5s  

#### 请求

```json
{
    "worker_id": "worker-code-gen-1",
    "idempotency_key": "claim-550e8400-e29b-41d4-a716-446655440000"
}
```

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "assignment_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
        "task_id": 42,
        "worker_id": "worker-code-gen-1",
        "lease_token": "lease-550e8400-e29b-41d4-a716-446655440002",
        "lease_expires_at": "2026-06-19T12:30:00Z",
        "status": "claimed"
    }
}
```

#### Lease 冲突

```json
{
    "ok": false,
    "error": "LEASE_CONFLICT",
    "message": "Task 42 already claimed by worker-code-gen-2 with lease lease-xxx, expires at 2026-06-19T12:25:00Z",
    "data": {
        "current_worker_id": "worker-code-gen-2",
        "lease_expires_at": "2026-06-19T12:25:00Z"
    }
}
```

#### 幂等重复请求

```json
{
    "ok": true,
    "data": {
        "assignment_id": "asgn-SAME",
        "task_id": 42,
        "worker_id": "worker-code-gen-1",
        "lease_token": "lease-SAME",
        "status": "claimed",
        "message": "Already claimed (idempotent)"
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 409 | LEASE_CONFLICT | 任务已被其他 Worker 领取且 lease 未过期 |
| 409 | STALE_LEASE | 任务有过期 lease，需等待 Supervisor 回收 |
| 404 | TASK_NOT_CLAIMABLE | 任务不存在或不在 queued 状态 |
| 404 | WORKER_NOT_REGISTERED | worker_id 未注册 |
| 422 | INVALID_STATE_TRANSITION | 任务状态不允许 claim |
| 409 | IDEMPOTENCY_CONFLICT | 相同 idempotency_key 但不同 worker_id |

**审计事件**: `task.claimed` → `task_events` (event_type='claim', from_state='queued', to_state='claimed')

---

### 4.3 `POST /api/v2/tasks/{id}/heartbeat`

**调用方**: Worker  
**状态前置**: task.status ∈ {`claimed`, `running`}  
**超时**: 3s  

#### 请求

```json
{
    "worker_id": "worker-code-gen-1",
    "assignment_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
    "lease_token": "lease-550e8400-e29b-41d4-a716-446655440002",
    "current_stage": "generating_code",
    "progress_pct": 45.0,
    "status": "running",
    "cpu_percent": 32.5,
    "memory_mb": 128.0
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| worker_id | ✅ | 当前 Worker |
| assignment_id | ✅ | 任务分配 ID |
| lease_token | ✅ | 租约令牌 |
| current_stage | ❌ | 执行阶段 |
| progress_pct | ❌ | 0-100 |
| status | ✅ | running / claimed |

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "acknowledged": true,
        "lease_renewed_until": "2026-06-19T12:35:00Z",
        "heartbeat_count": 15
    }
}
```

#### 错误：不能续租他人 lease

```json
{
    "ok": false,
    "error": "LEASE_CONFLICT",
    "message": "Lease token lease-xxx belongs to worker-code-gen-2, not worker-code-gen-1"
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 403 | LEASE_CONFLICT | lease_token 不属于该 worker_id |
| 409 | STALE_LEASE | lease 已过期 |
| 404 | WORKER_NOT_REGISTERED | worker_id 未注册 |
| 422 | INVALID_STATE_TRANSITION | 任务状态不允许 heartbeat |

**审计事件**: `task.heartbeat` → `agent_heartbeats` 表 + `task_events` (event_type='heartbeat')

---

### 4.4 `POST /api/v2/tasks/{id}/submit`

**调用方**: Worker  
**状态前置**: task.status ∈ {`claimed`, `running`}  
**超时**: 30s  

#### 请求

```json
{
    "worker_id": "worker-code-gen-1",
    "assignment_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
    "lease_token": "lease-550e8400-e29b-41d4-a716-446655440002",
    "result_status": "submitted",
    "exit_code": 0,
    "files_modified": ["routes/auth.py", "models/user.py"],
    "files_checked": ["config/database.py", "middleware/auth.py"],
    "diff_summary": "+120 -15 in 2 files",
    "tests": {
        "total": 12,
        "passed": 12,
        "failed": 0,
        "skipped": 0,
        "output": "All tests passed.\n"
    },
    "git_commit": "abc123def456",
    "git_branch": "task/42-auth",
    "base_commit": "xyz789",
    "stdout": "Build successful.",
    "stderr": "",
    "manual_actions": [],
    "errors": [],
    "evidence_refs": ["artifact-diff-001", "artifact-test_report-001"],
    "handoff_requested": false,
    "remaining_steps": [],
    "duration_ms": 45230,
    "model_calls": 1,
    "repair_attempts": 0,
    "idempotency_key": "submit-550e8400-e29b-41d4-a716-446655440003"
}
```

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "result_id": "rslt-550e8400-e29b-41d4-a716-446655440004",
        "task_id": 42,
        "result_status": "submitted",
        "new_task_status": "result_submitted",
        "next_action": "review",
        "submitted_at": "2026-06-19T12:10:00Z"
    }
}
```

#### 幂等重复请求

```json
{
    "ok": true,
    "data": {
        "result_id": "rslt-SAME",
        "result_status": "submitted",
        "message": "Already submitted (idempotent)"
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 409 | LEASE_CONFLICT | lease_token 不属于该 worker_id |
| 409 | STALE_LEASE | lease 已过期 |
| 404 | WORKER_NOT_REGISTERED | worker_id 未注册 |
| 422 | INVALID_STATE_TRANSITION | 任务状态不允许 submit |
| 409 | IDEMPOTENCY_CONFLICT | 相同 idempotency_key 但不同内容 |
| 400 | EVIDENCE_INCOMPLETE | 缺少必要的 evidence_refs |

**审计事件**: `task.submitted` → `task_events` (event_type='submit', from_state='running', to_state='result_submitted')

---

### 4.5 `POST /api/v2/tasks/{id}/review`

**调用方**: Reviewer（自动或人工）  
**状态前置**: task.status = `result_submitted`  
**超时**: 15s  

#### 请求

```json
{
    "reviewer_type": "auto",
    "reviewer_id": "reviewer-auto-1",
    "decision": "PASS",
    "reason": "All tests passed, file scope valid, no safety violations.",
    "evidence": {
        "tests_passed": 12,
        "tests_failed": 0,
        "files_in_scope": true,
        "safety_clear": true,
        "diff_within_bounds": true
    },
    "rework_steps": [],
    "idempotency_key": "review-550e8400-e29b-41d4-a716-446655440005"
}
```

**decision 枚举**: `PASS` | `REWORK` | `BLOCKED` | `NEED_USER`

#### PASS 响应 (200)

```json
{
    "ok": true,
    "data": {
        "review_id": "rvw-550e8400-e29b-41d4-a716-446655440006",
        "task_id": 42,
        "decision": "PASS",
        "new_task_status": "verified",
        "reviewed_at": "2026-06-19T12:11:00Z"
    }
}
```

#### REWORK 响应 (200)

```json
{
    "ok": true,
    "data": {
        "review_id": "rvw-xxx",
        "task_id": 42,
        "decision": "REWORK",
        "new_task_status": "rework",
        "rework_steps": ["Fix test failure in test_auth.py", "Re-run tests"],
        "rework_deadline": "2026-06-19T13:00:00Z",
        "rework_max_attempts": 2
    }
}
```

#### BLOCKED 响应 (200)

```json
{
    "ok": true,
    "data": {
        "review_id": "rvw-xxx",
        "task_id": 42,
        "decision": "BLOCKED",
        "new_task_status": "blocked",
        "blocked_reason": "Dependency task #40 not yet verified",
        "blocked_until": "2026-06-19T14:00:00Z",
        "unblock_condition": "Task #40 verified"
    }
}
```

#### NEED_USER 响应 (200)

```json
{
    "ok": true,
    "data": {
        "review_id": "rvw-xxx",
        "task_id": 42,
        "decision": "NEED_USER",
        "new_task_status": "need_user",
        "user_prompt": "文件 routes/auth.py 修改范围超出预期，请人工确认是否接受此变更。",
        "waiting_for_user": true
    }
}
```

#### 缺少证据拒绝 VERIFIED

```json
{
    "ok": false,
    "error": "EVIDENCE_INCOMPLETE",
    "message": "Cannot PASS: missing test results and safety check evidence"
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 422 | INVALID_STATE_TRANSITION | 任务状态不是 result_submitted |
| 400 | REVIEW_NOT_ALLOWED | 调用方无审查权限 |
| 400 | EVIDENCE_INCOMPLETE | 证据不足，不允许 PASS |
| 409 | IDEMPOTENCY_CONFLICT | 同一任务已被审查 |

**审计事件**: `task.reviewed` → `task_events` (event_type='review', from_state='result_submitted', to_state='verified'/'rework'/'blocked'/'need_user')

---

### 4.6 `POST /api/v2/tasks/{id}/handoff`

**调用方**: Worker / Supervisor  
**状态前置**: task.status ∈ {`claimed`, `running`, `result_submitted`}  
**超时**: 10s  

#### 请求

```json
{
    "from_worker_id": "worker-code-gen-1",
    "handoff_reason": "worker_unresponsive",
    "current_stage": "generating_code",
    "completed_steps": ["1. Setup workspace", "2. Analyze requirements"],
    "remaining_steps": ["3. Generate code", "4. Run tests", "5. Commit"],
    "allowed_files": ["routes/auth.py", "models/user.py"],
    "forbidden_actions": ["rm", "format"],
    "last_error": {
        "step": "generating_code",
        "message": "Worker heartbeat timeout after 3 attempts",
        "timestamp": "2026-06-19T12:15:00Z"
    },
    "file_snapshot": {
        "routes/auth.py": "sha256:abc123...",
        "models/user.py": "sha256:def456..."
    },
    "git_head": "abc123def456",
    "environment": {
        "NODE_ENV": "development",
        "PYTHONPATH": "./src"
    },
    "idempotency_key": "handoff-550e8400-e29b-41d4-a716-446655440007"
}
```

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "handoff_id": "hndf-550e8400-e29b-41d4-a716-446655440008",
        "task_id": 42,
        "new_task_status": "queued",
        "from_worker_id": "worker-code-gen-1",
        "to_worker_id": null,
        "status": "pending",
        "expires_at": "2026-06-19T13:16:00Z",
        "message": "Task returned to queue. Remaining steps preserved."
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 422 | INVALID_STATE_TRANSITION | 任务状态不允许 handoff |
| 400 | HANDOFF_NOT_ALLOWED | 调用方无 handoff 权限 |
| 404 | WORKER_NOT_REGISTERED | from_worker_id 不存在 |

**审计事件**: `task.handoff` → `task_events` (event_type='handoff', from_state='running', to_state='queued')

---

### 4.7 `GET /api/v2/tasks/{id}/context`

**调用方**: Worker  
**状态前置**: task.status ∈ {`queued`, `claimed`, `running`}（需要任务处于可执行状态）  
**超时**: 5s  

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "task": {
            "id": 42,
            "title": "搭建用户认证模块",
            "task_type": "backend",
            "status": "claimed",
            "priority": "high",
            "project_id": 1,
            "dependencies": [10, 11],
            "dependencies_status": {
                "10": "verified",
                "11": "verified"
            }
        },
        "context": {
            "codex_prompt": "实现 JWT 认证中间件，包括：\n1. 创建 routes/auth.py...",
            "files_to_modify": ["routes/auth.py", "models/user.py"],
            "files_to_check": ["config/database.py", "middleware/auth.py"],
            "implementation_steps": [
                "1. Setup workspace",
                "2. Analyze requirements",
                "3. Generate code",
                "4. Run tests",
                "5. Commit"
            ],
            "test_commands": ["pytest tests/test_auth.py -v"],
            "success_criteria": [
                "All tests pass",
                "JWT token valid for 1 hour",
                "Invalid token returns 401"
            ],
            "evidence_required": ["diff", "test_report"],
            "forbidden_actions": ["rm", "format", "shutdown"],
            "allowed_files": ["routes/auth.py", "models/user.py", "config/database.py"],
            "allowed_task_ids": [42],
            "timeout_seconds": 1800,
            "max_repairs": 2,
            "workspace": {
                "path": "C:/workspace/project-1",
                "branch": "task/42-auth",
                "base_commit": "xyz789"
            },
            "toolchain": {
                "python": "3.12.0",
                "node": "20.0.0",
                "git": "2.40.0"
            },
            "sandbox": {
                "profile_id": "sandbox-default",
                "memory_limit_mb": 512,
                "cpu_limit_percent": 50
            }
        },
        "history": {
            "handoff_count": 0,
            "previous_workers": [],
            "last_handoff_context": null
        }
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 404 | TASK_NOT_CLAIMABLE | 任务不存在或不在可执行状态 |
| 403 | TASK_SCOPE_VIOLATION | 调用 Worker 不在允许的 Worker 列表中 |

---

### 4.8 `GET /api/v2/events`

**调用方**: 任意（Supervisor / Reviewer / 前端）  
**超时**: 5s  

#### Query Parameters

| 参数 | 必填 | 说明 |
|------|------|------|
| task_id | ❌ | 按任务过滤 |
| project_id | ❌ | 按项目过滤 |
| event_type | ❌ | state_change / claim / heartbeat / submit / review / handoff / error |
| from_state | ❌ | 原状态 |
| to_state | ❌ | 目标状态 |
| operator_type | ❌ | system / supervisor / worker / reviewer / user |
| from_time | ❌ | ISO 8601 起始时间 |
| to_time | ❌ | ISO 8601 结束时间 |
| limit | ❌ | 默认 50，最大 200 |
| offset | ❌ | 分页偏移 |

#### 成功响应 (200)

```json
{
    "ok": true,
    "data": {
        "events": [
            {
                "event_id": "evt-550e8400-e29b-41d4-a716-446655440009",
                "task_id": 42,
                "event_type": "state_change",
                "from_state": "running",
                "to_state": "result_submitted",
                "reason": "Worker submitted result",
                "operator_type": "worker",
                "operator_id": "worker-code-gen-1",
                "detail": {
                    "result_id": "rslt-xxx",
                    "tests_passed": 12,
                    "duration_ms": 45230
                },
                "state_version_before": 3,
                "state_version_after": 4,
                "created_at": "2026-06-19T12:10:00Z"
            },
            {
                "event_id": "evt-550e8400-e29b-41d4-a716-446655440010",
                "task_id": 42,
                "event_type": "review",
                "from_state": "result_submitted",
                "to_state": "verified",
                "reason": "All tests passed, file scope valid",
                "operator_type": "reviewer",
                "operator_id": "reviewer-auto-1",
                "detail": {
                    "review_id": "rvw-xxx",
                    "decision": "PASS"
                },
                "state_version_before": 4,
                "state_version_after": 5,
                "created_at": "2026-06-19T12:11:00Z"
            }
        ],
        "total": 128,
        "limit": 50,
        "offset": 0
    }
}
```

#### 错误响应

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 400 | INVALID_FILTER | 过滤参数无效 |
| 404 | PROJECT_NOT_FOUND | project_id 不存在 |

---

## 5. 统一错误码

| 错误码 | HTTP | 说明 |
|--------|------|------|
| TASK_NOT_CLAIMABLE | 404 | 任务不可领取 |
| LEASE_CONFLICT | 409 | 租约冲突 |
| STALE_LEASE | 409 | 租约过期 |
| WORKER_NOT_REGISTERED | 404 | Worker 未注册 |
| INVALID_STATE_TRANSITION | 422 | 非法状态转换 |
| IDEMPOTENCY_CONFLICT | 409 | 幂等冲突 |
| TASK_SCOPE_VIOLATION | 403 | 任务范围违规 |
| EVIDENCE_INCOMPLETE | 400 | 证据不完整 |
| REVIEW_NOT_ALLOWED | 400 | 不允许审查 |
| HANDOFF_NOT_ALLOWED | 400 | 不允许交接 |
| WORKER_ALREADY_REGISTERED | 400 | Worker 已注册 |
| SUPERVISOR_ALREADY_RUNNING | 409 | 项目已有活跃 Supervisor |
| NO_AVAILABLE_WORKER | 503 | 无可用 Worker |
| DISPATCH_TIMEOUT | 504 | 任务分派超时 |
| MESSAGE_DELIVERY_FAILED | 502 | 消息投递失败 |

### 统一错误响应格式

```json
{
    "ok": false,
    "error": "ERROR_CODE",
    "message": "Human-readable error description",
    "data": null
}
```

---

## 6. Idempotency-Key 规范

| 端点 | Idempotency-Key 支持 | 重复请求行为 |
|------|---------------------|-------------|
| POST /api/v2/workers/register | ✅ 支持 | 返回已注册结果 |
| POST /api/v2/tasks/{id}/claim | ✅ 支持 | 返回已领取结果 |
| POST /api/v2/tasks/{id}/heartbeat | ❌ 无幂等需求 | - |
| POST /api/v2/tasks/{id}/submit | ✅ 支持 | 返回已提交结果 |
| POST /api/v2/tasks/{id}/review | ✅ 支持 | 返回已审查结果 |
| POST /api/v2/tasks/{id}/handoff | ✅ 支持 | 返回已交接结果 |
| GET /api/v2/tasks/{id}/context | ❌ 只读 | - |
| GET /api/v2/events | ❌ 只读 | - |

**幂等键格式**: `{prefix}-{UUIDv4}`
**幂等有效期**: 24 小时（通过 `task_events.idempotency_key` UNIQUE 约束保证）

---

## 7. API 版本策略

| 版本 | 端点前缀 | 状态 |
|------|---------|------|
| V1 | `/api/executor/*` | 保留（兼容） |
| V1 | `/api/planner/*` | 保留（兼容） |
| V2 | `/api/v2/workers/*` | 新增 |
| V2 | `/api/v2/tasks/*` | 新增 |
| V2 | `/api/v2/events` | 新增 |
| V2 | `/api/v2/supervisor/*` | 新增（查询） |

### 版本发现

```
GET /api/version
→ { "api_version": "2.0-A-R", "protocol_version": "1.0", "architecture": "supervisor" }
```

---

## 8. 附录

### 8.1 与 V1 API 的关系

| V1 端点 | V2 等效 |
|---------|--------|
| `POST /api/executor/start` | Supervisor 启动后自动分派，Worker 通过 `POST /api/v2/tasks/{id}/claim` 领取 |
| `GET /api/executor/status` | `GET /api/v2/supervisor/status/{project_id}` + `GET /api/v2/events` |
| `POST /api/executor/run-one` | Supervisor 分派 → Worker claim → submit → review |

### 8.2 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
