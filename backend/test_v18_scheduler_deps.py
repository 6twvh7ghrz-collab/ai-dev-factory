"""
V1.8A-R: Scheduler 依赖标准化测试

测试 normalize_dependencies 函数对各种输入格式的处理。
使用临时数据库，不污染正式数据库。
"""
import sys
import os
import tempfile
import sqlite3
import json
import unittest

# 添加 backend 到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.executor.task_scheduler import (
    normalize_dependencies,
    DependencyRef,
    DependencyType,
    TaskScheduler,
    SchedulableTask,
)


class TestNormalizeDependencies(unittest.TestCase):
    """测试 normalize_dependencies 函数"""

    def test_empty_list(self):
        """空列表 → 返回空"""
        result = normalize_dependencies([])
        self.assertEqual(len(result), 0)

    def test_none(self):
        """None → 返回空"""
        result = normalize_dependencies(None)
        self.assertEqual(len(result), 0)

    def test_integer_id(self):
        """整数 26 → task_id 引用"""
        result = normalize_dependencies([26])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.TASK_ID)
        self.assertEqual(result[0].task_id, 26)

    def test_string_digit_id(self):
        """纯数字字符串 "26" → task_id 引用"""
        result = normalize_dependencies(["26"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.TASK_ID)
        self.assertEqual(result[0].task_id, 26)

    def test_title_string(self):
        """非数字字符串 → task title 引用"""
        result = normalize_dependencies(["搭建Electron项目基础框架"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.TASK_TITLE)
        self.assertEqual(result[0].title, "搭建Electron项目基础框架")

    def test_mixed_types(self):
        """混合类型: [26, "另一个任务"]"""
        result = normalize_dependencies([26, "另一个任务"])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].ref_type, DependencyType.TASK_ID)
        self.assertEqual(result[0].task_id, 26)
        self.assertEqual(result[1].ref_type, DependencyType.TASK_TITLE)
        self.assertEqual(result[1].title, "另一个任务")

    def test_empty_string_ignored(self):
        """空字符串 → 被忽略"""
        result = normalize_dependencies([26, "", "   "])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task_id, 26)

    def test_null_ignored(self):
        """None 值 → 被忽略"""
        result = normalize_dependencies([26, None, "标题"])
        self.assertEqual(len(result), 2)

    def test_dict_invalid(self):
        """字典 → INVALID"""
        result = normalize_dependencies([{"key": "value"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.INVALID)

    def test_nested_list_invalid(self):
        """嵌套数组 → INVALID"""
        result = normalize_dependencies([[26]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.INVALID)

    def test_boolean_invalid(self):
        """布尔值 → INVALID"""
        result = normalize_dependencies([True])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.INVALID)

    def test_float_invalid(self):
        """浮点数 → INVALID"""
        result = normalize_dependencies([26.0])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ref_type, DependencyType.INVALID)


class TestSchedulerDependencyResolution(unittest.TestCase):
    """测试 TaskScheduler 依赖解析（使用临时数据库）"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_scheduler.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        # 创建精简表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'developing'
            );
            CREATE TABLE IF NOT EXISTS development_tasks (
                id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT,
                status TEXT DEFAULT 'pending', readiness_status TEXT DEFAULT 'draft',
                dependencies TEXT, files_to_modify TEXT, files_to_check TEXT,
                codex_prompt TEXT, implementation_steps TEXT, test_steps TEXT,
                task_type TEXT DEFAULT 'code', acceptance_criteria TEXT DEFAULT '',
                priority INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS task_leases (
                id INTEGER PRIMARY KEY, task_id INTEGER, worker_id TEXT,
                status TEXT, locked_at TEXT, expires_at TEXT, released_at TEXT
            );
        """)
        conn.execute("INSERT INTO projects VALUES (56, '测试项目', 'developing')")
        # Task 26 = completed (依赖目标)
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (26, 56, '搭建Electron项目基础框架', 'completed', 'ready',
             '[]', '["src/main.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, 1)
        """)
        # Task 30 = completed (第二个依赖目标)
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (30, 56, '另一个任务', 'completed', 'ready',
             '[]', '["src/a.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, 2)
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_task(self, task_id, deps_json):
        """创建测试任务"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (?, 56, '测试任务', 'pending', 'ready',
             ?, '["src/test.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, ?)
        """, (task_id, deps_json, task_id))
        conn.commit()
        conn.close()

    def test_deps_empty_array_runnable(self):
        """dependencies = [] → 任务 runnable"""
        self._create_task(31, "[]")
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, 31)

    def test_deps_integer_id_runnable(self):
        """dependencies = [26] → 依赖 completed → runnable"""
        self._create_task(31, "[26]")
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, 31)

    def test_deps_string_id_runnable(self):
        """dependencies = ["26"] → 依赖 completed → runnable"""
        self._create_task(31, '["26"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, 31)

    def test_deps_title_runnable(self):
        """dependencies = ["搭建Electron项目基础框架"] → 依赖 completed → runnable"""
        self._create_task(31, '["搭建Electron项目基础框架"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, 31)

    def test_deps_pending_blocked(self):
        """依赖任务 pending → blocked（依赖不满足）"""
        # 先创建一个 pending 的依赖任务（但不是 ready，以免自身出现在 runnable 中）
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (40, 56, '未完成依赖', 'pending', 'needs_planning',
             '[]', '["src/b.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, 40)
        """)
        conn.commit()
        conn.close()
        self._create_task(31, "[40]")
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        # 任务 31 依赖任务 40（pending 且 needs_planning）→ 依赖不满足 → blocked
        # 任务 40 自身 readiness_status=needs_planning → 也不 runnable
        self.assertEqual(len(runnable), 0)

    def test_deps_id_not_exists_blocked(self):
        """依赖 ID 不存在 → blocked"""
        self._create_task(31, "[999]")
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 0)

    def test_deps_title_not_exists_blocked(self):
        """依赖标题不存在 → blocked"""
        self._create_task(31, '["不存在的任务"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 0)

    def test_deps_mixed_runnable(self):
        """dependencies = [26, "另一个任务"] → 都 completed → runnable"""
        self._create_task(31, '[26, "另一个任务"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, 31)

    def test_deps_duplicate_title_ambiguous(self):
        """同名标题多个 → AMBIGUOUS_DEPENDENCY_TITLE → blocked"""
        # 创建第二个同名 completed 任务
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (41, 56, '搭建Electron项目基础框架', 'completed', 'ready',
             '[]', '["src/c.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, 41)
        """)
        conn.commit()
        conn.close()
        self._create_task(31, '["搭建Electron项目基础框架"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 0)

    def test_deps_invalid_type_blocked(self):
        """非法依赖类型 → INVALID → blocked"""
        self._create_task(31, '[{"key": "value"}]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        self.assertEqual(len(runnable), 0)

    def test_deps_cross_project_title(self):
        """跨项目同名任务不满足依赖（title 匹配限定 project_id）"""
        # 创建另一个项目的同名 completed 任务
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO projects VALUES (99, '其他项目', 'developing')")
        conn.execute("""
            INSERT INTO development_tasks VALUES
            (50, 99, '搭建Electron项目基础框架', 'completed', 'ready',
             '[]', '["src/d.ts"]', '[]', 'prompt', 'steps', 'tests', 'code', 'criteria', 1, 50)
        """)
        conn.commit()
        conn.close()
        # 但依赖标题的任务在 project_id=56 中，而标题匹配限定 project_id
        # Task 26 在 project 56 中已完成，所以应该 runnable
        self._create_task(31, '["搭建Electron项目基础框架"]')
        scheduler = TaskScheduler(self.db_path)
        runnable = scheduler.find_runnable_tasks(56)
        # Task 26 在 project 56 中已完成，所以 runnable
        self.assertEqual(len(runnable), 1)

    def test_queue_status_blocked_reasons(self):
        """测试 get_queue_status 返回正确的阻塞原因"""
        self._create_task(31, "[999]")
        scheduler = TaskScheduler(self.db_path)
        status = scheduler.get_queue_status(56)
        blocked = [b for b in status["blocked_tasks"] if b["id"] == 31]
        self.assertEqual(len(blocked), 1)
        self.assertIn("依赖ID不存在", blocked[0]["blocked_reasons"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
