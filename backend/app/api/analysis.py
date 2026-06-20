"""AI 分析 API（需求分析、架构生成等）"""
import json
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import Project, RequirementAnalysis, AiGenerationLog
from app.core.response import ApiResponse
from app.core.logging import get_logger
from app.ai.prompts import (
    ANALYSIS_SYSTEM_PROMPT, MVP_SYSTEM_PROMPT, DATABASE_SYSTEM_PROMPT,
    API_SYSTEM_PROMPT, TASK_SYSTEM_PROMPT
)
from app.api.ai_config import get_active_provider
from app.ai.provider import AiProvider

logger = get_logger(__name__)
router = APIRouter()


@router.post("/projects/{project_id}/analyze", summary="AI需求分析")
async def analyze_requirements(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    try:
        provider = get_active_provider(db)
    except ValueError as e:
        return ApiResponse.ai_error(detail=str(e))

    # 构建用户消息
    user_msg = _build_analysis_input(project)

    try:
        project.status = "analyzing"
        db.commit()

        result = await provider.async_chat_json(
            system_prompt=ANALYSIS_SYSTEM_PROMPT,
            user_message=user_msg,
        )

        # 保存分析结果
        analysis = db.query(RequirementAnalysis).filter(
            RequirementAnalysis.project_id == project_id
        ).first()

        if not analysis:
            analysis = RequirementAnalysis(project_id=project_id)
            db.add(analysis)

        analysis.product_definition = result.get("product_definition", "")
        analysis.problem = result.get("problem", "")
        analysis.target_users = json.dumps(result.get("target_users", []), ensure_ascii=False)
        analysis.usage_scenarios = json.dumps(result.get("usage_scenarios", []), ensure_ascii=False)
        analysis.core_value = json.dumps(result.get("core_value", []), ensure_ascii=False)
        analysis.business_flow = json.dumps(result.get("business_flow", []), ensure_ascii=False)
        analysis.risks = json.dumps(result.get("risks", []), ensure_ascii=False)
        analysis.technical_notes = json.dumps(result.get("technical_notes", []), ensure_ascii=False)
        analysis.third_party_deps = json.dumps(result.get("third_party_deps", []), ensure_ascii=False)
        analysis.data_security = json.dumps(result.get("data_security", []), ensure_ascii=False)
        analysis.mvp_success_criteria = json.dumps(result.get("mvp_success_criteria", []), ensure_ascii=False)
        analysis.raw_result = json.dumps(result, ensure_ascii=False)

        project.status = "generated"
        project.current_stage = "analysis_complete"
        db.commit()

        _log_generation(db, project_id, "analysis", provider.model, user_msg[:200], "成功", True, None)
        return ApiResponse.success(data=result, message="需求分析完成")

    except Exception as e:
        project.status = "draft"
        db.commit()
        _log_generation(db, project_id, "analysis", provider.model, user_msg[:200], None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


@router.post("/projects/{project_id}/generate-mvp", summary="生成MVP规划")
async def generate_mvp(project_id: int, db: Session = Depends(get_db)):
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
            system_prompt=MVP_SYSTEM_PROMPT,
            user_message=user_msg,
        )

        project.current_stage = "mvp_complete"
        db.commit()

        _log_generation(db, project_id, "mvp", provider.model, user_msg[:200], "成功", True, None)
        return ApiResponse.success(data=result, message="MVP规划生成完成")

    except Exception as e:
        _log_generation(db, project_id, "mvp", provider.model, user_msg[:200], None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


@router.post("/projects/{project_id}/generate-database", summary="生成数据库设计")
async def generate_database(project_id: int, db: Session = Depends(get_db)):
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
            system_prompt=DATABASE_SYSTEM_PROMPT,
            user_message=user_msg,
        )

        project.current_stage = "database_complete"
        db.commit()

        _log_generation(db, project_id, "database", provider.model, user_msg[:200], "成功", True, None)
        return ApiResponse.success(data=result, message="数据库设计生成完成")

    except Exception as e:
        _log_generation(db, project_id, "database", provider.model, user_msg[:200], None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


@router.post("/projects/{project_id}/generate-apis", summary="生成API设计")
async def generate_apis(project_id: int, db: Session = Depends(get_db)):
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
            system_prompt=API_SYSTEM_PROMPT,
            user_message=user_msg,
        )

        project.current_stage = "api_complete"
        db.commit()

        _log_generation(db, project_id, "api", provider.model, user_msg[:200], "成功", True, None)
        return ApiResponse.success(data=result, message="API设计生成完成")

    except Exception as e:
        _log_generation(db, project_id, "api", provider.model, user_msg[:200], None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


@router.get("/projects/{project_id}/analysis", summary="获取需求分析结果")
async def get_analysis(project_id: int, db: Session = Depends(get_db)):
    analysis = db.query(RequirementAnalysis).filter(
        RequirementAnalysis.project_id == project_id
    ).first()
    if not analysis:
        return ApiResponse.not_found("需求分析")
    return ApiResponse.success(data=_analysis_out(analysis))


def _build_analysis_input(project: Project) -> str:
    """构建需求分析输入"""
    parts = [f"软件名称：{project.name}"]
    if project.idea:
        parts.append(f"软件想法：{project.idea}")
    if project.description:
        parts.append(f"详细说明：{project.description}")
    if project.target_users:
        parts.append(f"目标用户：{project.target_users}")
    if project.core_features:
        parts.append(f"核心功能：{project.core_features}")
    if project.scenarios:
        parts.append(f"使用场景：{project.scenarios}")
    if project.platform:
        parts.append(f"平台类型：{project.platform}")
    if project.need_ai and project.need_ai != "unknown":
        parts.append(f"是否需要AI：{project.need_ai}")
    if project.need_database and project.need_database != "unknown":
        parts.append(f"是否需要数据库：{project.need_database}")
    if project.need_login and project.need_login != "unknown":
        parts.append(f"是否需要登录：{project.need_login}")
    if project.need_third_party and project.need_third_party != "unknown":
        parts.append(f"是否需要第三方接口：{project.need_third_party}")
    if project.tech_requirements:
        parts.append(f"技术要求：{project.tech_requirements}")
    if project.exclude_features:
        parts.append(f"不希望出现的功能：{project.exclude_features}")
    if project.additional_notes:
        parts.append(f"其他补充：{project.additional_notes}")
    return "\n".join(parts)


def _analysis_out(a: RequirementAnalysis) -> dict:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "product_definition": a.product_definition,
        "problem": a.problem,
        "target_users": json.loads(a.target_users) if a.target_users else [],
        "usage_scenarios": json.loads(a.usage_scenarios) if a.usage_scenarios else [],
        "core_value": json.loads(a.core_value) if a.core_value else [],
        "business_flow": json.loads(a.business_flow) if a.business_flow else [],
        "risks": json.loads(a.risks) if a.risks else [],
        "technical_notes": json.loads(a.technical_notes) if a.technical_notes else [],
        "third_party_deps": json.loads(a.third_party_deps) if a.third_party_deps else [],
        "data_security": json.loads(a.data_security) if a.data_security else [],
        "mvp_success_criteria": json.loads(a.mvp_success_criteria) if a.mvp_success_criteria else [],
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _log_generation(db: Session, project_id: int, gen_type: str, model: str,
                     input_summary: str, output_summary: str = None,
                     success: bool = False, error_message: str = None):
    """记录AI生成日志"""
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
