"""
功能回归测试 v3 - Bug完整生命周期 + 状态机校验
适配新的状态机规则：analyzed/fix_ready/waiting_test/resolved 只能通过专用接口进入
"""
import sys
import os
import time
import json
import sqlite3
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = "http://localhost:8000/api"
TEST_PROJECT_ID = None
TEST_BUG_IDS = []
RESULTS = {"passed": 0, "failed": 0, "errors": [], "warnings": []}


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {msg}", flush=True)


def api_call(method, path, data=None, expected_status=200):
    url = f"{BASE_URL}{path}"
    try:
        if method == "GET":
            r = requests.get(url, timeout=10)
        elif method == "POST":
            r = requests.post(url, json=data, timeout=120)
        elif method == "PUT":
            r = requests.put(url, json=data, timeout=10)
        else:
            raise ValueError(f"Unknown method: {method}")
        resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        return resp, resp.get("ok", False) if resp else False, r.status_code
    except Exception as e:
        RESULTS["errors"].append(f"{method} {path}: {type(e).__name__}: {str(e)[:200]}")
        return None, False, 0


def assert_true(name, condition, detail=""):
    if not condition:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(f"FAIL [{name}]: {detail}")
        return False
    RESULTS["passed"] += 1
    return True


def assert_eq(name, actual, expected):
    if actual != expected:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(f"FAIL [{name}]: expected={expected}, got={actual}")
        return False
    RESULTS["passed"] += 1
    return True


def get_db_connection():
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_factory.db")
    return sqlite3.connect(db_path)


def get_db_bug(bug_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bugs WHERE id=?", (bug_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_db_status_logs(bug_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT from_status, to_status, reason FROM bug_status_logs WHERE bug_id=? ORDER BY id", (bug_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def inject_mock_analysis(bug_id):
    """直接写入模拟AI分析数据（绕过AI调用，用于回归测试）"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""UPDATE bugs SET
        bug_type='logic_error', severity='medium',
        probable_cause=?, affected_module='test_module',
        affected_files=?, fix_plan=?, regression_risks=?,
        test_steps=?, is_blocking='no', status='analyzed'
        WHERE id=?""",
        (json.dumps(["Mock cause 1", "Mock cause 2"], ensure_ascii=False),
         json.dumps(["test.py"], ensure_ascii=False),
         json.dumps(["Fix step 1", "Fix step 2"], ensure_ascii=False),
         json.dumps(["Risk 1"], ensure_ascii=False),
         json.dumps(["Test step 1", "Test step 2"], ensure_ascii=False),
         bug_id))
    conn.commit()
    conn.close()


def inject_status_log(bug_id, from_status, to_status, reason=""):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO bug_status_logs (bug_id, from_status, to_status, reason) VALUES (?, ?, ?, ?)",
              (bug_id, from_status, to_status, reason))
    conn.commit()
    conn.close()


def setup_test_project():
    global TEST_PROJECT_ID
    resp, ok, _ = api_call("POST", "/projects", {"name": f"QA-v3-{int(time.time())}", "idea": "regression test v3"})
    if ok and resp and resp.get("data"):
        TEST_PROJECT_ID = resp["data"]["id"]
        return True
    resp, ok, _ = api_call("GET", "/projects")
    if ok and resp and resp.get("data"):
        TEST_PROJECT_ID = resp["data"][0]["id"]
        return True
    return False


def test_full_lifecycle(run_id, use_real_ai=False):
    """
    完整 Bug 生命周期
    use_real_ai=True: 调用真实AI分析接口（慢但真实）
    use_real_ai=False: 直接注入模拟数据（快但绕过AI）
    """
    log(f"\n--- Run #{run_id} (AI={'real' if use_real_ai else 'mock'}) ---")

    # 1. 创建Bug
    bug_data = {
        "title": f"QA-v3 Bug #{run_id}",
        "description": f"Regression test #{run_id}",
        "error_message": f"Test error #{run_id}",
        "reproduction_steps": "1. Open 2. Click 3. Error",
        "expected_result": "Normal response",
        "actual_result": "500 error",
    }
    resp, ok, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", bug_data)
    assert_true(f"[{run_id}] Create Bug", sc == 200, f"got {sc}")
    if not ok or not resp or not resp.get("data"):
        return False
    bug_id = resp["data"]["id"]
    TEST_BUG_IDS.append(bug_id)

    # 2. 验证数据库
    db_bug = get_db_bug(bug_id)
    assert_true(f"[{run_id}] Bug in DB", db_bug is not None)
    assert_eq(f"[{run_id}] DB status=reported", db_bug["status"], "reported")

    # 3. AI分析或模拟
    if use_real_ai:
        resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/analyze")
        assert_true(f"[{run_id}] AI analyze", ok, f"resp={resp}")
        if not ok:
            log(f"  AI failed, falling back to mock")
            inject_mock_analysis(bug_id)
            inject_status_log(bug_id, "reported", "analyzing", "AI分析（降级为模拟）")
            inject_status_log(bug_id, "analyzing", "analyzed", "模拟分析完成")
    else:
        # 模拟：先设为 analyzing 再注入数据
        resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "analyzing", "reason": "Start mock"})
        assert_true(f"[{run_id}] reported->analyzing", ok)
        inject_mock_analysis(bug_id)
        inject_status_log(bug_id, "analyzing", "analyzed", "模拟分析完成")

    # 4. 验证 analyzed 状态
    db_bug = get_db_bug(bug_id)
    assert_eq(f"[{run_id}] DB status=analyzed", db_bug["status"], "analyzed")

    # 5. 生成CODEX修复指令 (analyzed -> fix_ready)
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/generate-fix-prompt")
    assert_true(f"[{run_id}] generate-fix-prompt", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=fix_ready", resp["data"]["status"], "fix_ready")
        assert_true(f"[{run_id}] Has fix_prompt", bool(resp["data"].get("fix_prompt")))

    # 6. fix_ready -> fixing
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "fixing", "reason": "Start fix"})
    assert_true(f"[{run_id}] fix_ready->fixing", ok)
    if ok and resp:
        assert_eq(f"[{run_id}] Status=fixing", resp["data"]["status"], "fixing")

    # 7. fixing -> waiting_test (保存执行结果)
    exec_data = {
        "execution_result": f"CODEX execution done #{run_id}",
        "files_changed": "test.py, utils.py",
        "test_result": "Unit tests passed",
        "remaining_issues": "None",
    }
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/execution-result", exec_data)
    assert_true(f"[{run_id}] Save execution result", ok)
    if ok and resp:
        assert_eq(f"[{run_id}] Status=waiting_test", resp["data"]["status"], "waiting_test")

    # 8. waiting_test -> resolved (回归测试通过)
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/test-result", {"passed": True, "test_notes": f"Regression passed #{run_id}"})
    assert_true(f"[{run_id}] Test pass", ok)
    if ok and resp:
        assert_eq(f"[{run_id}] Status=resolved", resp["data"]["status"], "resolved")
        assert_true(f"[{run_id}] resolved_at exists", bool(resp["data"].get("resolved_at")))

    # 9. 验证数据库
    db_bug = get_db_bug(bug_id)
    assert_eq(f"[{run_id}] DB status=resolved", db_bug["status"], "resolved")

    # 10. resolved -> reopened
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "reopened", "reason": "Bug reproduced"})
    assert_true(f"[{run_id}] resolved->reopened", ok)

    # 11. 刷新验证数据完整性
    resp, ok, _ = api_call("GET", f"/bugs/{bug_id}")
    assert_true(f"[{run_id}] Refresh detail", ok)
    if ok and resp:
        assert_true(f"[{run_id}] execution_result not lost", resp["data"].get("execution_result") is not None)
        assert_true(f"[{run_id}] fix_prompt not lost", resp["data"].get("fix_prompt") is not None)

    # 12. 状态日志完整性
    logs = get_db_status_logs(bug_id)
    assert_true(f"[{run_id}] >= 6 status logs", len(logs) >= 6, f"got {len(logs)}")

    log(f"  Run #{run_id} completed")
    return True


def test_state_machine_enforcement():
    """验证状态机强制执行"""
    log("\n--- State Machine Enforcement ---")

    resp, ok, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "SM test bug"})
    if not ok or not resp:
        return
    bug_id = resp["data"]["id"]
    TEST_BUG_IDS.append(bug_id)

    # reported -> analyzed (必须被阻止)
    resp, ok, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "analyzed"}, expected_status=409)
    assert_true("[SM] reported->analyzed blocked", sc == 409, f"got {sc}")

    # reported -> fix_ready (必须被阻止)
    resp, ok, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "fix_ready"}, expected_status=409)
    assert_true("[SM] reported->fix_ready blocked", sc == 409, f"got {sc}")

    # reported -> resolved (必须被阻止)
    resp, ok, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "resolved"}, expected_status=409)
    assert_true("[SM] reported->resolved blocked", sc == 409, f"got {sc}")

    # reported -> waiting_test (必须被阻止)
    resp, ok, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "waiting_test"}, expected_status=409)
    assert_true("[SM] reported->waiting_test blocked", sc == 409, f"got {sc}")

    # execution-result on reported (必须被阻止)
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/execution-result", {"execution_result": "test"})
    assert_true("[SM] execution-result on reported blocked", not ok)

    # test-result on reported (必须被阻止)
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/test-result", {"passed": True})
    assert_true("[SM] test-result on reported blocked", not ok)

    # 验证状态仍为 reported
    db_bug = get_db_bug(bug_id)
    assert_eq("[SM] Final status still reported", db_bug["status"], "reported")


def test_validation():
    log("\n--- Validation ---")
    # Empty title
    resp, ok, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": ""})
    assert_true("[Valid] Empty title rejected", sc == 422, f"got {sc}")

    # Nonexistent bug
    resp, ok, _ = api_call("GET", "/bugs/99999")
    assert_true("[Valid] Nonexistent bug rejected", not ok)


def test_data_consistency():
    log("\n--- Data Consistency ---")
    resp, ok, _ = api_call("GET", f"/projects/{TEST_PROJECT_ID}/bugs")
    if ok and resp:
        api_count = len(resp["data"])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bugs WHERE project_id=?", (TEST_PROJECT_ID,))
        db_count = c.fetchone()[0]
        conn.close()
        assert_eq("[Consistency] API count = DB count", api_count, db_count)

    # No orphans
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bugs WHERE project_id IS NULL")
    orphans = c.fetchone()[0]
    conn.close()
    assert_eq("[Consistency] No orphan bugs", orphans, 0)

    # Status logs match
    inconsistencies = 0
    for bug_id in TEST_BUG_IDS[-10:]:
        db_bug = get_db_bug(bug_id)
        if not db_bug:
            continue
        logs = get_db_status_logs(bug_id)
        if logs:
            last_to = logs[-1][1]
            if last_to != db_bug["status"]:
                inconsistencies += 1
    assert_true("[Consistency] Status logs match DB", inconsistencies == 0)

    # No half-state bugs
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, status FROM bugs WHERE status IN ('analyzing') AND project_id=?", (TEST_PROJECT_ID,))
    stuck = c.fetchall()
    conn.close()
    assert_true("[Consistency] No stuck analyzing bugs", len(stuck) == 0, f"found {len(stuck)}")


def main():
    log("=" * 60)
    log("FUNCTIONAL REGRESSION TEST v3")
    log("With State Machine Enforcement")
    log("=" * 60)

    try:
        r = requests.get(f"{BASE_URL}/health", timeout=3)
        log(f"Backend: {r.status_code}")
    except:
        log("Backend NOT running", "ERROR")
        return

    if not setup_test_project():
        log("Failed to setup test project", "ERROR")
        return

    # 10 full lifecycle runs (7 mock + 3 real AI)
    for i in range(1, 8):
        test_full_lifecycle(i, use_real_ai=False)
    for i in range(8, 11):
        test_full_lifecycle(i, use_real_ai=True)

    # Additional tests
    test_state_machine_enforcement()
    test_validation()
    test_data_consistency()

    # Summary
    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    log(f"Passed: {RESULTS['passed']}")
    log(f"Failed: {RESULTS['failed']}")
    log(f"Warnings: {len(RESULTS['warnings'])}")

    if RESULTS["errors"]:
        log(f"\nERRORS ({len(RESULTS['errors'])}):")
        for e in RESULTS["errors"][:20]:
            log(f"  X {e}")

    result_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regression_v3_result.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    log(f"\nResults saved: {result_file}")


if __name__ == "__main__":
    main()
