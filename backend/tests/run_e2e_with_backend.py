"""
E2E 测试后端生命周期管理脚本

功能：
1. 复制正式数据库为临时测试数据库
2. 使用测试数据库启动后端
3. 等待 /api/health 返回成功
4. 执行 pytest -m e2e (state_machine 测试)
5. 关闭测试后端
6. 清理临时数据库和进程

使用：
    cd backend
    python tests/run_e2e_with_backend.py

环境变量：
    AI_FACTORY_DB_PATH: 测试数据库路径（脚本自动设置）
"""
import sys
import os
import time
import shutil
import subprocess
from pathlib import Path

# 配置
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROD_DB = BACKEND_DIR / "data" / "ai_factory.db"
BACKEND_PORT = 18000
BACKEND_URL = f"http://localhost:{BACKEND_PORT}"

import requests


def log(msg):
    print(f"[e2e-runner] {msg}", flush=True)


def check_db(db_path: Path, label: str):
    """检查数据库状态"""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM executor_runs WHERE status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')")
    active_runs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM task_leases WHERE status='active'")
    active_leases = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM executor_resource_locks WHERE status='active'")
    active_locks = cur.fetchone()[0]
    conn.close()
    log(f"[{label}] active_runs={active_runs}, leases={active_leases}, locks={active_locks}")
    return active_runs, active_leases, active_locks


def main():
    log("=" * 60)
    log("E2E 测试后端生命周期管理器")
    log(f"后端目录: {BACKEND_DIR}")
    log(f"测试端口: {BACKEND_PORT}")
    log("=" * 60)

    # 1. 检查正式数据库
    prod_runs, prod_leases, prod_locks = check_db(PROD_DB, "正式DB-前")
    if prod_runs > 0 or prod_leases > 0 or prod_locks > 0:
        log("警告: 正式数据库存在活跃记录！")

    # 2. 复制测试数据库
    test_db = BACKEND_DIR / "data" / f"ai_factory_e2e_test_{int(time.time())}.db"
    shutil.copy2(str(PROD_DB), str(test_db))
    log(f"测试数据库: {test_db}")

    # 3. 启动测试后端
    log("启动测试后端...")
    env = os.environ.copy()
    env["AI_FACTORY_DB_PATH"] = str(test_db)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
         "--log-level", "warning"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    log(f"后端 PID={proc.pid}")

    # 等待健康检查
    backend_ok = False
    for i in range(30):
        try:
            r = requests.get(f"{BACKEND_URL}/api/health", timeout=3)
            if r.status_code == 200:
                log(f"健康检查通过 (尝试 {i+1})")
                backend_ok = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not backend_ok:
        log("错误: 后端启动超时")
        stderr = b""
        try:
            stderr = proc.stderr.read(4096)
        except Exception:
            pass
        log(f"stderr: {stderr.decode('utf-8', errors='replace')[:500]}")
        proc.kill()
        cleanup_db(test_db)
        sys.exit(1)

    # 4. 运行 e2e 测试
    log("=" * 60)
    log("运行 State Machine E2E 测试")
    log("=" * 60)

    # 设置 BASE URL 为测试后端
    test_env = os.environ.copy()
    test_env["E2E_BASE_URL"] = BACKEND_URL

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_state_machine.py",
         "-m", "e2e", "-v", "--tb=short", "--no-header"],
        cwd=str(BACKEND_DIR),
        env=test_env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr[:1000])

    e2e_ok = result.returncode == 0
    log(f"E2E 测试退出码: {result.returncode} ({'PASS' if e2e_ok else 'FAIL'})")

    # 5. 关闭后端
    log("关闭后端...")
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log("后端未响应，强制关闭")
        proc.kill()
        proc.wait(timeout=5)
    log("后端已关闭")

    # 6. 检查残留进程
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, timeout=10)
        python_count = len([l for l in r.stdout.strip().split('\n') if l.strip()])
        log(f"残留 Python 进程: {python_count}")
    except Exception:
        python_count = -1

    # 7. 清理测试数据库
    cleanup_db(test_db)

    # 8. 最终检查正式数据库
    final_runs, final_leases, final_locks = check_db(PROD_DB, "正式DB-后")
    db_clean = (final_runs == prod_runs and final_leases == prod_leases and final_locks == prod_locks)

    # 9. 汇总
    log("=" * 60)
    log("E2E 测试汇总")
    log(f"  State Machine: {'PASS' if e2e_ok else 'FAIL'}")
    log(f"  后端启动: {'成功' if backend_ok else '失败'}")
    log(f"  正式数据库: {'未污染' if db_clean else '被修改(警告!)'}")
    log(f"  active run/lease/lock: {final_runs}/{final_leases}/{final_locks}")
    log(f"  残留进程: {python_count}")
    log("=" * 60)

    sys.exit(0 if e2e_ok and db_clean else 1)


def cleanup_db(db_path: Path):
    """清理数据库文件"""
    for suffix in ["", "-wal", "-shm"]:
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


if __name__ == "__main__":
    main()
