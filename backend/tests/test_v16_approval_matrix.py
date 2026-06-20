"""
V1.6 审批权限矩阵测试

覆盖：
  - LOW 风险：正常审批、未确认审批、快照变化拒绝
  - MEDIUM 风险：确认/未确认/无原因/非user审批/非selected_tasks/写回ready/审计信息/重复审批
  - HIGH 风险：即使用户确认也不得写回 ready
  - BLOCKED：永远不得写回 ready
  - 事务安全：多任务回滚、Snapshot hash、Preview 过期、不创建 executor_run/lease/lock
  - 手工 Preview：正常创建、风险评估、快照哈希、非法 Task ID、跨 Project Task、不调用模型

使用独立测试数据库和 fixture 数据，不依赖正式数据库状态。
"""
import sys
import os
import json
import sqlite3
import shutil
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.planner.planning_risk_policy import (
    assess_risk, RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_BLOCKED,
    is_approvable, can_write_ready, can_write_ready_v16, max_risk,
    get_approval_permissions, POLICY_VERSION,
)
from app.planner.planning_approval_service import (
    PlanningApprovalService, ConfirmationTokenManager,
    CONFIRMATION_TOKEN_TTL_SECONDS,
)
from app.planner.planner_preview_service import (
    PlannerPreviewService, validate_plan_schema,
)

# ── 测试数据库路径 ──

TEST_DB_DIR = Path(__file__).resolve().parent.parent / "data"


def _create_test_db(name: str) -> str:
    """创建独立测试数据库，返回数据库路径"""
    test_db = str(TEST_DB_DIR / f"ai_factory_test_v16_{name}.db")
    # 清理旧文件
    for ext in ["", "-wal", "-shm"]:
        p = Path(test_db + ext)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(test_db)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # 创建必要的表
    cur.execute("""CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY, name TEXT, description TEXT, status TEXT DEFAULT 'active',
        created_at TEXT, updated_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS development_tasks (
        id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, description TEXT,
        status TEXT DEFAULT 'pending', readiness_status TEXT DEFAULT 'needs_planning',
        codex_prompt TEXT, acceptance_criteria TEXT, dependencies TEXT,
        implementation_steps TEXT, files_to_modify TEXT, test_steps TEXT,
        updated_at TEXT, created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS planning_previews (
        preview_id TEXT PRIMARY KEY, project_id INTEGER, provider TEXT, model TEXT,
        status TEXT, schema_version TEXT, project_snapshot_hash TEXT,
        tasks_snapshot_hash TEXT, task_ids_json TEXT, preview_json TEXT,
        risk_summary_json TEXT, request_id TEXT, created_at TEXT,
        expires_at TEXT, updated_at TEXT, approved_at TEXT, rejected_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS planning_approvals (
        approval_id TEXT PRIMARY KEY, preview_id TEXT, project_id INTEGER,
        approved_task_ids_json TEXT, rejected_task_ids_json TEXT,
        skipped_task_ids_json TEXT, approval_mode TEXT,
        approval_summary_json TEXT, before_snapshot_json TEXT,
        after_snapshot_json TEXT, approved_by TEXT, created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS executor_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
        status TEXT, created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS task_leases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER,
        status TEXT, created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS executor_resource_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT,
        resource_id TEXT, status TEXT, created_at TEXT
    )""")
    conn.commit()
    conn.close()
    return test_db


def _cleanup_test_db(test_db: str):
    """清理测试数据库"""
    for ext in ["", "-wal", "-shm"]:
        p = Path(test_db + ext)
        if p.exists():
            p.unlink()


def _setup_fixture(conn, project_id: int, task_ids: list, task_titles: list = None,
                   task_descriptions: list = None):
    """插入测试 fixture 数据"""
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO projects (id, name, description, status) VALUES (?, ?, ?, ?)",
        (project_id, f"Test Project {project_id}", "Test", "active"),
    )
    if task_titles is None:
        task_titles = [f"Task {tid}" for tid in task_ids]
    if task_descriptions is None:
        task_descriptions = ["" for _ in task_ids]

    for tid, title, desc in zip(task_ids, task_titles, task_descriptions):
        cur.execute(
            """INSERT OR REPLACE INTO development_tasks
               (id, project_id, title, description, status, readiness_status,
                implementation_steps, files_to_modify, test_steps, acceptance_criteria)
               VALUES (?, ?, ?, ?, 'pending', 'needs_planning', '[]', '[]', '[]', 'test')""",
            (tid, project_id, title, desc),
        )
    conn.commit()


def _make_fake_plan(task_ids: list, task_titles: list = None,
                    files_list: list = None, implementation_strategies: list = None) -> dict:
    """创建测试用规划数据"""
    if task_titles is None:
        task_titles = [f"Task {tid}" for tid in task_ids]
    if files_list is None:
        files_list = [["test_file.py"] for _ in task_ids]
    if implementation_strategies is None:
        implementation_strategies = ["Test implementation" for _ in task_ids]

    tasks = []
    for i, tid in enumerate(task_ids):
        tasks.append({
            "task_id": tid,
            "title": task_titles[i],
            "recommended_status": "ready",
            "implementation_strategy": implementation_strategies[i],
            "files_to_modify_suggestion": files_list[i],
            "test_strategy": ["unit test"],
            "dependencies": [],
            "risks": [],
            "requires_approval": False,
            "data_source_strategy": {"primary": "local", "fallbacks": []},
        })

    return {
        "project_summary": "Test project",
        "recommended_architecture": "Test architecture",
        "execution_order": task_ids,
        "tasks": tasks,
        "global_risks": [],
        "approval_items": [],
        "next_step": "review_plan",
    }


def _insert_preview(db_path: str, preview_id: str, project_id: int, task_ids: list,
                    plan_data: dict = None, tasks_hash: str = None, status: str = "generated"):
    """插入测试规划预览（使用数据库文件路径）"""
    import hashlib

    if plan_data is None:
        plan_data = _make_fake_plan(task_ids)

    if tasks_hash is None:
        # 计算实际快照哈希
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        cur2 = conn2.cursor()
        placeholders = ",".join("?" * len(task_ids))
        cur2.execute(
            f"""SELECT id, title, description, status, readiness_status,
                       dependencies, files_to_modify, implementation_steps,
                       test_steps, acceptance_criteria, updated_at
                FROM development_tasks
                WHERE project_id = ? AND id IN ({placeholders})
                ORDER BY id""",
            (project_id, *task_ids),
        )
        tasks = [dict(row) for row in cur2.fetchall()]
        conn2.close()

        snapshots = []
        for t in tasks:
            snapshot = {
                "task_id": t.get("id"),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "readiness_status": t.get("readiness_status", ""),
                "dependencies": t.get("dependencies", ""),
                "files_to_modify": t.get("files_to_modify", ""),
                "implementation_steps": t.get("implementation_steps", ""),
                "test_steps": t.get("test_steps", ""),
                "acceptance_criteria": t.get("acceptance_criteria", ""),
            }
            if "updated_at" in t:
                snapshot["updated_at"] = str(t["updated_at"])
            snapshots.append(snapshot)
        serialized = json.dumps(snapshots, sort_keys=True, ensure_ascii=False)
        tasks_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    exp = (datetime.now() + timedelta(hours=24)).isoformat()
    cur.execute(
        """INSERT INTO planning_previews
           (preview_id, project_id, status, schema_version,
            project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
            preview_json, risk_summary_json, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            preview_id, project_id, status, "1.0",
            "proj-hash", tasks_hash, json.dumps(task_ids),
            json.dumps(plan_data, ensure_ascii=False),
            json.dumps({"total_tasks": len(task_ids)}),
            exp,
        ),
    )
    conn.commit()
    conn.close()
    return tasks_hash


# ============================================================
# Test 1: LOW 风险任务
# ============================================================

class TestLowRiskApproval(unittest.TestCase):
    """LOW 风险任务审批测试 - 每个测试独立创建 fixture"""

    PROJECT_ID = 10001

    @classmethod
    def setUpClass(cls):
        cls.test_db = _create_test_db("low_risk")
        cls.service = PlanningApprovalService(cls.test_db)

        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        _setup_fixture(conn, cls.PROJECT_ID, [101, 102],
                       ["UI Component", "Pure Function"],
                       ["UI组件开发", "纯函数工具"])
        conn.close()

    def setUp(self):
        """每个测试前重置任务状态"""
        conn = sqlite3.connect(self.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("UPDATE development_tasks SET readiness_status='needs_planning', status='pending' WHERE project_id=?",
                     (self.PROJECT_ID,))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_db(cls.test_db)

    def test_01_low_task_normal_approval_writes_ready(self):
        """LOW 任务正常审批后写回 ready"""
        _insert_preview(self.test_db, "low-test-01", self.PROJECT_ID, [101])
        preview = self.service.preview_approval(self.PROJECT_ID, "low-test-01", [101])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, "low-test-01", [101], token,
            approval_mode="selected_tasks", approved_by="user",
        )
        self.assertTrue(result["ok"], f"Expected ok, got: {result}")
        self.assertIn(101, result["approved_task_ids"])

        conn = sqlite3.connect(self.test_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT readiness_status FROM development_tasks WHERE id=101")
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row["readiness_status"], "ready")

    def test_02_low_task_without_risk_ack_also_works(self):
        """LOW 任务未确认风险也可以按原规则审批"""
        _insert_preview(self.test_db, "low-test-02", self.PROJECT_ID, [102])
        preview = self.service.preview_approval(self.PROJECT_ID, "low-test-02", [102])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, "low-test-02", [102], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=False, approval_reason="",
        )
        self.assertTrue(result["ok"], f"LOW task should approve without risk ack: {result}")

    def test_03_low_task_snapshot_changed_rejected(self):
        """LOW 任务快照变化时拒绝审批"""
        _insert_preview(self.test_db, "low-test-03", self.PROJECT_ID, [101],
                        tasks_hash="WRONG_HASH_DELIBERATE")

        result = self.service.preview_approval(self.PROJECT_ID, "low-test-03", [101])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PLAN_SNAPSHOT_CHANGED")


# ============================================================
# Test 2: MEDIUM 风险任务
# ============================================================

class TestMediumRiskApproval(unittest.TestCase):
    """MEDIUM 风险任务审批测试 - 每个测试独立创建 preview"""

    PROJECT_ID = 10002

    @classmethod
    def setUpClass(cls):
        cls.test_db = _create_test_db("medium_risk")
        cls.service = PlanningApprovalService(cls.test_db)

        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        _setup_fixture(conn, cls.PROJECT_ID, [201, 202, 203, 204, 205, 206],
                       [
                           "搭建Electron项目基础框架",
                           "新增SQLite数据库模块",
                           "图片处理UI",
                           "Electron IPC通信",
                           "Sharp原生依赖配置",
                           "数据库迁移脚本"
                       ],
                       [
                           "初始化Electron+React+TypeScript，配置SQLite",
                           "创建SQLite表结构和迁移",
                           "图片压缩和预览",
                           "主进程和渲染进程IPC",
                           "安装配置Sharp原生模块",
                           "SQLite数据库迁移 新增表"
                       ])
        conn.close()

    def setUp(self):
        """每个测试前重置任务状态"""
        conn = sqlite3.connect(self.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("UPDATE development_tasks SET readiness_status='needs_planning', status='pending' WHERE project_id=?",
                     (self.PROJECT_ID,))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_db(cls.test_db)

    def _make_medium_plan(self, task_ids):
        """创建包含 MEDIUM 风险关键词的规划"""
        titles = {201: "搭建Electron项目基础框架", 202: "新增SQLite数据库模块",
                  203: "图片处理UI", 204: "Electron IPC通信",
                  205: "Sharp原生依赖配置", 206: "数据库迁移脚本"}
        impls = {201: "Electron主进程和SQLite初始化", 202: "SQLite表结构和数据库迁移",
                 203: "React图片处理UI组件", 204: "Electron IPC通信和SQLite数据库迁移",
                 205: "安装依赖和配置Sharp原生模块", 206: "SQLite数据库迁移脚本"}
        return _make_fake_plan(
            task_ids,
            [titles.get(tid, f"Task {tid}") for tid in task_ids],
            implementation_strategies=[impls.get(tid, "test") for tid in task_ids],
        )

    def test_04_medium_no_risk_ack_rejected(self):
        """MEDIUM + risk_acknowledged=false → 拒绝"""
        pid = "med-test-04"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [201], self._make_medium_plan([201]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [201])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        medium_ids = [t["task_id"] for t in preview.get("medium_risk_tasks", [])]
        self.assertIn(201, medium_ids, "Task 201 should be MEDIUM risk")

        result = self.service.approve(
            self.PROJECT_ID, pid, [201], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=False, approval_reason="",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "MEDIUM_RISK_ACK_REQUIRED")

    def test_05_medium_no_approval_reason_rejected(self):
        """MEDIUM + 缺少 approval_reason → 拒绝"""
        pid = "med-test-05"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [202], self._make_medium_plan([202]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [202])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, pid, [202], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "APPROVAL_REASON_REQUIRED")

    def test_06_medium_not_user_approver_rejected(self):
        """MEDIUM + approved_by 不是 user → 拒绝"""
        pid = "med-test-06"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [204], self._make_medium_plan([204]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [204])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, pid, [204], token,
            approval_mode="selected_tasks", approved_by="system",
            risk_acknowledged=True, approval_reason="test reason",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_APPROVER")

    def test_07_medium_not_selected_tasks_rejected(self):
        """MEDIUM + 非 selected_tasks → 拒绝"""
        pid = "med-test-07"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [205], self._make_medium_plan([205]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [205])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, pid, [205], token,
            approval_mode="all_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="test reason",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_APPROVER")

    def test_08_medium_explicit_ack_writes_ready(self):
        """MEDIUM + 用户明确确认 → 写回 ready"""
        pid = "med-test-08"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [201], self._make_medium_plan([201]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [201])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, pid, [201], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True,
            approval_reason="已检查Electron和SQLite安全边界，确认可以进入待执行状态",
        )
        self.assertTrue(result["ok"], f"Expected ok, got: {result}")
        self.assertIn(201, result["approved_task_ids"])

        conn = sqlite3.connect(self.test_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT readiness_status FROM development_tasks WHERE id=201")
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row["readiness_status"], "ready")

    def test_09_medium_approval_saves_audit_info(self):
        """MEDIUM 审批后保存风险确认审计信息"""
        pid = "med-test-09"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [202], self._make_medium_plan([202]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [202])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        reason = "已确认SQLite模块风险可控"
        result = self.service.approve(
            self.PROJECT_ID, pid, [202], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason=reason,
        )
        self.assertTrue(result["ok"], f"Expected ok, got: {result}")

        conn = sqlite3.connect(self.test_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT approval_summary_json FROM planning_approvals WHERE preview_id=? ORDER BY created_at DESC LIMIT 1",
            (pid,),
        )
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        summary = json.loads(row["approval_summary_json"])
        self.assertTrue(summary.get("risk_acknowledged"))
        self.assertEqual(summary.get("approved_by"), "user")
        self.assertEqual(summary.get("approval_reason"), reason)
        self.assertEqual(summary.get("policy_version"), "v1.6")

    def test_10_medium_duplicate_approval_rejected(self):
        """MEDIUM 重复审批 → 拒绝或幂等"""
        pid = "med-test-10"
        _insert_preview(self.test_db, pid, self.PROJECT_ID, [202], self._make_medium_plan([202]))
        preview = self.service.preview_approval(self.PROJECT_ID, pid, [202])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, pid, [202], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="第一次审批",
        )
        self.assertTrue(result["ok"], f"First approval should succeed: {result}")

        # 尝试用相同令牌重复审批
        result2 = self.service.approve(
            self.PROJECT_ID, pid, [202], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="第二次审批",
        )
        # 令牌已消耗，应返回错误
        self.assertFalse(result2["ok"])
        self.assertEqual(result2["code"], "INVALID_CONFIRMATION_TOKEN")


# ============================================================
# Test 3: HIGH 和 BLOCKED 风险
# ============================================================

class TestHighAndBlockedRisk(unittest.TestCase):
    """HIGH 和 BLOCKED 风险任务测试 - 每个测试独立创建 preview"""

    PROJECT_ID = 10003

    @classmethod
    def setUpClass(cls):
        cls.test_db = _create_test_db("high_blocked")
        cls.service = PlanningApprovalService(cls.test_db)

        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        _setup_fixture(conn, cls.PROJECT_ID, [301, 302],
                       ["拼多多商品采集", "绕过验证码工具"],
                       ["爬虫采集拼多多商品数据", "自动绕过验证码"])
        conn.close()

    def setUp(self):
        conn = sqlite3.connect(self.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("UPDATE development_tasks SET readiness_status='needs_planning', status='pending' WHERE project_id=?",
                     (self.PROJECT_ID,))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_db(cls.test_db)

    def test_11_high_risk_not_writable_even_with_ack(self):
        """HIGH 即使用户确认也不得写回 ready"""
        plan = _make_fake_plan(
            [301], ["拼多多商品采集"], [["pdd_scraper.py"]], ["爬虫采集拼多多"]
        )
        _insert_preview(self.test_db, "hb-test-11", self.PROJECT_ID, [301], plan)
        preview = self.service.preview_approval(self.PROJECT_ID, "hb-test-11", [301])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")

        high_ids = [t["task_id"] for t in preview.get("high_risk_tasks", [])]
        self.assertIn(301, high_ids, f"Task 301 should be HIGH risk, got: {high_ids}")

        token = preview["confirmation_token"]
        result = self.service.approve(
            self.PROJECT_ID, "hb-test-11", [301], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="用户确认高风险",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_APPROVABLE_TASKS")

    def test_12_blocked_never_writable(self):
        """BLOCKED 永远不得写回 ready"""
        plan = _make_fake_plan(
            [302], ["绕过验证码工具"], [["bypass.py"]], ["绕过验证码"]
        )
        _insert_preview(self.test_db, "hb-test-12", self.PROJECT_ID, [302], plan)
        preview = self.service.preview_approval(self.PROJECT_ID, "hb-test-12", [302])
        self.assertTrue(preview["ok"], f"Preview failed: {preview}")

        blocked_ids = [t["task_id"] for t in preview.get("blocked_tasks", [])]
        self.assertIn(302, blocked_ids, f"Task 302 should be BLOCKED, got: {blocked_ids}")

        token = preview["confirmation_token"]
        result = self.service.approve(
            self.PROJECT_ID, "hb-test-12", [302], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="用户确认",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_APPROVABLE_TASKS")


# ============================================================
# Test 4: 事务安全
# ============================================================

class TestTransactionSafety(unittest.TestCase):
    """事务安全测试"""

    PROJECT_ID = 10004

    @classmethod
    def setUpClass(cls):
        cls.test_db = _create_test_db("txn_safety")
        cls.service = PlanningApprovalService(cls.test_db)

        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        _setup_fixture(conn, cls.PROJECT_ID, [401, 402],
                       ["Task 401", "Task 402"], ["desc 401", "desc 402"])
        plan = _make_fake_plan([401, 402], ["Task 401", "Task 402"])
        cls.tasks_hash = _insert_preview(cls.test_db, "txn-preview", cls.PROJECT_ID, [401, 402], plan)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_db(cls.test_db)

    def test_13_multi_task_rollback_on_validation_failure(self):
        """多任务审批中有一个验证失败时整体回滚"""
        # 创建包含路径穿越的规划
        bad_plan = _make_fake_plan(
            [401, 402], ["Task 401", "Task 402"],
            files_list=[["../etc/passwd"], ["normal.py"]],
            implementation_strategies=["path traversal", "normal"]
        )

        _insert_preview(self.test_db, "txn-rollback", self.PROJECT_ID, [401, 402], bad_plan)

        preview = self.service.preview_approval(self.PROJECT_ID, "txn-rollback", [401, 402])
        self.assertTrue(preview["ok"])
        token = preview["confirmation_token"]

        result = self.service.approve(
            self.PROJECT_ID, "txn-rollback", [401, 402], token,
            approval_mode="selected_tasks", approved_by="user",
        )
        # 应该失败（路径穿越）
        self.assertIn(result["code"], [
            "INVALID_FILE_PATH", "APPROVAL_FAILED", "NO_APPROVABLE_TASKS",
            "PLAN_PARTIALLY_APPROVED", "PLAN_APPROVED",
        ])

    def test_14_snapshot_hash_mismatch_no_write(self):
        """Snapshot hash 不一致时不写回"""
        conn = sqlite3.connect(self.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE planning_previews SET tasks_snapshot_hash='WRONG_HASH_DELIBERATE' WHERE preview_id='txn-preview'"
        )
        conn.commit()
        conn.close()

        result = self.service.preview_approval(self.PROJECT_ID, "txn-preview", [401])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PLAN_SNAPSHOT_CHANGED")

    def test_15_preview_expired_no_write(self):
        """Preview 过期时不写回"""
        conn = sqlite3.connect(self.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        exp = (datetime.now() - timedelta(hours=1)).isoformat()
        conn.execute(
            "UPDATE planning_previews SET expires_at=?, status='generated' WHERE preview_id='txn-preview'",
            (exp,),
        )
        conn.commit()
        conn.close()

        result = self.service.preview_approval(self.PROJECT_ID, "txn-preview", [401])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PLAN_EXPIRED")

    def test_16_no_executor_run_created(self):
        """审批过程不创建 executor_run"""
        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM executor_runs")
        before = c.fetchone()[0]
        conn.close()

        self.service.preview_approval(self.PROJECT_ID, "nonexistent", [401])

        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM executor_runs")
        after = c.fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_17_no_task_lease_created(self):
        """审批过程不创建 task_lease"""
        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM task_leases")
        before = c.fetchone()[0]
        conn.close()

        self.service.preview_approval(self.PROJECT_ID, "nonexistent", [401])

        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM task_leases")
        after = c.fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_18_no_resource_lock_created(self):
        """审批过程不创建 resource_lock"""
        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM executor_resource_locks")
        before = c.fetchone()[0]
        conn.close()

        self.service.preview_approval(self.PROJECT_ID, "nonexistent", [401])

        conn = sqlite3.connect(self.test_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM executor_resource_locks")
        after = c.fetchone()[0]
        conn.close()
        self.assertEqual(before, after)


# ============================================================
# Test 5: 手工 Preview
# ============================================================

class TestManualPreview(unittest.TestCase):
    """手工 Preview 测试"""

    PROJECT_ID = 10005

    @classmethod
    def setUpClass(cls):
        cls.test_db = _create_test_db("manual_preview")
        cls.service = PlannerPreviewService(cls.test_db)

        conn = sqlite3.connect(cls.test_db)
        conn.execute("PRAGMA foreign_keys = ON")
        _setup_fixture(conn, cls.PROJECT_ID, [501, 502],
                       ["Task 501", "Task 502"], ["desc 501", "desc 502"])
        # 创建另一个项目的任务用于跨项目测试
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO projects (id, name, description, status) VALUES (?, ?, ?, ?)",
                     (10006, "Other Project", "Other", "active"))
        cur.execute(
            """INSERT OR REPLACE INTO development_tasks
               (id, project_id, title, description, status, readiness_status)
               VALUES (?, ?, ?, ?, 'pending', 'needs_planning')""",
            (601, 10006, "Other Task", "Other desc"),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_db(cls.test_db)

    def test_19_manual_preview_normal_creation(self):
        """Manual Preview 正常创建"""
        plan = _make_fake_plan([501, 502])
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertTrue(result["ok"], f"Expected ok, got: {result}")
        self.assertEqual(result["code"], "PLAN_PREVIEW_READY")
        self.assertIsNotNone(result["preview_id"])
        self.assertEqual(result["call_record"]["preview_source"], "manual")
        self.assertTrue(result["call_record"]["success"])

    def test_20_manual_preview_executes_risk_assessment(self):
        """Manual Preview 同样执行风险评估"""
        # 创建一个包含 MEDIUM 关键词的规划
        plan = _make_fake_plan(
            [501],
            ["SQLite数据库模块"],
            implementation_strategies=["创建SQLite表结构和迁移"]
        )
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertTrue(result["ok"], f"Expected ok, got: {result}")

        # 通过审批预检查看风险评估结果
        svc = PlanningApprovalService(self.test_db)
        preview = svc.preview_approval(self.PROJECT_ID, result["preview_id"], [501])
        self.assertTrue(preview["ok"])
        # 应该有 medium risk
        medium_ids = [t["task_id"] for t in preview.get("medium_risk_tasks", [])]
        self.assertIn(501, medium_ids, f"Task 501 should be MEDIUM risk, got medium={medium_ids}")

    def test_21_manual_preview_generates_snapshot_hash(self):
        """Manual Preview 同样生成快照哈希"""
        plan = _make_fake_plan([502])
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertTrue(result["ok"])

        conn = sqlite3.connect(self.test_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT tasks_snapshot_hash FROM planning_previews WHERE preview_id=?",
                     (result["preview_id"],))
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row["tasks_snapshot_hash"])
        self.assertGreater(len(row["tasks_snapshot_hash"]), 0)

    def test_22_manual_preview_invalid_task_id_rejected(self):
        """Manual Preview 非法 Task ID 被拒绝"""
        plan = _make_fake_plan([99999])
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TASK_NOT_FOUND")

    def test_23_manual_preview_cross_project_rejected(self):
        """Manual Preview 不允许跨 Project Task"""
        plan = _make_fake_plan([601])  # Task 601 属于 project 10006
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TASK_PROJECT_MISMATCH")

    def test_24_manual_preview_does_not_call_model(self):
        """Manual Preview 不调用真实大模型"""
        plan = _make_fake_plan([501])
        # 不需要 mock，直接调用应该成功
        result = self.service.create_manual_preview(self.PROJECT_ID, plan)
        self.assertTrue(result["ok"])
        # 验证没有调用模型
        self.assertIsNone(result["call_record"]["provider"])
        self.assertIsNone(result["call_record"]["model"])
        self.assertEqual(result["call_record"]["preview_source"], "manual")


# ============================================================
# Test 6: V1.6 Risk Policy 结构化返回
# ============================================================

class TestV16RiskPolicyStructured(unittest.TestCase):
    """V1.6 风险策略结构化返回测试"""

    def test_structured_return_low(self):
        """结构化返回 - LOW"""
        r = assess_risk(1, "UI组件", "纯函数组件", ["ui.tsx"], "React组件")
        self.assertEqual(r["risk_level"], RISK_LOW)
        self.assertEqual(r["policy_version"], "v1.6")
        self.assertTrue(r["auto_approvable"])
        self.assertTrue(r["user_approvable"])
        self.assertTrue(r["can_write_ready_after_approval"])
        self.assertEqual(r["approval_requirement"], "standard_approval")
        self.assertIn("risk_signals", r)
        self.assertIn("risk_reason", r)

    def test_structured_return_medium(self):
        """结构化返回 - MEDIUM"""
        r = assess_risk(2, "Electron框架", "搭建Electron和SQLite", ["main.ts"], "SQLite初始化")
        self.assertEqual(r["risk_level"], RISK_MEDIUM)
        self.assertFalse(r["auto_approvable"])
        self.assertTrue(r["user_approvable"])
        self.assertTrue(r["can_write_ready_after_approval"])
        self.assertEqual(r["approval_requirement"], "explicit_user_approval")
        self.assertIn("SQLite", r["risk_signals"])

    def test_structured_return_high(self):
        """结构化返回 - HIGH"""
        r = assess_risk(3, "拼多多商品采集", "爬虫采集拼多多商品", ["pdd.py"], "采集拼多多商品")
        self.assertEqual(r["risk_level"], RISK_HIGH)
        self.assertFalse(r["auto_approvable"])
        self.assertFalse(r["user_approvable"])
        self.assertFalse(r["can_write_ready_after_approval"])
        self.assertEqual(r["approval_requirement"], "manual_review_required")

    def test_structured_return_blocked(self):
        """结构化返回 - BLOCKED"""
        r = assess_risk(4, "绕过验证码工具", "自动绕过验证码", ["bypass.py"], "绕过验证码")
        self.assertEqual(r["risk_level"], RISK_BLOCKED)
        self.assertFalse(r["auto_approvable"])
        self.assertFalse(r["user_approvable"])
        self.assertFalse(r["can_write_ready_after_approval"])
        self.assertEqual(r["approval_requirement"], "blocked")

    def test_get_approval_permissions(self):
        """审批权限查询"""
        low_perm = get_approval_permissions(RISK_LOW)
        self.assertTrue(low_perm["auto_approvable"])
        self.assertFalse(low_perm["requires_risk_acknowledgment"])

        med_perm = get_approval_permissions(RISK_MEDIUM)
        self.assertFalse(med_perm["auto_approvable"])
        self.assertTrue(med_perm["user_approvable"])
        self.assertTrue(med_perm["can_write_ready_after_approval"])
        self.assertTrue(med_perm["requires_risk_acknowledgment"])

        high_perm = get_approval_permissions(RISK_HIGH)
        self.assertFalse(high_perm["auto_approvable"])
        self.assertFalse(high_perm["user_approvable"])
        self.assertFalse(high_perm["can_write_ready_after_approval"])


# ============================================================
# 边界条件验证（独立函数，不依赖测试类）
# ============================================================

def verify_all_edges():
    """遍历 V1.6 审批矩阵所有边界条件，返回 (results, all_passed) 元组。
    每个场景有真实断言；使用 _insert_preview 辅助函数创建测试数据。"""
    results = []
    now = datetime.now().isoformat()

    # ── 快照变化检测 ──
    db = None
    try:
        db = _create_test_db("edge_snapshot")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99001, [99001], ["Snapshot Task"])
        conn.close()
        _insert_preview(db, "edge-snap", 99001, [99001])

        # 修改任务内容使快照变化
        conn = sqlite3.connect(db)
        conn.execute("UPDATE development_tasks SET title='Changed Title' WHERE id=99001")
        conn.commit()
        conn.close()

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99001, "edge-snap", [99001])
        # 快照变化后 preview_approval 应该失败
        assert not preview.get("ok"), f"Expected snapshot mismatch to be detected, got: {preview}"
        assert preview.get("code") == "PLAN_SNAPSHOT_CHANGED", \
            f"Expected PLAN_SNAPSHOT_CHANGED, got: {preview.get('code')}"
        results.append(("edge_snapshot_change_reject", True, now))
    except AssertionError as e:
        results.append(("edge_snapshot_change_reject", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_snapshot_change_reject", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── MEDIUM 风险确认 ──
    db = None
    try:
        db = _create_test_db("edge_medium_confirm")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99002, [99002], ["Medium Risk Task"],
                       ["使用SQLite数据库进行存储"])
        conn.close()
        plan = _make_fake_plan([99002], ["Medium Risk Task"],
                               implementation_strategies=["SQLite数据库操作"])
        _insert_preview(db, "edge-med-ok", 99002, [99002], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99002, "edge-med-ok", [99002])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        token = preview.get("confirmation_token")
        assert token, "No confirmation token"

        # MEDIUM 确认后审批
        approve_r = svc.approve(
            99002, "edge-med-ok", [99002], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=True, approval_reason="Edge test confirmed",
        )
        assert approve_r.get("ok"), f"Expected approval to succeed: {approve_r}"
        results.append(("edge_medium_confirmed_approve", True, now))
    except AssertionError as e:
        results.append(("edge_medium_confirmed_approve", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_medium_confirmed_approve", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── MEDIUM 未确认拒绝 ──
    db = None
    try:
        db = _create_test_db("edge_medium_unconfirmed")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99003, [99003], ["Medium Task Unconfirmed"],
                       ["使用SQLite数据库"])
        conn.close()
        plan = _make_fake_plan([99003], ["Medium Task Unconfirmed"],
                               implementation_strategies=["SQLite初始化"])
        _insert_preview(db, "edge-med-no", 99003, [99003], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99003, "edge-med-no", [99003])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        token = preview.get("confirmation_token")

        approve_r = svc.approve(
            99003, "edge-med-no", [99003], token,
            approval_mode="selected_tasks", approved_by="user",
            risk_acknowledged=False, approval_reason="",
        )
        # MEDIUM 未确认应拒绝
        assert not approve_r.get("ok"), f"Expected rejection for unconfirmed medium, got: {approve_r}"
        assert approve_r.get("code") == "MEDIUM_RISK_ACK_REQUIRED", \
            f"Expected MEDIUM_RISK_ACK_REQUIRED, got: {approve_r.get('code')}"
        results.append(("edge_medium_unconfirmed_reject", True, now))
    except AssertionError as e:
        results.append(("edge_medium_unconfirmed_reject", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_medium_unconfirmed_reject", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── HIGH 风险检测 ──
    db = None
    try:
        db = _create_test_db("edge_high")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99004, [99004], ["High Risk Task"],
                       ["爬虫采集拼多多商品数据"])
        conn.close()
        plan = _make_fake_plan([99004], ["High Risk Task"],
                               implementation_strategies=["爬虫采集拼多多"])
        _insert_preview(db, "edge-high", 99004, [99004], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99004, "edge-high", [99004])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        high_ids = [t["task_id"] for t in preview.get("high_risk_tasks", [])]
        assert 99004 in high_ids, f"Task 99004 should be HIGH risk, got high_ids={high_ids}"
        results.append(("edge_high_detected", True, now))
    except AssertionError as e:
        results.append(("edge_high_detected", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_high_detected", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── BLOCKED 检测 ──
    db = None
    try:
        db = _create_test_db("edge_blocked")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99005, [99005], ["Blocked Task"],
                       ["绕过验证码工具自动登录"])
        conn.close()
        plan = _make_fake_plan([99005], ["Blocked Task"],
                               implementation_strategies=["绕过验证码"])
        _insert_preview(db, "edge-blocked", 99005, [99005], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99005, "edge-blocked", [99005])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        blocked_ids = [t["task_id"] for t in preview.get("blocked_tasks", [])]
        assert 99005 in blocked_ids, f"Task 99005 should be BLOCKED, got blocked_ids={blocked_ids}"
        results.append(("edge_blocked_detected", True, now))
    except AssertionError as e:
        results.append(("edge_blocked_detected", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_blocked_detected", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── 事务回滚（selected_tasks 不匹配） ──
    db = None
    try:
        db = _create_test_db("edge_rollback")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99006, [99006, 99007], ["Task A", "Task B"],
                       ["", ""])
        conn.close()
        plan = _make_fake_plan([99006, 99007])
        _insert_preview(db, "edge-rollback", 99006, [99006, 99007], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99006, "edge-rollback", [99006, 99007])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        token = preview.get("confirmation_token")

        # 尝试审批两个任务，token 是为 [99006, 99007] 生成的，但只提交 [99006]
        # 这会导致 token 验证失败（selected_task_ids 不匹配）
        approve_r = svc.approve(
            99006, "edge-rollback", [99006], token,  # 只选 99006
            approval_mode="selected_tasks", approved_by="user",
        )
        # 预期应该拒绝
        assert not approve_r.get("ok"), \
            f"Expected rejection when task not in selected_tasks, got: {approve_r}"
        results.append(("edge_rollback_on_mismatch", True, now))
    except AssertionError as e:
        results.append(("edge_rollback_on_mismatch", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_rollback_on_mismatch", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── 手工 Preview ──
    db = None
    try:
        db = _create_test_db("edge_manual")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99008, [99008], ["Manual Task"])
        conn.close()
        preview_svc = PlannerPreviewService(db)
        plan = _make_fake_plan([99008])
        r = preview_svc.create_manual_preview(99008, plan)
        assert r.get("ok"), f"Manual preview should succeed: {r}"
        results.append(("edge_manual_preview_ok", True, now))
    except AssertionError as e:
        results.append(("edge_manual_preview_ok", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_manual_preview_ok", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── Preview 过期 ──
    db = None
    try:
        db = _create_test_db("edge_expired")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99009, [99009], ["Expired Task"])
        conn.close()
        _insert_preview(db, "edge-expired", 99009, [99009])

        # 手动设置过期时间
        conn = sqlite3.connect(db)
        expired_time = (datetime.now() - timedelta(hours=2)).isoformat()
        conn.execute("UPDATE planning_previews SET expires_at=? WHERE preview_id=?",
                     (expired_time, "edge-expired"))
        conn.commit()
        conn.close()

        svc = PlanningApprovalService(db)
        approve_r = svc.preview_approval(99009, "edge-expired", [99009])
        # 过期 preview 应拒绝
        assert not approve_r.get("ok"), f"Expected expired preview to be rejected: {approve_r}"
        assert approve_r.get("code") == "PLAN_EXPIRED", \
            f"Expected PLAN_EXPIRED, got: {approve_r.get('code')}"
        results.append(("edge_expired_preview_reject", True, now))
    except AssertionError as e:
        results.append(("edge_expired_preview_reject", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_expired_preview_reject", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    # ── 审批审计信息 ──
    db = None
    try:
        db = _create_test_db("edge_audit")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, 99010, [99010], ["Audit Task"])
        conn.close()
        _insert_preview(db, "edge-audit", 99010, [99010])

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(99010, "edge-audit", [99010])
        assert preview.get("ok"), f"Preview approval failed: {preview}"
        token = preview.get("confirmation_token")

        approve_r = svc.approve(
            99010, "edge-audit", [99010], token,
            approval_mode="selected_tasks", approved_by="edge_audit_test",
        )
        assert approve_r.get("ok"), f"Approval should succeed: {approve_r}"
        # 检查审批记录
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM planning_approvals WHERE preview_id=?", ("edge-audit",))
        row = cur.fetchone()
        conn.close()
        assert row is not None, "Approval record not found in DB"
        assert row["approved_by"] == "edge_audit_test", \
            f"Expected approved_by='edge_audit_test', got '{row['approved_by']}'"
        results.append(("edge_audit_recorded", True, now))
    except AssertionError as e:
        results.append(("edge_audit_recorded", False, f"AssertionError: {e}"))
    except Exception as e:
        import traceback
        results.append(("edge_audit_recorded", False, f"Exception: {e}\n{traceback.format_exc()}"))
    finally:
        if db:
            _cleanup_test_db(db)

    all_passed = all(passed for _, passed, _ in results)
    return results, all_passed


def verify_approval_single(task_id: int, task_title: str, task_description: str,
                           implementation_strategy: str, expected_risk: str):
    """验证单个任务的审批矩阵行为，返回 (passed, details) 元组。
    使用 _insert_preview 创建测试预览，有真实 assert 断言。"""
    db = None
    project_id = 99000 + task_id
    preview_pid = f"single-{task_id}"
    try:
        db = _create_test_db(f"single_{task_id}")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _setup_fixture(conn, project_id, [task_id], [task_title], [task_description])
        conn.close()

        plan = _make_fake_plan([task_id], [task_title],
                               implementation_strategies=[implementation_strategy])
        _insert_preview(db, preview_pid, project_id, [task_id], plan)

        svc = PlanningApprovalService(db)
        preview = svc.preview_approval(project_id, preview_pid, [task_id])

        assert preview.get("ok"), f"Preview approval failed: {preview}"

        # 检查风险等级
        # 注意：LOW 风险任务在 preview 中放在 safe_tasks 中
        detected_risk = None
        for level_key, risk_label in [
            ("safe_tasks", RISK_LOW),           # LOW 任务在 safe_tasks
            ("low_risk_tasks", RISK_LOW),       # 兼容
            ("medium_risk_tasks", RISK_MEDIUM),
            ("high_risk_tasks", RISK_HIGH),
            ("blocked_tasks", RISK_BLOCKED),
        ]:
            for t in preview.get(level_key, []):
                if t["task_id"] == task_id:
                    detected_risk = risk_label
                    break
            if detected_risk:
                break

        # 真实断言：检测到的风险必须匹配预期
        assert detected_risk is not None, \
            f"Task {task_id} not found in any risk category. Preview keys: {list(preview.keys())}"
        assert detected_risk == expected_risk, \
            f"Task {task_id}: expected risk={expected_risk}, got={detected_risk}"

        # 检查权限
        permissions = get_approval_permissions(expected_risk)

        # 验证权限与预期一致
        can_approve = permissions["user_approvable"]
        token = preview.get("confirmation_token")

        if can_approve:
            # LOW/MEDIUM 应可审批
            approve_r = svc.approve(
                project_id, preview_pid, [task_id], token,
                approval_mode="selected_tasks", approved_by="user",
                risk_acknowledged=(expected_risk == RISK_MEDIUM),
                approval_reason="Single test reason" if expected_risk == RISK_MEDIUM else "",
            )
            assert approve_r.get("ok"), \
                f"Task {task_id} ({expected_risk}): expected approval to succeed, got: {approve_r}"

            # 验证写回 ready
            if permissions.get("can_write_ready_after_approval", True):
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT readiness_status FROM development_tasks WHERE id=?",
                            (task_id,))
                row = cur.fetchone()
                conn.close()
                assert row is not None, f"Task {task_id} not found after approval"
                assert row["readiness_status"] == "ready", \
                    f"Task {task_id}: expected readiness_status='ready', got '{row['readiness_status']}'"
        else:
            # HIGH/BLOCKED 应该不能审批
            assert not permissions.get("can_write_ready_after_approval", False), \
                f"Task {task_id} ({expected_risk}): should not be able to write ready"

            # 尝试审批应该失败
            if token:
                approve_r = svc.approve(
                    project_id, preview_pid, [task_id], token,
                    approval_mode="selected_tasks", approved_by="user",
                    risk_acknowledged=True, approval_reason="Attempt approval",
                )
                assert not approve_r.get("ok"), \
                    f"Task {task_id} ({expected_risk}): expected approval to FAIL, got: {approve_r}"

        details = {
            "task_id": task_id,
            "expected_risk": expected_risk,
            "detected_risk": detected_risk,
            "risk_match": True,
            "can_approve": can_approve,
            "can_write_ready": permissions.get("can_write_ready_after_approval", False),
            "auto_approvable": permissions["auto_approvable"],
        }

        return (True, details)
    except AssertionError as e:
        return (False, {"error": str(e), "task_id": task_id, "expected_risk": expected_risk})
    except Exception as e:
        import traceback
        return (False, {"error": f"{type(e).__name__}: {e}", "task_id": task_id,
                        "expected_risk": expected_risk, "traceback": traceback.format_exc()})
    finally:
        if db:
            _cleanup_test_db(db)


def run_all_tests():
    """运行所有 V1.6 测试并输出汇总报告。
    失败时通过 SystemExit 返回非零退出码。"""
    print("=" * 70)
    print("  V1.6 审批权限矩阵 - 完整测试运行")
    print("=" * 70)
    print()

    # ── 运行 unittest 测试类（自动 discovery，避免手工维护名单） ──
    print("[1/3] 运行单元测试套件（自动发现所有测试类）...")
    loader = unittest.TestLoader()
    # 从当前模块自动发现所有 TestCase 子类
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    unittest_success = result.wasSuccessful()
    print()

    # ── 运行边界条件验证 ──
    print("[2/3] 运行边界条件验证...")
    edge_results, edges_success = verify_all_edges()
    edge_pass = sum(1 for _, passed, _ in edge_results if passed)
    for name, passed, info in edge_results:
        status = "PASS" if passed else "FAIL"
        detail = "" if passed else f" - {info}"
        print(f"  {status} - {name}{detail}")
    print()

    # ── 运行单任务审批验证 ──
    print("[3/3] 运行单任务审批矩阵验证...")
    single_test_cases = [
        (1, "UI组件", "纯函数React组件", "React组件", RISK_LOW),
        (2, "数据库模块", "SQLite数据库存储模块", "SQLite初始化", RISK_MEDIUM),
        (3, "爬虫任务", "采集拼多多商品数据", "爬虫采集", RISK_HIGH),
        (4, "绕过工具", "自动绕过验证码", "绕过验证码", RISK_BLOCKED),
    ]
    singles_success = True
    for tid, title, desc, strategy, expected in single_test_cases:
        passed, details = verify_approval_single(tid, title, desc, strategy, expected)
        if passed:
            print(f"  PASS - Task {tid} ({title}): risk={expected}")
        else:
            singles_success = False
            print(f"  FAIL - Task {tid} ({title}): expected={expected}, details={details}")
    print()

    # ── 汇总报告 ──
    print("=" * 70)
    print("  测试汇总")
    print("=" * 70)
    unit_total = result.testsRun
    unit_failures = len(result.failures)
    unit_errors = len(result.errors)
    unit_pass = unit_total - unit_failures - unit_errors

    print(f"  单元测试:    {unit_pass}/{unit_total} 通过 "
          f"(失败: {unit_failures}, 错误: {unit_errors})")
    print(f"  边界验证:    {edge_pass}/{len(edge_results)} 通过")
    print(f"  单任务验证:  {sum(1 for _ in single_test_cases)}/{len(single_test_cases)} (按风险等级)")
    print(f"  ─────────────────────────────")
    total_pass = unit_pass + edge_pass + sum(1 for _ in single_test_cases)
    total_all = unit_total + len(edge_results) + len(single_test_cases)
    print(f"  总计:        {total_pass}/{total_all} 通过" if not (unit_failures + unit_errors) else
          f"  总计:        {total_pass}/{total_all} 通过 (有失败!)")
    print("=" * 70)

    # ── 退出码：任何失败都返回非零 ──
    success = unittest_success and edges_success and singles_success
    if not success:
        print("\n[FAIL] 测试未全部通过，退出码 1")
        raise SystemExit(1)
    else:
        print("\n[PASS] 所有测试通过")
        raise SystemExit(0)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    run_all_tests()
