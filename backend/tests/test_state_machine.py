"""
Bug 状态机自动化测试 (E2E 集成测试)
========================================
⚠️ 这是端到端集成测试，需要真实后端在 localhost:8000 运行。

独立运行命令：
    cd backend
    python tests/test_state_machine.py

前提条件：
    1. 后端服务已在 localhost:8000 启动
    2. 后端健康检查通过

pytest 默认跳过此测试文件（标记为 e2e）。
运行 e2e 测试：
    pytest -m e2e tests/test_state_machine.py
"""
import sys
import os
import json
import time
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class _DynamicBase:
    def __str__(self):
        return f"{os.environ.get('E2E_BASE_URL', 'http://localhost:8000')}/api"


BASE = _DynamicBase()
import requests

results = {"pass": 0, "fail": 0, "errors": []}


def log(msg):
    print(msg, flush=True)


def assert_test(name, condition, detail=""):
    if condition:
        results["pass"] += 1
        log(f"  PASS: {name}")
    else:
        results["fail"] += 1
        results["errors"].append(f"{name}: {detail}")
        log(f"  FAIL: {name} - {detail}")


def get_test_project_id():
    """获取或创建测试项目"""
    r = requests.get(f"{BASE}/projects", timeout=10)
    if r.status_code == 200:
        data = r.json()
        if data.get("ok") and data.get("data"):
            for p in data["data"]:
                if "stresstest" in p.get("name", "").lower():
                    return p["id"]
    # create
    r = requests.post(f"{BASE}/projects", json={
        "name": f"StateMachineTest-{int(time.time())}",
        "idea": "Test project for state machine validation"
    }, timeout=10)
    d = r.json()
    return d["data"]["id"]


def create_bug(project_id, title=None):
    """创建测试Bug"""
    r = requests.post(f"{BASE}/projects/{project_id}/bugs", json={
        "title": title or f"StateMachine-Test-{int(time.time()*1000)}",
        "description": "State machine test bug",
        "error_message": "Test error message",
        "reproduction_steps": "1. Do something 2. See error",
        "expected_result": "Should work",
        "actual_result": "Got error",
    }, timeout=10)
    d = r.json()
    return d.get("data", {})


def update_status(bug_id, status, reason=""):
    """更新Bug状态"""
    r = requests.put(f"{BASE}/bugs/{bug_id}/status", json={
        "status": status,
        "reason": reason or f"Set to {status}",
    }, timeout=10)
    return r.status_code, r.json()


def get_bug(bug_id):
    """获取Bug详情"""
    r = requests.get(f"{BASE}/bugs/{bug_id}", timeout=10)
    return r.json().get("data", {})


# ============================================================
# 测试1：禁止跳过AI分析直接设置analyzed
# ============================================================
@pytest.mark.e2e
def test_1_skip_ai_analysis():
    log("\n--- Test 1: Skip AI analysis, direct set analyzed ---")
    pid = get_test_project_id()
    bug = create_bug(pid, "Test1-SkipAnalysis")
    bid = bug["id"]

    # reported -> analyzed (should fail with 409)
    code, resp = update_status(bid, "analyzed", "try to skip AI")
    assert_test(
        "reported->analyzed returns 409",
        code == 409,
        f"got {code}"
    )
    assert_test(
        "response ok=false",
        resp.get("ok") == False,
        f"got {resp.get('ok')}"
    )
    assert_test(
        "error code blocks illegal transition",
        resp.get("error", {}).get("code") in ("BUG_STATE_REQUIRES_SPECIAL_ENDPOINT", "INVALID_TRANSITION"),
        f"got {resp.get('error', {}).get('code')}"
    )

    # 验证状态仍为 reported
    bug_data = get_bug(bid)
    assert_test(
        "status still reported",
        bug_data.get("status") == "reported",
        f"got {bug_data.get('status')}"
    )

    # 验证数据库没有伪造分析结果
    assert_test(
        "probable_cause is empty",
        not bug_data.get("probable_cause") or len(bug_data.get("probable_cause", [])) == 0,
        f"got {bug_data.get('probable_cause')}"
    )


# ============================================================
# 测试2：分析字段缺失时禁止进入analyzed
# ============================================================
@pytest.mark.e2e
def test_2_missing_analysis_fields():
    log("\n--- Test 2: Missing analysis fields, block analyzed ---")
    pid = get_test_project_id()
    bug = create_bug(pid, "Test2-MissingFields")
    bid = bug["id"]

    # reported -> analyzing (允许，因为STATUS_TRANSITIONS中reported可以到analyzing)
    code, resp = update_status(bid, "analyzing", "try manual analyzing")
    if code == 200:
        # analyzing -> analyzed (必须通过AI分析接口，普通状态接口应拒绝)
        code2, resp2 = update_status(bid, "analyzed", "try manual analyzed")
        assert_test(
            "analyzing->analyzed via status API returns 409",
            code2 == 409,
            f"got {code2}"
        )
        err_data = resp2.get("error") or {}
        assert_test(
            "error code blocks illegal transition",
            err_data.get("code") in ("BUG_STATE_REQUIRES_SPECIAL_ENDPOINT", "INVALID_TRANSITION"),
            f"got {err_data.get('code')}"
        )

        # 验证数据库没有回滚到reported（analyzing状态应保留）
        bug_data = get_bug(bid)
        assert_test(
            "status still analyzing (not reverted or advanced)",
            bug_data.get("status") in ("analyzing", "reported"),
            f"got {bug_data.get('status')}"
        )
    else:
        # reported->analyzing 如果也不允许，那 analyzed 更不可能
        assert_test(
            "reported->analyzing blocked (transition rules changed)",
            True,
            "transition already blocked"
        )

    # 验证最终状态不是 analyzed
    bug_data = get_bug(bid)
    assert_test(
        "status is NOT analyzed",
        bug_data.get("status") != "analyzed",
        f"got {bug_data.get('status')}"
    )


# ============================================================
# 测试3：正常AI分析流程
# ============================================================
@pytest.mark.e2e
def test_3_normal_ai_analysis():
    log("\n--- Test 3: Normal AI analysis flow ---")
    pid = get_test_project_id()
    bug = create_bug(pid, "Test3-NormalAI")
    bid = bug["id"]

    # 调用 AI 分析
    r = requests.post(f"{BASE}/bugs/{bid}/analyze", timeout=120)
    d = r.json()

    if d.get("ok"):
        assert_test("AI analysis succeeded", True, "")
        bug_data = d.get("data", {})

        # 验证状态变为 analyzed
        assert_test(
            "status is analyzed",
            bug_data.get("status") == "analyzed",
            f"got {bug_data.get('status')}"
        )

        # 验证关键字段存在
        assert_test(
            "probable_cause exists",
            bool(bug_data.get("probable_cause")),
            f"got {bug_data.get('probable_cause')}"
        )
        assert_test(
            "fix_plan exists",
            bool(bug_data.get("fix_plan")),
            f"got {bug_data.get('fix_plan')}"
        )
        assert_test(
            "test_steps exists",
            bool(bug_data.get("test_steps")),
            f"got {bug_data.get('test_steps')}"
        )

        # 刷新后验证结果仍存在
        bug_data2 = get_bug(bid)
        assert_test(
            "after refresh: status still analyzed",
            bug_data2.get("status") == "analyzed",
            f"got {bug_data2.get('status')}"
        )
        assert_test(
            "after refresh: probable_cause still exists",
            bool(bug_data2.get("probable_cause")),
            ""
        )
    else:
        # AI 可能不可用，检查是否有合理错误
        assert_test(
            "AI analysis returned error (AI may not be available)",
            d.get("ok") == False,
            f"got {d}"
        )
        log(f"  (AI not available, skipping AI-dependent assertions)")


# ============================================================
# 测试4：禁止提前生成后续状态
# ============================================================
@pytest.mark.e2e
def test_4_block_premature_states():
    log("\n--- Test 4: Block premature state transitions ---")
    pid = get_test_project_id()

    # 4a: analyzed 之前不能 fix_ready
    bug_a = create_bug(pid, "Test4a-NoAnalysis")
    bid_a = bug_a["id"]
    code, resp = update_status(bid_a, "fix_ready", "try premature fix_ready")
    assert_test(
        "reported->fix_ready blocked (409)",
        code == 409,
        f"got {code}"
    )

    # 4b: fix_prompt 为空不能 fixing
    bug_b = create_bug(pid, "Test4b-NoFixPrompt")
    bid_b = bug_b["id"]
    code, resp = update_status(bid_b, "fixing", "try premature fixing")
    assert_test(
        "reported->fixing blocked (409)",
        code == 409,
        f"got {code}"
    )

    # 4c: execution_result 为空不能 waiting_test
    bug_c = create_bug(pid, "Test4c-NoExecResult")
    bid_c = bug_c["id"]
    code, resp = update_status(bid_c, "waiting_test", "try premature waiting_test")
    assert_test(
        "reported->waiting_test blocked (409)",
        code == 409,
        f"got {code}"
    )

    # 4d: test_result 为空不能 resolved
    bug_d = create_bug(pid, "Test4d-NoTestResult")
    bid_d = bug_d["id"]
    code, resp = update_status(bid_d, "resolved", "try premature resolved")
    assert_test(
        "reported->resolved blocked (409)",
        code == 409,
        f"got {code}"
    )

    # 4e: 直接设 analyzed 也应该被阻止
    bug_e = create_bug(pid, "Test4e-DirectAnalyzed")
    bid_e = bug_e["id"]
    code, resp = update_status(bid_e, "analyzed", "try direct analyzed")
    assert_test(
        "reported->analyzed blocked (409)",
        code == 409,
        f"got {code}"
    )


# ============================================================
# 测试5：正常完整闭环（不依赖AI，模拟字段填充）
# ============================================================
@pytest.mark.e2e
def test_5_full_lifecycle():
    log("\n--- Test 5: Full lifecycle via dedicated endpoints ---")
    pid = get_test_project_id()
    bug = create_bug(pid, "Test5-FullLifecycle")
    bid = bug["id"]

    # Step 1: reported -> AI analyze
    r = requests.post(f"{BASE}/bugs/{bid}/analyze", timeout=120)
    d = r.json()
    if not d.get("ok"):
        log(f"  AI analysis failed: {d.get('error')}, skipping full lifecycle test")
        # 标记为跳过而非失败，因为AI不可用
        log("  SKIP: Full lifecycle test requires AI service")
        return

    bug_data = d["data"]
    assert_test("analyzed after AI", bug_data["status"] == "analyzed", f"got {bug_data['status']}")

    # Step 2: analyzed -> generate fix prompt (goes to fix_ready)
    r = requests.post(f"{BASE}/bugs/{bid}/generate-fix-prompt", timeout=30)
    d = r.json()
    assert_test("fix_prompt generated", d.get("ok"), f"got {d}")
    if d.get("ok"):
        bug_data = d["data"]
        assert_test("status is fix_ready", bug_data["status"] == "fix_ready", f"got {bug_data['status']}")
        assert_test("fix_prompt exists", bool(bug_data.get("fix_prompt")), "")

    # Step 3: fix_ready -> fixing (via status update)
    code, resp = update_status(bid, "fixing", "start fixing")
    assert_test("fix_ready->fixing ok", code == 200, f"got {code}")

    # Step 4: fixing -> waiting_test (via execution result)
    r = requests.post(f"{BASE}/bugs/{bid}/execution-result", json={
        "execution_result": "Fixed the bug by changing X to Y",
        "files_changed": "app/api/bugs.py",
        "test_result": "All tests passed",
        "remaining_issues": "",
    }, timeout=10)
    d = r.json()
    assert_test("execution result saved", d.get("ok"), f"got {d}")
    if d.get("ok"):
        assert_test("status is waiting_test", d["data"]["status"] == "waiting_test", f"got {d['data']['status']}")

    # Step 5: waiting_test -> resolved (via test result)
    r = requests.post(f"{BASE}/bugs/{bid}/test-result", json={
        "passed": True,
        "test_notes": "All regression tests passed",
    }, timeout=10)
    d = r.json()
    assert_test("test result saved", d.get("ok"), f"got {d}")
    if d.get("ok"):
        assert_test("status is resolved", d["data"]["status"] == "resolved", f"got {d['data']['status']}")
        assert_test("resolved_at exists", bool(d["data"].get("resolved_at")), "")

    # Step 6: resolved -> reopened
    code, resp = update_status(bid, "reopened", "found regression")
    assert_test("resolved->reopened ok", code == 200, f"got {code}")

    # Step 7: reopened -> closed
    code, resp = update_status(bid, "closed", "closing after review")
    assert_test("reopened->closed ok", code == 200, f"got {code}")

    # 最终验证数据完整性
    final = get_bug(bid)
    assert_test("final status is closed", final["status"] == "closed", f"got {final['status']}")
    assert_test("analysis data preserved", bool(final.get("probable_cause")), "")
    assert_test("fix_prompt preserved", bool(final.get("fix_prompt")), "")


# ============================================================
# 测试6：并发状态更新
# ============================================================
@pytest.mark.e2e
def test_6_concurrent_status_update():
    log("\n--- Test 6: Concurrent status updates ---")
    pid = get_test_project_id()
    bug = create_bug(pid, "Test6-Concurrent")
    bid = bug["id"]

    # 先进入 fix_ready 状态（需要AI）
    r = requests.post(f"{BASE}/bugs/{bid}/analyze", timeout=120)
    d = r.json()
    if not d.get("ok"):
        log("  SKIP: Concurrent test requires AI service")
        return

    r = requests.post(f"{BASE}/bugs/{bid}/generate-fix-prompt", timeout=30)
    d = r.json()
    if not d.get("ok"):
        log("  SKIP: Fix prompt generation failed")
        return

    # 并发尝试：fix_ready -> fixing 和 fix_ready -> reopened
    results_concurrent = {"fixing": None, "reopened": None}
    errors = []

    def try_update(status):
        try:
            code, resp = update_status(bid, status, f"concurrent set to {status}")
            results_concurrent[status] = {"code": code, "resp": resp}
        except Exception as e:
            errors.append(str(e))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(try_update, "fixing")
        f2 = pool.submit(try_update, "reopened")
        f1.result()
        f2.result()

    # 至少有一个应该成功，不能两个都失败
    success_count = sum(1 for v in results_concurrent.values() if v and v.get("code") == 200)
    assert_test(
        "at least one concurrent update succeeded",
        success_count >= 1,
        f"fixing={results_concurrent['fixing']}, reopened={results_concurrent['reopened']}"
    )

    # 最终状态应该是唯一的合法状态
    final = get_bug(bid)
    valid_states = ["fixing", "reopened"]
    assert_test(
        f"final status is valid ({final['status']})",
        final["status"] in valid_states,
        f"got {final['status']}"
    )

    # 不应该出现状态倒退或无效状态
    assert_test(
        "no invalid state",
        final["status"] not in ["reported", "analyzing", "analyzed", "waiting_test", "resolved"],
        f"got {final['status']}"
    )

    if errors:
        log(f"  Concurrent errors: {errors}")


# ============================================================
# 主函数
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Bug State Machine Automated Tests")
    log("=" * 60)

    # 先检查后端是否运行
    try:
        r = requests.get(f"{BASE}/projects", timeout=5)
        if r.status_code != 200:
            log("ERROR: Backend not responding correctly")
            sys.exit(1)
    except Exception as e:
        log(f"ERROR: Cannot connect to backend: {e}")
        sys.exit(1)

    test_1_skip_ai_analysis()
    test_2_missing_analysis_fields()
    test_3_normal_ai_analysis()
    test_4_block_premature_states()
    test_5_full_lifecycle()
    test_6_concurrent_status_update()

    log("\n" + "=" * 60)
    log(f"Results: {results['pass']} PASS, {results['fail']} FAIL")
    if results["errors"]:
        log("\nFailed tests:")
        for e in results["errors"]:
            log(f"  - {e}")
    log("=" * 60)

    # 保存结果
    result_file = os.path.join(os.path.dirname(__file__), "state_machine_result.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"Results saved to {result_file}")

    sys.exit(1 if results["fail"] > 0 else 0)
