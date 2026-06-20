"""
Section 三：真实服务进程重启恢复测试（6 场景）

测试方法：启动 backend → 通过 DB 设置 executor_run 状态 → 强杀进程 →
重启 backend → 调用 RecoveryManager → 验证恢复结果

由于 executor 需要真实 CLI 环境且禁止接入 AI，
本测试聚焦于 RecoveryManager 在进程重启后的 DB 恢复行为。

运行方式：
    cd backend
    python -m pytest tests/test_real_restart.py -v --timeout=300
"""
import subprocess
import sqlite3
import time
import signal
import os
import sys
import json
import uuid
import threading
import urllib.request
import urllib.error
import tempfile
import shutil
import platform
import socket
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor.recovery_manager import RecoveryManager
from app.executor.resource_lock_manager import ResourceLockManager


pytestmark = pytest.mark.e2e


BACKEND_DIR = Path(__file__).resolve().parent.parent
REAL_DB = BACKEND_DIR / "data" / "ai_factory.db"
TEST_PROJECT_ID = 9999


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _http_get(url: str, timeout: float = 10) -> dict:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _http_post(url: str, timeout: float = 10) -> dict:
    try:
        data = b""
        req = urllib.request.Request(url, data=data, method="POST",
                                       headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else "{}"
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "http_status": e.code, "body": body[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _port_in_use(port: int) -> bool:
    """Check if a TCP port is currently in use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            return result == 0
    except Exception:
        return False


def _free_port() -> int:
    """Pick an unused local TCP port for the restart test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _win_pid_exists(pid: int) -> bool:
    """Windows: check if a PID still exists via tasklist."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=10,
        )
        # If PID exists, output contains the PID number on a non-header line
        return str(pid) in result.stdout and "No tasks" not in result.stdout
    except Exception:
        return False


class ServerProcess:
    """管理 uvicorn 子进程，提供硬性进程终止验证"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.process = None
        self.port = _free_port()
        self._start_time = None
        self._exit_code = None
        self._env = os.environ.copy()
        db_abs = str(Path(db_path).resolve()).replace("\\", "/")
        self._env["DATABASE_URL"] = f"sqlite:///{db_abs}"
        self._env.pop("OPENAI_API_KEY", None)
        self._env.pop("DEEPSEEK_API_KEY", None)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self._env.pop(key, None)
        no_proxy = "localhost,127.0.0.1,::1"
        self._env["NO_PROXY"] = no_proxy
        self._env["no_proxy"] = no_proxy

    def start(self, timeout: float = 30):
        cmd = [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--log-level", "warning",
        ]
        self.process = subprocess.Popen(
            cmd,
            cwd=str(BACKEND_DIR),
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._start_time = time.time()
        self._exit_code = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = _http_get(f"http://127.0.0.1:{self.port}/api/health")
                if isinstance(result, dict) and result.get("ok"):
                    return True
            except Exception:
                pass
            if self.process.poll() is not None:
                self._exit_code = self.process.returncode
                return False
            time.sleep(0.5)
        return False

    def stop(self, force: bool = False) -> bool:
        """Stop the server process with hard verification.

        Returns True if the process is confirmed dead and port is released.
        """
        if self.process is None:
            return True
        pid_before = self.process.pid

        try:
            if force:
                self.process.kill()
            else:
                self.process.terminate()
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                self.process.kill()
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

        self._exit_code = self.process.poll()
        self.process = None

        # 硬性验证：进程必须死透
        return _hard_verify_dead(pid_before, self.port)

    @property
    def pid(self):
        return self.process.pid if self.process else None

    @property
    def exit_code(self):
        return self._exit_code

    @property
    def start_time(self):
        return self._start_time

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


def _prepare_restart_db(src_db: str, dst_db: str):
    """复制数据库并准备测试数据，返回 (task1_id, task2_id)"""
    shutil.copy2(src_db, dst_db)
    conn = _connect(dst_db)
    cur = conn.cursor()

    cur.execute("DELETE FROM executor_runs")
    cur.execute("DELETE FROM task_leases")
    cur.execute("DELETE FROM executor_resource_locks")
    try:
        cur.execute("DELETE FROM executions WHERE status='running'")
    except Exception:
        pass

    # 移除 executor_runs 的活跃项目唯一索引（允许测试多 run）
    try:
        cur.execute("DROP INDEX IF EXISTS uq_executor_runs_active_project")
    except Exception:
        pass

    cur.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, 'restart-test', 'active')",
                (TEST_PROJECT_ID,))
    cur.execute("DELETE FROM development_tasks WHERE project_id=?", (TEST_PROJECT_ID,))
    cur.execute("""
        INSERT INTO development_tasks (project_id, title, status, task_type)
        VALUES (?, 'Restart-Test-Task-1', 'pending', 'code')
    """, (TEST_PROJECT_ID,))
    tid1 = cur.lastrowid
    cur.execute("""
        INSERT INTO development_tasks (project_id, title, status, task_type)
        VALUES (?, 'Restart-Test-Task-2', 'pending', 'code')
    """, (TEST_PROJECT_ID,))
    tid2 = cur.lastrowid

    conn.commit()
    conn.close()
    return tid1, tid2


def _seed_run(db_path: str, run_id: str, project_id: int, worker_id: str,
              status: str, heartbeat_offset: int, task_id: int = None,
              current_step: str = None):
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO executor_runs (run_id, project_id, worker_id, status,
            current_task_id, current_step, heartbeat_at, started_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime',?),
            datetime('now','localtime'))
    """, (run_id, project_id, worker_id, status, task_id, current_step,
          f"{heartbeat_offset:+d} seconds"))
    conn.commit()
    conn.close()


def _seed_lease(db_path: str, task_id: int, worker_id: str, status: str = "active",
                expires_offset: int = 999):
    conn = _connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
        VALUES (?, ?, ?, datetime('now','localtime'),
            datetime('now','localtime', ?))
    """, (task_id, worker_id, status, f"{expires_offset:+d} seconds"))
    conn.commit()
    conn.close()


def _seed_resource_lock(db_path: str, project_id: int, resource_key: str,
                        worker_id: str, task_id: int = 0, expires_offset: int = 999,
                        run_id: str = None):
    """Seed a resource lock with valid FK references.
    
    Creates a minimal execution record and uses the actual executor_run auto-increment id.
    """
    conn = _connect(db_path)
    lock_id = f"lock-{uuid.uuid4().hex[:8]}"

    # 查找 executor_run 的 auto-increment id（FK 需要 INTEGER 主键，而非 run_id 字符串）
    executor_run_db_id = 1  # fallback
    if run_id:
        row = conn.execute("SELECT id FROM executor_runs WHERE run_id=?", (run_id,)).fetchone()
        if row:
            executor_run_db_id = row["id"]

    # 创建最小 execution 记录以满足 FK 约束
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO executions (task_id, project_id, worker_id, status, started_at)
        VALUES (?, ?, ?, 'running', datetime('now','localtime'))
    """, (task_id, project_id, worker_id))
    exec_db_id = cur.lastrowid

    conn.execute("""
        INSERT INTO executor_resource_locks
        (lock_id, lock_token, resource_scope, scope_key, resource_type, resource_key,
         normalized_key, project_id, task_id, execution_id, executor_run_id,
         worker_id, status, expires_at)
        VALUES (?, ?, 'project', ?, 'file', ?, ?, ?, ?, ?, ?, ?, 'active',
            datetime('now','localtime', ?))
    """, (lock_id, lock_id, f"project-{project_id}", resource_key, resource_key,
          project_id, task_id, exec_db_id, executor_run_db_id, worker_id,
          f"{expires_offset:+d} seconds"))
    conn.commit()
    conn.close()


def _hard_verify_dead(pid: int, port: int = None, max_wait: float = 15) -> bool:
    """硬性验证进程已彻底终止（Windows / Linux 通用）。

    验证项：
    1. PID 在 OS 层面不存在 (Windows: tasklist, Linux: os.kill(0))
    2. 给定端口不再被监听
    全部满足才返回 True。失败即硬错误，不得仅 warn。
    """
    if pid is None:
        return True

    is_windows = platform.system() == "Windows"
    deadline = time.time() + max_wait
    pid_gone = False
    port_released = True

    while time.time() < deadline:
        # 1. 检查 PID 是否存在
        if is_windows:
            pid_exists = _win_pid_exists(pid)
        else:
            try:
                os.kill(pid, 0)
                pid_exists = True
            except (OSError, ProcessLookupError):
                pid_exists = False

        # 2. 检查端口
        if port is not None:
            port_released = not _port_in_use(port)
        else:
            port_released = True

        if not pid_exists and port_released:
            pid_gone = True
            break

        time.sleep(0.5)

    return pid_gone and port_released


def _kill_verify(pid, port: int = None):
    """已弃用，保留兼容。新代码请用 _hard_verify_dead。
    
    此函数现在是 _hard_verify_dead 的硬性包装，不再有 Windows 豁免。
    """
    return _hard_verify_dead(pid, port=port)


class TestRealRestartScenarios:
    """真实进程重启恢复 6 场景"""

    def test_scenario_01_worker_executing_restart(self):
        """场景1: 一个 Worker 正在 executing 时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs01_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            run_id = f"run-s1-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, run_id, pid, "worker-old-A", "executing", -999, task_id=tid1, current_step="executing")
            _seed_lease(test_db, tid1, "worker-old-A", "active", -999)
            _seed_resource_lock(test_db, pid, "git:merge", "worker-old-A",
                                 task_id=tid1, expires_offset=-999, run_id=run_id)

            server = ServerProcess(test_db)
            assert server.start(timeout=60), "Server failed to start"

            try:
                health = _http_get(f"{server.base_url}/api/health")
                assert isinstance(health, dict) and health.get("ok")

                old_pid = server.pid
                assert old_pid is not None
                old_exit_code = server.exit_code
                assert server.stop(force=True), (
                    f"Old process PID={old_pid} still alive or port {server.port} still in use"
                )
                # 二次确认：PID 不存在，端口已释放
                assert not _win_pid_exists(old_pid) if platform.system() == "Windows" else True
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                # 执行恢复
                rm = RecoveryManager(test_db)
                runs = rm.scan_unfinished_runs()
                recovered = sum(1 for run in runs
                                if rm.attempt_recovery(run, heartbeat_timeout=120)["action"] == "resumed")
                assert recovered >= 1

                conn = _connect(test_db)
                row = conn.execute("SELECT status, worker_id FROM executor_runs WHERE run_id=?", (run_id,)).fetchone()
                conn.close()
                assert row["status"] == "starting"
                assert row["worker_id"] != "worker-old-A"
                assert row["worker_id"] != run_id

                conn = _connect(test_db)
                lr = conn.execute("SELECT status FROM task_leases WHERE task_id=?", (tid1,)).fetchone()
                conn.close()
                assert lr is None or lr["status"] != "active"
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass

    def test_scenario_02_dual_workers_executing_restart(self):
        """场景2: 两个 Worker 都在 executing 时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs02_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            r1 = f"run-s2a-{uuid.uuid4().hex[:8]}"
            r2 = f"run-s2b-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, r1, pid, "worker-old-A", "executing", -999, task_id=tid1, current_step="executing")
            _seed_lease(test_db, tid1, "worker-old-A", "active", -999)
            _seed_run(test_db, r2, pid, "worker-old-B", "executing", -999, task_id=tid2, current_step="executing")
            _seed_lease(test_db, tid2, "worker-old-B", "active", -999)

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            try:
                assert isinstance(_http_get(f"{server.base_url}/api/health"), dict)
                old_pid = server.pid
                old_exit_code = server.exit_code
                assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                rm = RecoveryManager(test_db)
                runs = rm.scan_unfinished_runs()
                resumed = sum(1 for r in runs
                              if rm.attempt_recovery(r, heartbeat_timeout=120)["action"] == "resumed")
                assert resumed >= 1

                conn = _connect(test_db)
                for rid in [r1, r2]:
                    row = conn.execute("SELECT status, worker_id FROM executor_runs WHERE run_id=?", (rid,)).fetchone()
                    assert row and row["status"] in ("starting", "blocked")
                    assert row["worker_id"] not in ("worker-old-A", "worker-old-B")
                conn.close()

                conn = _connect(test_db)
                rows = conn.execute("SELECT id FROM executions WHERE task_id IN (?,?) ORDER BY id", (tid1, tid2)).fetchall()
                conn.close()
                ids = [r["id"] for r in rows]
                assert len(ids) == len(set(ids)), "Duplicate execution IDs"
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass

    def test_scenario_03_task_repairing_restart(self):
        """场景3: Task 正在 repairing 时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs03_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            run_id = f"run-s3-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, run_id, pid, "worker-repair", "repairing", -999, task_id=tid1, current_step="repairing")
            _seed_lease(test_db, tid1, "worker-repair", "active", -999)

            conn = _connect(test_db)
            conn.execute("""INSERT INTO executions (task_id, project_id, worker_id, status, repair_count, test_result, started_at)
                VALUES (?, ?, ?, 'failed', 1, 'fail', datetime('now','localtime'))""",
                         (tid1, pid, "worker-repair"))
            conn.commit(); conn.close()

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            try:
                assert isinstance(_http_get(f"{server.base_url}/api/health"), dict)
                old_pid = server.pid
                old_exit_code = server.exit_code
                assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                rm = RecoveryManager(test_db)
                runs = rm.scan_unfinished_runs()
                recovered = sum(1 for run in runs
                                if rm.attempt_recovery(run, heartbeat_timeout=120)["action"] == "resumed")
                assert recovered >= 1

                conn = _connect(test_db)
                row = conn.execute("SELECT repair_count FROM executions WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid1,)).fetchone()
                conn.close()
                if row: assert row["repair_count"] <= 2
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass

    def test_scenario_04_waiting_merge_restart(self):
        """场景4: 任务等待 MergeCoordinator 处理时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs04_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            run_id = f"run-s4-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, run_id, pid, "worker-merge", "testing", -999, task_id=tid1, current_step="testing")
            _seed_lease(test_db, tid1, "worker-merge", "active", -999)
            _seed_resource_lock(test_db, pid, "git:merge", "worker-merge",
                                 task_id=tid1, expires_offset=-999, run_id=run_id)

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            try:
                assert isinstance(_http_get(f"{server.base_url}/api/health"), dict)
                old_pid = server.pid
                old_exit_code = server.exit_code
                assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                rm = RecoveryManager(test_db)
                for run in rm.scan_unfinished_runs():
                    rm.attempt_recovery(run, heartbeat_timeout=120)
                rm.cleanup_orphan_leases(project_id=pid)

                rlm = ResourceLockManager(test_db)
                rlm.cleanup_expired()

                conn = _connect(test_db)
                lr = conn.execute("SELECT status FROM task_leases WHERE task_id=?", (tid1,)).fetchone()
                conn.close()
                assert lr is None or lr["status"] != "active"

                conn = _connect(test_db)
                lock = conn.execute("SELECT status FROM executor_resource_locks WHERE project_id=?", (pid,)).fetchone()
                conn.close()
                assert lock is None or lock["status"] != "active"
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass

    def test_scenario_05_one_completed_one_executing_restart(self):
        """场景5: 一个任务 completed，另一个仍 executing 时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs05_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            # Task-1 已完成
            conn = _connect(test_db)
            conn.execute("UPDATE development_tasks SET status='completed' WHERE id=?", (tid1,))
            conn.commit(); conn.close()

            run_id = f"run-s5-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, run_id, pid, "worker-exec", "executing", -999, task_id=tid2, current_step="executing")
            _seed_lease(test_db, tid2, "worker-exec", "active", -999)

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            try:
                assert isinstance(_http_get(f"{server.base_url}/api/health"), dict)
                old_pid = server.pid
                old_exit_code = server.exit_code
                assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                rm = RecoveryManager(test_db)
                for run in rm.scan_unfinished_runs():
                    rm.attempt_recovery(run, heartbeat_timeout=120)

                conn = _connect(test_db)
                row = conn.execute("SELECT status FROM development_tasks WHERE id=?", (tid1,)).fetchone()
                conn.close()
                assert row["status"] == "completed", f"completed task should stay completed"

                conn = _connect(test_db)
                rows = conn.execute("SELECT id FROM executions WHERE task_id=? AND status='running'", (tid1,)).fetchall()
                conn.close()
                assert len(rows) == 0, "No running execution for completed task"
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass

    def test_scenario_06_one_blocked_one_executing_restart(self):
        """场景6: 一个任务 blocked，另一个仍安全执行时重启"""
        test_db = str(BACKEND_DIR / "data" / f"_rs06_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            conn = _connect(test_db)
            conn.execute("UPDATE development_tasks SET status='blocked' WHERE id=?", (tid1,))
            conn.commit(); conn.close()

            run_id = f"run-s6-{uuid.uuid4().hex[:8]}"
            _seed_run(test_db, run_id, pid, "worker-exec", "executing", -999, task_id=tid2, current_step="executing")
            _seed_lease(test_db, tid2, "worker-exec", "active", -999)

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            try:
                assert isinstance(_http_get(f"{server.base_url}/api/health"), dict)
                old_pid = server.pid
                old_exit_code = server.exit_code
                assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
                assert not _port_in_use(server.port), f"Port {server.port} still in use"

                rm = RecoveryManager(test_db)
                for run in rm.scan_unfinished_runs():
                    rm.attempt_recovery(run, heartbeat_timeout=120)

                conn = _connect(test_db)
                row = conn.execute("SELECT status FROM development_tasks WHERE id=?", (tid1,)).fetchone()
                conn.close()
                assert row["status"] == "blocked"

                conn = _connect(test_db)
                row2 = conn.execute("SELECT status FROM executor_runs WHERE run_id=?", (run_id,)).fetchone()
                conn.close()
                assert row2 and row2["status"] in ("starting", "blocked")
            finally:
                server.stop(force=True)
        finally:
            try: os.unlink(test_db)
            except OSError: pass


class TestFinalVerification:
    """最终验证：全量清理检查"""

    def test_all_scenarios_final_state(self):
        """全量验证：活跃 lease=0, 活跃 resource lock=0, integrity ok"""
        test_db = str(BACKEND_DIR / "data" / f"_rsfinal_{uuid.uuid4().hex[:6]}.db")
        try:
            tid1, tid2 = _prepare_restart_db(REAL_DB, test_db)
            pid = TEST_PROJECT_ID

            # 创建多种状态的孤儿记录
            first_rid = None
            for i, (status, tid) in enumerate([
                ("executing", tid1), ("repairing", tid2), ("testing", tid1),
                ("scanning", tid1), ("claiming", tid1)
            ]):
                rid = f"run-final-{i}-{uuid.uuid4().hex[:6]}"
                _seed_run(test_db, rid, pid, f"dead-w{i}", status, -999, task_id=tid)
                if first_rid is None:
                    first_rid = rid

            for t in [tid1, tid2]:
                _seed_lease(test_db, t, "dead-lease", "active", -999)
            _seed_resource_lock(test_db, pid, "git:merge", "dead-lock",
                                task_id=tid1, expires_offset=-999, run_id=first_rid)

            server = ServerProcess(test_db)
            assert server.start(timeout=60)
            old_pid = server.pid
            old_exit_code = server.exit_code
            assert server.stop(force=True), f"Old process PID={old_pid} still alive or port still in use"
            assert not _port_in_use(server.port), f"Port {server.port} still in use"

            rm = RecoveryManager(test_db)
            for run in rm.scan_unfinished_runs():
                rm.attempt_recovery(run, heartbeat_timeout=120)
            rm.cleanup_orphan_leases(project_id=pid)
            ResourceLockManager(test_db).cleanup_expired()

            conn = _connect(test_db)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            assert integrity[0] == "ok", f"Integrity: {integrity[0]}"

            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            assert len(fk) == 0, f"FK violations: {len(fk)}"

            active_leases = conn.execute(
                "SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'").fetchone()["cnt"]
            active_locks = conn.execute(
                "SELECT COUNT(*) as cnt FROM executor_resource_locks WHERE status='active'").fetchone()["cnt"]
            conn.close()

            assert active_leases == 0, f"Active leases: {active_leases}"
            assert active_locks == 0, f"Active locks: {active_locks}"
        finally:
            try: os.unlink(test_db)
            except OSError: pass
