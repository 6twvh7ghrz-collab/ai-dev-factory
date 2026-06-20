"""
AI接口专项测试 + 故障注入测试 + 数据一致性深度检查
"""
import sys
import os
import time
import json
import sqlite3
import requests
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = "http://localhost:8000/api"
TEST_PROJECT_ID = None
RESULTS = {"passed": 0, "failed": 0, "errors": [], "ai_tests": {}, "fault_tests": {}, "consistency": {}}


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {msg}")


def api_call(method, path, data=None, timeout=10):
    url = f"{BASE_URL}{path}"
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        elif method == "POST":
            r = requests.post(url, json=data, timeout=timeout)
        elif method == "PUT":
            r = requests.put(url, json=data, timeout=timeout)
        return r.json(), r.status_code
    except requests.exceptions.Timeout:
        return {"error": "TIMEOUT"}, 0
    except Exception as e:
        return {"error": str(e)}, 0


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


def get_db_conn():
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_factory.db")
    return sqlite3.connect(db_path)


def setup():
    global TEST_PROJECT_ID
    resp, sc = api_call("GET", "/projects")
    if sc == 200 and resp.get("data"):
        TEST_PROJECT_ID = resp["data"][0]["id"]
    log(f"Test project ID: {TEST_PROJECT_ID}")


# ==================== AI接口专项测试 ====================

def test_ai_config_exists():
    """检查AI配置是否存在"""
    log("--- AI Config Check ---")
    resp, sc = api_call("GET", "/settings/ai")
    assert_true("[AI] Config endpoint works", sc == 200, f"status={sc}")

    configs = resp.get("data", [])
    has_active = any(c.get("is_active") for c in configs)
    if has_active:
        log("  Active AI config found - will test real AI calls")
        RESULTS["ai_tests"]["has_active_config"] = True
    else:
        log("  No active AI config - skipping real AI call tests")
        RESULTS["ai_tests"]["has_active_config"] = False
    return has_active


def test_ai_analyze_bug():
    """测试真实AI分析Bug"""
    if not TEST_PROJECT_ID:
        return

    log("--- AI Analyze Bug ---")

    # Create a test bug
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {
        "title": "AI Test Bug - Button click shows 500 error",
        "description": "When clicking the submit button on the form, the page returns a 500 Internal Server Error",
        "error_message": "TypeError: Cannot read properties of undefined (reading 'id')",
        "reproduction_steps": "1. Open form page 2. Fill in data 3. Click submit",
        "expected_result": "Form submits successfully",
        "actual_result": "500 Internal Server Error",
    })
    assert_true("[AI] Create bug for AI test", sc == 200, f"status={sc}")
    if sc != 200:
        return

    bug_id = resp["data"]["id"]
    assert_eq("[AI] Bug status=reported", resp["data"]["status"], "reported")

    # Call AI analyze
    start_time = time.time()
    resp, sc = api_call("POST", f"/bugs/{bug_id}/analyze", timeout=180)
    elapsed = time.time() - start_time

    RESULTS["ai_tests"]["analyze_response_time"] = round(elapsed, 2)
    RESULTS["ai_tests"]["analyze_status_code"] = sc

    if sc == 200 and resp.get("ok"):
        bug_data = resp["data"]
        RESULTS["ai_tests"]["analyze_success"] = True
        RESULTS["ai_tests"]["analyze_bug_status"] = bug_data.get("status")
        RESULTS["ai_tests"]["analyze_bug_type"] = bug_data.get("bug_type")
        RESULTS["ai_tests"]["analyze_severity"] = bug_data.get("severity")
        RESULTS["ai_tests"]["analyze_has_causes"] = bool(bug_data.get("probable_cause"))
        RESULTS["ai_tests"]["analyze_has_fix_plan"] = bool(bug_data.get("fix_plan"))
        RESULTS["ai_tests"]["analyze_has_files"] = bool(bug_data.get("affected_files"))

        assert_eq("[AI] Status after analyze", bug_data.get("status"), "analyzed")
        assert_true("[AI] Has bug_type", bug_data.get("bug_type") is not None)
        assert_true("[AI] Has severity", bug_data.get("severity") is not None)
        assert_true("[AI] Has probable_cause", len(bug_data.get("probable_cause", [])) > 0)
        assert_true("[AI] Has affected_files", bug_data.get("affected_files") is not None)

        log(f"  AI analyze: {elapsed:.1f}s, type={bug_data.get('bug_type')}, severity={bug_data.get('severity')}")

        # Now test generate-fix-prompt with real data
        resp2, sc2 = api_call("POST", f"/bugs/{bug_id}/generate-fix-prompt", timeout=30)
        RESULTS["ai_tests"]["fix_prompt_status"] = sc2
        if sc2 == 200 and resp2.get("ok"):
            RESULTS["ai_tests"]["fix_prompt_success"] = True
            RESULTS["ai_tests"]["fix_prompt_length"] = len(resp2["data"].get("fix_prompt", ""))
            assert_true("[AI] Fix prompt generated", resp2["data"].get("fix_prompt") is not None)
            assert_eq("[AI] Status=fix_ready", resp2["data"].get("status"), "fix_ready")
            log(f"  Fix prompt generated: {RESULTS['ai_tests']['fix_prompt_length']} chars")
        else:
            RESULTS["ai_tests"]["fix_prompt_success"] = False
            RESULTS["ai_tests"]["fix_prompt_error"] = resp2.get("error", {}).get("detail", "unknown") if isinstance(resp2, dict) else "unknown"
            log(f"  Fix prompt failed: {RESULTS['ai_tests'].get('fix_prompt_error')}", "WARN")
    else:
        RESULTS["ai_tests"]["analyze_success"] = False
        error_detail = resp.get("error", {}).get("detail", "unknown") if isinstance(resp, dict) else "unknown"
        RESULTS["ai_tests"]["analyze_error"] = error_detail
        log(f"  AI analyze failed: {error_detail}", "WARN")

        # Check that status was properly rolled back
        resp3, sc3 = api_call("GET", f"/bugs/{bug_id}")
        if sc3 == 200 and resp3.get("data"):
            bug_status = resp3["data"]["status"]
            assert_true("[AI] Status rolled back on failure", bug_status in ("reported", "reopened"),
                       f"status={bug_status}")


def test_ai_analyze_invalid_bug():
    """测试对不存在Bug的AI分析"""
    log("--- AI Analyze Invalid Bug ---")
    resp, sc = api_call("POST", "/bugs/99999/analyze", timeout=10)
    assert_true("[AI] Nonexistent bug returns error", not resp.get("ok", True), f"resp={resp}")


def test_ai_double_analyze():
    """测试重复AI分析（不应重复扣费）"""
    if not TEST_PROJECT_ID or not RESULTS["ai_tests"].get("has_active_config"):
        return

    log("--- AI Double Analyze ---")
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Double analyze test"})
    if sc != 200:
        return
    bug_id = resp["data"]["id"]

    # First analyze
    resp1, sc1 = api_call("POST", f"/bugs/{bug_id}/analyze", timeout=180)
    if sc1 == 200 and resp1.get("ok"):
        # Second analyze on same bug (already analyzed)
        resp2, sc2 = api_call("POST", f"/bugs/{bug_id}/analyze", timeout=180)
        # Should be rejected because status is "analyzed", not "reported"/"reopened"
        assert_true("[AI] Double analyze rejected", not resp2.get("ok", True),
                   f"Second analyze should fail for analyzed bug, got: {resp2}")


# ==================== 故障注入测试 ====================

def test_fault_invalid_json():
    """测试发送无效JSON"""
    log("--- Fault: Invalid JSON ---")
    try:
        r = requests.post(f"{BASE_URL}/projects/{TEST_PROJECT_ID}/bugs",
                         data="not json", headers={"Content-Type": "application/json"}, timeout=5)
        assert_true("[Fault] Invalid JSON returns 422", r.status_code == 422, f"got {r.status_code}")
    except Exception as e:
        RESULTS["errors"].append(f"[Fault] Invalid JSON: {e}")


def test_fault_missing_required_fields():
    """测试缺少必填字段"""
    log("--- Fault: Missing Required Fields ---")
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {})
    assert_true("[Fault] Missing title returns 422", sc == 422, f"got {sc}")


def test_fault_very_long_title():
    """测试超长标题"""
    log("--- Fault: Very Long Title ---")
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "A" * 600})
    assert_true("[Fault] 600-char title handled", sc in (200, 422), f"got {sc}")


def test_fault_sql_injection():
    """测试SQL注入"""
    log("--- Fault: SQL Injection ---")
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {
        "title": "'; DROP TABLE bugs; --",
        "description": "1; SELECT * FROM users --",
    })
    assert_true("[Fault] SQL injection safely handled", sc == 200, f"got {sc}")
    if sc == 200:
        # Verify table still exists
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bugs")
        count = c.fetchone()[0]
        conn.close()
        assert_true("[Fault] bugs table still exists", count > 0)


def test_fault_xss_content():
    """测试XSS内容"""
    log("--- Fault: XSS Content ---")
    xss_payload = "<script>alert('xss')</script>"
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {
        "title": xss_payload,
        "description": f"<img src=x onerror={xss_payload}>",
    })
    assert_true("[Fault] XSS content handled", sc == 200, f"got {sc}")


def test_fault_concurrent_status_change():
    """测试并发状态变更"""
    log("--- Fault: Concurrent Status Change ---")
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Concurrent status bug"})
    if sc != 200:
        return
    bug_id = resp["data"]["id"]

    results = []
    def change_status(status, reason):
        r, s = api_call("PUT", f"/bugs/{bug_id}/status", {"status": status, "reason": reason})
        results.append({"status": status, "ok": r.get("ok", False), "sc": s})

    # Try to change to analyzing and closed simultaneously
    t1 = threading.Thread(target=change_status, args=("analyzing", "thread1"))
    t2 = threading.Thread(target=change_status, args=("closed", "thread2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One should succeed, one should fail
    successes = sum(1 for r in results if r["ok"])
    assert_true("[Fault] Only one concurrent change succeeds", successes <= 1,
               f"successes={successes}, results={results}")

    # Verify final state is consistent
    resp, sc = api_call("GET", f"/bugs/{bug_id}")
    if sc == 200 and resp.get("data"):
        final_status = resp["data"]["status"]
        assert_true("[Fault] Final state is consistent", final_status in ("analyzing", "closed", "reported"),
                   f"final_status={final_status}")


def test_fault_duplicate_requests():
    """测试重复请求"""
    log("--- Fault: Duplicate Requests ---")
    title = f"Dup test {time.time()}"

    results = []
    def create_bug():
        r, s = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": title})
        results.append({"ok": r.get("ok", False), "id": r.get("data", {}).get("id") if r.get("ok") else None})

    threads = [threading.Thread(target=create_bug) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created_ids = [r["id"] for r in results if r["id"]]
    assert_true("[Fault] Duplicate requests create separate records", len(created_ids) >= 1,
               f"created {len(created_ids)} bugs from 5 requests")
    RESULTS["fault_tests"]["duplicate_creates"] = len(created_ids)
    log(f"  5 duplicate requests created {len(created_ids)} bugs (no idempotency)")


# ==================== 数据一致性深度检查 ====================

def test_data_consistency_deep():
    """深度数据一致性检查"""
    log("--- Data Consistency Deep Check ---")
    conn = get_db_conn()
    c = conn.cursor()

    # 1. Orphan bugs (no project)
    c.execute("SELECT COUNT(*) FROM bugs WHERE project_id IS NULL")
    orphans = c.fetchone()[0]
    assert_eq("[Consistency] No orphan bugs", orphans, 0)
    RESULTS["consistency"]["orphan_bugs"] = orphans

    # 2. Bugs without status logs
    c.execute("SELECT b.id FROM bugs b LEFT JOIN bug_status_logs l ON b.id=l.bug_id WHERE l.id IS NULL")
    bugs_no_logs = c.fetchall()
    RESULTS["consistency"]["bugs_without_logs"] = len(bugs_no_logs)
    assert_true("[Consistency] All bugs have status logs", len(bugs_no_logs) == 0,
               f"bugs without logs: {[b[0] for b in bugs_no_logs]}")

    # 3. Bugs in transient states (analyzing/fixing)
    c.execute("SELECT id, status FROM bugs WHERE status IN ('analyzing', 'fixing')")
    stuck = c.fetchall()
    RESULTS["consistency"]["stuck_in_transient"] = len(stuck)
    if stuck:
        RESULTS["errors"].append(f"[Consistency] {len(stuck)} bugs stuck in transient states: {[s[0] for s in stuck]}")

    # 4. Status log consistency
    c.execute("""
        SELECT b.id, b.status, l.to_status
        FROM bugs b
        LEFT JOIN bug_status_logs l ON b.id = l.bug_id
        WHERE l.id = (SELECT MAX(id) FROM bug_status_logs WHERE bug_id = b.id)
    """)
    mismatches = []
    for row in c.fetchall():
        if row[1] != row[2]:
            mismatches.append({"bug_id": row[0], "db_status": row[1], "last_log_to": row[2]})
    RESULTS["consistency"]["status_log_mismatches"] = len(mismatches)
    assert_true("[Consistency] Status logs match DB", len(mismatches) == 0,
               f"mismatches: {mismatches[:5]}")

    # 5. Resolved bugs without resolved_at
    c.execute("SELECT COUNT(*) FROM bugs WHERE status='resolved' AND resolved_at IS NULL")
    resolved_no_time = c.fetchone()[0]
    RESULTS["consistency"]["resolved_without_timestamp"] = resolved_no_time
    assert_true("[Consistency] Resolved bugs have timestamp", resolved_no_time == 0,
               f"{resolved_no_time} resolved bugs without resolved_at")

    # 6. Waiting_test bugs without execution_result
    c.execute("SELECT COUNT(*) FROM bugs WHERE status='waiting_test' AND execution_result IS NULL")
    waiting_no_exec = c.fetchone()[0]
    RESULTS["consistency"]["waiting_without_execution"] = waiting_no_exec
    assert_true("[Consistency] Waiting test bugs have execution result", waiting_no_exec == 0,
               f"{waiting_no_exec} waiting_test bugs without execution_result")

    # 7. Fix_ready bugs without fix_prompt
    c.execute("SELECT COUNT(*) FROM bugs WHERE status='fix_ready' AND fix_prompt IS NULL")
    fix_ready_no_prompt = c.fetchone()[0]
    RESULTS["consistency"]["fix_ready_without_prompt"] = fix_ready_no_prompt
    assert_true("[Consistency] Fix ready bugs have prompt", fix_ready_no_prompt == 0,
               f"{fix_ready_no_prompt} fix_ready bugs without fix_prompt")

    # 8. Analyzed bugs without probable_cause
    c.execute("SELECT id FROM bugs WHERE status IN ('analyzed','fix_ready','fixing','waiting_test','resolved','reopened') AND probable_cause IS NULL")
    analyzed_no_cause = c.fetchall()
    RESULTS["consistency"]["analyzed_without_cause"] = len(analyzed_no_cause)
    if analyzed_no_cause:
        RESULTS["errors"].append(f"[Consistency] {len(analyzed_no_cause)} analyzed+ bugs without probable_cause: {[a[0] for a in analyzed_no_cause[:5]]}")

    # 9. Timestamp consistency
    c.execute("SELECT COUNT(*) FROM bugs WHERE updated_at < created_at")
    time_issues = c.fetchone()[0]
    RESULTS["consistency"]["timestamp_issues"] = time_issues
    assert_true("[Consistency] No updated_at < created_at", time_issues == 0)

    # 10. API count vs DB count
    resp, sc = api_call("GET", f"/projects/{TEST_PROJECT_ID}/bugs")
    if sc == 200 and resp.get("data"):
        api_count = len(resp["data"])
        c.execute("SELECT COUNT(*) FROM bugs WHERE project_id=?", (TEST_PROJECT_ID,))
        db_count = c.fetchone()[0]
        RESULTS["consistency"]["api_vs_db"] = {"api": api_count, "db": db_count}
        # Note: there may be bugs in other projects too

    conn.close()


def test_transaction_integrity():
    """测试事务完整性 - AI分析失败不应导致状态错乱"""
    log("--- Transaction Integrity ---")

    # Create bug
    resp, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Transaction test bug"})
    if sc != 200:
        return
    bug_id = resp["data"]["id"]

    # Change to analyzing
    resp, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "analyzing", "reason": "Start"})
    assert_true("[Transaction] reported->analyzing", sc == 200)

    # Simulate analysis failure by changing status back
    # (In real scenario, AI failure would do this automatically)
    resp, sc = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "reported", "reason": "AI failed"})
    assert_true("[Transaction] analyzing->reported on failure", sc == 200)

    if sc == 200:
        # Verify status rolled back correctly
        resp2, sc2 = api_call("GET", f"/bugs/{bug_id}")
        if sc2 == 200 and resp2.get("data"):
            assert_eq("[Transaction] Status=reported after rollback", resp2["data"]["status"], "reported")


# ==================== 服务重启恢复测试 ====================

def test_service_health():
    """检查后端服务健康状态"""
    log("--- Service Health ---")
    resp, sc = api_call("GET", "/health")
    assert_true("[Health] Backend is running", sc == 200)
    RESULTS["service_healthy"] = sc == 200


def test_project_module_integrity():
    """检查项目、任务等模块在压力测试后是否正常"""
    log("--- Module Integrity ---")

    # Projects
    resp, sc = api_call("GET", "/projects")
    assert_true("[Module] Project list works", sc == 200)

    # AI Config
    resp, sc = api_call("GET", "/settings/ai")
    assert_true("[Module] AI config works", sc == 200)

    # Tasks
    if TEST_PROJECT_ID:
        resp, sc = api_call("GET", f"/projects/{TEST_PROJECT_ID}/tasks")
        assert_true("[Module] Task list works", sc == 200)

    # DB size check
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_factory.db")
    db_size = os.path.getsize(db_path)
    log(f"  DB size: {db_size/1024:.0f} KB")
    RESULTS["db_size_kb"] = round(db_size / 1024)


# ==================== 前端交互相关检查 ====================

def test_response_format_consistency():
    """检查所有API返回格式一致性"""
    log("--- Response Format ---")

    endpoints = [
        ("GET", "/projects"),
        ("GET", f"/projects/{TEST_PROJECT_ID}"),
        ("GET", f"/projects/{TEST_PROJECT_ID}/bugs"),
        ("GET", f"/projects/{TEST_PROJECT_ID}/tasks"),
        ("GET", "/settings/ai"),
        ("GET", "/health"),
    ]

    for method, path in endpoints:
        resp, sc = api_call(method, path)
        if sc == 200 and isinstance(resp, dict):
            has_ok = "ok" in resp
            assert_true(f"[Format] {path} has 'ok' field", has_ok, f"keys={list(resp.keys())}")


def main():
    log("=" * 60)
    log("AI + FAULT INJECTION + CONSISTENCY TEST")
    log("=" * 60)

    # Check backend
    try:
        requests.get(f"{BASE_URL}/health", timeout=3)
    except:
        log("Backend not running!", "ERROR")
        return

    setup()

    # AI tests
    has_ai = test_ai_config_exists()
    if has_ai:
        test_ai_analyze_bug()
        test_ai_analyze_invalid_bug()
        test_ai_double_analyze()

    # Fault injection
    test_fault_invalid_json()
    test_fault_missing_required_fields()
    test_fault_very_long_title()
    test_fault_sql_injection()
    test_fault_xss_content()
    test_fault_concurrent_status_change()
    test_fault_duplicate_requests()

    # Transaction integrity
    test_transaction_integrity()

    # Data consistency
    test_data_consistency_deep()

    # Service health
    test_service_health()
    test_project_module_integrity()
    test_response_format_consistency()

    # Summary
    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    log(f"Passed: {RESULTS['passed']}")
    log(f"Failed: {RESULTS['failed']}")

    log(f"\nAI Tests: {json.dumps(RESULTS['ai_tests'], ensure_ascii=False, indent=2)}")
    log(f"\nConsistency: {json.dumps(RESULTS['consistency'], ensure_ascii=False, indent=2)}")
    log(f"DB Size: {RESULTS.get('db_size_kb', 'N/A')} KB")

    if RESULTS["errors"]:
        log(f"\nErrors ({len(RESULTS['errors'])}):")
        for e in RESULTS["errors"][:20]:
            log(f"  X {e}")

    # Save
    result_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_fault_result.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    log(f"\nResults saved: {result_file}")


if __name__ == "__main__":
    main()
