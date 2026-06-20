"""规划 API V1.4

POST /api/planner/preview          - 生成工程规划预览
GET  /api/planner/previews/{id}    - 获取规划预览详情
POST /api/planner/approval-preview - 审批预检
POST /api/planner/approve          - 正式批准
POST /api/planner/reject           - 拒绝规划
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.response import ApiResponse
from app.core.config import settings
from app.executor.start_decision import (
    get_start_decision_service, Decision,
)
from app.planner.planner_preview_service import (
    get_planner_preview_service,
    is_planning_in_progress,
)
from app.planner.planning_approval_service import (
    get_planning_approval_service,
)
import logging

logger = logging.getLogger("planner.api")

router = APIRouter()


# ── 请求模型 ──

class PlannerPreviewRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    task_ids: Optional[List[int]] = Field(
        default=None,
        description="要规划的任务ID列表，为空则规划所有needs_planning任务"
    )
    force_regenerate: bool = Field(
        default=False,
        description="强制重新生成，即使已有未过期预览"
    )


class ApprovalPreviewRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    preview_id: str = Field(..., description="规划预览ID")
    selected_task_ids: List[int] = Field(..., description="选中的任务ID列表")


class ApproveRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    preview_id: str = Field(..., description="规划预览ID")
    selected_task_ids: List[int] = Field(..., description="选中的任务ID列表")
    confirmation_token: str = Field(..., description="确认令牌")


class RejectRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    preview_id: str = Field(..., description="规划预览ID")


def _get_db_path() -> str:
    """获取数据库绝对路径"""
    from pathlib import Path

    db_url = settings.DATABASE_URL
    if db_url.startswith("sqlite:///"):
        return db_url.replace("sqlite:///", "")
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")


# ── POST /api/planner/preview ──

@router.post("/planner/preview", summary="生成工程规划预览")
async def generate_plan_preview(req: PlannerPreviewRequest):
    """
    V1.4：为 needs_planning 任务生成 AI 工程规划预览，并持久化。

    前置条件：
    - 项目存在
    - StartDecisionService 决策为 PLAN_EXISTING_TASKS
    - needs_planning_count > 0
    - 不存在活跃规划请求
    - 模型配置可用

    持久化到 planning_previews 表，24小时有效期。
    不修改 development_tasks 等业务数据。
    """
    project_id = req.project_id
    db_path = _get_db_path()

    # 1. 检查项目存在
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM projects WHERE id = ?", (project_id,))
        proj = cur.fetchone()
    finally:
        conn.close()

    if not proj:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "data": None,
                "message": f"项目 #{project_id} 不存在",
                "error": {
                    "code": "PROJECT_NOT_FOUND",
                    "detail": f"项目 #{project_id} 不存在",
                },
            },
        )

    # 2. 检查启动决策
    decision_svc = get_start_decision_service(db_path)
    decision_result = decision_svc.decide(project_id)

    if not decision_result.get("ok"):
        return ApiResponse.error(
            "DECISION_ERROR",
            decision_result.get("summary", "无法获取启动决策"),
        )

    if decision_result.get("decision") != Decision.PLAN_EXISTING_TASKS.value:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "code": "WRONG_DECISION",
                "project_id": project_id,
                "message": f"当前项目决策为 {decision_result.get('decision')}，不是 PLAN_EXISTING_TASKS，无法生成规划预览",
                "data": {
                    "decision": decision_result.get("decision"),
                    "summary": decision_result.get("summary"),
                },
            },
        )

    # 3. 检查 needs_planning_count
    needs_count = decision_result.get("details", {}).get("needs_planning", 0)
    if needs_count <= 0:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "code": "NO_NEEDS_PLANNING_TASKS",
                "project_id": project_id,
                "message": "该项目没有待规划的任务",
                "data": None,
            },
        )

    # 4. 检查并发规划
    if is_planning_in_progress(project_id):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "PLANNING_ALREADY_IN_PROGRESS",
                "project_id": project_id,
                "message": "该项目已有规划请求正在进行中",
                "data": None,
            },
        )

    # 5. 调用 PlannerPreviewService
    planner = get_planner_preview_service(db_path)
    result = planner.generate_preview(project_id, req.task_ids, req.force_regenerate)

    if result.get("ok"):
        return ApiResponse.success(
            data={
                "ok": True,
                "code": result["code"],
                "executed": result.get("executed", False),
                "project_id": result["project_id"],
                "project_name": result.get("project_name", ""),
                "preview_id": result.get("preview_id"),
                "expires_at": result.get("expires_at"),
                "preview": result.get("preview"),
                "call_record": result.get("call_record"),
            },
            message="规划预览生成成功",
        )
    else:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "code": result.get("code", "PLANNER_ERROR"),
                "project_id": project_id,
                "message": result.get("message", "规划预览生成失败"),
                "data": None,
                "error": {
                    "code": result.get("code", "PLANNER_ERROR"),
                    "detail": result.get("message", "规划预览生成失败"),
                },
            },
        )


# ── GET /api/planner/previews/{preview_id} ──

@router.get("/planner/previews/{preview_id}", summary="获取规划预览详情")
async def get_plan_preview(preview_id: str):
    """只读获取规划预览详情"""
    db_path = _get_db_path()
    approval_svc = get_planning_approval_service(db_path)
    result = approval_svc.get_preview(preview_id)

    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "code": "PLAN_NOT_FOUND",
                "message": "规划预览不存在",
                "data": None,
            },
        )

    return ApiResponse.success(data=result, message="获取规划预览成功")


# ── POST /api/planner/approval-preview ──

@router.post("/planner/approval-preview", summary="审批预检")
async def approval_preview(req: ApprovalPreviewRequest):
    """
    V1.4：审批预检，不修改任何数据库数据。

    检查：规划存在性、有效期、快照一致性、任务状态、活跃run/lease/lock等。
    返回确认令牌（60秒有效期）和风险分级结果。
    """
    db_path = _get_db_path()
    approval_svc = get_planning_approval_service(db_path)
    result = approval_svc.preview_approval(
        project_id=req.project_id,
        preview_id=req.preview_id,
        selected_task_ids=req.selected_task_ids,
    )

    if result.get("ok"):
        return ApiResponse.success(data=result, message="审批预检通过")
    else:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "code": result.get("code", "APPROVAL_ERROR"),
                "message": result.get("message", "审批预检失败"),
                "data": result,
            },
        )


# ── POST /api/planner/approve ──

@router.post("/planner/approve", summary="正式批准规划")
async def approve_plan(req: ApproveRequest):
    """
    V1.4：正式审批并安全写回任务。

    事务化执行：验证令牌 → 重新预检 → BEGIN IMMEDIATE → 写回安全任务 → 创建审批记录 → COMMIT
    任一步骤失败则全部回滚。

    高风险任务保持 needs_planning，不启动 Executor。
    """
    db_path = _get_db_path()
    approval_svc = get_planning_approval_service(db_path)
    result = approval_svc.approve(
        project_id=req.project_id,
        preview_id=req.preview_id,
        selected_task_ids=req.selected_task_ids,
        confirmation_token=req.confirmation_token,
    )

    if result.get("ok"):
        return ApiResponse.success(data=result, message="审批完成")
    else:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "code": result.get("code", "APPROVAL_ERROR"),
                "message": result.get("message", "审批失败"),
                "data": result,
            },
        )


# ── POST /api/planner/reject ──

@router.post("/planner/reject", summary="拒绝规划")
async def reject_plan(req: RejectRequest):
    """V1.4：拒绝规划预览，只修改 planning_previews 状态，不修改任务。"""
    db_path = _get_db_path()
    approval_svc = get_planning_approval_service(db_path)
    result = approval_svc.reject(
        project_id=req.project_id,
        preview_id=req.preview_id,
    )

    if result.get("ok"):
        return ApiResponse.success(data=result, message="规划已拒绝")
    else:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "code": result.get("code", "REJECT_ERROR"),
                "message": result.get("message", "拒绝失败"),
                "data": result,
            },
        )
