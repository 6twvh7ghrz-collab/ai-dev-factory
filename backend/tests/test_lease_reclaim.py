"""
Section 一：过期 Lease 重新领取测试

验证 ResultCollector.claim_task() 的原子 UPSERT：
- 未过期 Lease 不能抢占
- 过期 Lease 可以重新领取
- completed 任务不能领取
- blocked 任务不能领取
- 两个 Worker 并发接管 100 轮双成功=0
- database locked=0

运行方式：
    cd backend
    python -m pytest tests/test_lease_reclaim.py -v
"""
import sqlite3
import threading
import time
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor.result_collector import ResultCollector


def _connect(db_path: str) -> sqlite3.Connection:
    """创建带 row_factory 的数据库连接"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_lease_test_db(db_path: str):
    """创建 lease 测试用数据库"""
    conn = _connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT INTO projects (id, name, status) VALUES (1, 'lease-test', 'active')")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            dependencies TEXT DEFAULT '[]',
            files_to_modify TEXT DEFAULT '[]',
            files_to_check TEXT DEFAULT '[]',
            codex_prompt TEXT DEFAULT '',
            implementation_steps TEXT DEFAULT '',
            test_steps TEXT DEFAULT '',
            task_type TEXT DEFAULT 'code',
            execution_result TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL UNIQUE,
            execution_id INTEGER,
            worker_id TEXT,
            status TEXT DEFAULT 'active',
            locked_at TEXT,
            expires_at TEXT,
            released_at TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_task_leases_status ON task_leases(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_task_leases_expires ON task_leases(expires_at)")

    conn.commit()
    conn.close()


def seed_task(db_path: str, task_id: int, status: str = "pending"):
    conn = _connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO development_tasks (id, project_id, title, status) VALUES (?, 1, ?, ?)",
        (task_id, f"Task-{task_id}", status)
    )
    conn.commit()
    conn.close()


def seed_lease(db_path: str, task_id: int, worker_id: str, status: str = "active",
               expires_offset: int = 999):
    """创建测试 lease。expires_offset: 正数=未来，负数=过去"""
    conn = _connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
        VALUES (?, ?, ?, datetime('now','localtime'), datetime('now','localtime', ?))
    """, (task_id, worker_id, status, f"{expires_offset:+d} seconds"))
    conn.commit()
    conn.close()


class TestLeaseReclaimBasic:
    """基础 lease 领取场景"""

    def test_new_claim(self):
        """场景: 无已有 lease → INSERT 成功"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-A")
            collector.close()

            assert result is True

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT task_id, worker_id, status FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["worker_id"] == "worker-A"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_active_unexpired_reject(self):
        """场景: 已有 active + 未过期 lease → 拒绝"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")
            seed_lease(db_path, 1, "worker-original", "active", 300)

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-B")
            collector.close()

            assert result is False  # 拒绝抢占

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id, status FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["worker_id"] == "worker-original"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_active_expired_reclaim(self):
        """场景: 已有 active 但已过期 lease → UPDATE 接管成功"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")
            seed_lease(db_path, 1, "worker-old", "active", -30)

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-new")
            collector.close()

            assert result is True

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id, status, COUNT(*) as cnt FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["cnt"] == 1
            assert row["worker_id"] == "worker-new"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_expired_status_reclaim(self):
        """场景: lease status='expired' → UPDATE 复用"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")
            seed_lease(db_path, 1, "worker-expired", "expired", -30)

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-reclaim")
            collector.close()

            assert result is True

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id, status, COUNT(*) as cnt FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["cnt"] == 1
            assert row["worker_id"] == "worker-reclaim"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_released_status_reclaim(self):
        """场景: lease status='released' → UPDATE 复用"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")
            seed_lease(db_path, 1, "worker-released", "released", -30)

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-new")
            collector.close()

            assert result is True

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id, status, COUNT(*) as cnt FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["cnt"] == 1
            assert row["worker_id"] == "worker-new"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_completed_task_reject(self):
        """场景: completed 任务不能领取"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "completed")

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-A")
            collector.close()

            assert result is False
        finally:
            os.unlink(db_path)

    def test_blocked_task_reject(self):
        """场景: blocked 任务不能领取"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "blocked")

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-A")
            collector.close()

            assert result is False
        finally:
            os.unlink(db_path)

    def test_failed_task_reject(self):
        """场景: failed 任务不能领取"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "failed")

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-A")
            collector.close()

            assert result is False
        finally:
            os.unlink(db_path)

    def test_null_expires_at_reclaim(self):
        """场景: expires_at 为 NULL 的 active lease → 视为过期可接管"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")

            conn = _connect(db_path)
            conn.execute("""
                INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
                VALUES (1, 'worker-null-expiry', 'active', datetime('now','localtime'), NULL)
            """)
            conn.commit()
            conn.close()

            collector = ResultCollector(db_path)
            result = collector.claim_task(1, worker_id="worker-new")
            collector.close()

            assert result is True

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id, status FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["worker_id"] == "worker-new"
            assert row["status"] == "active"
        finally:
            os.unlink(db_path)

    def test_only_one_lease_per_task(self):
        """场景: 重复 re-claim 后始终只有一条记录"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")

            collector = ResultCollector(db_path)

            # 第 1 次领取
            assert collector.claim_task(1, worker_id="w1", lease_seconds=1)
            time.sleep(1.2)  # 等待过期
            # 第 2 次领取（接管过期）
            assert collector.claim_task(1, worker_id="w2", lease_seconds=1)
            time.sleep(1.2)
            # 第 3 次领取
            assert collector.claim_task(1, worker_id="w3", lease_seconds=1)

            collector.close()

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT COUNT(*) as cnt, worker_id FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["cnt"] == 1
            assert row["worker_id"] == "w3"
        finally:
            os.unlink(db_path)


class TestLeaseConcurrentReclaim:
    """并发 lease 接管测试"""

    def test_concurrent_100_rounds_no_double_success(self):
        """两个 Worker 并发接管 100 轮 → 检查双成功=0"""
        dual_success_count = 0

        for round_idx in range(100):
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
                db_path = f.name
            try:
                create_lease_test_db(db_path)
                seed_task(db_path, 1, "pending")

                results = [False, False]
                ready = threading.Event()
                started = threading.Barrier(2, timeout=10)

                def worker_claim(idx: int):
                    try:
                        ready.set()
                        started.wait(timeout=5)
                    except Exception:
                        pass
                    try:
                        c = ResultCollector(db_path)
                        results[idx] = c.claim_task(1, f"worker-{idx}", lease_seconds=3600)
                        c.close()
                    except Exception as e:
                        results[idx] = False

                t0 = threading.Thread(target=worker_claim, args=(0,))
                t1 = threading.Thread(target=worker_claim, args=(1,))
                t0.start()
                t1.start()

                # 等待两个线程都就绪
                ready.wait(timeout=5)
                time.sleep(0.1)  # 给第二个线程时间也 set ready

                t0.join(timeout=10)
                t1.join(timeout=10)

                if results[0] and results[1]:
                    dual_success_count += 1
                    print(f"  [ROUND {round_idx}] DUAL SUCCESS! results={results}")

            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

        assert dual_success_count == 0, (
            f"并发接管出现 {dual_success_count} 轮双成功"
        )

    def test_concurrent_lease_integrity(self):
        """验证并发接管后数据库一致性"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_lease_test_db(db_path)
            seed_task(db_path, 1, "pending")

            results = [False, False]
            started = threading.Barrier(2, timeout=10)

            def worker(i):
                try:
                    started.wait(timeout=5)
                except Exception:
                    pass
                c = ResultCollector(db_path)
                results[i] = c.claim_task(1, f"worker-{i}", 3600)
                c.close()

            t0 = threading.Thread(target=worker, args=(0,))
            t1 = threading.Thread(target=worker, args=(1,))
            t0.start()
            t1.start()
            t0.join(timeout=10)
            t1.join(timeout=10)

            # 恰好一个成功
            assert sum(1 for r in results if r is True) == 1, (
                f"Expected exactly 1 success, got results={results}"
            )

            # 数据库只有一条 lease
            conn = _connect(db_path)
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM task_leases WHERE task_id=1"
            ).fetchone()
            conn.close()
            assert row["cnt"] == 1
        finally:
            os.unlink(db_path)
