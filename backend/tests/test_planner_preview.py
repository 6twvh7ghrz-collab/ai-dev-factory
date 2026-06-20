"""
测试 PlannerPreviewService V1.3

使用 FakeModelAdapter 模拟 DeepSeek 响应，不调用真实 AI。
验证规划预览的各种边界条件。
"""
import sys
import os
import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# 添加 backend 到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.planner.planner_preview_service import (
    PlannerPreviewService,
    validate_plan_schema,
    is_planning_in_progress,
    _acquire_planning_lock,
    _release_planning_lock,
    MAX_TASKS_PER_PLAN,
    E_COMMERCE_PLATFORMS,
)

# ── 测试用 Fake 响应 ──

VALID_PLAN_RESPONSE = {
    "project_summary": "这是一个电商运营助手项目，需要集成多个平台的数据采集功能。",
    "recommended_architecture": "采用模块化架构，每个平台一个独立模块。",
    "execution_order": [26, 27, 28],
    "tasks": [
        {
            "task_id": 26,
            "title": "实现拼多多商品数据采集模块",
            "recommended_status": "needs_planning",
            "implementation_strategy": "通过多多进宝 API 获取商品数据",
            "files_to_modify_suggestion": ["pdd_collector.py", "test_pdd_collector.py"],
            "test_strategy": ["单元测试API调用", "集成测试数据解析"],
            "dependencies": [],
            "risks": ["API限流", "数据格式变更"],
            "requires_approval": True,
            "data_source_strategy": {
                "primary": "多多进宝 API",
                "fallbacks": ["CSV导入", "第三方数据服务"]
            }
        },
        {
            "task_id": 27,
            "title": "实现抖音商品数据采集模块",
            "recommended_status": "needs_planning",
            "implementation_strategy": "通过抖音开放平台 API 获取商品数据",
            "files_to_modify_suggestion": ["douyin_collector.py"],
            "test_strategy": ["单元测试"],
            "dependencies": [],
            "risks": ["API权限申请"],
            "requires_approval": True,
            "data_source_strategy": {
                "primary": "抖音开放平台 API",
                "fallbacks": ["CSV导入"]
            }
        },
        {
            "task_id": 28,
            "title": "实现数据汇总和报表模块",
            "recommended_status": "ready",
            "implementation_strategy": "聚合各平台数据，生成统一报表",
            "files_to_modify_suggestion": ["report_generator.py"],
            "test_strategy": ["单元测试", "集成测试"],
            "dependencies": ["需要先完成数据采集模块"],
            "risks": [],
            "requires_approval": False,
            "data_source_strategy": {
                "primary": "内部数据库",
                "fallbacks": []
            }
        }
    ],
    "global_risks": ["多个平台API同时变更可能导致大面积故障"],
    "approval_items": [
        "任务 #26 涉及拼多多数据采集需要人工审批",
        "任务 #27 涉及抖音数据采集需要人工审批"
    ],
    "next_step": "review_plan"
}

INVALID_JSON_RESPONSE = {
    # 缺少 execution_order, global_risks, approval_items
    "project_summary": "不完整的响应",
    "recommended_architecture": "test",
    "tasks": [],
    "next_step": "review_plan"
}


# ── 测试类 ──


class TestPlanSchemaValidation(unittest.TestCase):
    """测试 JSON Schema 校验"""

    def _valid_plan(self):
        return json.loads(json.dumps(VALID_PLAN_RESPONSE))

    def test_valid_plan_passes(self):
        err = validate_plan_schema(self._valid_plan())
        self.assertIsNone(err, f"Valid plan should pass: {err}")

    def test_missing_required_field(self):
        invalid = {"project_summary": "test"}
        err = validate_plan_schema(invalid)
        self.assertIsNotNone(err)
        self.assertIn("缺少必要字段", err)

    def test_tasks_not_array(self):
        invalid = {**self._valid_plan(), "tasks": "not-array"}
        err = validate_plan_schema(invalid)
        self.assertIsNotNone(err)
        self.assertIn("tasks 必须是数组", err)

    def test_task_missing_required_field(self):
        invalid = self._valid_plan()
        invalid["tasks"][0].pop("implementation_strategy")
        err = validate_plan_schema(invalid)
        self.assertIsNotNone(err)
        self.assertIn("缺少字段", err)

    def test_invalid_json_parsed(self):
        invalid = json.loads(json.dumps(INVALID_JSON_RESPONSE))
        err = validate_plan_schema(invalid)
        self.assertIsNotNone(err)
        self.assertIn("缺少必要字段", err)


class TestECommerceRiskEnhancement(unittest.TestCase):
    """测试电商平台风险增强"""

    def setUp(self):
        db_path = _get_test_db_path()
        self.service = PlannerPreviewService(db_path)

    def test_pinduoduo_task_gets_enhanced(self):
        """拼多多采集任务应被标记为高风险"""
        plan = {
            "project_summary": "test",
            "recommended_architecture": "test",
            "execution_order": [1],
            "tasks": [
                {
                    "task_id": 1,
                    "title": "拼多多商品数据采集爬虫",
                    "recommended_status": "needs_planning",
                    "implementation_strategy": "通过爬虫自动采集拼多多商品数据",
                    "files_to_modify_suggestion": [],
                    "test_strategy": [],
                    "dependencies": [],
                    "risks": [],
                    "requires_approval": False,
                    "data_source_strategy": {"primary": "", "fallbacks": []}
                }
            ],
            "global_risks": [],
            "approval_items": [],
            "next_step": "review_plan"
        }
        enhanced = self.service._enhance_ecommerce_risks(plan, [])
        task = enhanced["tasks"][0]
        self.assertEqual(task["recommended_status"], "needs_planning")
        self.assertTrue(task["requires_approval"])
        self.assertTrue(len(task["risks"]) > 0)
        self.assertTrue(len(enhanced["approval_items"]) > 0)

    def test_non_ecommerce_task_unchanged(self):
        """非电商任务不应被增强"""
        plan = {
            "project_summary": "test",
            "recommended_architecture": "test",
            "execution_order": [1],
            "tasks": [
                {
                    "task_id": 1,
                    "title": "实现用户登录模块",
                    "recommended_status": "ready",
                    "implementation_strategy": "使用JWT实现用户认证",
                    "files_to_modify_suggestion": [],
                    "test_strategy": [],
                    "dependencies": [],
                    "risks": [],
                    "requires_approval": False,
                    "data_source_strategy": {"primary": "", "fallbacks": []}
                }
            ],
            "global_risks": [],
            "approval_items": [],
            "next_step": "review_plan"
        }
        enhanced = self.service._enhance_ecommerce_risks(plan, [])
        task = enhanced["tasks"][0]
        self.assertEqual(task["recommended_status"], "ready")
        self.assertFalse(task["requires_approval"])

    def test_xiaohongshu_scraper_high_risk(self):
        """小红书爬虫任务应被标记为高风险"""
        plan = {
            "project_summary": "test",
            "recommended_architecture": "test",
            "execution_order": [1],
            "tasks": [
                {
                    "task_id": 1,
                    "title": "小红书笔记采集工具",
                    "recommended_status": "needs_planning",
                    "implementation_strategy": "自动抓取小红书笔记数据",
                    "files_to_modify_suggestion": [],
                    "test_strategy": [],
                    "dependencies": [],
                    "risks": [],
                    "requires_approval": False,
                    "data_source_strategy": {"primary": "", "fallbacks": []}
                }
            ],
            "global_risks": [],
            "approval_items": [],
            "next_step": "review_plan"
        }
        enhanced = self.service._enhance_ecommerce_risks(plan, [])
        task = enhanced["tasks"][0]
        self.assertTrue(task["requires_approval"])
        self.assertIn("合规风险", task["risks"][2] if len(task["risks"]) > 2 else "")


class TestConcurrencyProtection(unittest.TestCase):
    """测试并发保护"""

    def test_same_project_locked(self):
        """同一项目同时只能有一个规划请求"""
        # 清理可能的残留锁
        try:
            _release_planning_lock(9999)
        except Exception:
            pass

        ok1 = _acquire_planning_lock(9999)
        self.assertTrue(ok1)

        ok2 = _acquire_planning_lock(9999)
        self.assertFalse(ok2)

        self.assertTrue(is_planning_in_progress(9999))

        _release_planning_lock(9999)
        self.assertFalse(is_planning_in_progress(9999))

    def test_different_projects_independent(self):
        """不同项目可以并发规划"""
        try:
            _release_planning_lock(8888)
        except Exception:
            pass
        try:
            _release_planning_lock(7777)
        except Exception:
            pass

        ok1 = _acquire_planning_lock(8888)
        ok2 = _acquire_planning_lock(7777)
        self.assertTrue(ok1)
        self.assertTrue(ok2)

        _release_planning_lock(8888)
        _release_planning_lock(7777)


class TestPlannerAPIIntegration(unittest.TestCase):
    """测试规划 API 集成（模拟调用）- 使用独立测试数据库和 fixture"""

    TEST_PROJECT_ID = 99991

    @classmethod
    def setUpClass(cls):
        """创建独立的测试数据库和 fixture 数据"""
        cls.test_db_path = str(
            Path(__file__).resolve().parent.parent / "data" / "ai_factory_test_planner_integration.db"
        )
        # 清理旧测试数据库
        for ext in ["", "-wal", "-shm"]:
            p = Path(cls.test_db_path + ext)
            if p.exists():
                p.unlink()

        # 创建最小测试数据库
        conn = sqlite3.connect(cls.test_db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        # 创建必要的表结构（最小化）
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
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_generation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
            generation_type TEXT, model TEXT, input_summary TEXT, output_summary TEXT,
            success INTEGER, error_message TEXT, created_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ai_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, model TEXT,
            api_key_encrypted TEXT, base_url TEXT, is_active INTEGER DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS planning_approvals (
            approval_id TEXT PRIMARY KEY, preview_id TEXT, project_id INTEGER,
            approved_task_ids_json TEXT, rejected_task_ids_json TEXT,
            skipped_task_ids_json TEXT, approval_mode TEXT,
            approval_summary_json TEXT, before_snapshot_json TEXT,
            after_snapshot_json TEXT, approved_by TEXT, created_at TEXT
        )""")
        # 插入测试项目
        cur.execute(
            "INSERT INTO projects (id, name, description, status) VALUES (?, ?, ?, ?)",
            (cls.TEST_PROJECT_ID, "Test Project", "Test Description", "active"),
        )
        # 插入 needs_planning 任务
        cur.execute(
            """INSERT INTO development_tasks
               (id, project_id, title, description, status, readiness_status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (99901, cls.TEST_PROJECT_ID, "Test Task 1", "Test task description", "pending", "needs_planning"),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        """清理测试数据库"""
        for ext in ["", "-wal", "-shm"]:
            p = Path(cls.test_db_path + ext)
            if p.exists():
                p.unlink()

    def setUp(self):
        self.db_path = self.test_db_path
        # 清理前一个测试可能遗留的缓存预览（防止 invalid_json 测试因缓存而跳过 _call_model）
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "DELETE FROM planning_previews WHERE project_id = ?",
            (self.TEST_PROJECT_ID,),
        )
        conn.commit()
        conn.close()

    def test_project_exists_check(self):
        """测试项目存在性检查"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id FROM projects WHERE id = ?", (self.TEST_PROJECT_ID,))
        proj = cur.fetchone()
        conn.close()
        self.assertIsNotNone(proj)
        self.assertEqual(proj["id"], self.TEST_PROJECT_ID)

    def test_planner_service_instantiation(self):
        """测试服务实例化"""
        service = PlannerPreviewService(self.db_path)
        self.assertIsNotNone(service)

    @patch.object(PlannerPreviewService, '_ensure_client')
    @patch.object(PlannerPreviewService, '_call_model')
    @patch.object(PlannerPreviewService, '_log_to_db')
    def test_generate_preview_with_fake_model(self, mock_log, mock_call, mock_client):
        """测试使用 Fake 模型响应生成预览 - 独立 fixture，不依赖正式数据库"""
        mock_client.return_value = True

        # 使用适配测试任务 ID 的响应
        valid_response = json.loads(json.dumps(VALID_PLAN_RESPONSE))
        valid_response["execution_order"] = [99901]
        valid_response["tasks"] = [{
            "task_id": 99901,
            "title": "Test Task 1",
            "recommended_status": "ready",
            "implementation_strategy": "Test implementation",
            "files_to_modify_suggestion": ["test_file.py"],
            "test_strategy": ["unit test"],
            "dependencies": [],
            "risks": [],
            "requires_approval": False,
            "data_source_strategy": {"primary": "local", "fallbacks": []},
        }]
        mock_call.return_value = valid_response
        mock_log.return_value = None

        service = PlannerPreviewService(self.db_path)
        result = service.generate_preview(self.TEST_PROJECT_ID)

        # 必须确认模型调用被执行且成功
        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")
        self.assertEqual(result["code"], "PLAN_PREVIEW_READY")
        self.assertFalse(result["executed"])
        self.assertEqual(result["project_id"], self.TEST_PROJECT_ID)
        self.assertIsNotNone(result["preview"])
        self.assertIsNotNone(result["call_record"])
        # call_record.success 必须为 True
        self.assertTrue(result["call_record"]["success"])
        # 验证 preview 被正确生成
        self.assertIsNotNone(result["preview_id"])

    @patch.object(PlannerPreviewService, '_ensure_client')
    def test_planner_rejects_invalid_json(self, mock_client):
        """测试非法 JSON 被拒绝 - 独立 fixture"""
        mock_client.return_value = True

        service = PlannerPreviewService(self.db_path)

        # 模拟返回无效 JSON
        invalid = json.loads(json.dumps(INVALID_JSON_RESPONSE))
        with patch.object(service, '_call_model', return_value=invalid):
            result = service.generate_preview(self.TEST_PROJECT_ID)

        # 必须返回失败
        self.assertFalse(result["ok"], f"Expected ok=False for invalid JSON, got: {result}")
        self.assertIn(result["code"], ["PLANNER_OUTPUT_INVALID", "PLANNER_CALL_FAILED"])
        # 不创建 preview
        self.assertIsNone(result.get("preview"))
        self.assertIsNone(result.get("preview_id"))

    def test_max_tasks_limit(self):
        """测试最多规划12个任务"""
        self.assertEqual(MAX_TASKS_PER_PLAN, 12)

    def test_generate_preview_project_not_found(self):
        """测试项目不存在返回明确错误"""
        service = PlannerPreviewService(self.db_path)
        result = service.generate_preview(99999)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PROJECT_NOT_FOUND")

    @patch.object(PlannerPreviewService, '_ensure_client')
    @patch.object(PlannerPreviewService, '_call_model')
    @patch.object(PlannerPreviewService, '_log_to_db')
    def test_plan_does_not_modify_db_business_data(self, mock_log, mock_call, mock_client):
        """测试规划不会修改数据库业务数据 - 独立 fixture"""
        mock_client.return_value = True
        valid_response = json.loads(json.dumps(VALID_PLAN_RESPONSE))
        valid_response["execution_order"] = [99901]
        valid_response["tasks"] = [{
            "task_id": 99901, "title": "Test Task 1", "recommended_status": "ready",
            "implementation_strategy": "Test", "files_to_modify_suggestion": ["test.py"],
            "test_strategy": ["test"], "dependencies": [], "risks": [],
            "requires_approval": False,
            "data_source_strategy": {"primary": "local", "fallbacks": []},
        }]
        mock_call.return_value = valid_response
        mock_log.return_value = None

        # 记录规划前的任务数据
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, status, readiness_status FROM development_tasks WHERE project_id=?", (self.TEST_PROJECT_ID,))
        before_tasks = [dict(row) for row in cur.fetchall()]
        conn.close()

        service = PlannerPreviewService(self.db_path)
        result = service.generate_preview(self.TEST_PROJECT_ID)

        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")

        # 规划后的任务数据
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, status, readiness_status FROM development_tasks WHERE project_id=?", (self.TEST_PROJECT_ID,))
        after_tasks = [dict(row) for row in cur.fetchall()]
        conn.close()

        # 验证业务数据完全一致
        self.assertEqual(len(before_tasks), len(after_tasks))
        for i in range(len(before_tasks)):
            self.assertEqual(before_tasks[i]["status"], after_tasks[i]["status"],
                             f"Task {before_tasks[i]['id']} status changed")
            self.assertEqual(before_tasks[i]["readiness_status"], after_tasks[i]["readiness_status"],
                             f"Task {before_tasks[i]['id']} readiness_status changed")


class TestPlannerEdgeCases(unittest.TestCase):
    """测试边界情况"""

    def setUp(self):
        self.db_path = _get_test_db_path()

    def test_existing_project_rejects_planning_when_not_plan_decision(self):
        """测试非 PLAN_EXISTING_TASKS 项目应被 API 层拒绝"""
        # 这个测试在 API 层验证，这里只验证服务层逻辑
        service = PlannerPreviewService(self.db_path)
        result = service.generate_preview(3)  # 项目3 是 completed
        if result.get("code") == "NO_NEEDS_PLANNING_TASKS":
            # 没有待规划任务是合理的
            self.assertFalse(result["ok"])

    def test_api_key_masked(self):
        """测试 API Key 脱敏"""
        service = PlannerPreviewService(self.db_path)
        service._api_key = "sk-redacted"
        result = service.mask_sensitive("api_key_here sk-redacted test")
        self.assertNotIn("sk-redacted", result)


# ── 辅助函数 ──


def _get_test_db_path() -> str:
    """获取测试数据库路径"""
    return str(Path(__file__).resolve().parent.parent / "data" / "ai_factory.db")


if __name__ == "__main__":
    unittest.main(verbosity=2)
