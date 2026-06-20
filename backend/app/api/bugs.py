"""Bug 分析 API - 完整生命周期管理"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import Project, Bug, BugStatusLog, AiGenerationLog
from app.core.response import ApiResponse
from app.core.logging import get_logger
from app.ai.prompts import BUG_SYSTEM_PROMPT
from app.api.ai_config import get_active_provider
from app.schemas.bug import BugCreate, BugStatusUpdate, BugExecutionResult, BugTestResult

logger = get_logger(__name__)
router = APIRouter()

# 状态流转规则：当前状态 -> 允许的目标状态列表
STATUS_TRANSITIONS: dict[str, list[str]] = {
    "reported":     ["analyzing", "closed"],
    "analyzing":    ["reported"],          # analyzed 只能通过 AI 分析接口进入
    "analyzed":     ["fix_ready", "reopened"],  # fix_ready 只能通过生成指令接口进入
    "fix_ready":    ["fixing", "reopened", "closed"],
    "fixing":       ["waiting_test", "reopened"],  # waiting_test 只能通过保存执行结果接口进入
    "waiting_test": ["resolved", "reopened"],  # resolved 只能通过测试结果接口进入
    "resolved":     ["reopened", "closed"],
    "reopened":     ["analyzing", "fix_ready", "fixing", "closed"],
    "closed":       ["reopened"],
}

# 中文状态标签
STATUS_LABELS = {
    "reported": "已报告", "analyzing": "分析中", "analyzed": "已完成分析",
    "fix_ready": "修复指令已生成", "fixing": "修复中",
    "waiting_test": "等待测试", "resolved": "已解决",
    "reopened": "已重新打开", "closed": "已关闭",
}

# 状态前置条件：进入某状态必须满足的字段要求
STATUS_PRECONDITIONS: dict[str, list[tuple[str, str, str]]] = {
    # 目标状态: [(字段名, 中文名, 检查说明)]
    "analyzed": [
        ("probable_cause", "根本原因", "尚未完成AI分析"),
        ("fix_plan", "修复方案", "尚未完成AI分析"),
        ("test_steps", "测试步骤", "尚未完成AI分析"),
    ],
    "fix_ready": [
        ("fix_prompt", "修复指令", "尚未生成CODEX修复指令"),
        ("probable_cause", "根本原因", "尚未完成AI分析"),
        ("fix_plan", "修复方案", "尚未完成AI分析"),
    ],
    "fixing": [
        ("fix_prompt", "修复指令", "尚未生成CODEX修复指令"),
    ],
    "waiting_test": [
        ("execution_result", "执行结果", "尚未保存CODEX执行结果"),
    ],
    "resolved": [
        ("test_result", "测试结果", "尚未完成回归测试"),
    ],
}

# 只能通过专用接口进入的状态（普通状态更新接口不允许直接设置）
STATUS_REQUIRES_SPECIAL_ENDPOINT = {
    "analyzed": "/bugs/{id}/analyze",
    "fix_ready": "/bugs/{id}/generate-fix-prompt",
    "waiting_test": "/bugs/{id}/execution-result",
    "resolved": "/bugs/{id}/test-result",
}


def _transition_allowed(from_status: str, to_status: str) -> bool:
    allowed = STATUS_TRANSITIONS.get(from_status, [])
    return to_status in allowed


def _check_preconditions(bug: Bug, to_status: str) -> tuple[bool, str, str]:
    """检查进入目标状态的前置条件。返回 (是否满足, 错误码, 错误详情)"""
    # 检查是否需要通过专用接口
    if to_status in STATUS_REQUIRES_SPECIAL_ENDPOINT:
        endpoint = STATUS_REQUIRES_SPECIAL_ENDPOINT[to_status]
        return False, "BUG_STATE_REQUIRES_SPECIAL_ENDPOINT", (
            f"不能通过状态更新接口直接设置为「{STATUS_LABELS.get(to_status, to_status)}」，"
            f"请使用专用接口 {endpoint}"
        )

    # 检查字段前置条件
    preconditions = STATUS_PRECONDITIONS.get(to_status, [])
    missing = []
    for field_name, field_label, hint in preconditions:
        value = getattr(bug, field_name, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(field_label)

    if missing:
        return False, "BUG_STATE_PRECONDITION_FAILED", (
            f"缺少必要数据：{'、'.join(missing)}，不能进入「{STATUS_LABELS.get(to_status, to_status)}」状态"
        )

    return True, "", ""


def _log_status_change(db: Session, bug_id: int, from_status: str | None, to_status: str, reason: str | None = None):
    """记录状态变更日志。注意：不自动 commit，由调用方统一提交事务。"""
    log = BugStatusLog(
        bug_id=bug_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
    )
    db.add(log)
    db.flush()


@router.post("/projects/{project_id}/bugs", summary="创建Bug")
async def create_bug(project_id: int, data: BugCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    # 指纹去重：同一 task+execution+fingerprint 不重复创建
    if data.task_id and data.execution_id and data.error_message:
        import hashlib
        finger_raw = f"{data.task_id}|{data.execution_id}|{data.error_message[:200]}"
        failure_fingerprint = hashlib.sha256(finger_raw.encode()).hexdigest()[:32]
        existing = db.query(Bug).filter(
            Bug.task_id == data.task_id,
            Bug.execution_id == data.execution_id,
            Bug.failure_fingerprint == failure_fingerprint,
        ).first()
        if existing:
            return ApiResponse.success(
                data=_bug_out(existing),
                message=f"Bug已存在（去重），编号：BUG-{existing.id:04d}"
            )

    bug = Bug(
        project_id=project_id,
        title=data.title,
        description=data.description,
        error_message=data.error_message,
        reproduction_steps=data.reproduction_steps,
        expected_result=data.expected_result,
        actual_result=data.actual_result,
        related_code=data.related_code,
        backend_logs=data.backend_logs,
        console_errors=data.console_errors,
        network_requests=data.network_requests,
    )
    # 设置 task_id / execution_id / failure_fingerprint
    if data.task_id:
        bug.task_id = data.task_id
    if data.execution_id:
        bug.execution_id = data.execution_id
    if data.error_message:
        import hashlib
        finger_raw = f"{data.task_id or 0}|{data.execution_id or 0}|{data.error_message[:200]}"
        bug.failure_fingerprint = hashlib.sha256(finger_raw.encode()).hexdigest()[:32]

    db.add(bug)
    db.commit()
    db.refresh(bug)

    _log_status_change(db, bug.id, None, "reported", "Bug创建")

    return ApiResponse.success(data=_bug_out(bug), message=f"Bug已保存，编号：BUG-{bug.id:04d}")


@router.get("/projects/{project_id}/bugs", summary="获取项目Bug列表")
async def list_bugs(project_id: int, db: Session = Depends(get_db)):
    bugs = db.query(Bug).filter(Bug.project_id == project_id).order_by(Bug.created_at.desc()).all()
    return ApiResponse.success(data=[_bug_out(b) for b in bugs])


@router.get("/bugs/{bug_id}", summary="获取Bug详情")
async def get_bug(bug_id: int, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")
    # 同时返回状态变更日志
    logs = db.query(BugStatusLog).filter(BugStatusLog.bug_id == bug_id).order_by(BugStatusLog.created_at).all()
    return ApiResponse.success(data={
        **_bug_out(bug),
        "status_logs": [_status_log_out(l) for l in logs],
    })


@router.put("/bugs/{bug_id}/status", summary="更新Bug状态")
async def update_bug_status(bug_id: int, data: BugStatusUpdate, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    from_status = bug.status
    to_status = data.status

    # 1. 检查状态流转是否合法
    if not _transition_allowed(from_status, to_status):
        allowed = STATUS_TRANSITIONS.get(from_status, [])
        allowed_cn = [STATUS_LABELS.get(s, s) for s in allowed]
        return JSONResponse(
            status_code=409,
            content=ApiResponse.error(
                "INVALID_TRANSITION",
                f"无法从「{STATUS_LABELS.get(from_status, from_status)}」变为「{STATUS_LABELS.get(to_status, to_status)}」，允许的状态：{'、'.join(allowed_cn)}",
            ),
        )

    # 2. 检查前置条件（字段是否满足 + 是否需要专用接口）
    ok, err_code, err_detail = _check_preconditions(bug, to_status)
    if not ok:
        return JSONResponse(
            status_code=409,
            content=ApiResponse.error(err_code, err_detail, "Bug状态更新失败"),
        )

    # 3. 执行状态更新（事务保护）
    try:
        bug.status = to_status
        if to_status == "resolved":
            bug.resolved_at = datetime.now()
        _log_status_change(db, bug.id, from_status, to_status, data.reason)
        db.commit()
        db.refresh(bug)
    except Exception as e:
        db.rollback()
        logger.error(f"Bug状态更新失败: {e}")
        return JSONResponse(
            status_code=500,
            content=ApiResponse.error("STATE_UPDATE_FAILED", str(e), "Bug状态更新失败"),
        )

    return ApiResponse.success(data=_bug_out(bug), message=f"Bug状态已更新为「{STATUS_LABELS.get(to_status, to_status)}」")


@router.post("/bugs/{bug_id}/analyze", summary="AI分析Bug")
async def analyze_bug(bug_id: int, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    # 只允许从 reported / reopened 状态分析
    if bug.status not in ("reported", "reopened"):
        return ApiResponse.error(
            "INVALID_STATUS",
            f"当前状态「{STATUS_LABELS.get(bug.status, bug.status)}」不允许分析，请先重新打开Bug",
        )

    try:
        provider = get_active_provider(db)
    except ValueError as e:
        return ApiResponse.error("AI_NOT_CONFIGURED", str(e), "AI接口未配置")

    # 构建Bug信息
    bug_info = f"Bug标题：{bug.title}\n"
    if bug.description:
        bug_info += f"描述：{bug.description}\n"
    if bug.error_message:
        bug_info += f"错误信息：{bug.error_message}\n"
    if bug.reproduction_steps:
        bug_info += f"复现步骤：{bug.reproduction_steps}\n"
    if bug.expected_result:
        bug_info += f"预期结果：{bug.expected_result}\n"
    if bug.actual_result:
        bug_info += f"实际结果：{bug.actual_result}\n"
    if bug.related_code:
        bug_info += f"相关代码：{bug.related_code}\n"
    if bug.backend_logs:
        bug_info += f"后端日志：{bug.backend_logs}\n"
    if bug.console_errors:
        bug_info += f"浏览器控制台错误：{bug.console_errors}\n"
    if bug.network_requests:
        bug_info += f"网络请求结果：{bug.network_requests}\n"

    from_status = bug.status
    try:
        bug.status = "analyzing"
        db.commit()

        result = await provider.async_chat_json(
            system_prompt=BUG_SYSTEM_PROMPT,
            user_message=bug_info,
        )

        # 事务：保存分析结果 + 更新状态为 analyzed
        try:
            bug.bug_type = result.get("bug_type", "")
            bug.severity = result.get("severity", "medium")
            bug.probable_cause = json.dumps(result.get("probable_causes", []), ensure_ascii=False)
            bug.affected_module = result.get("affected_module", "")
            bug.affected_files = json.dumps(result.get("files_to_check", []), ensure_ascii=False)
            bug.fix_plan = json.dumps(result.get("fix_plan", []), ensure_ascii=False)
            bug.regression_risks = json.dumps(result.get("regression_risks", []), ensure_ascii=False)
            bug.fix_prompt = result.get("fix_prompt", "")
            bug.test_steps = json.dumps(result.get("test_steps", []), ensure_ascii=False)
            bug.is_blocking = result.get("is_blocking", "unknown")
            bug.status = "analyzed"

            _log_status_change(db, bug.id, from_status, "analyzing", "开始AI分析")
            _log_status_change(db, bug.id, "analyzing", "analyzed", "AI分析完成")
            db.commit()
        except Exception as save_err:
            db.rollback()
            # 状态回滚到 analyzing 之前
            bug.status = from_status if from_status in ("reported", "reopened") else "reported"
            db.commit()
            raise save_err

        _log_generation(db, bug.project_id, "bug", provider.model, bug_info[:200], "成功", True, None)

        return ApiResponse.success(data=_bug_out(bug), message="Bug分析完成")

    except Exception as e:
        # 确保 Bug 不会卡在 analyzing 状态
        if bug.status == "analyzing":
            bug.status = from_status if from_status in ("reported", "reopened") else "reported"
            db.commit()
        _log_generation(db, bug.project_id, "bug", "", "Bug分析", None, False, str(e))
        return ApiResponse.error("AI_ANALYSIS_FAILED", str(e), f"AI分析失败：{e}")


@router.post("/bugs/{bug_id}/generate-fix-prompt", summary="生成CODEX修复指令")
async def generate_fix_prompt(bug_id: int, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    if not bug.probable_cause:
        return ApiResponse.error("NO_ANALYSIS", "请先完成Bug AI分析")

    if bug.status not in ("analyzed", "fix_ready", "reopened"):
        return ApiResponse.error(
            "INVALID_STATUS",
            f"当前状态「{STATUS_LABELS.get(bug.status, bug.status)}」不允许生成修复指令，需在「已完成分析」「修复指令已生成」或「已重新打开」状态",
        )

    # 构建完整的 CODEX 修复指令
    causes = json.loads(bug.probable_cause) if bug.probable_cause else []
    fix_plan = json.loads(bug.fix_plan) if bug.fix_plan else []
    test_steps = json.loads(bug.test_steps) if bug.test_steps else []
    regression_risks = json.loads(bug.regression_risks) if bug.regression_risks else []
    affected_files = json.loads(bug.affected_files) if bug.affected_files else []

    prompt = f"""【项目背景】
Bug编号：BUG-{bug.id:04d}
影响模块：{bug.affected_module or '未知'}
Bug类型：{bug.bug_type or '未知'}
严重等级：{bug.severity or '未知'}

【Bug标题】
{bug.title}

【复现步骤】
{bug.reproduction_steps or '未提供'}

【预期结果】
{bug.expected_result or '未提供'}

【实际结果】
{bug.actual_result or '未提供'}

【错误信息】
{bug.error_message or '未提供'}

【根本原因】
{chr(10).join(f'- {c}' for c in causes) if causes else '待分析'}

【允许修改范围】
{chr(10).join(f'- {f}' for f in affected_files) if affected_files else '根据实际情况确定'}

【禁止修改范围】
- 不修改与当前Bug无关的模块
- 不修改数据库表结构（除非Bug明确涉及）
- 不修改其他Bug的修复代码
- 不修改项目配置文件（除非Bug明确涉及）

【具体修复步骤】
{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(fix_plan)) if fix_plan else '待分析'}

【测试方法】
{chr(10).join(f'- {s}' for s in test_steps) if test_steps else '待分析'}

【验收标准】
1. 原问题已无法复现
2. 正常流程通过
3. 异常流程有正确提示
4. 页面刷新后数据正常
5. 数据库数据正确
6. 没有影响其他模块

【完成后输出要求】
1. 修改文件清单
2. 每个文件的具体修改说明
3. 测试执行结果
4. 仍然存在的问题（如有）

【重要约束】
1. 先检查现有代码，理解上下文
2. 只修复当前Bug，不做其他改动
3. 不修改无关模块
4. 不重构整个项目
5. 不伪造测试成功
6. 修复后执行测试
7. 输出修改文件清单
8. 输出仍然存在的问题

【回归风险】
{chr(10).join(f'- {r}' for r in regression_risks) if regression_risks else '暂无'}
"""

    # 更新Bug状态（事务保护）
    from_status = bug.status
    try:
        bug.fix_prompt = prompt
        if bug.status == "analyzed" or bug.status == "reopened":
            bug.status = "fix_ready"
            _log_status_change(db, bug.id, from_status, "fix_ready", "修复指令已生成")
        db.commit()
        db.refresh(bug)
    except Exception as e:
        db.rollback()
        logger.error(f"修复指令保存失败: {e}")
        return ApiResponse.error("FIX_PROMPT_SAVE_FAILED", str(e), "修复指令保存失败")

    return ApiResponse.success(data=_bug_out(bug), message="CODEX修复指令已生成")


@router.post("/bugs/{bug_id}/execution-result", summary="保存CODEX执行结果")
async def save_execution_result(bug_id: int, data: BugExecutionResult, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    if bug.status not in ("fix_ready", "fixing"):
        return ApiResponse.error(
            "INVALID_STATUS",
            f"当前状态「{STATUS_LABELS.get(bug.status, bug.status)}」不允许保存执行结果，需在「修复指令已生成」或「修复中」状态",
        )

    # 事务：保存执行结果 + 更新状态为 waiting_test
    from_status = bug.status
    try:
        bug.execution_result = data.execution_result
        bug.files_changed = data.files_changed
        bug.test_result = data.test_result
        bug.remaining_issues = data.remaining_issues
        bug.executed_at = datetime.now()
        bug.status = "waiting_test"
        _log_status_change(db, bug.id, from_status, "waiting_test", "CODEX执行结果已保存，等待测试")
        db.commit()
        db.refresh(bug)
    except Exception as e:
        db.rollback()
        logger.error(f"执行结果保存失败: {e}")
        return ApiResponse.error("EXECUTION_RESULT_SAVE_FAILED", str(e), "执行结果保存失败")

    return ApiResponse.success(data=_bug_out(bug), message="CODEX执行结果已保存")


@router.post("/bugs/{bug_id}/test-result", summary="保存回归测试结果")
async def save_test_result(bug_id: int, data: BugTestResult, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    if bug.status != "waiting_test":
        return ApiResponse.error(
            "INVALID_STATUS",
            f"当前状态「{STATUS_LABELS.get(bug.status, bug.status)}」不允许提交测试结果，需在「等待测试」状态",
        )

    from_status = bug.status

    # 事务：保存测试结果 + 更新状态
    try:
        if data.passed:
            bug.status = "resolved"
            bug.resolved_at = datetime.now()
            bug.test_result = data.test_notes or "测试通过"
            _log_status_change(db, bug.id, from_status, "resolved", data.test_notes or "回归测试通过")
        else:
            bug.status = "reopened"
            bug.test_result = data.test_notes or "测试失败"
            _log_status_change(db, bug.id, from_status, "reopened", data.test_notes or "回归测试失败")

        db.commit()
        db.refresh(bug)
    except Exception as e:
        db.rollback()
        logger.error(f"测试结果保存失败: {e}")
        return ApiResponse.error("TEST_RESULT_SAVE_FAILED", str(e), "测试结果保存失败")

    label = STATUS_LABELS.get(bug.status, bug.status)
    return ApiResponse.success(data=_bug_out(bug), message=f"Bug状态已更新为「{label}」")


@router.get("/bugs/{bug_id}/status-logs", summary="获取Bug状态变更日志")
async def get_bug_status_logs(bug_id: int, db: Session = Depends(get_db)):
    bug = db.query(Bug).filter(Bug.id == bug_id).first()
    if not bug:
        return ApiResponse.not_found("Bug")

    logs = db.query(BugStatusLog).filter(BugStatusLog.bug_id == bug_id).order_by(BugStatusLog.created_at).all()
    return ApiResponse.success(data=[_status_log_out(l) for l in logs])


def _bug_out(b: Bug) -> dict:
    return {
        "id": b.id,
        "project_id": b.project_id,
        "title": b.title,
        "description": b.description,
        "error_message": b.error_message,
        "reproduction_steps": b.reproduction_steps,
        "expected_result": b.expected_result,
        "actual_result": b.actual_result,
        "bug_type": b.bug_type,
        "severity": b.severity,
        "probable_cause": json.loads(b.probable_cause) if b.probable_cause else [],
        "affected_module": b.affected_module,
        "affected_files": json.loads(b.affected_files) if b.affected_files else [],
        "fix_plan": json.loads(b.fix_plan) if b.fix_plan else [],
        "regression_risks": json.loads(b.regression_risks) if b.regression_risks else [],
        "fix_prompt": b.fix_prompt,
        "test_steps": json.loads(b.test_steps) if b.test_steps else [],
        "is_blocking": b.is_blocking,
        "execution_result": b.execution_result,
        "files_changed": b.files_changed,
        "test_result": b.test_result,
        "remaining_issues": b.remaining_issues,
        "executed_at": b.executed_at.isoformat() if b.executed_at else None,
        "status": b.status,
        "resolved_at": b.resolved_at.isoformat() if b.resolved_at else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


def _status_log_out(l: BugStatusLog) -> dict:
    return {
        "id": l.id,
        "bug_id": l.bug_id,
        "from_status": l.from_status,
        "to_status": l.to_status,
        "from_status_label": STATUS_LABELS.get(l.from_status, l.from_status) if l.from_status else None,
        "to_status_label": STATUS_LABELS.get(l.to_status, l.to_status),
        "reason": l.reason,
        "created_at": l.created_at.isoformat() if l.created_at else None,
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
