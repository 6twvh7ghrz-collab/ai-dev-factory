"""执行器控制台 API

Step 3 范围：
  POST /api/executor/start          - 启动自动循环（V1.2.2：统一决策入口）
  POST /api/executor/pause          - 暂停循环
  POST /api/executor/resume         - 恢复循环
  POST /api/executor/stop           - 停止循环
  GET  /api/executor/status         - 执行器状态（含 loop/workers/locks）
  GET  /api/executor/queue          - 队列状态（project_id 必填）
  GET  /api/executor/workers        - Worker 列表
  GET  /api/executor/resource-locks - 资源锁列表
  GET  /api/executor/merge-queue    - 合并队列
  GET  /api/executor/executions     - 执行记录列表
  GET  /api/executor/executions/{id} - 单条执行记录详情
  GET  /api/executor/logs           - 执行日志
  POST /api/executor/run-one        - 手动触发单任务执行（保留）

V1.2.2 新增:
  GET  /api/executor/start-decision - 统一启动决策（审计+决策）
"""
import json
import threading
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Depends, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database.engine import get_db
from app.core.config import settings
from app.core.response import ApiResponse
from app.models import DevelopmentTask, Project
from app.executor import (
    run_single_task, detect_available_adapters,
    RunStore, LoopController, TaskScheduler, RecoveryManager, BudgetGuard,
    ResourceLockManager, WorkerPool, MergeCoordinator,
)
from app.executor.result_collector import ResultCollector
from app.executor.workspace_guard import get_workspace_guard
from app.executor.project_execution_guard import get_project_execution_guard
from app.executor.start_decision import (
    get_start_decision_service, StartDecisionService, Decision,
)
from app.executor.execution_approval_service import (
    get_execution_approval_service, ExecutionApprovalService,
)
import logging

logger = logging.getLogger("executor.api")

router = APIRouter()

# 全局 LoopController 实例
_loop_controller: Optional[LoopController] = None
_loop_lock = threading.Lock()

# Step 3 新增全局实例
_worker_pool: Optional[WorkerPool] = None
_merge_coordinator: Optional[MergeCoordinator] = None


def _get_or_create_worker_pool() -> WorkerPool:
    """获取或创建全局 WorkerPool（需要时创建）"""
    global _worker_pool
    if _worker_pool is None:
        db_path = _get_db_path()
        repo_path = r"<EXECUTOR_SANDBOX_ROOT>"
        _worker_pool = WorkerPool(db_path, repo_path)
    return _worker_pool


def _check_provider_available(db_path: str) -> tuple:
    """检查 AI provider 是否可用，返回 (available: bool, reason: str)"""
    import sqlite3
    from app.core.security import decrypt_value
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT provider, model, api_key_encrypted, base_url FROM ai_configs WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return False, "未配置活跃的AI提供商"
        enc_key = row["api_key_encrypted"] or ""
        if not enc_key:
            return False, f"AI提供商 {row['provider']} 的API Key未配置"
        # 使用系统统一的 decrypt_value 解密
        try:
            decrypted = decrypt_value(enc_key)
            if not decrypted or len(decrypted) < 8:
                return False, f"AI提供商 {row['provider']} 的Key解密后无效"
        except Exception as e:
            return False, f"AI提供商 {row['provider']} 的Key解密失败: {e}"
        # 验证 base_url 配置
        if not row["base_url"]:
            return False, f"AI提供商 {row['provider']} 的Base URL未配置"
        return True, f"{row['provider']} {row['model']}"
    except Exception as e:
        return False, f"检查AI配置失败: {e}"


def _get_db_path() -> str:
    """获取数据库绝对路径"""
    db_url = settings.DATABASE_URL
    if db_url.startswith("sqlite:///"):
        return db_url.replace("sqlite:///", "")
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")


def _get_repo_path() -> str:
    """获取项目仓库根目录"""
    return str(Path(__file__).resolve().parent.parent.parent.parent)


def _get_loop_controller() -> LoopController:
    """获取或创建全局 LoopController"""
    global _loop_controller
    if _loop_controller is None:
        with _loop_lock:
            if _loop_controller is None:
                db_path = _get_db_path()
                _loop_controller = LoopController(db_path)
    return _loop_controller


# ═══════════════════════════════════════════════════════════
# Step 2 新增端点
# ═══════════════════════════════════════════════════════════

@router.post("/executor/start", summary="启动自动循环（V1.2.2：统一决策入口）")
async def start_loop(
    project_id: int = Query(..., description="项目ID"),
    mode: str = Query("auto_until_blocked", description="循环模式"),
):
    """
    V1.2.2：统一决策入口。

    点击"开始自动开发"后先调用 StartDecisionService，
    只有 decision=EXECUTE_READY_TASKS 才真正启动执行器。
    其他决策返回对应的指导信息，不创建 executor_run。

    启动前检查：
      - 项目执行配置验证（ProjectExecutionGuard）
      - 项目存在性
      - AI 提供商可用性
      - 至少有一个可执行任务（status=pending + 依赖满足 + 字段完整）

    - 无活跃 run：原子创建 starting 并启动循环线程
    - 已有活跃 run：返回 already_running=true
    - paused run：提示使用 resume
    """
    db_path = _get_db_path()

    # V1.2.2: 先做启动决策
    decision_svc = get_start_decision_service(db_path)
    decision_result = decision_svc.decide(project_id)

    # 只允许 EXECUTE_READY_TASKS 真正启动
    if decision_result.get("decision") != Decision.EXECUTE_READY_TASKS.value:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "data": {
                    "started": False,
                    "code": decision_result.get("decision"),
                    "reason": decision_result.get("summary", ""),
                    "decision": decision_result,
                },
                "message": decision_result.get("summary", "无法启动"),
                "error": {
                    "code": decision_result.get("decision"),
                    "detail": decision_result.get("summary", ""),
                },
            },
        )

    # 1. ProjectExecutionGuard 统一校验
    proj_guard = get_project_execution_guard(db_path)
    allowed, reason, sandbox_path, guard_detail = proj_guard.validate_for_start(project_id)
    if not allowed:
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "data": {
                    "started": False,
                    "code": guard_detail["code"] if guard_detail else "WORKSPACE_FORBIDDEN",
                    "reason": guard_detail["message"] if guard_detail else reason,
                },
                "message": "工作区安全验证失败",
                "error": {
                    "code": guard_detail["code"] if guard_detail else "WORKSPACE_FORBIDDEN",
                    "detail": guard_detail["message"] if guard_detail else reason,
                },
            },
        )

    # 2. 检查项目存在
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, name, status FROM projects WHERE id = ?", (project_id,))
    proj = cur.fetchone()
    conn.close()
    if not proj:
        return ApiResponse.error("INVALID_PROJECT", f"项目 #{project_id} 不存在",
                                 message=f"项目 #{project_id} 不存在")

    # 3. 检查 AI 提供商
    prov_ok, prov_status = _check_provider_available(db_path)
    if not prov_ok:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "data": {
                    "started": False,
                    "code": "PROVIDER_UNAVAILABLE",
                    "reason": prov_status,
                },
                "message": f"AI服务不可用: {prov_status}",
                "error": {"code": "PROVIDER_UNAVAILABLE", "detail": prov_status},
            },
        )

    # 4. 获取可执行任务 (V1.8C-R: filtered by approval scope)
    scheduler = TaskScheduler(db_path)
    runnable = scheduler.find_runnable_tasks(project_id)
    runnable_ids = [t.id for t in runnable]

    # V1.8C-R: Filter by execution approval scope if project has valid approval
    from app.executor.execution_approval_service import get_execution_approval_service as _get_ea_svc
    ea_svc = _get_ea_svc(db_path)
    approval = ea_svc.get_valid_approval(project_id)
    if approval:
        allowed = set(approval.get("allowed_task_ids", []))
        if allowed:
            runnable_ids = [tid for tid in runnable_ids if tid in allowed]
    logger.info(f"[executor/start] runnable_tasks({project_id})={runnable_ids}")

    # 5. 启动循环
    controller = _get_loop_controller()
    controller.repo_path = sandbox_path

    result = controller.start(project_id, mode)

    # 6. 初始化 WorkerPool
    _get_or_create_worker_pool()

    return JSONResponse(
        status_code=200,
        content={
            "ok": result.get("success", False),
            "data": {
                "started": result.get("success", False) and not result.get("already_running", False),
                "already_running": result.get("already_running", False),
                "run_id": result.get("run", {}).get("run_id") if result.get("run") else None,
                "status": result.get("run", {}).get("status", "unknown") if result.get("run") else None,
                "message": result.get("message", ""),
                "provider": prov_status,
                "runnable_tasks": len(runnable),
            },
            "message": result.get("message", "操作完成"),
            "error": None if result.get("success") else {
                "code": "START_FAILED", "detail": result.get("error", "unknown")
            },
        },
    )


@router.post("/executor/pause", summary="暂停循环")
async def pause_loop():
    """暂停当前循环，不再领取新任务"""
    controller = _get_loop_controller()
    if controller is None:
        return ApiResponse.error("NO_CONTROLLER", "没有活跃的循环控制器")

    result = controller.pause()
    if result.get("success"):
        return ApiResponse.success(data=result)
    else:
        return ApiResponse.error("PAUSE_FAILED", result.get("error", "unknown"))


@router.post("/executor/resume", summary="恢复循环")
async def resume_loop():
    """恢复暂停的循环"""
    controller = _get_loop_controller()
    if controller is None:
        return ApiResponse.error("NO_CONTROLLER", "没有活跃的循环控制器")

    result = controller.resume()
    if result.get("success"):
        return ApiResponse.success(data=result)
    else:
        return ApiResponse.error("RESUME_FAILED", result.get("error", "unknown"))


@router.post("/executor/stop", summary="停止循环")
async def stop_loop():
    """安全停止当前循环"""
    controller = _get_loop_controller()
    if controller is None:
        return ApiResponse.success(message="没有活跃的循环")

    result = controller.stop()
    if result.get("success"):
        return ApiResponse.success(data=result)
    else:
        return ApiResponse.error("STOP_FAILED", result.get("error", "unknown"))


@router.get("/executor/queue", summary="获取队列状态")
async def get_queue(
    project_id: int = Query(..., description="项目ID（必填）"),
):
    """获取项目任务队列状态。project_id 为必填参数。"""
    db_path = _get_db_path()
    scheduler = TaskScheduler(db_path)
    try:
        queue = scheduler.get_queue_status(project_id)
        return ApiResponse.success(data=queue)
    except Exception as e:
        return ApiResponse.error("QUEUE_ERROR", str(e))


# ═══════════════════════════════════════════════════════════
# Step 3 新增端点
# ═══════════════════════════════════════════════════════════

@router.get("/executor/workers", summary="获取 Worker 列表")
async def get_workers():
    """获取所有 Worker 状态"""
    if _worker_pool is None:
        return ApiResponse.success(data={"workers": [], "active_count": 0})
    return ApiResponse.success(data={
        "workers": _worker_pool.get_all_worker_statuses(),
        "active_count": _worker_pool.get_active_count(),
        "max_workers": WorkerPool.MAX_WORKERS,
    })


@router.get("/executor/resource-locks", summary="获取资源锁列表")
async def get_resource_locks(
    project_id: Optional[int] = Query(None, description="项目ID"),
    worker_id: Optional[str] = Query(None, description="Worker ID"),
):
    """查询活跃资源锁"""
    db_path = _get_db_path()
    lock_mgr = ResourceLockManager(db_path)
    try:
        locks = lock_mgr.get_active_locks(
            project_id=project_id,
            worker_id=worker_id,
        )
        return ApiResponse.success(data={
            "locks": locks,
            "count": len(locks),
        })
    except Exception as e:
        return ApiResponse.error("LOCKS_ERROR", str(e))


@router.get("/executor/merge-queue", summary="获取合并队列")
async def get_merge_queue():
    """获取合并队列状态"""
    if _merge_coordinator is None:
        return ApiResponse.success(data={"queue": [], "is_merging": False})
    return ApiResponse.success(data={
        "queue": _merge_coordinator.get_queue(),
        "is_merging": _merge_coordinator.is_merging(),
    })


# ═══════════════════════════════════════════════════════════
# 增强的 status 端点
# ═══════════════════════════════════════════════════════════

@router.get("/executor/status", summary="获取执行器状态")
async def get_executor_status():
    """返回执行器当前状态、Worker列表、资源锁、合并队列、运行中任务"""
    adapters = detect_available_adapters()
    db_path = _get_db_path()
    collector = ResultCollector(db_path)

    try:
        recent = collector.get_executions(limit=5)
        running = collector.get_running_executions()
    finally:
        collector.close()

    # 循环状态
    loop_status = None
    if _loop_controller:
        loop_status = _loop_controller.get_status()

    # 最近 run 记录
    store = RunStore(db_path)
    recent_runs = store.get_all_runs(limit=10)

    # Worker 状态
    workers = []
    active_count = 0
    if _worker_pool:
        workers = _worker_pool.get_all_worker_statuses()
        active_count = _worker_pool.get_active_count()

    # 资源锁
    lock_mgr = ResourceLockManager(db_path)
    active_locks = lock_mgr.get_active_locks()

    # 合并队列
    merge_queue = []
    is_merging = False
    if _merge_coordinator:
        merge_queue = _merge_coordinator.get_queue()
        is_merging = _merge_coordinator.is_merging()

    return ApiResponse.success(data={
        "loop": loop_status,
        "recent_runs": recent_runs,
        "available_adapters": [a.to_dict() for a in adapters],
        "workers": {
            "list": workers,
            "active_count": active_count,
            "max_workers": 1,  # 单Worker模式
        },
        "resource_locks": {
            "locks": active_locks,
            "count": len(active_locks),
        },
        "merge_queue": {
            "queue": merge_queue,
            "is_merging": is_merging,
        },
        "running_executions": [
            {"id": e.id, "task_id": e.task_id, "status": e.status, "started_at": e.started_at}
            for e in running
        ],
        "recent_executions": [
            {"id": e.id, "task_id": e.task_id, "status": e.status,
             "test_result": e.test_result, "duration_ms": e.duration_ms}
            for e in recent
        ],
    })


# ═══════════════════════════════════════════════════════════
# 保留 Step 1 端点
# ═══════════════════════════════════════════════════════════

@router.get("/executor/project-config/{project_id}", summary="获取项目执行配置")
async def get_project_execution_config(project_id: int):
    """获取项目执行配置信息（用于前端显示）"""
    from app.executor.project_execution_guard import get_project_execution_guard
    db_path = _get_db_path()
    guard = get_project_execution_guard(db_path)
    config = guard.get_config(project_id)

    if not config:
        return ApiResponse.success(data={
            "configured": False,
            "project_id": project_id,
            "message": "此项目尚未授权AI自动执行",
        })

    # 获取项目名称
    import sqlite3 as _sql
    conn = _sql.connect(db_path)
    conn.row_factory = _sql.Row
    c = conn.cursor()
    c.execute("SELECT name FROM projects WHERE id=?", (project_id,))
    proj = c.fetchone()
    conn.close()

    import json
    workspace = config.get("workspace_path", "")
    # 提取工作区文件夹名
    import os
    workspace_name = os.path.basename(workspace) if workspace else "未配置"

    return ApiResponse.success(data={
        "configured": True,
        "project_id": project_id,
        "project_name": proj["name"] if proj else "",
        "workspace_path": workspace,
        "workspace_name": workspace_name,
        "execution_enabled": bool(config.get("execution_enabled", 0)),
        "execution_mode": config.get("execution_mode", "sandbox"),
        "allowed_models": json.loads(config.get("allowed_models_json", "[]")),
        "max_workers": config.get("max_workers", 1),
        "max_tasks": config.get("max_tasks", 10),
        "requires_confirmation": bool(config.get("requires_confirmation", 1)),
    })


@router.get("/executor/start-decision", summary="统一启动决策（V1.2.2）")
async def get_start_decision(
    project_id: int = Query(..., description="项目ID"),
):
    """
    V1.2.2：统一启动决策端点。

    返回启动决策和项目审计信息，前端根据决策类型显示不同的UI。
    不创建 executor_run，不启动任何执行。

    决策类型：
    - EXECUTE_READY_TASKS: 有可执行任务，可以开始执行
    - PLAN_EXISTING_TASKS: 有 needs_planning 任务，需要规划
    - GENERATE_TASKS: 没有任务，需要生成
    - BIND_WORKSPACE: 缺少工作区绑定
    - WAIT_DEPENDENCIES: 依赖未完成
    - REQUEST_APPROVAL: 高风险项目需确认
    - ALREADY_RUNNING: 已有活跃 run
    - PROJECT_COMPLETED: 全部完成
    - BLOCK_UNSAFE: 真正不安全
    """
    db_path = _get_db_path()
    decision_svc = get_start_decision_service(db_path)
    result = decision_svc.decide(project_id)

    if not result.get("ok"):
        return ApiResponse.error(
            result.get("error", "DECISION_ERROR"),
            result.get("summary", "决策失败"),
        )

    return ApiResponse.success(data=result)


@router.get("/executor/preflight", summary="执行前检查")
async def preflight_check(
    project_id: int = Query(..., description="项目ID"),
):
    """
    执行前完整检查，用于前端判断"开始自动开发"按钮是否可用。

    返回：
    - can_start: 是否可以启动
    - runnable_task_ids: 当前可执行任务 ID 列表
    - blocked_task_ids: 阻塞任务 ID 列表
    - active_run: 是否有活跃 executor_run
    - active_leases: 活跃 lease 数量
    - database_path: 数据库绝对路径
    """
    import os as _os
    db_path = _get_db_path()
    scheduler = TaskScheduler(db_path)
    store = RunStore(db_path)

    runnable = scheduler.find_runnable_tasks(project_id)
    runnable_ids = [t.id for t in runnable]

    # V1.8C-R: Check execution approval scope for high-risk projects
    approval_svc = get_execution_approval_service(db_path)
    approval = approval_svc.get_valid_approval(project_id)
    allowed_task_ids = approval.get("allowed_task_ids", []) if approval else []
    is_high_risk_with_approval = bool(approval)
    if allowed_task_ids:
        runnable_ids_before = len(runnable_ids)
        runnable_ids = [tid for tid in runnable_ids if tid in allowed_task_ids]
        excluded_count = runnable_ids_before - len(runnable_ids)
        if excluded_count > 0:
            logger.info(
                f"[preflight] V1.8C-R: project {project_id} approval scope "
                f"excluded {excluded_count} tasks outside {allowed_task_ids}"
            )

    active_run = store.get_active_run(project_id)
    queue_status = scheduler.get_queue_status(project_id)
    blocked_ids = [bt["id"] for bt in queue_status.get("blocked_tasks", [])]

    # active leases count
    import sqlite3 as _sql
    conn = _sql.connect(db_path)
    conn.row_factory = _sql.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active' AND expires_at > datetime('now')")
    lease_cnt = cur.fetchone()["cnt"]
    conn.close()

    # 检查是否有 failed/blocked/test_failed 任务（可重试）
    conn2 = _sql.connect(db_path)
    conn2.row_factory = _sql.Row
    cur2 = conn2.cursor()
    cur2.execute("SELECT id, status FROM development_tasks WHERE project_id=? AND status IN ('failed','test_failed','blocked')", (project_id,))
    retryable = [{"id": r["id"], "status": r["status"]} for r in cur2.fetchall()]
    conn2.close()

    can_start = len(runnable_ids) > 0 and not active_run and lease_cnt == 0

    return ApiResponse.success(data={
        "can_start": can_start,
        "runnable_task_ids": runnable_ids,
        "blocked_task_ids": blocked_ids,
        "retryable_tasks": retryable,
        "active_run": active_run is not None,
        "active_run_status": active_run["status"] if active_run else None,
        "active_leases": lease_cnt,
        "database_path": db_path,
        "pid": _os.getpid(),
        # V1.8C-R: Execution approval status
        "has_execution_approval": is_high_risk_with_approval,
        "allowed_task_ids": allowed_task_ids,
    })


# ═══════════════════════════════════════════════════════════
# V1.8C: Project Execution Approval API
# ═══════════════════════════════════════════════════════════

@router.get("/executor/execution-approval/status", summary="查询项目执行审批状态（V1.8C）")
async def get_execution_approval_status(
    project_id: int = Query(..., description="项目ID"),
):
    """查询指定项目的执行审批状态。

    返回是否有有效的执行审批，以及最近审批记录的状态。
    """
    db_path = _get_db_path()
    svc = get_execution_approval_service(db_path)
    result = svc.get_approval_status(project_id)
    return ApiResponse.success(data=result)


@router.post("/executor/execution-approval/request", summary="请求项目执行审批（V1.8C）")
async def request_execution_approval(
    project_id: int = Query(..., description="项目ID"),
    allowed_task_ids: str = Query("[]", description="允许执行的任务ID列表（JSON数组，如[31]）"),
    max_workers: int = Query(1, ge=1, le=4, description="最大并发Worker数"),
    auto_run_downstream: bool = Query(False, description="是否自动执行下游任务"),
    approval_reason: str = Query("", description="审批原因"),
    expiry_hours: int = Query(1, ge=1, le=24, description="审批有效期（小时）"),
):
    """请求一个一次性项目级执行审批。

    用于高风险项目启动Executor前的审批流程。
    返回一个确认令牌（confirmation_token），需在5分钟内调用 /approve 确认。

    限制：
    - allowed_task_ids 必须精确指定，防止未授权任务执行
    - 审批默认有效期为1小时
    - 审批为一次性使用（启动Executor后自动消费）
    """
    try:
        task_ids = json.loads(allowed_task_ids)
        if not isinstance(task_ids, list):
            return ApiResponse.error("INVALID_TASK_IDS", "allowed_task_ids 必须是JSON数组")
    except json.JSONDecodeError:
        return ApiResponse.error("INVALID_JSON", "allowed_task_ids 不是有效的JSON")

    db_path = _get_db_path()
    svc = get_execution_approval_service(db_path)
    result = svc.request_approval(
        project_id=project_id,
        allowed_task_ids=task_ids,
        max_workers=max_workers,
        auto_run_downstream=auto_run_downstream,
        approval_reason=approval_reason,
        expiry_hours=expiry_hours,
    )

    if result.get("ok"):
        return ApiResponse.success(
            data=result,
            message=result.get("message", "审批请求已创建")
        )
    else:
        return ApiResponse.error(
            result.get("error", "REQUEST_FAILED"),
            result.get("message", "审批请求失败")
        )


@router.post("/executor/execution-approval/approve", summary="批准项目执行审批（V1.8C）")
async def approve_execution_approval(
    project_id: int = Query(..., description="项目ID"),
    confirmation_token: str = Query(..., description="确认令牌（从 /request 返回）"),
    approved_by: str = Query("user", description="批准人"),
):
    """批准项目执行审批。

    验证确认令牌（一次性消费），将审批状态设为 approved。
    之后 StartDecisionService 将识别有效审批，允许高风险项目启动。

    注意：令牌仅5分钟有效，且只能使用一次。
    """
    db_path = _get_db_path()
    svc = get_execution_approval_service(db_path)
    result = svc.approve(
        project_id=project_id,
        confirmation_token=confirmation_token,
        approved_by=approved_by,
    )

    if result.get("ok"):
        return ApiResponse.success(
            data=result,
            message=result.get("message", "审批已批准")
        )
    else:
        return ApiResponse.error(
            result.get("error", "APPROVE_FAILED"),
            result.get("message", "审批批准失败")
        )


@router.post("/executor/execution-approval/reject", summary="拒绝项目执行审批（V1.8C）")
async def reject_execution_approval(
    project_id: int = Query(..., description="项目ID"),
    confirmation_token: str = Query(..., description="确认令牌（从 /request 返回）"),
):
    """拒绝项目执行审批。

    将审批状态设为 rejected，之后该令牌不可再用于批准。
    """
    db_path = _get_db_path()
    svc = get_execution_approval_service(db_path)
    result = svc.reject(
        project_id=project_id,
        confirmation_token=confirmation_token,
    )

    if result.get("ok"):
        return ApiResponse.success(
            data=result,
            message=result.get("message", "审批已拒绝")
        )
    else:
        return ApiResponse.error(
            result.get("error", "REJECT_FAILED"),
            result.get("message", "审批拒绝失败")
        )


@router.get("/executor/executions", summary="获取执行记录列表")
async def list_executions(
    task_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """查询执行记录"""
    db_path = _get_db_path()
    collector = ResultCollector(db_path)
    try:
        executions = collector.get_executions(task_id=task_id, status=status, limit=limit)
    finally:
        collector.close()

    return ApiResponse.success(data=[
        {
            "id": e.id,
            "task_id": e.task_id,
            "project_id": e.project_id,
            "worker_id": e.worker_id,
            "status": e.status,
            "worktree_path": e.worktree_path,
            "start_commit": e.start_commit,
            "started_at": e.started_at,
            "completed_at": e.completed_at,
            "duration_ms": e.duration_ms,
            "repair_count": e.repair_count,
            "test_result": e.test_result,
            "exit_code": e.exit_code,
            "error_message": e.error_message,
            "safety_passed": e.safety_passed,
            "files_modified": e.files_modified,
        }
        for e in executions
    ])


@router.get("/executor/executions/{execution_id}", summary="获取执行记录详情")
async def get_execution(execution_id: int):
    """获取单条执行记录详情（含步骤日志）"""
    db_path = _get_db_path()
    collector = ResultCollector(db_path)
    try:
        execution = collector.get_execution(execution_id)
        if not execution:
            return ApiResponse.not_found("执行记录")

        logs = collector.get_logs(execution_id)
    finally:
        collector.close()

    return ApiResponse.success(data={
        "execution": {
            "id": execution.id,
            "task_id": execution.task_id,
            "project_id": execution.project_id,
            "worker_id": execution.worker_id,
            "status": execution.status,
            "worktree_path": execution.worktree_path,
            "start_commit": execution.start_commit,
            "started_at": execution.started_at,
            "completed_at": execution.completed_at,
            "duration_ms": execution.duration_ms,
            "repair_count": execution.repair_count,
            "test_result": execution.test_result,
            "exit_code": execution.exit_code,
            "execution_result": execution.execution_result,
            "error_message": execution.error_message,
            "safety_passed": execution.safety_passed,
            "files_checked": execution.files_checked,
            "files_modified": execution.files_modified,
            "model_calls": execution.model_calls,
        },
        "logs": [
            {
                "id": log.id,
                "step_name": log.step_name,
                "step_status": log.step_status,
                "command": log.command,
                "stdout": log.stdout[:2000] if log.stdout else "",
                "stderr": log.stderr[:2000] if log.stderr else "",
                "exit_code": log.exit_code,
                "duration_ms": log.duration_ms,
                "detail": log.detail,
            }
            for log in logs
        ],
    })


@router.get("/executor/logs", summary="获取执行日志")
async def get_execution_logs(
    execution_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """获取执行日志"""
    db_path = _get_db_path()
    collector = ResultCollector(db_path)
    try:
        if execution_id:
            logs = collector.get_logs(execution_id)
        else:
            recent = collector.get_executions(limit=1)
            if recent:
                logs = collector.get_logs(recent[0].id)
            else:
                logs = []
    finally:
        collector.close()

    return ApiResponse.success(data=[
        {
            "id": log.id,
            "execution_id": log.execution_id,
            "step_name": log.step_name,
            "step_status": log.step_status,
            "command": log.command,
            "exit_code": log.exit_code,
            "duration_ms": log.duration_ms,
            "detail": log.detail,
        }
        for log in logs[:limit]
    ])


@router.post("/executor/run-one", summary="手动触发单任务执行")
async def run_one_task(
    task_id: int,
    project_id: int,
    allowed_files: Optional[str] = Query(None, description="允许修改的文件，逗号分隔"),
    test_command: Optional[str] = Query(None, description="测试命令"),
    execute_command: Optional[str] = Query(None, description="执行命令"),
    repo_path: Optional[str] = Query(None, description="Git仓库路径"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """
    手动触发单个任务执行（保留 Step 1 接口）。

    示例:
      POST /api/executor/run-one?task_id=1&project_id=1
           &allowed_files=calculator.py
           &test_command=pytest test_calculator.py -v
           &execute_command=python fix_calculator.py
           &repo_path=C:/SandboxUser/本机/Desktop/executor-sandbox-v2
    """
    # 验证任务存在
    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    # 解析参数
    files = [f.strip() for f in allowed_files.split(",")] if allowed_files else None
    test_cmd = test_command.split() if test_command else None
    exec_cmd = execute_command.split() if execute_command else None
    actual_repo = repo_path or _get_repo_path()

    # 工作区安全边界验证（使用 ProjectExecutionGuard 统一校验）
    proj_guard = get_project_execution_guard(_get_db_path())
    allowed, reason, ws_path, guard_detail = proj_guard.validate_for_start(project_id)
    if not allowed:
        logger.warning(f"[SECURITY] project execution rejected: {reason}")
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "data": None,
                "message": "工作区安全验证失败",
                "error": {
                    "code": guard_detail["code"] if guard_detail else "WORKSPACE_FORBIDDEN",
                    "detail": guard_detail["message"] if guard_detail else reason,
                },
            },
        )
    actual_repo = ws_path

    # 验证任务状态为 pending
    if task.status != "pending":
        return ApiResponse.error(
            "INVALID_TASK_STATUS",
            f"任务状态必须为 pending，当前: {task.status}"
        )

    # 在后台线程执行
    def _execute():
        import datetime
        try:
            run_single_task(
                db_path=_get_db_path(),
                task_id=task_id,
                project_id=project_id,
                repo_path=actual_repo,
                allowed_files=files,
                test_command=test_cmd,
                execute_command=exec_cmd,
            )
        except Exception as e:
            logger.error(f"run-one failed: {e}")

    if background_tasks:
        background_tasks.add_task(_execute)
    else:
        threading.Thread(target=_execute, daemon=True).start()

    return ApiResponse.success(
        data={
            "task_id": task_id,
            "project_id": project_id,
            "allowed_files": files,
            "test_command": test_cmd,
            "execute_command": exec_cmd,
        },
        message="任务已提交执行"
    )
