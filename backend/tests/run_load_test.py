"""
压力测试自动化脚本 - 分阶段执行Locust测试
阶段A: 1用户5分钟
阶段B: 5用户10分钟
阶段C: 10用户15分钟
阶段D: 20用户15分钟
"""
import subprocess
import sys
import os
import json
import time
import sqlite3
import requests

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCUST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locustfile.py")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load_results")
os.makedirs(RESULTS_DIR, exist_ok=True)

BASE_URL = "http://localhost:8000/api"


def get_db_stats():
    db_path = os.path.join(BACKEND_DIR, "data", "ai_factory.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    stats = {}
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for t in c.fetchall():
        c.execute(f"SELECT COUNT(*) FROM {t[0]}")
        stats[t[0]] = c.fetchone()[0]
    conn.close()
    return stats


def get_system_stats():
    try:
        import psutil
        return {
            "cpu": psutil.cpu_percent(interval=1),
            "memory": psutil.virtual_memory().percent,
            "disk_free_gb": round(psutil.disk_usage("C:\\").free / 1024**3, 1),
        }
    except:
        return {}


def run_locust_stage(stage_name, users, spawn_rate, run_time_min, tags=None):
    """运行单个Locust压力测试阶段"""
    csv_prefix = os.path.join(RESULTS_DIR, f"{stage_name}")
    cmd = [
        sys.executable, "-m", "locust",
        "-f", LOCUST_FILE,
        "--host", "http://localhost:8000",
        "--headless",
        "--users", str(users),
        "--spawn-rate", str(spawn_rate),
        "--run-time", f"{run_time_min}m",
        "--csv", csv_prefix,
        "--only-summary",
    ]
    if tags:
        for t in tags:
            cmd.extend(["--tags", t])

    print(f"\n{'='*60}")
    print(f"Stage: {stage_name} | Users: {users} | Duration: {run_time_min}min")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    before = get_db_stats()
    before_sys = get_system_stats()
    start_time = time.time()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=run_time_min*60+60, encoding="utf-8", errors="replace")
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = "TIMEOUT"
    except Exception as e:
        stdout = ""
        stderr = str(e)

    elapsed = time.time() - start_time
    after = get_db_stats()
    after_sys = get_system_stats()

    # Parse CSV results
    stats_file = f"{csv_prefix}_stats.csv"
    stats_data = {}
    if os.path.exists(stats_file):
        with open(stats_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            if len(lines) > 1:
                headers = lines[0].strip().split(",")
                for line in lines[1:]:
                    vals = line.strip().split(",")
                    if len(vals) >= 6 and vals[0] != "Aggregated":
                        stats_data[vals[0]] = {
                            "requests": int(vals[1]) if vals[1].isdigit() else 0,
                            "failures": int(vals[2]) if vals[2].isdigit() else 0,
                            "avg_ms": float(vals[3]) if vals[3] else 0,
                            "p50_ms": float(vals[5]) if len(vals) > 5 and vals[5] else 0,
                        }

    # Parse failures
    failures_file = f"{csv_prefix}_failures.csv"
    failures = []
    if os.path.exists(failures_file):
        with open(failures_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[1:]:
                vals = line.strip().split(",")
                if len(vals) >= 3:
                    failures.append({"method": vals[0], "path": vals[1], "count": vals[2]})

    stage_result = {
        "stage": stage_name,
        "users": users,
        "duration_min": run_time_min,
        "actual_duration_sec": round(elapsed, 1),
        "before_db": before,
        "after_db": after,
        "before_system": before_sys,
        "after_system": after_sys,
        "endpoint_stats": stats_data,
        "failures": failures,
        "stdout_tail": stdout[-2000:] if stdout else "",
        "stderr_tail": stderr[-1000:] if stderr else "",
    }

    # Save
    result_file = os.path.join(RESULTS_DIR, f"{stage_name}_result.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(stage_result, f, ensure_ascii=False, indent=2)

    print(f"\nStage {stage_name} completed in {elapsed:.0f}s")
    if stats_data:
        total_req = sum(s["requests"] for s in stats_data.values())
        total_fail = sum(s["failures"] for s in stats_data.values())
        print(f"  Total requests: {total_req}")
        print(f"  Total failures: {total_fail}")
        if total_req > 0:
            print(f"  Error rate: {total_fail/total_req*100:.1f}%")
    print(f"  DB bugs before: {before.get('bugs', 0)} -> after: {after.get('bugs', 0)}")

    return stage_result


def check_backend_alive():
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=3)
        return r.status_code == 200
    except:
        return False


def main():
    print("="*60)
    print("LOAD TEST AUTOMATION")
    print("="*60)

    if not check_backend_alive():
        print("Backend not running! Start it first.")
        return

    all_results = []

    # Stage A: Single user, 3 minutes (reduced for practical testing)
    r = run_locust_stage("stage_a_single", 1, 1, 3)
    all_results.append(r)

    if not check_backend_alive():
        print("Backend crashed during Stage A! Stopping.")
        save_final(all_results)
        return

    # Stage B: 5 users, 5 minutes
    r = run_locust_stage("stage_b_low", 5, 1, 5)
    all_results.append(r)

    if not check_backend_alive():
        print("Backend crashed during Stage B! Stopping.")
        save_final(all_results)
        return

    # Stage C: 10 users, 8 minutes
    r = run_locust_stage("stage_c_normal", 10, 2, 8)
    all_results.append(r)

    if not check_backend_alive():
        print("Backend crashed during Stage C! Stopping.")
        save_final(all_results)
        return

    # Stage D: 20 users, 10 minutes
    r = run_locust_stage("stage_d_high", 20, 2, 10)
    all_results.append(r)

    save_final(all_results)


def save_final(results):
    # Final data consistency check
    db_stats = get_db_stats()
    conn = sqlite3.connect(os.path.join(BACKEND_DIR, "data", "ai_factory.db"))
    c = conn.cursor()

    consistency = {
        "orphan_bugs": c.execute("SELECT COUNT(*) FROM bugs WHERE project_id IS NULL").fetchone()[0],
        "bugs_without_logs": 0,
        "stuck_in_transient": 0,
        "status_log_mismatches": 0,
    }

    # Check bugs without status logs
    c.execute("SELECT b.id FROM bugs b LEFT JOIN bug_status_logs l ON b.id=l.bug_id WHERE l.id IS NULL")
    consistency["bugs_without_logs"] = len(c.fetchall())

    # Check stuck in transient states
    c.execute("SELECT id FROM bugs WHERE status IN ('analyzing', 'fixing')")
    consistency["stuck_in_transient"] = len(c.fetchall())

    conn.close()

    final = {
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": results,
        "final_db_stats": db_stats,
        "data_consistency": consistency,
        "backend_alive_after": check_backend_alive(),
    }

    final_file = os.path.join(RESULTS_DIR, "final_report.json")
    with open(final_file, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print("FINAL REPORT")
    print(f"{'='*60}")
    print(f"Backend alive: {final['backend_alive_after']}")
    print(f"Orphan bugs: {consistency['orphan_bugs']}")
    print(f"Bugs without logs: {consistency['bugs_without_logs']}")
    print(f"Stuck in transient: {consistency['stuck_in_transient']}")
    print(f"Status log mismatches: {consistency['status_log_mismatches']}")
    for r in results:
        print(f"\n  Stage {r['stage']}: {r['users']} users, {r['actual_duration_sec']}s")
        ep = r.get("endpoint_stats", {})
        total_req = sum(s["requests"] for s in ep.values())
        total_fail = sum(s["failures"] for s in ep.values())
        err_rate = f"{total_fail/total_req*100:.1f}%" if total_req > 0 else "N/A"
        print(f"    Requests: {total_req}, Failures: {total_fail}, Error rate: {err_rate}")

    print(f"\nFull report: {final_file}")


if __name__ == "__main__":
    main()
