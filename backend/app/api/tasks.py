"""开发任务 API"""
import json
import logging
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import Project, DevelopmentTask, RequirementAnalysis, AiGenerationLog
from app.core.response import ApiResponse
from app.core.logging import get_logger
from app.ai.prompts import TASK_SYSTEM_PROMPT
from app.api.ai_config import get_active_provider
from app.schemas.task import TaskUpdate
from pathlib import Path

logger = get_logger(__name__)
router = APIRouter()


@router.get("/projects/{project_id}/tasks", summary="获取项目任务列表")
async def list_tasks(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    tasks = db.query(DevelopmentTask).filter(
        DevelopmentTask.project_id == project_id
    ).order_by(DevelopmentTask.sort_order, DevelopmentTask.priority.desc()).all()

    return ApiResponse.success(data=[_task_out(t) for t in tasks])


@router.get("/tasks/{task_id}", summary="获取任务详情")
async def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")
    return ApiResponse.success(data=_task_out(task))


@router.put("/tasks/{task_id}", summary="更新任务")
async def update_task(task_id: int, data: TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(task, key, value)

    db.commit()
    db.refresh(task)
    return ApiResponse.success(data=_task_out(task), message="任务更新成功")


@router.post("/projects/{project_id}/generate-tasks", summary="AI生成开发任务")
async def generate_tasks(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    analysis = db.query(RequirementAnalysis).filter(
        RequirementAnalysis.project_id == project_id
    ).first()
    if not analysis:
        return ApiResponse.error("NO_ANALYSIS", "请先完成需求分析")

    try:
        provider = get_active_provider(db)
    except ValueError as e:
        return ApiResponse.ai_error(detail=str(e))

    user_msg = f"项目名称：{project.name}\n\n需求分析结果：\n{analysis.raw_result}"

    try:
        result = await provider.async_chat_json(
            system_prompt=TASK_SYSTEM_PROMPT,
            user_message=user_msg,
            max_tokens=8192,
        )

        # 清除旧任务
        db.query(DevelopmentTask).filter(DevelopmentTask.project_id == project_id).delete()

        tasks_data = result.get("tasks", [])
        for idx, t in enumerate(tasks_data):
            task = DevelopmentTask(
                project_id=project_id,
                title=t.get("title", ""),
                description=t.get("goal", ""),
                task_type=t.get("task_type", "backend"),
                priority=t.get("priority", "medium"),
                dependencies=json.dumps(t.get("dependencies", []), ensure_ascii=False),
                files_to_check=json.dumps(t.get("files_to_check", []), ensure_ascii=False),
                files_to_modify=json.dumps(t.get("files_to_modify", []), ensure_ascii=False),
                implementation_steps=json.dumps(t.get("implementation_steps", []), ensure_ascii=False),
                test_steps=json.dumps(t.get("test_steps", []), ensure_ascii=False),
                acceptance_criteria=json.dumps(t.get("acceptance_criteria", []), ensure_ascii=False),
                codex_prompt=t.get("codex_prompt", ""),
                sort_order=idx,
            )
            db.add(task)

        project.current_stage = "tasks_complete"
        project.status = "developing"
        db.commit()

        _log_generation(db, project_id, "tasks", provider.model, user_msg[:200], f"生成{len(tasks_data)}个任务", True, None)
        return ApiResponse.success(data=result, message=f"已生成{len(tasks_data)}个开发任务")

    except Exception as e:
        db.rollback()
        _log_generation(db, project_id, "tasks", "", "任务生成", None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


@router.post("/tasks/{task_id}/generate-codex-prompt", summary="重新生成CODEX指令")
async def regenerate_codex_prompt(task_id: int, db: Session = Depends(get_db)):
    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")

    if task.codex_prompt:
        return ApiResponse.success(data={"codex_prompt": task.codex_prompt}, message="已存在CODEX指令")

    # 如果没有指令，用 AI 重新生成
    try:
        provider = get_active_provider(db)
    except ValueError as e:
        return ApiResponse.ai_error(detail=str(e))

    try:
        prompt = await provider.async_chat(
            system_prompt=TASK_SYSTEM_PROMPT,
            user_message=f"请为以下任务生成详细的CODEX执行指令：\n任务：{task.title}\n描述：{task.description}",
        )
        task.codex_prompt = prompt
        db.commit()
        return ApiResponse.success(data={"codex_prompt": prompt}, message="CODEX指令生成成功")
    except Exception as e:
        return ApiResponse.ai_error(detail=str(e))


@router.post("/tasks/{task_id}/complete", summary="标记任务完成")
async def complete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")

    task.status = "completed"
    db.commit()
    return ApiResponse.success(data=_task_out(task), message="任务已标记完成")


@router.post("/tasks/{task_id}/retry", summary="重试失败任务")
async def retry_task(task_id: int, db: Session = Depends(get_db)):
    """
    将失败/阻塞/测试失败的任务重置为 pending+ready，允许执行器重新调度。

    在一个事务中：
    1. 确认任务当前状态属于 failed/test_failed/blocked
    2. 确认没有 active lease
    3. 确认项目没有 active executor_run
    4. 将任务恢复为 status=pending, readiness_status=ready
    5. 清理临时状态字段（current_execution_id 等）
    6. 不删除历史 executions/executor_runs/日志/error_message
    """
    import sqlite3
    from app.core.config import settings

    task = db.query(DevelopmentTask).filter(DevelopmentTask.id == task_id).first()
    if not task:
        return ApiResponse.not_found("任务")

    # 1. 确认任务状态允许重试
    allowed_statuses = ['failed', 'test_failed', 'blocked']
    if task.status not in allowed_statuses:
        return ApiResponse.error(
            "INVALID_STATUS",
            f"任务状态为 '{task.status}'，只有 {', '.join(allowed_statuses)} 状态的任务可以重试"
        )

    # 2. 检查 active lease
    db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM task_leases
            WHERE task_id = ? AND status = 'active'
            AND expires_at > datetime('now')
        """, (task_id,))
        active_lease = cur.fetchone()
        if active_lease:
            # 释放过期 lease
            cur.execute("UPDATE task_leases SET status='released', released_at=datetime('now') WHERE task_id=? AND status='active'", (task_id,))
            conn.commit()
            logger.warning(f"[retry] task_id={task_id} 有活跃lease已释放")

        # 3. 检查项目 active executor_run
        cur.execute("""
            SELECT id, run_id, status FROM executor_runs
            WHERE project_id = ? AND status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')
        """, (task.project_id,))
        active_run = cur.fetchone()
        if active_run:
            return ApiResponse.error(
                "ACTIVE_EXECUTOR_RUN",
                f"项目 #{task.project_id} 有活跃的执行器运行 (run_id={active_run['run_id']}, status={active_run['status']})，请先停止执行器"
            )
    finally:
        conn.close()

    # 4. 恢复任务状态
    old_status = task.status
    task.status = 'pending'
    task.readiness_status = 'ready'
    # 清理临时执行状态（如有 current_execution_id 字段）
    if hasattr(task, 'current_execution_id'):
        task.current_execution_id = None
    db.commit()
    db.refresh(task)

    logger.info(f"[retry] task_id={task_id} 从 '{old_status}' 恢复为 'pending' (readiness=ready)")

    # 5. 返回最新任务和队列状态
    return ApiResponse.success(
        data={
            "task": _task_out(task),
            "previous_status": old_status,
        },
        message=f"任务 #{task_id} 已重置为待执行状态"
    )


@router.post("/tasks/reorder", summary="重新排序任务")
async def reorder_tasks(data: dict, db: Session = Depends(get_db)):
    """接收 {task_orders: [{id: 1, sort_order: 0}, ...]}"""
    task_orders = data.get("task_orders", [])
    for item in task_orders:
        task = db.query(DevelopmentTask).filter(DevelopmentTask.id == item.get("id")).first()
        if task:
            task.sort_order = item.get("sort_order", 0)
    db.commit()
    return ApiResponse.success(message="排序更新成功")


def _task_out(t: DevelopmentTask) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "module_id": t.module_id,
        "feature_id": t.feature_id,
        "title": t.title,
        "description": t.description,
        "task_type": t.task_type,
        "priority": t.priority,
        "dependencies": json.loads(t.dependencies) if t.dependencies else [],
        "files_to_check": json.loads(t.files_to_check) if t.files_to_check else [],
        "files_to_modify": json.loads(t.files_to_modify) if t.files_to_modify else [],
        "codex_prompt": t.codex_prompt,
        "test_steps": json.loads(t.test_steps) if t.test_steps else [],
        "acceptance_criteria": json.loads(t.acceptance_criteria) if t.acceptance_criteria else [],
        "status": t.status,
        "readiness_status": getattr(t, 'readiness_status', 'draft'),
        "execution_result": t.execution_result,
        "sort_order": t.sort_order,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _log_generation(db: Session, project_id: int, gen_type: str, model: str,
                     input_summary: str, output_summary: str = None,
                     success: bool = False, error_message: str = None):
    log = AiGenerationLog(
        project_id=project_id,
        generation_type=gen_type,
        model=model,
        input_summary=input_summary[:500] if input_summary else None,
        output_summary=str(output_summary)[:500] if output_summary else None,
        success=success,
        error_message=error_message[:500] if error_message else None,
    )
    db.add(log)
    db.commit()
