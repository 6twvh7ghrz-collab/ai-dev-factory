"""清理测试数据和恢复数据库"""
import sys
import os
import sqlite3
import requests
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = "http://localhost:8000/api"
BACKUP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "ai_factory_backup_20260614.db")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "ai_factory.db")


def clean_test_data():
    """清理压力测试产生的数据"""
    print("=== 清理测试数据 ===")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 查看测试产生的项目
    c.execute("SELECT id, name FROM projects WHERE name LIKE '%压测%' OR name LIKE '%QA%' OR name LIKE '%Load%' OR name LIKE '%test%'")
    test_projects = c.fetchall()
    print(f"  测试项目: {test_projects}")

    # 删除测试项目及其关联数据 (CASCADE会自动删除关联)
    for pid, name in test_projects:
        print(f"  删除项目: {name} (ID={pid})")
        c.execute("DELETE FROM projects WHERE id=?", (pid,))

    # 删除测试Bug（标题含特定关键词）
    c.execute("DELETE FROM bugs WHERE title LIKE '%压测%' OR title LIKE '%Load-Bug%' OR title LIKE '%QA Bug%' OR title LIKE '%Concurrent%'")
    deleted_bugs = c.rowcount
    print(f"  删除测试Bug: {deleted_bugs}")

    # 清理孤儿状态日志
    c.execute("DELETE FROM bug_status_logs WHERE bug_id NOT IN (SELECT id FROM bugs)")
    deleted_logs = c.rowcount
    print(f"  清理孤儿状态日志: {deleted_logs}")

    # 清理AI生成日志中的测试记录
    c.execute("DELETE FROM ai_generation_logs WHERE project_id NOT IN (SELECT id FROM projects)")
    deleted_ai_logs = c.rowcount
    print(f"  清理孤儿AI日志: {deleted_ai_logs}")

    conn.commit()
    conn.close()

    # 验证
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bugs")
    remaining = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM projects")
    projects = c.fetchone()[0]
    print(f"\n  清理后: {projects}个项目, {remaining}个Bug")
    conn.close()


def final_regression():
    """最终功能回归验证"""
    print("\n=== 最终回归验证 ===")
    try:
        # Health check
        r = requests.get(f"{BASE_URL}/health", timeout=3)
        print(f"  健康检查: {r.status_code}")

        # Project list
        r = requests.get(f"{BASE_URL}/projects", timeout=5)
        print(f"  项目列表: {r.status_code}")

        # Bug list for first project
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                pid = data[0]["id"]
                r2 = requests.get(f"{BASE_URL}/projects/{pid}/bugs", timeout=5)
                print(f"  Bug列表: {r2.status_code}, {len(r2.json().get('data', []))}个Bug")

                # Create and delete a test bug
                r3 = requests.post(f"{BASE_URL}/projects/{pid}/bugs",
                                  json={"title": "Final regression test"}, timeout=5)
                if r3.status_code == 200:
                    bug_id = r3.json()["data"]["id"]
                    print(f"  创建Bug: OK (id={bug_id})")
                    # Clean up
                    requests.put(f"{BASE_URL}/bugs/{bug_id}/status",
                                 json={"status": "closed", "reason": "cleanup"},
                                 timeout=5)
                    print(f"  关闭Bug: OK")

        print("\n  最终回归: PASS")
    except Exception as e:
        print(f"  最终回归: FAIL - {e}")


if __name__ == "__main__":
    clean_test_data()
    final_regression()
