"""待发布资料库管理 API"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.database.engine import get_db
from app.models import Project, Image, PendingRelease
from app.core.response import ApiResponse

router = APIRouter()


class AddToPendingRequest(BaseModel):
    """将选中图片加入待发布库"""
    image_ids: List[int]


class UpdatePendingRequest(BaseModel):
    """更新待发布项"""
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    platform: Optional[str] = None
    sort_order: Optional[int] = None


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int]


@router.get("/projects/{project_id}/pending", summary="获取待发布列表")
async def list_pending(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    items = db.query(PendingRelease).filter(
        PendingRelease.project_id == project_id
    ).order_by(PendingRelease.sort_order).all()

    result = []
    for item in items:
        img = item.image
        result.append({
            "id": item.id,
            "image_id": item.image_id,
            "title": item.title,
            "description": item.description,
            "price": item.price,
            "platform": item.platform,
            "sort_order": item.sort_order,
            "status": item.status,
            "image": {
                "id": img.id,
                "original_filename": img.original_filename,
                "cleaned_path": img.cleaned_path,
                "thumbnail_path": img.thumbnail_path,
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "file_size": img.file_size,
            } if img else None,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        })

    return ApiResponse.success(data=result)


@router.post("/projects/{project_id}/pending", summary="添加图片到待发布库")
async def add_to_pending(project_id: int, data: AddToPendingRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    if not data.image_ids:
        return ApiResponse.validation_error("请选择图片")

    images = db.query(Image).filter(
        Image.project_id == project_id,
        Image.id.in_(data.image_ids),
        Image.status == "selected"
    ).all()

    if not images:
        return ApiResponse.validation_error("请先选择图片再添加")

    added = []
    for img in images:
        # 检查是否已存在
        existing = db.query(PendingRelease).filter(
            PendingRelease.project_id == project_id,
            PendingRelease.image_id == img.id
        ).first()
        if existing:
            continue

        # 获取最大sort_order
        max_order = db.query(PendingRelease).filter(
            PendingRelease.project_id == project_id
        ).count()

        item = PendingRelease(
            project_id=project_id,
            image_id=img.id,
            title=img.original_filename,
            sort_order=max_order,
            status="draft",
        )
        db.add(item)
        added.append(item)

        # 更新图片状态
        img.status = "released"

    db.commit()

    return ApiResponse.success(
        data={"added_count": len(added)},
        message=f"已添加 {len(added)} 张图片到待发布库"
    )


@router.put("/pending/{pending_id}", summary="编辑待发布项")
async def update_pending(pending_id: int, data: UpdatePendingRequest, db: Session = Depends(get_db)):
    item = db.query(PendingRelease).filter(PendingRelease.id == pending_id).first()
    if not item:
        return ApiResponse.not_found("待发布项")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return ApiResponse.success(message="更新成功")


@router.delete("/pending/{pending_id}", summary="删除待发布项")
async def delete_pending(pending_id: int, db: Session = Depends(get_db)):
    item = db.query(PendingRelease).filter(PendingRelease.id == pending_id).first()
    if not item:
        return ApiResponse.not_found("待发布项")

    # 恢复图片状态
    img = item.image
    if img and img.status == "released":
        img.status = "selected"

    db.delete(item)
    db.commit()
    return ApiResponse.success(message="删除成功")


@router.post("/projects/{project_id}/pending/batch-delete", summary="批量删除")
async def batch_delete_pending(project_id: int, data: BatchDeleteRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    if not data.ids:
        return ApiResponse.validation_error("请选择要删除的项")

    items = db.query(PendingRelease).filter(
        PendingRelease.project_id == project_id,
        PendingRelease.id.in_(data.ids)
    ).all()

    for item in items:
        img = item.image
        if img and img.status == "released":
            img.status = "selected"
        db.delete(item)

    db.commit()
    return ApiResponse.success(data={"deleted_count": len(items)}, message=f"已删除 {len(items)} 项")


@router.post("/projects/{project_id}/pending/publish", summary="一键上架")
async def publish_pending(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    items = db.query(PendingRelease).filter(
        PendingRelease.project_id == project_id,
        PendingRelease.status.in_(["draft", "ready"])
    ).all()

    if not items:
        return ApiResponse.validation_error("待发布库为空")

    published = 0
    for item in items:
        item.status = "released"
        published += 1

    db.commit()
    return ApiResponse.success(data={"published_count": published}, message=f"已上架 {published} 项商品")


@router.post("/projects/{project_id}/pending/reorder", summary="重新排序")
async def reorder_pending(project_id: int, data: List[dict] = [], db: Session = Depends(get_db)):
    """接收 [{id: 1, sort_order: 0}, ...]"""
    if not data:
        return ApiResponse.success(message="无需排序")
    for item_data in data:
        item = db.query(PendingRelease).filter(
            PendingRelease.id == item_data.get("id"),
            PendingRelease.project_id == project_id
        ).first()
        if item:
            item.sort_order = item_data.get("sort_order", 0)
    db.commit()
    return ApiResponse.success(message="排序更新成功")
