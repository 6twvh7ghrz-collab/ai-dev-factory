"""模块和功能 API"""
import json
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import Project, Module, Feature, RequirementAnalysis, AiGenerationLog
from app.core.response import ApiResponse
from app.core.logging import get_logger
from app.ai.prompts import MVP_SYSTEM_PROMPT
from app.api.ai_config import get_active_provider

logger = get_logger(__name__)
router = APIRouter()


@router.get("/projects/{project_id}/modules", summary="获取模块列表")
async def list_modules(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    modules = db.query(Module).filter(Module.project_id == project_id).order_by(Module.sort_order).all()
    result = []
    for m in modules:
        features = db.query(Feature).filter(Feature.module_id == m.id).all()
        result.append({
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "input_definition": m.input_definition,
            "process_definition": m.process_definition,
            "output_definition": m.output_definition,
            "priority": m.priority,
            "version_stage": m.version_stage,
            "status": m.status,
            "sort_order": m.sort_order,
            "features": [
                {
                    "id": f.id,
                    "name": f.name,
                    "description": f.description,
                    "priority": f.priority,
                    "version_stage": f.version_stage,
                    "acceptance_criteria": json.loads(f.acceptance_criteria) if f.acceptance_criteria else [],
                    "status": f.status,
                }
                for f in features
            ],
        })
    return ApiResponse.success(data=result)


@router.post("/projects/{project_id}/generate-modules", summary="AI生成模块和功能")
async def generate_modules(project_id: int, db: Session = Depends(get_db)):
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

        # 清除旧模块
        db.query(Module).filter(Module.project_id == project_id).delete()

        # 从分析结果中提取模块信息
        raw_analysis = json.loads(analysis.raw_result) if analysis.raw_result else {}
        modules_data = raw_analysis.get("modules", [])

        for idx, mod_data in enumerate(modules_data):
            module = Module(
                project_id=project_id,
                name=mod_data.get("name", ""),
                description=mod_data.get("description", ""),
                input_definition=json.dumps(mod_data.get("inputs", []), ensure_ascii=False),
                process_definition=json.dumps(mod_data.get("processes", []), ensure_ascii=False),
                output_definition=json.dumps(mod_data.get("outputs", []), ensure_ascii=False),
                sort_order=idx,
            )
            db.add(module)
            db.flush()

            # 添加功能
            for feat_data in mod_data.get("features", []):
                feature = Feature(
                    module_id=module.id,
                    name=feat_data if isinstance(feat_data, str) else feat_data.get("name", ""),
                    description=feat_data if isinstance(feat_data, str) else feat_data.get("description", ""),
                )
                db.add(feature)

        # MVP 分类
        must_have = result.get("must_have", [])
        phase_two = result.get("phase_two", [])
        later = result.get("later", [])

        # 更新功能的版本阶段
        for item in must_have:
            _update_feature_stage(db, project_id, item.get("feature"), "mvp")
        for item in phase_two:
            _update_feature_stage(db, project_id, item.get("feature"), "phase2")
        for item in later:
            _update_feature_stage(db, project_id, item.get("feature"), "later")

        project.current_stage = "modules_complete"
        db.commit()

        _log_generation(db, project_id, "modules", provider.model, "模块和MVP生成", "成功", True, None)
        return ApiResponse.success(data=result, message="模块和MVP规划生成完成")

    except Exception as e:
        db.rollback()
        _log_generation(db, project_id, "modules", "", "模块和MVP生成", None, False, str(e))
        return ApiResponse.ai_error(detail=str(e))


def _update_feature_stage(db: Session, project_id: int, feature_name: str, stage: str):
    """根据MVP分类更新功能版本阶段"""
    if not feature_name:
        return
    # 优先精确匹配
    features = db.query(Feature).join(Module).filter(
        Module.project_id == project_id,
        Feature.name == feature_name,
    ).all()
    # 精确匹配未找到时，回退到包含匹配
    if not features:
        features = db.query(Feature).join(Module).filter(
            Module.project_id == project_id,
            Feature.name.contains(feature_name),
        ).all()
    for f in features:
        f.version_stage = stage


def _log_generation(db: Session, project_id: int, gen_type: str, model: str,
                     input_summary: str, output_summary: str = None,
                     success: bool = False, error_message: str = None):
    from app.models import AiGenerationLog
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
