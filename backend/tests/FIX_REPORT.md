# Bug 状态流转漏洞修复报告

## 1. 根本原因

Bug 的 `PUT /bugs/{id}/status` 接口仅检查状态流转路径是否合法（`STATUS_TRANSITIONS`），但**不检查关键字段是否已填充**。这导致：

- 用户可以通过普通状态更新接口将 Bug 从 `analyzing` 直接设为 `analyzed`，但 `probable_cause`、`fix_plan`、`test_steps` 仍为空
- 后续调用"生成CODEX修复指令"时因 `probable_cause` 为空而失败
- 类似地，`fix_ready`、`waiting_test`、`resolved` 也可以在没有必要数据的情况下被手动设置

**核心问题**：状态流转和数据填充脱钩，状态可以脱离实际数据独立推进。

## 2. 修改文件清单

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `backend/app/api/bugs.py` | 核心修改 | 添加状态前置条件检查、专用接口限制、事务保护 |
| `frontend/src/pages/ProjectDetail.tsx` | 前端修改 | 添加操作可用性检查函数、禁用非法按钮并提示原因 |
| `backend/tests/test_state_machine.py` | 新增 | 6场景状态机自动化测试 |
| `backend/tests/regression_v3.py` | 新增 | 适配新状态机的回归测试v3 |
| `backend/tests/locustfile.py` | 修改 | 适配新状态机，用AI分析接口替代手动analyzed |

## 3. 状态机规则

### 合法流转

```text
reported → analyzing → analyzed → fix_ready → fixing → waiting_test → resolved → closed
```

### 特殊流转

```text
waiting_test → reopened (测试失败)
resolved → reopened (重新打开)
closed → reopened (重新打开)
reopened → analyzing (重新分析)
reopened → closed (直接关闭)
任意未关闭状态 → closed (直接关闭，仅reported/resolved允许)
```

### 专用接口入口（普通状态接口不允许）

| 状态 | 必须通过 | 接口 |
|------|----------|------|
| `analyzed` | AI分析接口 | `POST /bugs/{id}/analyze` |
| `fix_ready` | 修复指令生成接口 | `POST /bugs/{id}/generate-fix-prompt` |
| `waiting_test` | 执行结果保存接口 | `POST /bugs/{id}/execution-result` |
| `resolved` | 测试结果接口 | `POST /bugs/{id}/test-result` |

### 关键字段前置条件

| 目标状态 | 必须存在的字段 |
|----------|---------------|
| `analyzed` | `probable_cause`, `fix_plan`, `test_steps` |
| `fix_ready` | `fix_prompt`, `probable_cause`, `fix_plan` |
| `fixing` | `fix_prompt` |
| `waiting_test` | `execution_result` |
| `resolved` | `test_result` |

## 4. 新增的后端校验

### 4.1 `STATUS_PRECONDITIONS` 字典

定义了进入每个状态所需的字段和中文名称，用于生成可读的错误提示。

### 4.2 `STATUS_REQUIRES_SPECIAL_ENDPOINT` 字典

定义了只能通过专用接口进入的状态及其对应的接口路径。

### 4.3 `_check_preconditions(bug, to_status)` 函数

双层检查：
1. **专用接口检查**：如果目标状态在 `STATUS_REQUIRES_SPECIAL_ENDPOINT` 中，返回 `BUG_STATE_REQUIRES_SPECIAL_ENDPOINT` 错误
2. **字段检查**：如果关键字段为空，返回 `BUG_STATE_PRECONDITION_FAILED` 错误

### 4.4 `update_bug_status` 接口修改

- 调用 `_check_preconditions` 进行前置检查
- 检查不通过返回 HTTP 409 + 具体错误原因
- 添加事务保护（try/except + rollback）

### 4.5 专用接口修改

- `generate_fix_prompt`：添加状态检查，只允许 `analyzed/fix_ready/reopened` 状态
- `save_execution_result`：添加状态检查，只允许 `fix_ready/fixing` 状态
- 所有专用接口：添加事务保护，失败时回滚

## 5. 新增的前端限制

### 5.1 操作可用性检查函数

| 函数 | 检查内容 |
|------|----------|
| `canAnalyzeBug(b)` | 状态必须是 `reported` 或 `reopened` |
| `canGenerateFix(b)` | 状态必须是 `analyzed/fix_ready/reopened`，且 `probable_cause` 非空 |
| `canMarkFixing(b)` | 状态必须是 `fix_ready`，且 `fix_prompt` 非空 |
| `canSaveExecution(b)` | 状态必须是 `fixing` |
| `canTestPass(b)` | 状态必须是 `waiting_test` |
| `canReopen(b)` | 状态必须是 `resolved` 或 `closed` |

### 5.2 禁用按钮 + 原因提示

所有操作按钮在条件不满足时被禁用（`disabled`），并通过 `title` 属性和旁边的文字提示显示禁用原因。

## 6. 数据库事务处理说明

| 操作 | 事务范围 | 失败回滚 |
|------|----------|----------|
| AI分析 | 保存分析结果 + 更新状态为analyzed + 记录状态日志 | 状态回滚到 `reported/reopened` |
| 生成修复指令 | 保存fix_prompt + 更新状态为fix_ready + 记录状态日志 | 回滚所有变更 |
| 保存执行结果 | 保存execution_result + 更新状态为waiting_test + 记录状态日志 | 回滚所有变更 |
| 保存测试结果 | 保存test_result + 更新状态为resolved/reopened + 记录状态日志 | 回滚所有变更 |
| 普通状态更新 | 更新状态 + 记录状态日志 | 回滚所有变更 |

关键改动：`_log_status_change` 从自动 `db.commit()` 改为 `db.flush()`，使其可参与调用方的事务。

## 7. 自动化测试结果

### 7.1 状态机测试（6场景）

```
Test 1: Skip AI analysis, direct set analyzed
  PASS: reported->analyzed returns 409
  PASS: response ok=false
  PASS: error code blocks illegal transition
  PASS: status still reported
  PASS: probable_cause is empty

Test 2: Missing analysis fields, block analyzed
  PASS: analyzing->analyzed via status API returns 409
  PASS: error code blocks illegal transition
  PASS: status still analyzing (not reverted or advanced)
  PASS: status is NOT analyzed

Test 3: Normal AI analysis flow
  PASS: AI analysis succeeded
  PASS: status is analyzed
  PASS: probable_cause/fix_plan/test_steps exist
  PASS: after refresh: data still exists

Test 4: Block premature state transitions
  PASS: reported->fix_ready blocked (409)
  PASS: reported->fixing blocked (409)
  PASS: reported->waiting_test blocked (409)
  PASS: reported->resolved blocked (409)
  PASS: reported->analyzed blocked (409)

Test 5: Full lifecycle via dedicated endpoints
  PASS: All 13 lifecycle steps
  PASS: Data integrity preserved after reopen

Test 6: Concurrent status updates
  PASS: At least one concurrent update succeeded
  PASS: Final state is valid
  PASS: No invalid state

Results: 39 PASS, 0 FAIL
```

### 7.2 回归测试（10次完整循环）

```
7 mock AI + 3 real AI lifecycle runs
State machine enforcement tests
Validation tests
Data consistency checks

Results: 223 PASS, 0 FAIL
```

### 7.3 状态机强制执行验证

| 非法操作 | 结果 |
|----------|------|
| `reported→analyzed` | 409 Conflict |
| `reported→fix_ready` | 409 Conflict |
| `reported→fixing` | 409 Conflict |
| `reported→waiting_test` | 409 Conflict |
| `reported→resolved` | 409 Conflict |
| `analyzing→analyzed` (手动) | 409 Conflict |
| 执行结果保存于reported状态 | 拒绝 |
| 测试结果保存于reported状态 | 拒绝 |

## 8. 5用户压力测试结果

```
5用户并发3分钟 (Locust)

Total requests: 900
Total failures: 0
Error rate: 0.0%
Avg response time: 19ms
P50: 4ms
P95: 19ms
P99: 63ms

所有接口正常响应，无数据损坏，无状态错乱。
```

## 9. 数据一致性检查结果

| 检查项 | 结果 |
|--------|------|
| API数量 = 数据库数量 | 通过 |
| 无孤立Bug记录 | 通过 |
| 状态日志与数据库一致 | 通过 |
| 无卡在analyzing状态的Bug | 通过 |
| 无假成功 | 通过 |
| 无半成品事务 | 通过 |
| 无状态倒退 | 通过 |

## 10. 是否具备正式上线条件

### 结论：**YES**

理由：

1. **核心漏洞已修复**：`analyzed`/`fix_ready`/`waiting_test`/`resolved` 四个状态现在只能通过专用接口进入，无法通过普通状态更新绕过
2. **字段前置条件已实施**：进入关键状态前检查必要字段，缺少数据时返回409 + 明确错误原因
3. **事务保护已添加**：AI分析、指令生成、执行保存、测试结果四个操作均在事务中执行，失败时回滚
4. **前端已适配**：非法操作按钮被禁用并显示原因
5. **自动化测试全部通过**：39个状态机测试 + 223个回归测试 + 0%错误率压力测试
6. **无数据损坏**：压力测试期间无假成功、无半成品事务、无状态错乱
7. **向后兼容**：已有的合法数据不受影响，只是阻止了未来的非法操作
