"""项目管理 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import Project, RequirementAnalysis
from app.schemas.project import ProjectCreate, ProjectUpdate, RequirementInput, ProjectOut
from app.core.response import ApiResponse

router = APIRouter()


@router.post("/projects", summary="创建项目")
async def create_project(data: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(name=data.name, idea=data.idea, description=data.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return ApiResponse.success(data=_project_out(project), message="项目创建成功")


@router.get("/projects", summary="获取项目列表")
async def list_projects(db: Session = Depends(get_db)):
    projects = db.query(Project).order_by(Project.updated_at.desc()).all()
    return ApiResponse.success(data=[_project_out(p) for p in projects])


@router.get("/projects/{project_id}", summary="获取项目详情")
async def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")
    return ApiResponse.success(data=_project_detail(project))


@router.put("/projects/{project_id}", summary="更新项目")
async def update_project(project_id: int, data: ProjectUpdate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(project, key, value)

    db.commit()
    db.refresh(project)
    return ApiResponse.success(data=_project_detail(project), message="项目更新成功")


@router.delete("/projects/{project_id}", summary="删除项目")
async def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")
    db.delete(project)
    db.commit()
    return ApiResponse.success(message="项目删除成功")


@router.put("/projects/{project_id}/requirements", summary="更新项目需求")
async def update_requirements(project_id: int, data: RequirementInput, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(project, key, value)

    # 仅在项目尚无明确状态时设为draft，避免覆盖已分析的状态
    if not project.status or project.status == "draft":
        project.status = "draft"
    db.commit()
    db.refresh(project)
    return ApiResponse.success(data=_project_detail(project), message="需求更新成功")


def _project_out(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "idea": p.idea,
        "status": p.status,
        "current_stage": p.current_stage,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _project_detail(p: Project) -> dict:
    result = _project_out(p)
    result.update({
        "description": p.description,
        "target_users": p.target_users,
        "core_features": p.core_features,
        "scenarios": p.scenarios,
        "platform": p.platform,
        "need_login": p.need_login,
        "need_database": p.need_database,
        "need_ai": p.need_ai,
        "need_third_party": p.need_third_party,
        "need_upload": p.need_upload,
        "need_export": p.need_export,
        "tech_requirements": p.tech_requirements,
        "exclude_features": p.exclude_features,
        "additional_notes": p.additional_notes,
    })
    return result
