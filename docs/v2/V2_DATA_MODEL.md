# V2.0-A-R 数据模型设计

> **状态**: 冻结  
> **版本**: V2.0-A-R  
> **日期**: 2026-06-19  
> **修订**: 补全 10 个核心实体，每个均有独立 SQL 草案

---

## 1. 概述

定义 V2.0 Supervisor + Multi-Agent 架构所需的全部数据模型。每个实体必须有独立建表语句，禁止用模糊 JSON 字段代替核心实体。

### 1.1 10 实体覆盖表

| # | 实体表 | 用途 | 与 V1 关系 |
|---|--------|------|-----------|
| 1 | `agent_workers` | Worker 注册与生命周期 | 替代 `agent_registrations`（旧名保留为兼容别名） |
| 2 | `agent_capabilities` | 能力注册（独立表，非 JSON） | 新表 |
| 3 | `task_assignments` | 任务分派记录 | 替代 `task_dispatch_records` |
| 4 | `task_handoffs` | 任务交接记录 | 新表 |
| 5 | `task_results` | 任务执行结果 | 替代 `agent_runs` |
| 6 | `review_decisions` | 审查决策记录 | 新表 |
| 7 | `execution_artifacts` | 执行产物引用 | 新表 |
| 8 | `agent_heartbeats` | Agent 心跳记录 | 新表（从 agent_registrations 拆出） |
| 9 | `task_events` | 任务事件日志 (append-only) | 替代 transition_logs 的任务部分 |
| 10 | `sandbox_profiles` | 沙箱环境配置 | 新表 |

### 1.2 命名兼容映射

| 旧名称（V2.0-A 初稿） | 新名称（V2.0-A-R） | 说明 |
|------------------------|-------------------|------|
| `agent_registrations` | `agent_workers` | 注册→Worker，强调运行时 |
| `task_dispatch_records` | `task_assignments` | 分派记录→分配记录 |
| `agent_runs` | `task_results` | 运行记录→结果记录 |
| `transition_logs` | `task_events` (task 部分) | 拆分，project/agent 部分另表 |
| `supervisor_runs` | 保留 | 不变 |
| `agent_messages` | 保留 | 不变 |

### 1.3 V1 表保留清单

| V1 表 | 处理方式 |
|-------|---------|
| `projects` | 保留，扩展 status 枚举 |
| `requirement_analyses` | 保留，不变 |
| `modules` | 保留，不变 |
| `features` | 保留，不变 |
| `development_tasks` | 保留，扩展 status 枚举 + 新增列 |
| `database_tables` | 保留，不变 |
| `api_definitions` | 保留，不变 |
| `bugs` | 保留，不变 |
| `executor_runs` | 保留，兼容 |
| `execution_approvals` | 保留，兼容 |
| `project_execution_configs` | 保留，兼容 |
| `planning_previews` | 保留，兼容 |

---

## 2. 十张核心新表

### 2.1 agent_workers（Worker 注册）

```sql
CREATE TABLE agent_workers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id       TEXT NOT NULL,                              -- 业务唯一键，如 "worker-code-gen-1"
    agent_type      TEXT NOT NULL,                              -- code_gen / test_runner / git / safety / cmd / merge / recovery
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'registered'
                    CHECK (status IN ('registered','idle','claimed','running','unresponsive','disconnected')),
    host            TEXT DEFAULT 'localhost',
    pid             INTEGER,
    version         TEXT NOT NULL DEFAULT '1.0.0',

    -- 关联 Supervisor
    supervisor_run_id INTEGER,

    -- 运行时指标
    current_load          INTEGER DEFAULT 0,
    max_concurrent_tasks  INTEGER DEFAULT 1,
    consecutive_errors    INTEGER DEFAULT 0,
    tasks_completed       INTEGER DEFAULT 0,
    tasks_failed          INTEGER DEFAULT 0,
    total_execution_ms    INTEGER DEFAULT 0,

    -- 时间
    registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_heartbeat_at TEXT,
    disconnected_at   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),

    -- 唯一约束与索引
    UNIQUE(worker_id),
    FOREIGN KEY (supervisor_run_id) REFERENCES supervisor_runs(id)
);
CREATE INDEX idx_agent_workers_status ON agent_workers(status);
CREATE INDEX idx_agent_workers_type   ON agent_workers(agent_type);
CREATE INDEX idx_agent_workers_heartbeat ON agent_workers(last_heartbeat_at);

-- 兼容别名
-- agent_registrations → agent_workers
```

### 2.2 agent_capabilities（能力注册，独立表）

> **强制要求**：能力必须独立成表，不得仅存在 JSON 字段中。

```sql
CREATE TABLE agent_capabilities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id       TEXT NOT NULL,                              -- FK → agent_workers.worker_id
    capability_type TEXT NOT NULL                              -- language / framework / toolchain / platform / task_type / review / testing / deployment
                    CHECK (capability_type IN ('language','framework','toolchain','platform',
                          'task_type','review','testing','deployment')),
    language        TEXT,                                       -- python / javascript / typescript / ...
    framework       TEXT,                                       -- fastapi / react / vue / ...
    tool_name       TEXT,                                       -- 具体工具名
    version         TEXT,                                       -- 工具版本
    priority        INTEGER DEFAULT 0,                          -- 能力优先级
    max_files       INTEGER,                                    -- 单次最大文件数
    supports_repair INTEGER DEFAULT 0,                          -- 0/1
    timeout_seconds INTEGER,                                    -- 默认超时
    metadata_json   TEXT DEFAULT '{}',                          -- 扩展元数据

    -- 业务唯一键：同一 worker 同一 capability_type + language 组合唯一
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(worker_id, capability_type, COALESCE(language, '')),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id) ON DELETE CASCADE
);
CREATE INDEX idx_agent_cap_worker ON agent_capabilities(worker_id);
CREATE INDEX idx_agent_cap_type   ON agent_capabilities(capability_type);
CREATE INDEX idx_agent_cap_lang   ON agent_capabilities(language);
```

### 2.3 task_assignments（任务分配）

```sql
CREATE TABLE task_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id     TEXT NOT NULL,                            -- 业务唯一键，UUID v4
    task_id           INTEGER NOT NULL,                         -- FK → development_tasks.id
    worker_id         TEXT NOT NULL,                            -- FK → agent_workers.worker_id
    supervisor_run_id INTEGER NOT NULL,                         -- FK → supervisor_runs.id
    project_id        INTEGER NOT NULL,                         -- FK → projects.id

    -- 分配决策
    agent_type_required TEXT NOT NULL,
    decision_reason     TEXT DEFAULT '',
    priority            TEXT DEFAULT 'normal'
                        CHECK (priority IN ('low','normal','high','critical')),

    -- 时间线
    status          TEXT NOT NULL DEFAULT 'assigned'
                    CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token     TEXT,                                       -- 租约令牌（用于 claim 冲突检测）
    lease_expires_at TEXT,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,

    -- 幂等键
    idempotency_key TEXT,                                       -- 客户端传入幂等键

    dispatched_at   TEXT,
    acknowledged_at TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(assignment_id),
    UNIQUE(idempotency_key),                                    -- 幂等约束
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id),
    FOREIGN KEY (supervisor_run_id) REFERENCES supervisor_runs(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE UNIQUE INDEX idx_task_assignments_active
ON task_assignments(task_id)
WHERE status NOT IN ('completed','failed','cancelled');
CREATE INDEX idx_task_assignments_worker ON task_assignments(worker_id);
CREATE INDEX idx_task_assignments_status ON task_assignments(status);
CREATE INDEX idx_task_assignments_lease  ON task_assignments(lease_token);

-- 兼容别名
-- task_dispatch_records → task_assignments
```

### 2.4 task_handoffs（任务交接）

> **强制要求**：必须保存交接前后 Worker、原因、上下文快照和未完成步骤。

```sql
CREATE TABLE task_handoffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id      TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    task_id         INTEGER NOT NULL,                           -- FK → development_tasks.id
    assignment_id   TEXT NOT NULL,                              -- FK → task_assignments.assignment_id

    -- 交接双方
    from_worker_id  TEXT NOT NULL,                              -- 原 Worker
    to_worker_id    TEXT,                                       -- 新 Worker（可为空，表示回到队列）
    handoff_reason  TEXT NOT NULL
                    CHECK (handoff_reason IN ('worker_unresponsive','worker_error','worker_disconnect',
                          'user_request','budget_exceeded','manual_reassign','preemption')),

    -- 上下文快照
    current_stage       TEXT,                                   -- 交接时阶段
    completed_steps_json TEXT DEFAULT '[]',                     -- 已完成步骤
    remaining_steps_json TEXT DEFAULT '[]',                     -- 未完成步骤
    allowed_files_json  TEXT DEFAULT '[]',                      -- 允许的文件
    forbidden_actions_json TEXT DEFAULT '[]',                   -- 禁止的操作
    last_error_json     TEXT DEFAULT '{}',                      -- 上一个错误
    file_snapshot_json  TEXT DEFAULT '{}',                      -- 文件状态快照（路径→hash）
    git_head            TEXT,                                   -- Git HEAD
    environment_json    TEXT DEFAULT '{}',                      -- 环境变量快照

    -- 结果
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','rejected','expired')),
    accepted_by     TEXT,
    accepted_at     TEXT,
    expires_at      TEXT NOT NULL DEFAULT (datetime('now','+1 hour')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(handoff_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (assignment_id) REFERENCES task_assignments(assignment_id),
    FOREIGN KEY (from_worker_id) REFERENCES agent_workers(worker_id),
    FOREIGN KEY (to_worker_id) REFERENCES agent_workers(worker_id)
);
CREATE INDEX idx_handoffs_task     ON task_handoffs(task_id);
CREATE INDEX idx_handoffs_from     ON task_handoffs(from_worker_id);
CREATE INDEX idx_handoffs_status   ON task_handoffs(status);
CREATE INDEX idx_handoffs_expires  ON task_handoffs(expires_at);
```

### 2.5 task_results（任务执行结果）

```sql
CREATE TABLE task_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id       TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    task_id         INTEGER NOT NULL,                           -- FK → development_tasks.id
    assignment_id   TEXT NOT NULL,                              -- FK → task_assignments.assignment_id
    worker_id       TEXT NOT NULL,                              -- FK → agent_workers.worker_id
    project_id      INTEGER NOT NULL,                           -- FK → projects.id

    -- 结果状态
    result_status   TEXT NOT NULL
                    CHECK (result_status IN ('submitted','verified','rework','blocked','failed','timeout')),

    -- 文件变更
    files_modified_json TEXT DEFAULT '[]',
    files_checked_json  TEXT DEFAULT '[]',
    diff_summary        TEXT DEFAULT '',

    -- 测试
    tests_total    INTEGER DEFAULT 0,
    tests_passed   INTEGER DEFAULT 0,
    tests_failed   INTEGER DEFAULT 0,
    tests_skipped  INTEGER DEFAULT 0,
    test_output    TEXT DEFAULT '',

    -- Git
    git_commit     TEXT DEFAULT '',
    git_branch     TEXT DEFAULT '',
    base_commit    TEXT DEFAULT '',

    -- 执行
    exit_code      INTEGER,
    error_message  TEXT,
    stdout         TEXT DEFAULT '',
    stderr         TEXT DEFAULT '',
    model_calls    INTEGER DEFAULT 0,
    repair_attempts INTEGER DEFAULT 0,
    duration_ms    INTEGER DEFAULT 0,
    workspace_path TEXT DEFAULT '',

    -- 人工操作
    manual_actions_json TEXT DEFAULT '[]',
    evidence_refs_json  TEXT DEFAULT '[]',

    -- 交接请求
    handoff_requested   INTEGER DEFAULT 0,
    remaining_steps_json TEXT DEFAULT '[]',

    -- 幂等
    idempotency_key TEXT,

    submitted_at    TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(result_id),
    UNIQUE(idempotency_key),
    UNIQUE(assignment_id),                                      -- 一次 assignment 一个 result
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (assignment_id) REFERENCES task_assignments(assignment_id),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX idx_task_results_task   ON task_results(task_id);
CREATE INDEX idx_task_results_worker ON task_results(worker_id);
CREATE INDEX idx_task_results_status ON task_results(result_status);

-- 兼容别名
-- agent_runs → task_results
```

### 2.6 review_decisions（审查决策）

> **强制要求**：必须保存 PASS / REWORK / BLOCKED / NEED_USER 四种决策。

```sql
CREATE TABLE review_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    result_id       TEXT NOT NULL,                              -- FK → task_results.result_id
    task_id         INTEGER NOT NULL,                           -- FK → development_tasks.id
    reviewer_type   TEXT NOT NULL
                    CHECK (reviewer_type IN ('auto','human','supervisor')),
    reviewer_id     TEXT NOT NULL,                              -- worker_id 或 user_id

    -- 决策
    decision        TEXT NOT NULL
                    CHECK (decision IN ('PASS','REWORK','BLOCKED','NEED_USER')),
    reason          TEXT DEFAULT '',
    evidence_json   TEXT DEFAULT '{}',                          -- 依据

    -- REWORK 详情
    rework_steps_json       TEXT DEFAULT '[]',                  -- 返工步骤
    rework_deadline         TEXT,                               -- 返工截止
    rework_max_attempts     INTEGER DEFAULT 1,

    -- BLOCKED 详情
    blocked_reason          TEXT DEFAULT '',
    blocked_until           TEXT,
    unblock_condition       TEXT DEFAULT '',

    -- NEED_USER 详情
    user_prompt             TEXT DEFAULT '',
    user_decision           TEXT,                               -- 用户回复后的决策
    user_responded_at       TEXT,

    -- 审计
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(review_id),
    FOREIGN KEY (result_id) REFERENCES task_results(result_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
);
CREATE INDEX idx_review_task     ON review_decisions(task_id);
CREATE INDEX idx_review_decision ON review_decisions(decision);
CREATE INDEX idx_review_result   ON review_decisions(result_id);
```

### 2.7 execution_artifacts（执行产物）

> **强制要求**：必须保存 diff、日志、测试报告、Git commit、截图或产物引用。

```sql
CREATE TABLE execution_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id     TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    result_id       TEXT NOT NULL,                              -- FK → task_results.result_id
    task_id         INTEGER NOT NULL,                           -- FK → development_tasks.id
    project_id      INTEGER NOT NULL,                           -- FK → projects.id

    -- 产物类型
    artifact_type   TEXT NOT NULL
                    CHECK (artifact_type IN ('diff','log','test_report','git_commit','screenshot',
                          'build_output','lint_report','coverage_report','binary','document','other')),
    artifact_subtype TEXT,                                      -- 子类型

    -- 存储
    storage_path    TEXT NOT NULL,                              -- 文件系统路径
    storage_url     TEXT,                                       -- 外部 URL（可选）
    content_hash    TEXT,                                       -- SHA256
    size_bytes      INTEGER,
    mime_type       TEXT,

    -- 描述
    description     TEXT DEFAULT '',
    tags_json       TEXT DEFAULT '[]',
    is_sensitive    INTEGER DEFAULT 0,                          -- 敏感标记

    -- 生命周期
    retention_policy TEXT DEFAULT 'permanent'
                    CHECK (retention_policy IN ('permanent','project_life','30_days','7_days','manual')),
    expires_at      TEXT,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(artifact_id),
    UNIQUE(result_id, artifact_type, COALESCE(artifact_subtype, '')),  -- 同一 result 同类型同子类型唯一
    FOREIGN KEY (result_id) REFERENCES task_results(result_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX idx_artifacts_result ON execution_artifacts(result_id);
CREATE INDEX idx_artifacts_type   ON execution_artifacts(artifact_type);
CREATE INDEX idx_artifacts_task   ON execution_artifacts(task_id);
CREATE INDEX idx_artifacts_hash   ON execution_artifacts(content_hash);
```

### 2.8 agent_heartbeats（Agent 心跳）

```sql
CREATE TABLE agent_heartbeats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    heartbeat_id    TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    worker_id       TEXT NOT NULL,                              -- FK → agent_workers.worker_id
    supervisor_run_id INTEGER,                                  -- FK → supervisor_runs.id

    -- 心跳内容
    status          TEXT NOT NULL
                    CHECK (status IN ('idle','claimed','running')),
    current_task_id INTEGER,
    current_assignment_id TEXT,
    current_stage   TEXT,                                       -- 当前执行阶段
    progress_pct    REAL DEFAULT 0,                             -- 进度 0-100

    -- 健康指标
    cpu_percent     REAL,
    memory_mb       REAL,
    disk_free_mb    REAL,
    uptime_seconds  INTEGER,
    active_threads  INTEGER,
    pending_tasks   INTEGER DEFAULT 0,

    -- 错误
    consecutive_errors INTEGER DEFAULT 0,
    last_error_json    TEXT DEFAULT '{}',

    -- 时间
    sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
    received_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(heartbeat_id),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id),
    FOREIGN KEY (supervisor_run_id) REFERENCES supervisor_runs(id)
);
CREATE INDEX idx_heartbeats_worker ON agent_heartbeats(worker_id);
CREATE INDEX idx_heartbeats_time   ON agent_heartbeats(received_at);
CREATE INDEX idx_heartbeats_status ON agent_heartbeats(status);
```

### 2.9 task_events（任务事件日志）

> **强制要求**：append-only，不可修改或删除已有记录。

```sql
CREATE TABLE task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,                              -- 业务唯一键，UUID v4
    task_id         INTEGER NOT NULL,                           -- FK → development_tasks.id
    assignment_id   TEXT,                                       -- 关联的 task_assignments.assignment_id
    project_id      INTEGER NOT NULL,                           -- FK → projects.id

    -- 事件
    event_type      TEXT NOT NULL
                    CHECK (event_type IN ('state_change','claim','heartbeat','submit','review',
                          'handoff','artifact_created','error','user_action','system','budget','lease_expired')),
    from_state      TEXT,                                       -- 状态变更前
    to_state        TEXT,                                       -- 状态变更后
    reason          TEXT DEFAULT '',
    detail_json     TEXT DEFAULT '{}',                          -- 事件详情

    -- 操作者
    operator_type   TEXT NOT NULL
                    CHECK (operator_type IN ('system','supervisor','worker','reviewer','user')),
    operator_id     TEXT NOT NULL,
    idempotency_key TEXT,                                       -- 幂等去重

    -- 版本
    state_version_before INTEGER,
    state_version_after  INTEGER,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(event_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- append-only 策略：无 UPDATE/DELETE 权限，只允许 INSERT
-- 应用层禁止 UPDATE/DELETE 操作
-- 保留策略：与 project 生命周期一致，project 删除时级联删除

CREATE INDEX idx_task_events_task   ON task_events(task_id);
CREATE INDEX idx_task_events_type   ON task_events(event_type);
CREATE INDEX idx_task_events_time   ON task_events(created_at);
CREATE INDEX idx_task_events_state  ON task_events(from_state, to_state);

-- 兼容别名
-- transition_logs (task 部分) → task_events
```

### 2.10 sandbox_profiles（沙箱配置）

> **强制要求**：必须描述运行平台、工具链、workspace 策略和资源限制。

```sql
CREATE TABLE sandbox_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT NOT NULL,                              -- 业务唯一键
    profile_name    TEXT NOT NULL,                              -- 可读名称
    project_id      INTEGER,                                    -- FK → projects.id（NULL 表示全局模板）

    -- 运行平台
    platform        TEXT NOT NULL DEFAULT 'windows'
                    CHECK (platform IN ('windows','linux','macos')),
    arch            TEXT DEFAULT 'x64'
                    CHECK (arch IN ('x86','x64','arm64')),
    shell           TEXT DEFAULT 'powershell',                  -- powershell / cmd / bash / zsh

    -- 工具链
    toolchain_json  TEXT NOT NULL DEFAULT '{}',                 -- {"node":"20.0.0","python":"3.12.0","git":"2.40.0",...}
    env_vars_json   TEXT DEFAULT '{}',                          -- 环境变量
    pre_install_commands_json TEXT DEFAULT '[]',                -- 初始化命令

    -- Workspace 策略
    workspace_root      TEXT NOT NULL DEFAULT './workspace',
    workspace_strategy  TEXT NOT NULL DEFAULT 'per_task_worktree'
                        CHECK (workspace_strategy IN ('per_project','per_task_worktree','shared','isolated_container')),
    cleanup_policy      TEXT NOT NULL DEFAULT 'on_completion'
                        CHECK (cleanup_policy IN ('on_completion','on_task_done','manual','never')),
    max_workspace_size_mb INTEGER DEFAULT 1024,

    -- 资源限制
    cpu_limit_percent   INTEGER DEFAULT 50,
    memory_limit_mb     INTEGER DEFAULT 512,
    disk_limit_mb       INTEGER DEFAULT 2048,
    max_processes       INTEGER DEFAULT 10,
    network_access      TEXT DEFAULT 'restricted'
                        CHECK (network_access IN ('full','restricted','none','allowlist')),
    network_allowlist_json TEXT DEFAULT '[]',

    -- 安全
    allowed_paths_json      TEXT DEFAULT '["workspace_root","temp"]',
    forbidden_paths_json    TEXT DEFAULT '["C:/Windows","C:/Program Files","/etc","/usr"]',
    allowed_commands_json   TEXT DEFAULT '["git","node","npm","python","pip","cmd","powershell"]',
    forbidden_commands_json TEXT DEFAULT '["rm","del","format","shutdown"]',

    -- 超时
    task_timeout_seconds    INTEGER DEFAULT 1800,
    idle_timeout_seconds    INTEGER DEFAULT 3600,

    -- 状态
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(profile_id),
    UNIQUE(project_id),                                         -- 每个 project 最多一个 profile
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX idx_sandbox_project ON sandbox_profiles(project_id);
CREATE INDEX idx_sandbox_platform ON sandbox_profiles(platform);
```

---

## 3. 保留的 V2.0-A 初稿表

以下表从 V2.0-A 初稿保留（已在上方 10 实体范围外，但架构需要）：

### 3.1 supervisor_runs（不变）

```sql
CREATE TABLE supervisor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    project_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting'
           CHECK (status IN ('starting','scanning','dispatching','waiting','completed','blocked','paused','stopped')),
    mode TEXT DEFAULT 'auto_until_blocked',
    current_step TEXT DEFAULT 'initializing',
    decision TEXT DEFAULT '',
    tasks_total INTEGER DEFAULT 0,
    tasks_dispatched INTEGER DEFAULT 0,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,
    tasks_repaired INTEGER DEFAULT 0,
    tasks_escalated INTEGER DEFAULT 0,
    active_agents_json TEXT DEFAULT '[]',
    budget_limit_seconds INTEGER DEFAULT 7200,
    budget_used_seconds INTEGER DEFAULT 0,
    budget_exceeded INTEGER DEFAULT 0,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE UNIQUE INDEX idx_supervisor_runs_active
ON supervisor_runs(project_id)
WHERE status NOT IN ('completed','stopped','blocked');
```

### 3.2 agent_messages（不变）

```sql
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    correlation_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    recipient_type TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK (status IN ('pending','delivered','processed','failed','discarded')),
    delivery_attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    processed_at TEXT,
    project_id INTEGER,
    task_id INTEGER,
    assignment_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX idx_agent_messages_correlation ON agent_messages(correlation_id);
CREATE INDEX idx_agent_messages_type_status ON agent_messages(message_type, status);
```

---

## 4. V1 表扩展

### 4.1 projects 表扩展

```sql
ALTER TABLE projects ADD COLUMN state_version INTEGER DEFAULT 1;
ALTER TABLE projects ADD COLUMN last_state_change TEXT;
ALTER TABLE projects ADD COLUMN last_decision TEXT DEFAULT '';
```

### 4.2 development_tasks 表扩展

```sql
-- V2 统一状态字段（扩展 status 枚举）
-- 新合法值: draft/planned/approved/queued/claimed/running/result_submitted/reviewing/verified/rework/blocked/need_user/failed/cancelled

ALTER TABLE development_tasks ADD COLUMN state_version INTEGER DEFAULT 1;
ALTER TABLE development_tasks ADD COLUMN last_state_change TEXT;
ALTER TABLE development_tasks ADD COLUMN current_worker_id TEXT DEFAULT '';
ALTER TABLE development_tasks ADD COLUMN current_assignment_id TEXT DEFAULT '';
ALTER TABLE development_tasks ADD COLUMN repair_count INTEGER DEFAULT 0;
ALTER TABLE development_tasks ADD COLUMN max_repairs INTEGER DEFAULT 2;
ALTER TABLE development_tasks ADD COLUMN timeout_seconds INTEGER DEFAULT 1800;
ALTER TABLE development_tasks ADD COLUMN lease_token TEXT DEFAULT '';
ALTER TABLE development_tasks ADD COLUMN lease_expires_at TEXT;
ALTER TABLE development_tasks ADD COLUMN idempotency_key TEXT DEFAULT '';
```

---

## 5. 实体关系图

```
┌──────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│    projects      │     │   supervisor_runs    │     │    agent_workers    │
│  id (PK)         │◄────│  project_id (FK)     │     │  worker_id (UQ)     │
│  status          │     │  run_id (UQ)         │     │  status             │
└────────┬─────────┘     └──────────┬──────────┘     └──────┬──────┬───────┘
         │ 1:N                      │ 1:N                   │      │
         ▼                          ▼                       │      │
┌──────────────────┐     ┌─────────────────────┐            │      │
│development_tasks │     │  task_assignments   │◄───────────┘      │
│  id (PK)         │◄────│  task_id (FK)       │                   │
│  project_id (FK) │     │  worker_id (FK)     │                   │
│  status          │     │  assignment_id (UQ) │                   │
│  state_version   │     │  idempotency_key(UQ)│                   │
└────────┬─────────┘     └──────────┬──────────┘                   │
         │ 1:N                      │ 1:1                          │
         │                          ▼                              │
         │               ┌─────────────────────┐                   │
         ├──────────────→│    task_results     │                   │
         │               │  result_id (UQ)     │                   │
         │               │  assignment_id (UQ) │                   │
         │               │  idempotency_key(UQ)│                   │
         │               └──────────┬──────────┘                   │
         │                          │ 1:N                          │
         │               ┌──────────┼──────────┐                   │
         │               ▼          ▼          ▼                   │
         │     ┌──────────────┐ ┌──────────┐ ┌──────────────┐     │
         │     │review_decisions│ │execution │ │task_handoffs │     │
         │     │ review_id (UQ)│ │_artifacts│ │handoff_id(UQ)│     │
         │     │ decision      │ │artifact  │ │from_worker_id│─────┘
         │     └──────────────┘ │_id (UQ)  │ │to_worker_id  │
         │                      └──────────┘ └──────────────┘
         │ 1:N
         ▼
┌──────────────────┐     ┌─────────────────────┐
│   task_events    │     │ agent_capabilities  │
│  event_id (UQ)   │     │  worker_id (FK)     │◄── agent_workers.worker_id
│  idempotency_key │     │  capability_type    │
│  (UQ)            │     │  language           │
│  (append-only)   │     └─────────────────────┘
└──────────────────┘

┌──────────────────────┐     ┌─────────────────────┐
│  agent_heartbeats    │     │  sandbox_profiles   │
│  heartbeat_id (UQ)   │     │  profile_id (UQ)    │
│  worker_id (FK)      │     │  project_id (UQ)    │
└──────────────────────┘     └─────────────────────┘
```

---

## 6. 迁移策略

### 6.1 迁移原则

1. 不修改 V1 表结构（仅新增列，不删除/重命名）
2. 新表通过迁移脚本创建
3. V1 代码仍可正常读写旧字段
4. V2 新增字段有合理默认值

### 6.2 V1 → V2 状态映射

```python
# develop_tasks V1 status → V2 status
TASK_STATUS_MAP = {
    "pending":       None,                # 按 readiness_status 决定
    "executing":     "running",
    "completed":     "verified",          # V1 completed → V2 verified
    "paused":        "cancelled",
    "cancelled":     "cancelled",
    "waiting_test":  "running",
    "test_failed":   "failed",
}
```

### 6.3 回滚支持

- 新增表：删除即可回滚
- 新增列：设 NULL 允许旧代码忽略
- **切换开关**：`settings.V2_ENABLED = False` 可回退到 V1 行为

---

## 7. 附录

### 7.1 删除与保留策略

| 实体 | 保留策略 | 删除触发 |
|------|---------|---------|
| agent_workers | project 生命周期 + 30 天 | worker.disconnected 后 30 天 |
| agent_capabilities | 与 worker 同生命周期 | Worker 删除时 CASCADE |
| task_assignments | project 生命周期 | project 删除时 CASCADE |
| task_handoffs | project 生命周期 + 7 天 | project 删除时 CASCADE |
| task_results | 永久（或 project 删除） | project 删除时 CASCADE |
| review_decisions | 永久（审计） | 不自动删除 |
| execution_artifacts | 按 retention_policy | expires_at 到达时清理 |
| agent_heartbeats | 7 天滚动窗口 | 定时清理 >7 天 |
| task_events | append-only，project 生命周期 | project 删除时 CASCADE |
| sandbox_profiles | 永久 | 手动删除 |

### 7.2 参考

- [V2_ARCHITECTURE.md](./V2_ARCHITECTURE.md)
- [V2_STATE_MACHINE.md](./V2_STATE_MACHINE.md)
- [V2_AGENT_PROTOCOL.md](./V2_AGENT_PROTOCOL.md)
- [V2_API_CONTRACT.md](./V2_API_CONTRACT.md)
- [V2_MIGRATION_PLAN.md](./V2_MIGRATION_PLAN.md)
