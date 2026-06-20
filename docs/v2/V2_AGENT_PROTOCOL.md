# V2.0-A-R Agent 间协议

> **状态**: 冻结  
> **版本**: V2.0-A-R (Protocol v1.0)  
> **日期**: 2026-06-19  
> **修订**: 补全 Task Packet 14 字段和 Result Packet 12 字段完整 JSON 示例

---

## 1. 概述

定义 Supervisor 与 Worker/Reviewer 之间的通信协议。所有消息通过 MessageBus 传递，使用统一的消息格式。

### 1.1 设计原则

- **结构化消息**：JSON 格式
- **幂等性**：每个消息带有 `message_id`，支持去重
- **可追溯**：所有消息写入 `agent_messages` 表
- **超时控制**：每类消息有明确超时
- **同步/异步双模**：支持 request-response 和 fire-and-forget

---

## 2. 通用信封

```json
{
    "protocol_version": "1.0",
    "message_id": "msg-550e8400-e29b-41d4-a716-446655440000",
    "correlation_id": "corr-550e8400-e29b-41d4-a716-446655440001",
    "timestamp": "2026-06-19T12:00:00Z",
    "sender": {
        "type": "supervisor",
        "id": "supervisor-main"
    },
    "recipient": {
        "type": "worker",
        "id": "worker-code-gen-1"
    },
    "message_type": "TASK_DISPATCH",
    "payload": {},
    "metadata": {
        "project_id": 1,
        "task_id": 42,
        "priority": "high",
        "ttl_seconds": 300
    }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| protocol_version | ✅ | "1.0" |
| message_id | ✅ | UUID v4 |
| correlation_id | ✅ | 关联 ID |
| timestamp | ✅ | ISO 8601 |
| sender.type | ✅ | "supervisor" / "worker" / "reviewer" |
| sender.id | ✅ | 发送者唯一 ID |
| recipient.type | ✅ | "supervisor" / "worker" / "reviewer" / "broadcast" |
| recipient.id | ✅ | 接收者 ID，"*" 表示广播 |
| message_type | ✅ | 消息类型枚举 |
| payload | ✅ | 消息体 |
| metadata | ✅ | 元数据 |

---

## 3. Task Packet（完整字段）

> Supervisor → Worker 的 TASK_DISPATCH 消息体

### 3.1 字段清单

| # | 字段 | 必填 | 类型 | 说明 |
|---|------|------|------|------|
| 1 | current_stage | ✅ | string | 任务当前阶段 |
| 2 | completed_steps | ✅ | string[] | 已完成的步骤列表 |
| 3 | remaining_steps | ✅ | string[] | 剩余待执行的步骤 |
| 4 | forbidden_actions | ✅ | string[] | 禁止执行的操作 |
| 5 | allowed_files | ✅ | string[] | 允许修改的文件路径 |
| 6 | allowed_task_ids | ✅ | int[] | 允许关联的任务 ID 范围 |
| 7 | git_head | ✅ | string | 当前 Git HEAD commit |
| 8 | last_error | ❌ | object/null | 上一个错误信息（handoff 时） |
| 9 | test_commands | ✅ | string[] | 测试命令清单 |
| 10 | success_criteria | ✅ | string[] | 成功标准 |
| 11 | evidence_required | ✅ | string[] | 必须提交的证据类型 |
| 12 | handoff_context | ❌ | object/null | 交接上下文（来自 task_handoffs 表） |
| 13 | budget_state | ✅ | object | 预算状态 |
| 14 | idempotency_key | ✅ | string | 幂等键 |

### 3.2 完整 JSON 示例

```json
{
    "message_type": "TASK_DISPATCH",
    "payload": {
        "assignment_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
        "task": {
            "id": 42,
            "title": "搭建用户认证模块",
            "task_type": "backend",
            "priority": "high",
            "codex_prompt": "实现 JWT 认证中间件：\n1. 创建 routes/auth.py 包含 login/logout/refresh 端点\n2. 创建 models/user.py 包含 User 模型\n3. 使用 PyJWT 库，token 有效期 1 小时\n4. 所有认证端点返回统一 JSON 格式"
        },
        "current_stage": "code_generation",
        "completed_steps": [
            "workspace_setup",
            "dependency_analysis"
        ],
        "remaining_steps": [
            "code_generation",
            "test_execution",
            "safety_check",
            "git_commit"
        ],
        "forbidden_actions": [
            "rm",
            "del",
            "format",
            "shutdown",
            "drop_table",
            "truncate"
        ],
        "allowed_files": [
            "routes/auth.py",
            "models/user.py",
            "config/database.py",
            "middleware/auth.py"
        ],
        "allowed_task_ids": [42],
        "git_head": "abc123def456789",
        "last_error": null,
        "test_commands": [
            "pytest tests/test_auth.py -v",
            "pytest tests/test_user_model.py -v"
        ],
        "success_criteria": [
            "All tests pass with 0 failures",
            "JWT token valid for 3600 seconds",
            "Invalid token returns 401 Unauthorized",
            "Expired token returns 401 with 'token_expired' message",
            "Login returns valid access_token and refresh_token"
        ],
        "evidence_required": [
            "diff",
            "test_report",
            "safety_report"
        ],
        "handoff_context": null,
        "budget_state": {
            "limit_seconds": 1800,
            "used_seconds": 0,
            "remaining_seconds": 1800,
            "exceeded": false
        },
        "idempotency_key": "dispatch-550e8400-e29b-41d4-a716-446655440002",
        "workspace": {
            "path": "C:/workspace/project-1",
            "branch": "task/42-auth",
            "base_commit": "xyz789"
        },
        "toolchain": {
            "python": "3.12.0",
            "pip_packages": ["pyjwt", "fastapi", "pytest"],
            "node": "20.0.0"
        },
        "sandbox": {
            "profile_id": "sandbox-default",
            "memory_limit_mb": 512,
            "cpu_limit_percent": 50,
            "max_workspace_size_mb": 1024
        },
        "timeout_seconds": 1800,
        "max_repairs": 2
    },
    "metadata": {
        "project_id": 1,
        "task_id": 42,
        "priority": "high",
        "ttl_seconds": 300
    }
}
```

### 3.3 带 handoff_context 的 Task Packet 示例

```json
{
    "task": { "...": "..." },
    "current_stage": "test_execution",
    "completed_steps": [
        "workspace_setup",
        "dependency_analysis",
        "code_generation"
    ],
    "remaining_steps": [
        "test_execution",
        "safety_check",
        "git_commit"
    ],
    "handoff_context": {
        "handoff_id": "hndf-550e8400-e29b-41d4-a716-446655440008",
        "from_worker_id": "worker-code-gen-1",
        "handoff_reason": "worker_unresponsive",
        "file_snapshot": {
            "routes/auth.py": "sha256:abc123def456...",
            "models/user.py": "sha256:789xyz..."
        },
        "last_error": {
            "step": "test_execution",
            "message": "Worker heartbeat timeout after 3 attempts",
            "timestamp": "2026-06-19T12:15:00Z"
        },
        "environment": {
            "NODE_ENV": "development",
            "PYTHONPATH": "./src"
        }
    },
    "budget_state": {
        "limit_seconds": 1800,
        "used_seconds": 450,
        "remaining_seconds": 1350,
        "exceeded": false
    }
}
```

---

## 4. Result Packet（完整字段）

> Worker → Supervisor 的 TASK_RESULT 消息体

### 4.1 字段清单

| # | 字段 | 必填 | 类型 | 说明 |
|---|------|------|------|------|
| 1 | execution_id | ✅ | string | 执行唯一 ID（= assignment_id） |
| 2 | result_status | ✅ | string | "submitted" |
| 3 | files_modified | ✅ | string[] | 修改的文件列表 |
| 4 | tests | ✅ | object | 测试结果 {total, passed, failed, skipped, output} |
| 5 | git_commit | ✅ | string | Git commit SHA |
| 6 | manual_actions | ✅ | object[] | 人工操作列表 |
| 7 | errors | ✅ | object[] | 错误列表 |
| 8 | evidence_refs | ✅ | string[] | 产物引用 ID 列表 |
| 9 | handoff_requested | ✅ | boolean | 是否请求交接 |
| 10 | remaining_steps | ✅ | string[] | 未完成步骤（handoff_requested=true 时） |
| 11 | worker_id | ✅ | string | 提交的 Worker ID |
| 12 | submitted_at | ✅ | string | ISO 8601 提交时间 |

### 4.2 完整 JSON 示例（成功）

```json
{
    "message_type": "TASK_RESULT",
    "payload": {
        "execution_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
        "assignment_id": "asgn-550e8400-e29b-41d4-a716-446655440001",
        "task_id": 42,
        "result_status": "submitted",
        "exit_code": 0,
        "files_modified": [
            "routes/auth.py",
            "models/user.py"
        ],
        "files_checked": [
            "config/database.py",
            "middleware/auth.py"
        ],
        "diff_summary": "+120 -15 in 2 files",
        "tests": {
            "total": 12,
            "passed": 12,
            "failed": 0,
            "skipped": 0,
            "output": "============================= test session starts =============================\ntests/test_auth.py::test_login_success PASSED\ntests/test_auth.py::test_login_invalid_credentials PASSED\ntests/test_auth.py::test_token_expiry PASSED\ntests/test_auth.py::test_refresh_token PASSED\ntests/test_auth.py::test_logout PASSED\ntests/test_user_model.py::test_user_creation PASSED\ntests/test_user_model.py::test_password_hashing PASSED\ntests/test_user_model.py::test_user_serialization PASSED\n============================= 12 passed in 2.34s ============================="
        },
        "git_commit": "abc123def456",
        "git_branch": "task/42-auth",
        "base_commit": "xyz789",
        "stdout": "Build successful. All tests passed.",
        "stderr": "",
        "manual_actions": [],
        "errors": [],
        "evidence_refs": [
            "artifact-diff-auth-routes-001",
            "artifact-test_report-auth-001",
            "artifact-safety_report-auth-001"
        ],
        "handoff_requested": false,
        "remaining_steps": [],
        "worker_id": "worker-code-gen-1",
        "submitted_at": "2026-06-19T12:10:00Z",
        "duration_ms": 45230,
        "model_calls": 1,
        "repair_attempts": 0,
        "idempotency_key": "submit-550e8400-e29b-41d4-a716-446655440003"
    },
    "metadata": {
        "project_id": 1,
        "task_id": 42,
        "priority": "high"
    }
}
```

### 4.3 Result Packet 示例（失败）

```json
{
    "message_type": "TASK_RESULT",
    "payload": {
        "execution_id": "asgn-550e8400-e29b-41d4-a716-446655440010",
        "task_id": 43,
        "result_status": "submitted",
        "exit_code": 1,
        "files_modified": ["routes/payment.py"],
        "files_checked": [],
        "diff_summary": "+45 -3 in 1 file",
        "tests": {
            "total": 8,
            "passed": 5,
            "failed": 3,
            "skipped": 0,
            "output": "FAILED tests/test_payment.py::test_refund - AssertionError: expected 200 got 500"
        },
        "git_commit": "",
        "stdout": "",
        "stderr": "3 tests failed",
        "manual_actions": [],
        "errors": [
            {
                "step": "test_execution",
                "message": "3 of 8 tests failed",
                "detail": "test_refund: AssertionError\ntest_partial_refund: AssertionError\ntest_double_refund: TimeoutError"
            }
        ],
        "evidence_refs": [
            "artifact-test_report-payment-001"
        ],
        "handoff_requested": false,
        "remaining_steps": [],
        "worker_id": "worker-code-gen-1",
        "submitted_at": "2026-06-19T12:11:00Z",
        "duration_ms": 32100,
        "model_calls": 1,
        "repair_attempts": 0,
        "idempotency_key": "submit-550e8400-e29b-41d4-a716-446655440011"
    }
}
```

### 4.4 Result Packet 示例（请求交接）

```json
{
    "message_type": "TASK_RESULT",
    "payload": {
        "execution_id": "asgn-550e8400-e29b-41d4-a716-446655440020",
        "task_id": 44,
        "result_status": "submitted",
        "exit_code": 1,
        "files_modified": [],
        "tests": { "total": 0, "passed": 0, "failed": 0, "skipped": 0, "output": "" },
        "git_commit": "",
        "manual_actions": [],
        "errors": [
            {
                "step": "code_generation",
                "message": "Unsupported framework: tensorflow not in allowed toolchain"
            }
        ],
        "evidence_refs": [],
        "handoff_requested": true,
        "remaining_steps": [
            "code_generation",
            "test_execution",
            "safety_check",
            "git_commit"
        ],
        "worker_id": "worker-code-gen-1",
        "submitted_at": "2026-06-19T12:12:00Z",
        "duration_ms": 1500,
        "idempotency_key": "submit-550e8400-e29b-41d4-a716-446655440021"
    }
}
```

---

## 5. 消息类型汇总

### 5.1 Supervisor → Worker

| 类型 | 说明 | 投递保证 |
|------|------|---------|
| TASK_DISPATCH | 分派任务（含 Task Packet） | At-Least-Once, 3 次重试 |
| TASK_CANCEL | 取消任务 | Best-Effort |
| HEARTBEAT_REQUEST | 心跳请求 | Best-Effort |

### 5.2 Worker → Supervisor

| 类型 | 说明 | 投递保证 |
|------|------|---------|
| TASK_ACK | 任务确认 | Best-Effort, 1 次重试 |
| TASK_RESULT | 任务结果（含 Result Packet） | Exactly-Once (message_id 去重) |
| TASK_PROGRESS | 进度报告 | Best-Effort |
| HEARTBEAT_RESPONSE | 心跳响应 | Best-Effort |

### 5.3 Reviewer → Supervisor / Worker

| 类型 | 说明 | 投递保证 |
|------|------|---------|
| REVIEW_DECISION | 审查决策 (PASS/REWORK/BLOCKED/NEED_USER) | Exactly-Once |

### 5.4 错误消息

```json
{
    "message_type": "ERROR_RESPONSE",
    "payload": {
        "original_message_id": "msg-xxx",
        "error_code": "INVALID_MESSAGE_FORMAT",
        "error_detail": "Missing required field: task_id"
    }
}
```

---

## 6. 消息传递语义

### 6.1 投递保证

| 消息类型 | 投递保证 | 重试策略 |
|---------|---------|---------|
| TASK_DISPATCH | At-Least-Once | 3 次，间隔 5s/15s/30s |
| TASK_RESULT | Exactly-Once | message_id 去重 |
| TASK_ACK | Best-Effort | 1 次重试 |
| HEARTBEAT_REQUEST/RESPONSE | Best-Effort | 不重试，等下个周期 |
| TASK_PROGRESS | Best-Effort | 不重试 |
| REVIEW_DECISION | Exactly-Once | review_id 去重 |

### 6.2 超时设置

| 操作 | 超时 | 超时处理 |
|------|------|---------|
| TASK_DISPATCH → TASK_ACK | 30s | 重新分派给其他 Worker |
| TASK_DISPATCH → TASK_RESULT | task.timeout_seconds | 标记 TIMEOUT，触发 handoff |
| HEARTBEAT_REQUEST → HEARTBEAT_RESPONSE | 10s | 连续 3 次标记 UNRESPONSIVE |

---

## 7. Agent 注册与发现

### 7.1 注册流程

```
Worker.start()
  → POST /api/v2/workers/register (含 capabilities)
  → Supervisor 写入 agent_workers + agent_capabilities
  → Worker 开始发送 heartbeat (每 10s)
```

### 7.2 发现流程

```
Supervisor 需要分派 code_gen 任务：
  → 查询 agent_capabilities WHERE capability_type='code_gen'
  → JOIN agent_workers WHERE status='idle' AND current_load < max_concurrent_tasks
  → 选择负载最低的 Worker
  → 发送 TASK_DISPATCH
```

---

## 8. 安全性

- `message_id` 重复检测：数据库 UNIQUE 约束
- `sender.id` 白名单：只有已注册的 Worker 可以发送消息
- `payload` 大小限制：最大 1MB
- API Key 不出现在任何消息体中
- 文件内容通过路径引用，不传完整内容

---

## 9. 附录

### 9.1 消息流程图

```
Supervisor              MessageBus               Worker              Reviewer
    │                        │                      │                    │
    │── TASK_DISPATCH ──────→│─────────────────────→│                    │
    │                        │←── TASK_ACK ────────│                    │
    │←── TASK_ACK ──────────│                      │                    │
    │                        │←── TASK_PROGRESS ───│                    │
    │                        │←── TASK_RESULT ─────│                    │
    │←── TASK_RESULT ───────│                      │                    │
    │                        │                      │                    │
    │── 状态机更新 ──────────│                      │                    │
    │── REVIEW_REQUEST ─────→│─────────────────────────────────────────→│
    │                        │←── REVIEW_DECISION ─────────────────────│
    │←── REVIEW_DECISION ───│                      │                    │
```

### 9.2 协议版本

| 版本 | 说明 |
|------|------|
| V1.0 (当前) | MVP 协议，8 种消息类型 |
| V1.1 (计划) | 增加 PRIORITY_CHANGE、WORKER_RECONFIGURE |
| V2.0 (计划) | 流式结果推送，Worker 间 P2P |

### 9.3 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_DATA_MODEL.md](./V2_DATA_MODEL.md)
- [V2_API_CONTRACT.md](./V2_API_CONTRACT.md)
