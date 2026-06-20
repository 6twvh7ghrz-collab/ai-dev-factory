"""
Locust 压力测试 - Bug分析生命周期 (v2 - 修正状态机路径)
使用方法:
  自动化: python tests/run_load_test.py
  手动: locust -f tests/locustfile.py --host=http://localhost:8000
"""
import json
import random
from locust import HttpUser, task, between, events


class BugLifecycleUser(HttpUser):
    """模拟用户进行Bug完整生命周期操作"""
    wait_time = between(1, 3)
    host = "http://localhost:8000"

    TEST_PROJECT_ID = None
    created_bugs = []

    def on_start(self):
        if BugLifecycleUser.TEST_PROJECT_ID is None:
            resp = self.client.get("/api/projects", name="/api/projects [init]")
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    BugLifecycleUser.TEST_PROJECT_ID = data[0]["id"]

        if BugLifecycleUser.TEST_PROJECT_ID is None:
            resp = self.client.post(
                "/api/projects",
                json={"name": f"Load-test-{random.randint(1000,9999)}", "idea": "load test"},
                name="/api/projects [create-init]"
            )
            if resp.status_code == 200:
                BugLifecycleUser.TEST_PROJECT_ID = resp.json()["data"]["id"]

    @task(5)
    def list_bugs(self):
        if BugLifecycleUser.TEST_PROJECT_ID:
            self.client.get(
                f"/api/projects/{BugLifecycleUser.TEST_PROJECT_ID}/bugs",
                name="/api/projects/{id}/bugs"
            )

    @task(3)
    def list_projects(self):
        self.client.get("/api/projects", name="/api/projects [list]")

    @task(2)
    def get_project_detail(self):
        if BugLifecycleUser.TEST_PROJECT_ID:
            self.client.get(
                f"/api/projects/{BugLifecycleUser.TEST_PROJECT_ID}",
                name="/api/projects/{id} [detail]"
            )

    @task(4)
    def create_and_walk_bug(self):
        """创建Bug并走完完整生命周期 (遵循新状态机: analyzed必须通过AI接口)"""
        if not BugLifecycleUser.TEST_PROJECT_ID:
            return

        pid = BugLifecycleUser.TEST_PROJECT_ID

        # 1. Create Bug
        resp = self.client.post(
            f"/api/projects/{pid}/bugs",
            json={
                "title": f"Load-Bug-{random.randint(10000,99999)}",
                "description": "Load test bug",
                "error_message": "Test error",
                "reproduction_steps": "1. Click 2. Error",
                "expected_result": "OK",
                "actual_result": "500",
            },
            name="/api/projects/{id}/bugs [create]"
        )
        if resp.status_code != 200:
            return

        try:
            bug_id = resp.json()["data"]["id"]
        except (KeyError, ValueError):
            return

        BugLifecycleUser.created_bugs.append(bug_id)

        # 2. Get detail
        self.client.get(f"/api/bugs/{bug_id}", name="/api/bugs/{id} [detail]")

        # 3. reported -> analyzing (via status API)
        self.client.put(
            f"/api/bugs/{bug_id}/status",
            json={"status": "analyzing", "reason": "AI start"},
            name="/api/bugs/{id}/status [analyzing]"
        )

        # 4. analyzing -> analyzed (MUST use AI analyze endpoint, not status API)
        #    AI call may fail if not configured, which is expected
        resp = self.client.post(
            f"/api/bugs/{bug_id}/analyze",
            name="/api/bugs/{id}/analyze"
        )

        # 5. Generate CODEX (only works if AI analysis succeeded)
        self.client.post(
            f"/api/bugs/{bug_id}/generate-fix-prompt",
            json={},
            name="/api/bugs/{id}/generate-fix-prompt"
        )

        # 6. Save execution result (only works if in fixing state)
        self.client.post(
            f"/api/bugs/{bug_id}/execution-result",
            json={
                "execution_result": "CODEX load test result",
                "files_changed": "test.py",
                "test_result": "pass",
                "remaining_issues": "none",
            },
            name="/api/bugs/{id}/execution-result"
        )

        # 7. Test result (only works if in waiting_test state)
        self.client.post(
            f"/api/bugs/{bug_id}/test-result",
            json={"passed": True, "test_notes": "Load test pass"},
            name="/api/bugs/{id}/test-result"
        )

        # 8. Status logs
        self.client.get(
            f"/api/bugs/{bug_id}/status-logs",
            name="/api/bugs/{id}/status-logs"
        )

    @task(1)
    def get_random_bug_detail(self):
        if BugLifecycleUser.created_bugs:
            bug_id = random.choice(BugLifecycleUser.created_bugs[-50:])
            self.client.get(f"/api/bugs/{bug_id}", name="/api/bugs/{id} [random]")

    @task(1)
    def health_check(self):
        self.client.get("/api/health", name="/api/health")


class ReadOnlyUser(HttpUser):
    """只读用户"""
    wait_time = between(2, 5)
    host = "http://localhost:8000"

    @task(3)
    def list_projects(self):
        self.client.get("/api/projects", name="/api/projects [ro]")

    @task(5)
    def list_bugs(self):
        resp = self.client.get("/api/projects", name="/api/projects [ro-init]")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                pid = data[0]["id"]
                self.client.get(f"/api/projects/{pid}/bugs", name="/api/projects/{id}/bugs [ro]")

    @task(2)
    def health_check(self):
        self.client.get("/api/health", name="/api/health [ro]")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats
    print("\n" + "=" * 60)
    print("LOCUST TEST SUMMARY")
    print("=" * 60)
    print(f"Total requests: {stats.total.num_requests}")
    print(f"Total failures: {stats.total.num_failures}")
    print(f"Error rate: {stats.total.fail_ratio*100:.1f}%")
    print(f"Avg response time: {stats.total.avg_response_time:.0f}ms")
    if stats.total.num_requests > 0:
        print(f"P50: {stats.total.get_response_time_percentile(0.5):.0f}ms")
        print(f"P95: {stats.total.get_response_time_percentile(0.95):.0f}ms")
        print(f"P99: {stats.total.get_response_time_percentile(0.99):.0f}ms")

    for name, entry in stats.entries.items():
        if entry.num_requests > 0:
            print(f"  {name}: {entry.num_requests}req, {entry.num_failures}fail, "
                  f"avg={entry.avg_response_time:.0f}ms, "
                  f"P95={entry.get_response_time_percentile(0.95):.0f}ms")
