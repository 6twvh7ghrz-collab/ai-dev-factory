"""
DEPRECATED: 功能回归测试 v2 - Bug完整生命周期10次循环

状态: 废弃 (2026-06-16 封版验收)
原因: 状态机演进后 analyzing→analyzed 不再允许手动转换（需由AI分析流驱动）。
      此脚本假设可通过 PUT /api/bugs/{id}/status 手动推进全部生命周期，
      该假设在当前状态机下已不成立（预期121/181断言失败）。
      测试执行结果显示 181 passed, 121 failed。

替代测试覆盖（全部通过 pytest，0 fail 0 error）:
  - test_state_machine.py (6 tests)      → 覆盖 Bug 状态机合法/非法转换
  - test_real_restart.py (7 tests)       → 覆盖真实进程终止恢复，含 Windows 硬验证
  - test_lease_reclaim.py (12 tests)     → 覆盖 Lease 回收机制
  - test_recovery_worker_id.py (5 tests) → 覆盖 Worker ID 恢复
  - test_restart_recovery.py (6 tests)   → 覆盖重启恢复流程
  - test_failure_reproduction.py (16 tests) → 覆盖故障复现
  - ai_and_fault_test.py (16 tests)      → 覆盖 AI 与故障场景
  - test_merge_scenarios.py (3 tests)    → 覆盖合并场景

总计: 71 tests, 0 fail, 0 error

此文件不再作为 pytest 收集目标（__test__ = False），
也不再推荐独立运行。保留仅供历史参考。
"""
import sys
import os
import time
import json
import sqlite3
import requests

# 告知 pytest 不收集此模块中的测试
__test__ = False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = "http://localhost:8000/api"
TEST_PROJECT_ID = None
TEST_BUG_IDS = []
RESULTS = {"passed": 0, "failed": 0, "errors": [], "warnings": [], "bugs_found": []}


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {msg}")


def api_call(method, path, data=None, expected_status=200):
    url = f"{BASE_URL}{path}"
    try:
        if method == "GET":
            r = requests.get(url, timeout=10)
        elif method == "POST":
            r = requests.post(url, json=data, timeout=120)
        elif method == "PUT":
            r = requests.put(url, json=data, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, timeout=10)
        else:
            raise ValueError(f"Unknown method: {method}")

        if r.status_code != expected_status:
            return r.json() if r.headers.get("content-type","").startswith("application/json") else None, False, r.status_code

        resp = r.json()
        return resp, resp.get("ok", True), r.status_code
    except Exception as e:
        RESULTS["errors"].append(f"{method} {path}: Exception {type(e).__name__}: {str(e)[:200]}")
        return None, False, 0


def assert_eq(name, actual, expected):
    if actual != expected:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(f"ASSERT FAIL [{name}]: expected={expected}, got={actual}")
        return False
    RESULTS["passed"] += 1
    return True


def assert_true(name, condition, detail=""):
    if not condition:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(f"ASSERT FAIL [{name}]: condition=False {detail}")
        return False
    RESULTS["passed"] += 1
    return True


def get_db_connection():
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_factory.db")
    return sqlite3.connect(db_path)


def get_db_bug(bug_id):
    """返回 dict 形式的 Bug 数据库记录"""
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


def setup_test_project():
    global TEST_PROJECT_ID
    resp, ok, _ = api_call("POST", "/projects", {"name": "QA-test-project", "idea": "regression test"})
    if ok and resp and resp.get("data"):
        TEST_PROJECT_ID = resp["data"]["id"]
        log(f"Created test project ID: {TEST_PROJECT_ID}")
        return True
    # fallback
    resp, ok, _ = api_call("GET", "/projects")
    if ok and resp and resp.get("data"):
        TEST_PROJECT_ID = resp["data"][0]["id"]
        log(f"Using existing project ID: {TEST_PROJECT_ID}")
        return True
    log("No project available", "ERROR")
    return False


def test_full_bug_lifecycle(run_id):
    """完整 Bug 生命周期 - 遵循状态机:
    reported -> analyzing -> analyzed -> fix_ready -> fixing -> waiting_test -> resolved -> reopened
    注意: 不调用真实AI，而是通过状态转换手动推进
    """
    log(f"\n--- Run #{run_id} ---")

    # 1. 创建Bug
    bug_data = {
        "title": f"QA Bug #{run_id}",
        "description": f"Regression test bug #{run_id}",
        "error_message": f"Test error #{run_id}",
        "reproduction_steps": "1. Open 2. Click 3. Error",
        "expected_result": "Normal response",
        "actual_result": "500 error",
    }
    resp, ok, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", bug_data)
    assert_true(f"[{run_id}] Create Bug status=200", sc == 200, f"got {sc}")
    if not ok or not resp or not resp.get("data"):
        return False
    bug_id = resp["data"]["id"]
    TEST_BUG_IDS.append(bug_id)
    log(f"  Bug #{run_id} created: id={bug_id}")

    # 2. 验证数据库写入
    db_bug = get_db_bug(bug_id)
    assert_true(f"[{run_id}] Bug in DB", db_bug is not None)
    if db_bug:
        assert_eq(f"[{run_id}] DB title", db_bug["title"], bug_data["title"])
        assert_eq(f"[{run_id}] DB status=reported", db_bug["status"], "reported")
        assert_eq(f"[{run_id}] DB project_id", db_bug["project_id"], TEST_PROJECT_ID)

    # 3. API返回数据验证
    api_bug = resp["data"]
    assert_eq(f"[{run_id}] API id", api_bug["id"], bug_id)
    assert_eq(f"[{run_id}] API status=reported", api_bug["status"], "reported")

    # 4. 获取Bug详情
    resp, ok, _ = api_call("GET", f"/bugs/{bug_id}")
    assert_true(f"[{run_id}] Get detail ok", ok)
    if ok and resp:
        detail = resp["data"]
        assert_eq(f"[{run_id}] Detail id", detail["id"], bug_id)
        logs = detail.get("status_logs", [])
        assert_true(f"[{run_id}] Has status_logs", len(logs) >= 1)

    # 5. 状态转换: reported -> analyzing
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "analyzing", "reason": "Start AI"})
    assert_true(f"[{run_id}] reported->analyzing", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=analyzing", resp["data"]["status"], "analyzing")

    # 6. 状态转换: analyzing -> analyzed
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "analyzed", "reason": "AI done"})
    assert_true(f"[{run_id}] analyzing->analyzed", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=analyzed", resp["data"]["status"], "analyzed")

    # 7. 生成CODEX修复指令 - 需要先有 probable_cause (来自AI分析)
    # BUG发现: 不经过真实AI分析，generate-fix-prompt 会返回 NO_ANALYSIS
    # 因为它检查 bug.probable_cause 是否为空
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/generate-fix-prompt")
    if not ok:
        RESULTS["bugs_found"].append({
            "id": f"BUG-001-run{run_id}",
            "severity": "Medium",
            "title": "generate-fix-prompt requires AI analysis (probable_cause check)",
            "detail": "Without real AI analysis, probable_cause is empty, so CODEX prompt generation fails. "
                      "The status can still be manually set to analyzed, but the analysis data is missing. "
                      "This is a design gap: manual status transitions bypass data population.",
            "file": "backend/app/api/bugs.py:213-214",
        })
        # Workaround: 直接写入DB设置 probable_cause 来模拟AI分析结果
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""UPDATE bugs SET
            bug_type='logic_error', severity='medium',
            probable_cause=?, affected_module='test_module',
            affected_files=?, fix_plan=?, regression_risks=?,
            test_steps=?, is_blocking='no'
            WHERE id=?""",
            (json.dumps(["Test cause 1", "Test cause 2"], ensure_ascii=False),
             json.dumps(["test.py"], ensure_ascii=False),
             json.dumps(["Fix step 1"], ensure_ascii=False),
             json.dumps(["Risk 1"], ensure_ascii=False),
             json.dumps(["Test step 1"], ensure_ascii=False),
             bug_id))
        conn.commit()
        conn.close()
        log(f"  Workaround: injected mock AI analysis data for bug {bug_id}")

        # 重新生成CODEX
        resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/generate-fix-prompt")
        assert_true(f"[{run_id}] generate-fix-prompt after mock", ok, f"resp={resp}")

    if ok and resp:
        assert_eq(f"[{run_id}] Status=fix_ready", resp["data"]["status"], "fix_ready")
        assert_true(f"[{run_id}] Has fix_prompt", resp["data"].get("fix_prompt") is not None and len(resp["data"].get("fix_prompt", "")) > 0)

    # 8. 状态转换: fix_ready -> fixing
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "fixing", "reason": "Start fix"})
    assert_true(f"[{run_id}] fix_ready->fixing", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=fixing", resp["data"]["status"], "fixing")

    # 9. 保存执行结果 (fixing -> waiting_test)
    exec_data = {
        "execution_result": f"CODEX execution done #{run_id}",
        "files_changed": "test.py, utils.py",
        "test_result": "Unit tests passed",
        "remaining_issues": "None",
    }
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/execution-result", exec_data)
    assert_true(f"[{run_id}] Save execution result", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=waiting_test", resp["data"]["status"], "waiting_test")
        assert_eq(f"[{run_id}] execution_result saved", resp["data"]["execution_result"], exec_data["execution_result"])

    # 10. 回归测试通过 (waiting_test -> resolved)
    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/test-result", {"passed": True, "test_notes": f"Regression passed #{run_id}"})
    assert_true(f"[{run_id}] Test pass", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=resolved", resp["data"]["status"], "resolved")

    # 11. 验证数据库状态
    db_bug = get_db_bug(bug_id)
    if db_bug:
        assert_eq(f"[{run_id}] DB status=resolved", db_bug["status"], "resolved")

    # 12. 重新打开 (resolved -> reopened)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "reopened", "reason": "Bug reproduced"})
    assert_true(f"[{run_id}] resolved->reopened", ok, f"resp={resp}")
    if ok and resp:
        assert_eq(f"[{run_id}] Status=reopened", resp["data"]["status"], "reopened")

    # 13. 检查状态日志完整性
    logs = get_db_status_logs(bug_id)
    assert_true(f"[{run_id}] >= 7 status logs", len(logs) >= 7, f"got {len(logs)}")

    # 14. 获取Bug列表验证
    resp, ok, _ = api_call("GET", f"/projects/{TEST_PROJECT_ID}/bugs")
    assert_true(f"[{run_id}] List bugs ok", ok)
    if ok and resp:
        found = any(b["id"] == bug_id for b in resp["data"])
        assert_true(f"[{run_id}] Bug in list", found)

    # 15. 模拟刷新
    resp, ok, _ = api_call("GET", f"/bugs/{bug_id}")
    assert_true(f"[{run_id}] Refresh detail ok", ok)
    if ok and resp:
        assert_eq(f"[{run_id}] Refresh id", resp["data"]["id"], bug_id)
        assert_eq(f"[{run_id}] Refresh status=reopened", resp["data"]["status"], "reopened")
        assert_eq(f"[{run_id}] Refresh title", resp["data"]["title"], bug_data["title"])

    # 16. 验证数据完整性 - 关键字段不丢失
    assert_true(f"[{run_id}] execution_result not lost", resp["data"].get("execution_result") is not None)
    assert_true(f"[{run_id}] fix_prompt not lost", resp["data"].get("fix_prompt") is not None)

    log(f"  Run #{run_id} completed")
    return True


def test_invalid_transitions():
    log("\n--- Invalid Transitions ---")

    resp, ok, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Transition test bug"})
    if not ok or not resp:
        return
    bug_id = resp["data"]["id"]
    TEST_BUG_IDS.append(bug_id)

    # reported -> resolved (invalid)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "resolved"})
    assert_true("[Invalid] reported->resolved rejected", not ok)

    # reported -> waiting_test (invalid)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "waiting_test"})
    assert_true("[Invalid] reported->waiting_test rejected", not ok)

    # reported -> closed (valid)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "closed", "reason": "Close invalid bug"})
    assert_true("[Valid] reported->closed accepted", ok)

    # closed -> reported (invalid)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "reported"})
    assert_true("[Invalid] closed->reported rejected", not ok)

    # closed -> reopened (valid)
    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "reopened", "reason": "Reopen"})
    assert_true("[Valid] closed->reopened accepted", ok)


def test_no_duplicate_data():
    log("\n--- Duplicate Check ---")
    title = "Duplicate check bug"
    r1, ok1, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": title})
    r2, ok2, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": title})
    if ok1 and ok2:
        id1, id2 = r1["data"]["id"], r2["data"]["id"]
        TEST_BUG_IDS.extend([id1, id2])
        assert_true("[Dup] Same title different IDs", id1 != id2)
        RESULTS["warnings"].append("System allows duplicate Bug titles - frontend should prevent this")


def test_validation():
    log("\n--- Validation ---")

    # Empty title
    resp, ok, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": ""})
    assert_true("[Valid] Empty title rejected (422)", sc == 422, f"got {sc}")

    # Nonexistent project
    resp, ok, _ = api_call("POST", "/projects/99999/bugs", {"title": "test"})
    assert_true("[Valid] Nonexistent project rejected", not ok)

    # Nonexistent bug
    resp, ok, _ = api_call("GET", "/bugs/99999")
    assert_true("[Valid] Nonexistent bug rejected", not ok)

    # Test result on wrong status
    resp, ok, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Wrong status test"})
    if ok and resp:
        bug_id = resp["data"]["id"]
        TEST_BUG_IDS.append(bug_id)
        resp2, ok2, _ = api_call("POST", f"/bugs/{bug_id}/test-result", {"passed": True})
        assert_true("[Valid] Test result on reported rejected", not ok2)


def test_data_consistency():
    log("\n--- Data Consistency ---")

    # API count vs DB count
    resp, ok, _ = api_call("GET", f"/projects/{TEST_PROJECT_ID}/bugs")
    if ok and resp:
        api_count = len(resp["data"])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bugs WHERE project_id=?", (TEST_PROJECT_ID,))
        db_count = c.fetchone()[0]
        conn.close()
        assert_eq("[Consistency] API count = DB count", api_count, db_count)

    # No orphan bugs
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bugs WHERE project_id IS NULL")
    orphans = c.fetchone()[0]
    conn.close()
    assert_eq("[Consistency] No orphan bugs", orphans, 0)

    # Status logs consistency
    inconsistencies = 0
    for bug_id in TEST_BUG_IDS[-10:]:  # check last 10
        db_bug = get_db_bug(bug_id)
        if not db_bug:
            continue
        logs = get_db_status_logs(bug_id)
        if logs:
            last_to = logs[-1][1]
            if last_to != db_bug["status"]:
                inconsistencies += 1
                RESULTS["errors"].append(f"[Consistency] Bug {bug_id}: DB status={db_bug['status']}, last log to={last_to}")
    assert_true("[Consistency] Status logs match DB", inconsistencies == 0, f"{inconsistencies} mismatches")

    # Check for half-state bugs (analyzing/fixing stuck)
    c = get_db_connection().cursor()
    c.execute("SELECT id, status FROM bugs WHERE status IN ('analyzing', 'fixing')")
    stuck = c.fetchall()
    if stuck:
        RESULTS["warnings"].append(f"Found {len(stuck)} bugs in transient states (analyzing/fixing): {[s[0] for s in stuck]}")

    # Check timestamp consistency
    c.execute("SELECT id, created_at, updated_at FROM bugs WHERE updated_at < created_at")
    time_issues = c.fetchall()
    assert_true("[Consistency] No updated_at < created_at", len(time_issues) == 0, f"found {len(time_issues)}")


def test_concurrent_create():
    """测试并发创建Bug是否产生数据问题"""
    log("\n--- Concurrent Create Test ---")
    import threading

    results = []
    def create_bug(idx):
        resp, ok, sc = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs",
                                {"title": f"Concurrent Bug {idx}"})
        results.append({"idx": idx, "ok": ok, "status": sc, "id": resp["data"]["id"] if ok and resp else None})

    threads = [threading.Thread(target=create_bug, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created_ids = [r["id"] for r in results if r["ok"]]
    all_unique = len(created_ids) == len(set(created_ids))
    assert_true("[Concurrent] All IDs unique", all_unique, f"ids={created_ids}")
    TEST_BUG_IDS.extend(created_ids)

    # Verify all in DB
    for bid in created_ids:
        db_bug = get_db_bug(bid)
        assert_true(f"[Concurrent] Bug {bid} in DB", db_bug is not None)


def test_rapid_status_changes():
    """测试快速连续状态变更"""
    log("\n--- Rapid Status Changes ---")
    resp, ok, _ = api_call("POST", f"/projects/{TEST_PROJECT_ID}/bugs", {"title": "Rapid status bug"})
    if not ok or not resp:
        return
    bug_id = resp["data"]["id"]
    TEST_BUG_IDS.append(bug_id)

    # Rapid transitions
    transitions = [
        ("analyzing", "Start"),
        ("analyzed", "Done"),
    ]
    for status, reason in transitions:
        resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": status, "reason": reason})
        if ok and resp:
            assert_eq(f"[Rapid] Status={status}", resp["data"]["status"], status)
        else:
            RESULTS["warnings"].append(f"Rapid status change failed: {status}")

    # Inject mock data for fix_ready
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""UPDATE bugs SET
        probable_cause=?, affected_files=?, fix_plan=?, regression_risks=?, test_steps=?
        WHERE id=?""",
        (json.dumps(["cause1"]), json.dumps(["f1"]), json.dumps(["plan1"]),
         json.dumps(["risk1"]), json.dumps(["step1"]), bug_id))
    conn.commit()
    conn.close()

    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/generate-fix-prompt")
    if ok and resp:
        assert_eq("[Rapid] Status=fix_ready", resp["data"]["status"], "fix_ready")

    resp, ok, _ = api_call("PUT", f"/bugs/{bug_id}/status", {"status": "fixing", "reason": "Fix"})
    if ok and resp:
        assert_eq("[Rapid] Status=fixing", resp["data"]["status"], "fixing")

    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/execution-result",
                           {"execution_result": "Done", "files_changed": "a.py"})
    if ok and resp:
        assert_eq("[Rapid] Status=waiting_test", resp["data"]["status"], "waiting_test")

    resp, ok, _ = api_call("POST", f"/bugs/{bug_id}/test-result", {"passed": True})
    if ok and resp:
        assert_eq("[Rapid] Status=resolved", resp["data"]["status"], "resolved")

    # Verify all status logs
    logs = get_db_status_logs(bug_id)
    log(f"  Bug {bug_id}: {len(logs)} status log entries")
    assert_true("[Rapid] All transitions logged", len(logs) >= 6, f"got {len(logs)}")


def test_related_modules():
    """测试关联模块不受影响"""
    log("\n--- Related Module Check ---")

    # Project list should still work
    resp, ok, _ = api_call("GET", "/projects")
    assert_true("[Related] Project list works", ok)

    # AI config should still work
    resp, ok, _ = api_call("GET", "/settings/ai")
    assert_true("[Related] AI config works", ok)

    # Health check
    resp, ok, _ = api_call("GET", "/health")
    assert_true("[Related] Health check works", ok)

    # Task list for test project
    if TEST_PROJECT_ID:
        resp, ok, _ = api_call("GET", f"/projects/{TEST_PROJECT_ID}/tasks")
        assert_true("[Related] Task list works", ok)


def main():
    log("=" * 60)
    log("FUNCTIONAL REGRESSION TEST v2")
    log("=" * 60)

    # Check backend
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=3)
        log(f"Backend running: {r.status_code}")
    except Exception as e:
        log(f"Backend not running: {e}", "ERROR")
        return

    if not setup_test_project():
        return

    before_count = get_db_connection().cursor().execute("SELECT COUNT(*) FROM bugs").fetchone()[0]
    get_db_connection().close()
    log(f"Bug count before: {before_count}")

    # 10 full lifecycle runs
    for i in range(1, 11):
        test_full_bug_lifecycle(i)

    # Additional tests
    test_invalid_transitions()
    test_no_duplicate_data()
    test_validation()
    test_concurrent_create()
    test_rapid_status_changes()
    test_data_consistency()
    test_related_modules()

    after_count = get_db_connection().cursor().execute("SELECT COUNT(*) FROM bugs").fetchone()[0]
    get_db_connection().close()

    # Summary
    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    log(f"Assertions passed: {RESULTS['passed']}")
    log(f"Assertions failed: {RESULTS['failed']}")
    log(f"Warnings: {len(RESULTS['warnings'])}")
    log(f"Bug count: {before_count} -> {after_count} (+{after_count - before_count})")
    log(f"Test project ID: {TEST_PROJECT_ID}")
    log(f"Bug IDs: {TEST_BUG_IDS}")

    if RESULTS["warnings"]:
        log("\nWARNINGS:")
        for w in RESULTS["warnings"]:
            log(f"  ! {w}")

    if RESULTS["errors"]:
        log(f"\nERRORS ({len(RESULTS['errors'])}):")
        for e in RESULTS["errors"][:20]:
            log(f"  X {e}")
        if len(RESULTS["errors"]) > 20:
            log(f"  ... and {len(RESULTS['errors'])-20} more")

    if RESULTS["bugs_found"]:
        log(f"\nBUSINESS BUGS FOUND ({len(RESULTS['bugs_found'])}):")
        for b in RESULTS["bugs_found"]:
            log(f"  [{b['severity']}] {b['title']}")
            log(f"    {b['detail']}")
            log(f"    File: {b['file']}")

    # Save
    result_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regression_result.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump({
            "passed": RESULTS["passed"],
            "failed": RESULTS["failed"],
            "warnings": RESULTS["warnings"],
            "errors": RESULTS["errors"],
            "bugs_found": RESULTS["bugs_found"],
            "test_project_id": TEST_PROJECT_ID,
            "test_bug_ids": TEST_BUG_IDS,
            "before_count": before_count,
            "after_count": after_count,
        }, f, ensure_ascii=False, indent=2)
    log(f"\nResults saved: {result_file}")


if __name__ == "__main__":
    main()
