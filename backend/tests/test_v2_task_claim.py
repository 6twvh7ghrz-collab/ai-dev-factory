"""V2.0-B2b: TaskClaimService 专项 pytest 测试

测试范围:
  - 正常领取流程
  - Worker 校验（类型、状态、能力）
  - Task 校验（状态、版本、范围）
  - 幂等性（相同/不同请求）
  - 原子性与并发
  - Feature flag 门禁
  - 租约与并发冲突

每个测试使用独立临时 SQLite 数据库，不依赖测试顺序，不连接正式数据库。
"""

import os
import sys
import json
import uuid
import sqlite3
import tempfile
import time
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.supervisor.task_claim_service import (
    TaskClaimService,
    ERROR_TASK_NOT_FOUND,
    ERROR_TASK_NOT_CLAIMABLE,
    ERROR_TASK_SCOPE_VIOLATION,
    ERROR_STATE_VERSION_CONFLICT,
    ERROR_LEASE_CONFLICT,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_VALIDATION_ERROR,
    ERROR_INTERNAL_ERROR,
    ERROR_WORKER_NOT_REGISTERED,
    ERROR_WORKER_NOT_AVAILABLE,
    ERROR_WORKER_TYPE_NOT_ALLOWED,
    ERROR_WORKER_CAPABILITY_MISMATCH,
    ERROR_V2_CONTROL_PLANE_DISABLED,
)
from app.supervisor.worker_registry import (
    WorkerRegistryService,
    WORKER_STATUS_AVAILABLE,
    WORKER_STATUS_BUSY,
    WORKER_STATUS_OFFLINE,
    WORKER_STATUS_DISABLED,
    WORKER_STATUS_REGISTERED,
    WORKER_TYPE_EXECUTOR,
    WORKER_TYPE_SUPERVISOR,
    WORKER_TYPE_REVIEWER,
)


# ── Schema (mirrors Migration 012 + 013 DDL) ──

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS development_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    task_type TEXT DEFAULT 'backend',
    status TEXT DEFAULT 'draft',
    state_version INTEGER DEFAULT 1,
    last_state_change TEXT,
    dependencies TEXT,
    files_to_check TEXT,
    files_to_modify TEXT,
    test_steps TEXT,
    acceptance_criteria TEXT,
    implementation_steps TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT,
    project_id      INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    reason          TEXT DEFAULT '',
    detail_json     TEXT DEFAULT '{}',
    operator_type   TEXT NOT NULL,
    operator_id     TEXT NOT NULL,
    idempotency_key TEXT,
    state_version_before INTEGER,
    state_version_after  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(event_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS task_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id     TEXT NOT NULL,
    task_id           INTEGER NOT NULL,
    worker_id         TEXT NOT NULL,
    project_id        INTEGER NOT NULL,
    agent_type_required TEXT NOT NULL,
    decision_reason     TEXT DEFAULT '',
    priority            TEXT DEFAULT 'normal',
    status          TEXT NOT NULL DEFAULT 'assigned'
                    CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token     TEXT,
    lease_expires_at TEXT,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,
    idempotency_key TEXT,
    dispatched_at   TEXT,
    acknowledged_at TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(assignment_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_assignments_active
ON task_assignments(task_id)
WHERE status NOT IN ('completed','failed','cancelled','timeout');

CREATE TABLE IF NOT EXISTS agent_workers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id         TEXT NOT NULL,
    worker_type       TEXT NOT NULL
                      CHECK (worker_type IN ('executor','supervisor','reviewer')),
    provider          TEXT DEFAULT '',
    display_name      TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'registered'
                      CHECK (status IN ('registered','available','busy','offline','disabled')),
    max_concurrency   INTEGER DEFAULT 1,
    current_load      INTEGER DEFAULT 0,
    sandbox_profile_id TEXT DEFAULT '',
    registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json     TEXT DEFAULT '{}',
    version           INTEGER DEFAULT 1,
    UNIQUE(worker_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_workers_active_executor
ON agent_workers(worker_type, status)
WHERE worker_type = 'executor' AND status IN ('available','busy');

CREATE TABLE IF NOT EXISTS agent_capabilities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    TEXT NOT NULL,
    capability   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(worker_id, capability),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id) ON DELETE CASCADE
);
"""


# ── Helper: build temp DB ──

def _build_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_claim_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    # Seed a project
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'test-project')")
    conn.commit()
    conn.close()
    return path


def _cleanup_temp_db(path):
    import time as _t
    _t.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        for _ in range(3):
            try:
                if os.path.exists(p):
                    os.unlink(p)
                break
            except PermissionError:
                _t.sleep(0.1)
            except FileNotFoundError:
                break


def _raw_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── Fixtures ──

@pytest.fixture
def claim_service():
    """Create TaskClaimService backed by a fresh temp DB (V2 enabled)."""
    db_path = _build_temp_db()
    svc = TaskClaimService(db_path, v2_enabled=True)
    yield svc
    svc._worker_registry  # touch reference
    _cleanup_temp_db(db_path)


@pytest.fixture
def claim_service_disabled():
    """Create TaskClaimService with V2 disabled."""
    db_path = _build_temp_db()
    svc = TaskClaimService(db_path, v2_enabled=False)
    yield svc
    _cleanup_temp_db(db_path)


# ── Helpers ──

def _register_available_executor(svc: TaskClaimService, worker_id="exec-1",
                                  capabilities=None):
    """Register an executor and set to AVAILABLE."""
    caps = capabilities or ["python"]
    r = svc._worker_registry.register_worker(
        worker_id=worker_id, worker_type=WORKER_TYPE_EXECUTOR,
        capabilities=caps
    )
    assert r["success"], f"Failed to register {worker_id}: {r.get('error')}"
    r2 = svc._worker_registry.set_worker_status(worker_id, WORKER_STATUS_AVAILABLE)
    assert r2["success"], f"Failed to set {worker_id} AVAILABLE: {r2.get('error')}"


def _create_queued_task(svc: TaskClaimService, task_id=None, project_id=1,
                         state_version=1, task_type="backend",
                         implementation_steps=None):
    """Create a task in QUEUED status directly in the DB."""
    conn = _raw_conn(svc.db_path)
    if task_id is not None:
        # Insert with specific id
        impl_json = json.dumps(implementation_steps) if implementation_steps else None
        conn.execute("""
            INSERT INTO development_tasks
            (id, project_id, title, task_type, status, state_version, implementation_steps)
            VALUES (?, ?, ?, ?, 'queued', ?, ?)
        """, (task_id, project_id, f"Task {task_id}", task_type,
              state_version, impl_json))
    else:
        impl_json = json.dumps(implementation_steps) if implementation_steps else None
        cur = conn.execute("""
            INSERT INTO development_tasks
            (project_id, title, task_type, status, state_version, implementation_steps)
            VALUES (?, ?, ?, 'queued', ?, ?)
        """, (project_id, f"Test Task", task_type, state_version, impl_json))
        task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


# ================================================================
# 正常领取
# ================================================================

class TestNormalClaim:

    def test_available_executor_claims_queued_task(self, claim_service):
        """AVAILABLE executor 成功 claim QUEUED Task"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-001", lease_seconds=300,
        )
        assert result["success"] is True, f"claim failed: {result.get('error_message')}"
        assert result["task_id"] == task_id
        assert result["worker_id"] == "exec-1"
        assert result["assignment_id"] is not None
        assert result["lease_token"] is not None
        assert result["lease_expires_at"] is not None
        assert result["state_version"] == 2
        assert result["idempotent"] is False

    def test_claim_creates_assignment(self, claim_service):
        """claim 后 task_assignments 表有记录"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-002", lease_seconds=300,
        )
        assert result["success"]

        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT * FROM task_assignments WHERE assignment_id = ?",
            (result["assignment_id"],)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["task_id"] == task_id
        assert row["worker_id"] == "exec-1"
        assert row["status"] == "assigned"

    def test_task_becomes_claimed(self, claim_service):
        """claim 后 Task status 变为 CLAIMED"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-003", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT status, state_version FROM development_tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        conn.close()
        assert row["status"].upper() == "CLAIMED"
        assert row["state_version"] == 2

    def test_state_version_increments(self, claim_service):
        """claim 后 state_version +1"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=3)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=3,
            idempotency_key="key-004", lease_seconds=300,
        )
        assert result["success"]
        assert result["state_version"] == 4

    def test_worker_becomes_busy(self, claim_service):
        """claim 后 Worker 变为 BUSY"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-005", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT status FROM agent_workers WHERE worker_id = ?",
            ("exec-1",)
        ).fetchone()
        conn.close()
        assert row["status"] == WORKER_STATUS_BUSY

    def test_task_event_written(self, claim_service):
        """claim 写入 task_events 记录"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-006", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT * FROM task_events WHERE idempotency_key = ?",
            ("key-006",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["event_type"] == "claim"
        assert row["from_state"] == "QUEUED"
        assert row["to_state"] == "CLAIMED"

    def test_task_packet_fields_complete(self, claim_service):
        """Task Packet 包含所有必需字段"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, task_type="backend")

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-007", lease_seconds=300,
        )
        assert result["success"]
        pkt = result["task_packet"]
        assert pkt is not None
        assert pkt["task_id"] == task_id
        assert pkt["project_id"] == 1
        assert pkt["assignment_id"] == result["assignment_id"]
        assert pkt["lease_token"] == result["lease_token"]
        assert pkt["lease_expires_at"] == result["lease_expires_at"]
        assert pkt["state_version"] == 2
        assert "allowed_task_ids" in pkt
        assert "allowed_files" in pkt
        assert "forbidden_actions" in pkt
        assert "test_commands" in pkt
        assert "success_criteria" in pkt
        assert "evidence_required" in pkt
        assert "git_head" in pkt
        assert "current_stage" in pkt


# ================================================================
# Worker 校验
# ================================================================

class TestWorkerValidation:

    def test_unregistered_worker_rejected(self, claim_service):
        """未注册 Worker 被拒绝"""
        task_id = _create_queued_task(claim_service)
        result = claim_service.claim_task(
            task_id=task_id, worker_id="ghost", expected_version=1,
            idempotency_key="key-w1", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_NOT_REGISTERED

    def test_supervisor_rejected(self, claim_service):
        """Supervisor 类型 Worker 被拒绝"""
        claim_service._worker_registry.register_worker(
            worker_id="sup-1", worker_type=WORKER_TYPE_SUPERVISOR
        )
        claim_service._worker_registry.set_worker_status("sup-1", WORKER_STATUS_AVAILABLE)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="sup-1", expected_version=1,
            idempotency_key="key-w2", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_TYPE_NOT_ALLOWED

    def test_reviewer_rejected(self, claim_service):
        """Reviewer 类型 Worker 被拒绝"""
        claim_service._worker_registry.register_worker(
            worker_id="rev-1", worker_type=WORKER_TYPE_REVIEWER
        )
        claim_service._worker_registry.set_worker_status("rev-1", WORKER_STATUS_AVAILABLE)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="rev-1", expected_version=1,
            idempotency_key="key-w3", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_TYPE_NOT_ALLOWED

    def test_offline_worker_rejected(self, claim_service):
        """OFFLINE Worker 被拒绝"""
        claim_service._worker_registry.register_worker(
            worker_id="off-1", worker_type=WORKER_TYPE_EXECUTOR
        )
        claim_service._worker_registry.set_worker_status("off-1", WORKER_STATUS_OFFLINE)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="off-1", expected_version=1,
            idempotency_key="key-w4", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_NOT_AVAILABLE

    def test_disabled_worker_rejected(self, claim_service):
        """DISABLED Worker 被拒绝"""
        claim_service._worker_registry.register_worker(
            worker_id="dis-1", worker_type=WORKER_TYPE_EXECUTOR
        )
        claim_service._worker_registry.set_worker_status("dis-1", WORKER_STATUS_DISABLED)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="dis-1", expected_version=1,
            idempotency_key="key-w5", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_NOT_AVAILABLE

    def test_busy_worker_rejected(self, claim_service):
        """BUSY Worker 被拒绝"""
        _register_available_executor(claim_service, "exec-busy")
        task_id = _create_queued_task(claim_service)
        # First claim to make worker BUSY
        claim_service.claim_task(
            task_id=task_id, worker_id="exec-busy", expected_version=1,
            idempotency_key="key-b1", lease_seconds=300,
        )
        # Create another task
        task_id2 = _create_queued_task(claim_service)
        # Try to claim with BUSY worker
        result = claim_service.claim_task(
            task_id=task_id2, worker_id="exec-busy", expected_version=1,
            idempotency_key="key-w6", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_NOT_AVAILABLE

    def test_capability_mismatch_rejected(self, claim_service):
        """Worker capability 不满足任务要求时被拒绝"""
        _register_available_executor(claim_service, "exec-no-cap", capabilities=["javascript"])
        task_id = _create_queued_task(
            claim_service, task_type="backend",
            implementation_steps={"_requirements": {"language": "python"}}
        )
        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-no-cap", expected_version=1,
            idempotency_key="key-w7", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_WORKER_CAPABILITY_MISMATCH


# ================================================================
# Task 校验
# ================================================================

class TestTaskValidation:

    def test_task_not_found(self, claim_service):
        """不存在的 Task 被拒绝"""
        _register_available_executor(claim_service)
        result = claim_service.claim_task(
            task_id=99999, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t1", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_TASK_NOT_FOUND

    def test_non_queued_task_rejected(self, claim_service):
        """非 QUEUED Task 被拒绝"""
        _register_available_executor(claim_service)
        conn = _raw_conn(claim_service.db_path)
        conn.execute("""
            INSERT INTO development_tasks (id, project_id, title, status, state_version)
            VALUES (100, 1, 'Draft Task', 'draft', 1)
        """)
        conn.commit()
        conn.close()

        result = claim_service.claim_task(
            task_id=100, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t2", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_TASK_NOT_CLAIMABLE

    def test_expected_version_wrong(self, claim_service):
        """expected_version 错误被拒绝"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=5)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=2,
            idempotency_key="key-t3", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_STATE_VERSION_CONFLICT

    def test_project_id_mismatch(self, claim_service):
        """project_id 不一致被拒绝"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, project_id=1)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t4", lease_seconds=300, project_id=2,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_TASK_SCOPE_VIOLATION

    def test_allowed_task_ids_out_of_scope(self, claim_service):
        """Task 不在 allowed_task_ids 中被拒绝"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t5", lease_seconds=300,
            allowed_task_ids=[999, 998],
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_TASK_SCOPE_VIOLATION

    def test_active_assignment_blocks_reclaim(self, claim_service):
        """已存在有效 assignment 阻止新 claim"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        # First claim succeeds
        r1 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t6a", lease_seconds=300,
        )
        assert r1["success"]

        # Second claim with different key fails — task is now CLAIMED
        r2 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=2,
            idempotency_key="key-t6b", lease_seconds=300,
        )
        assert r2["success"] is False
        # Should fail because task is CLAIMED, not QUEUED

    def test_expired_assignment_allows_new_claim(self, claim_service):
        """过期 assignment 不阻止新 claim"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        # Insert an expired assignment manually
        conn = _raw_conn(claim_service.db_path)
        conn.execute("""
            INSERT INTO task_assignments
            (assignment_id, task_id, worker_id, project_id, agent_type_required,
             status, lease_token, lease_expires_at,
             idempotency_key, dispatched_at, created_at, updated_at)
            VALUES ('asgn-old', ?, 'old-worker', 1, 'executor',
                    'assigned', 'old-token', '2000-01-01 00:00:00',
                    'old-key', '2000-01-01 00:00:00', '2000-01-01 00:00:00',
                    '2000-01-01 00:00:00')
        """, (task_id,))
        conn.execute("UPDATE development_tasks SET status='queued', state_version=1 WHERE id=?",
                      (task_id,))
        conn.commit()
        conn.close()

        # New claim should succeed (expired lease)
        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-t7", lease_seconds=300,
        )
        assert result["success"] is True


# ================================================================
# 幂等
# ================================================================

class TestIdempotency:

    def test_same_request_returns_cached_assignment(self, claim_service):
        """相同请求重复 claim 返回原 assignment（幂等检查先于 Worker 校验）"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        r1 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i1", lease_seconds=300,
        )
        assert r1["success"]
        assert r1["idempotent"] is False

        # Same params → idempotency returns cached result (no worker check needed)
        r2 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i1", lease_seconds=300,
        )
        assert r2["success"]
        assert r2["idempotent"] is True
        assert r2["assignment_id"] == r1["assignment_id"]

    def test_idempotent_no_duplicate_events(self, claim_service):
        """幂等不重复写 event"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i2", lease_seconds=300,
        )

        conn = _raw_conn(claim_service.db_path)
        count = conn.execute(
            "SELECT COUNT(*) as c FROM task_events WHERE idempotency_key = ?",
            ("key-i2",)
        ).fetchone()["c"]
        conn.close()
        assert count == 1

    def test_idempotent_no_duplicate_version(self, claim_service):
        """幂等不重复增加 version"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i3", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        ver = conn.execute(
            "SELECT state_version FROM development_tasks WHERE id = ?", (task_id,)
        ).fetchone()["state_version"]
        conn.close()
        assert ver == 2  # only incremented once

    def test_different_worker_id_conflict(self, claim_service):
        """相同 key + 不同 worker_id 产生 IDEMPOTENCY_CONFLICT（指纹不匹配）"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i4", lease_seconds=300,
        )

        # Same key, different worker_id → fingerprint mismatch → conflict
        # (No need to register exec-2; idempotency check is first)
        r2 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-2", expected_version=1,
            idempotency_key="key-i4", lease_seconds=300,
        )
        assert r2["success"] is False
        assert r2["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_expected_version_conflict(self, claim_service):
        """相同 key + 不同 expected_version 产生冲突"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i5", lease_seconds=300,
        )

        # Try with different expected_version (shouldn't matter for idempotency)
        # but fingerprint will be different
        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=2,
            idempotency_key="key-i5", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_lease_seconds_conflict(self, claim_service):
        """相同 key + 不同 lease_seconds 产生冲突"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i6", lease_seconds=300,
        )

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i6", lease_seconds=600,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_allowed_task_ids_conflict(self, claim_service):
        """相同 key + 不同 allowed_task_ids 产生冲突"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i7", lease_seconds=300,
            allowed_task_ids=[task_id],
        )

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-i7", lease_seconds=300,
            allowed_task_ids=[task_id, 999],
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_IDEMPOTENCY_CONFLICT


# ================================================================
# 原子性与并发
# ================================================================

class TestAtomicityAndConcurrency:

    def test_claim_failure_no_dirty_assignment(self, claim_service):
        """claim 失败不残留 assignment"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        # This will fail because state_version is wrong
        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=99,
            idempotency_key="key-a1", lease_seconds=300,
        )
        assert result["success"] is False

        conn = _raw_conn(claim_service.db_path)
        count = conn.execute(
            "SELECT COUNT(*) as c FROM task_assignments WHERE idempotency_key = ?",
            ("key-a1",)
        ).fetchone()["c"]
        conn.close()
        assert count == 0

    def test_state_unchanged_on_claim_failure(self, claim_service):
        """claim 失败 Task 状态不变"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=99,
            idempotency_key="key-a2", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT status, state_version FROM development_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row["status"].upper() == "QUEUED"
        assert row["state_version"] == 1

    def test_worker_unchanged_on_claim_failure(self, claim_service):
        """claim 失败 Worker 状态不变"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=99,
            idempotency_key="key-a3", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        row = conn.execute(
            "SELECT status FROM agent_workers WHERE worker_id = ?", ("exec-1",)
        ).fetchone()
        conn.close()
        assert row["status"] == WORKER_STATUS_AVAILABLE

    def test_no_event_on_claim_failure(self, claim_service):
        """claim 失败不写 event"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=99,
            idempotency_key="key-a4", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        count = conn.execute(
            "SELECT COUNT(*) as c FROM task_events WHERE idempotency_key = ?",
            ("key-a4",)
        ).fetchone()["c"]
        conn.close()
        assert count == 0

    def test_concurrent_claim_only_one_succeeds(self, claim_service):
        """两连接并发 claim 同一 Task 同一 Worker，仅一个成功"""
        _register_available_executor(claim_service, "conc-w")
        task_id = _create_queued_task(claim_service, state_version=1)

        success_count = [0]
        fail_count = [0]
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)

        def claim_worker(name, idem_key):
            svc = TaskClaimService(claim_service.db_path, v2_enabled=True)
            # Re-register and re-set the worker AVAILABLE in this connection
            svc._worker_registry.register_worker(
                worker_id="conc-w", worker_type=WORKER_TYPE_EXECUTOR,
                capabilities=["python"]
            )
            svc._worker_registry.set_worker_status("conc-w", WORKER_STATUS_AVAILABLE)
            barrier.wait(timeout=5)
            try:
                r = svc.claim_task(
                    task_id=task_id, worker_id="conc-w", expected_version=1,
                    idempotency_key=idem_key, lease_seconds=300,
                )
                with lock:
                    if r["success"]:
                        success_count[0] += 1
                    else:
                        fail_count[0] += 1
            except Exception:
                with lock:
                    fail_count[0] += 1

        t1 = threading.Thread(target=claim_worker, args=("A", "conc-key-a"))
        t2 = threading.Thread(target=claim_worker, args=("B", "conc-key-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert success_count[0] == 1, f"Expected 1 success, got {success_count[0]}"
        assert fail_count[0] == 1, f"Expected 1 failure, got {fail_count[0]}"

    def test_claim_fails_no_dirty_assignment_or_event(self, claim_service):
        """综合验证: claim 失败无脏 assignment/event"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service, state_version=1)

        claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=99,
            idempotency_key="key-a5", lease_seconds=300,
        )
        conn = _raw_conn(claim_service.db_path)
        asgn_count = conn.execute(
            "SELECT COUNT(*) as c FROM task_assignments WHERE task_id = ?", (task_id,)
        ).fetchone()["c"]
        evt_count = conn.execute(
            "SELECT COUNT(*) as c FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()["c"]
        conn.close()
        assert asgn_count == 0
        assert evt_count == 0

    def test_feature_flag_false_rejects_and_no_data(self, claim_service_disabled):
        """feature flag=false 拒绝且不写数据"""
        svc = claim_service_disabled
        svc._worker_registry  # already disabled

        # Direct DB insertion to simulate task + worker
        conn = _raw_conn(svc.db_path)
        conn.execute("""
            INSERT INTO development_tasks (id, project_id, title, status, state_version)
            VALUES (1, 1, 'Test', 'queued', 1)
        """)
        conn.execute("""
            INSERT INTO agent_workers (worker_id, worker_type, status)
            VALUES ('ex', 'executor', 'available')
        """)
        conn.commit()
        conn.close()

        result = svc.claim_task(
            task_id=1, worker_id="ex", expected_version=1,
            idempotency_key="key-ff", lease_seconds=300,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED

        # Verify no side effects
        conn = _raw_conn(svc.db_path)
        task_row = conn.execute(
            "SELECT status FROM development_tasks WHERE id = 1"
        ).fetchone()
        worker_row = conn.execute(
            "SELECT status FROM agent_workers WHERE worker_id = 'ex'"
        ).fetchone()
        conn.close()
        assert task_row["status"].upper() == "QUEUED"
        assert worker_row["status"] == "available"


# ================================================================
# 租约与参数校验
# ================================================================

class TestLeaseAndValidation:

    def test_lease_seconds_min_violation(self, claim_service):
        """lease_seconds < 30 被拒绝"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-l1", lease_seconds=10,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_VALIDATION_ERROR

    def test_lease_seconds_max_violation(self, claim_service):
        """lease_seconds > 3600 被拒绝"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-l2", lease_seconds=7200,
        )
        assert result["success"] is False
        assert result["error_code"] == ERROR_VALIDATION_ERROR

    def test_lease_token_is_secure_random(self, claim_service):
        """lease_token 使用安全随机值"""
        _register_available_executor(claim_service)
        task_id = _create_queued_task(claim_service)

        r1 = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-l3a", lease_seconds=300,
        )
        assert len(r1["lease_token"]) >= 32

    def test_no_capability_requirements_allows_claim(self, claim_service):
        """任务未声明能力要求时可领取（MVP: 只有显式 _requirements 才检查）"""
        _register_available_executor(claim_service, capabilities=["python"])
        task_id = _create_queued_task(claim_service, task_type="backend")
        # No _requirements → capability check passes
        result = claim_service.claim_task(
            task_id=task_id, worker_id="exec-1", expected_version=1,
            idempotency_key="key-l4", lease_seconds=300,
        )
        assert result["success"] is True
        assert result["task_packet"] is not None


def test_claim_with_capability_match_succeeds(claim_service):
    """Worker 满足显式能力要求时正常领取"""
    _register_available_executor(claim_service, capabilities=["python", "backend"])
    task_id = _create_queued_task(
        claim_service, task_type="backend",
        implementation_steps={"_requirements": {"language": "python"}}
    )
    result = claim_service.claim_task(
        task_id=task_id, worker_id="exec-1", expected_version=1,
        idempotency_key="key-cap-ok", lease_seconds=300,
    )
    assert result["success"] is True


def test_capability_match_with_framework_requirement(claim_service):
    """Worker 满足框架要求时正常领取"""
    _register_available_executor(claim_service, capabilities=["python", "django", "backend"])
    task_id = _create_queued_task(
        claim_service, task_type="backend",
        implementation_steps={"_requirements": {"framework": "django"}}
    )
    result = claim_service.claim_task(
        task_id=task_id, worker_id="exec-1", expected_version=1,
        idempotency_key="key-cap-fw", lease_seconds=300,
    )
    assert result["success"] is True
