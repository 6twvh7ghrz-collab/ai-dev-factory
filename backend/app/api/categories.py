"""分类与图片选择 API"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.database.engine import get_db
from app.models import Project, Image, Category, ImageCategory
from app.core.response import ApiResponse

router = APIRouter()


class ImageSelectionRequest(BaseModel):
    """图片选择请求"""
    image_ids: List[int]
    action: str = "select"  # select / deselect / toggle


class CategoryCreate(BaseModel):
    """创建分类请求"""
    name: str
    description: Optional[str] = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    """更新分类请求"""
    name: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None


# ==================== 分类管理 ====================

@router.get("/projects/{project_id}/categories", summary="获取项目分类列表")
async def list_categories(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    categories = db.query(Category).filter(
        Category.project_id == project_id
    ).order_by(Category.sort_order).all()

    result = []
    for c in categories:
        image_count = db.query(ImageCategory).filter(
            ImageCategory.category_id == c.id
        ).count()
        result.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "sort_order": c.sort_order,
            "image_count": image_count,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    return ApiResponse.success(data=result)


@router.post("/projects/{project_id}/categories", summary="创建分类")
async def create_category(project_id: int, data: CategoryCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    category = Category(
        project_id=project_id,
        name=data.name,
        description=data.description,
        sort_order=data.sort_order,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return ApiResponse.success(data={"id": category.id, "name": category.name}, message="分类创建成功")


@router.put("/categories/{category_id}", summary="更新分类")
async def update_category(category_id: int, data: CategoryUpdate, db: Session = Depends(get_db)):
    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        return ApiResponse.not_found("分类")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(category, key, value)
    db.commit()
    return ApiResponse.success(message="分类更新成功")


@router.delete("/categories/{category_id}", summary="删除分类")
async def delete_category(category_id: int, db: Session = Depends(get_db)):
    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        return ApiResponse.not_found("分类")
    db.delete(category)
    db.commit()
    return ApiResponse.success(message="分类删除成功")


# ==================== 分类下的图片列表 ====================

@router.get("/projects/{project_id}/images", summary="获取项目图片列表(支持按分类筛选)")
async def list_images(
    project_id: int,
    category_id: Optional[int] = None,
    status: Optional[str] = None,
    selected: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    query = db.query(Image).filter(Image.project_id == project_id)

    if status:
        query = query.filter(Image.status == status)
    if selected is not None:
        target_status = "selected" if selected else "cleaned"
        query = query.filter(Image.status == target_status)

    if category_id:
        query = query.join(ImageCategory, Image.id == ImageCategory.image_id).filter(
            ImageCategory.category_id == category_id
        )

    images = query.order_by(Image.sort_order, Image.created_at.desc()).all()

    result = []
    for img in images:
        img_categories = db.query(ImageCategory).filter(
            ImageCategory.image_id == img.id
        ).all()
        result.append({
            "id": img.id,
            "original_filename": img.original_filename,
            "thumbnail_path": img.thumbnail_path,
            "cleaned_path": img.cleaned_path,
            "file_size": img.file_size,
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "status": img.status,
            "sort_order": img.sort_order,
            "category_ids": [ic.category_id for ic in img_categories],
            "created_at": img.created_at.isoformat() if img.created_at else None,
        })

    return ApiResponse.success(data=result)


# ==================== 图片选择操作 ====================

@router.post("/projects/{project_id}/images/select", summary="批量选择/取消选择图片")
async def select_images(project_id: int, data: ImageSelectionRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    if not data.image_ids:
        return ApiResponse.validation_error("请提供图片ID列表")

    images = db.query(Image).filter(
        Image.project_id == project_id,
        Image.id.in_(data.image_ids)
    ).all()

    if not images:
        return ApiResponse.validation_error("未找到有效图片")

    updated_count = 0
    for img in images:
        if data.action == "select":
            if img.status != "selected":
                img.status = "selected"
                updated_count += 1
        elif data.action == "deselect":
            if img.status == "selected":
                img.status = "cleaned"
                updated_count += 1
        elif data.action == "toggle":
            if img.status == "selected":
                img.status = "cleaned"
            else:
                img.status = "selected"
            updated_count += 1

    db.commit()
    return ApiResponse.success(
        data={
            "updated_count": updated_count,
            "total_images": len(images),
            "selected_ids": [img.id for img in images if img.status == "selected"],
        },
        message=f"批量{data.action}操作完成"
    )


@router.get("/projects/{project_id}/images/selected", summary="获取已选图片列表")
async def list_selected_images(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    images = db.query(Image).filter(
        Image.project_id == project_id,
        Image.status == "selected"
    ).order_by(Image.sort_order).all()

    result = []
    for img in images:
        result.append({
            "id": img.id,
            "original_filename": img.original_filename,
            "thumbnail_path": img.thumbnail_path,
            "cleaned_path": img.cleaned_path,
            "file_size": img.file_size,
            "width": img.width,
            "height": img.height,
            "format": img.format,
        })

    return ApiResponse.success(data=result)


@router.post("/projects/{project_id}/images/select-all", summary="全选/反选分类下图片")
async def select_all_images(
    project_id: int,
    category_id: Optional[int] = None,
    action: str = "select",
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    query = db.query(Image).filter(
        Image.project_id == project_id,
        Image.status.in_(["cleaned", "selected"])
    )

    if category_id:
        query = query.join(ImageCategory, Image.id == ImageCategory.image_id).filter(
            ImageCategory.category_id == category_id
        )

    images = query.all()
    target_status = "selected" if action == "select" else "cleaned"
    updated = 0
    for img in images:
        if img.status != target_status:
            img.status = target_status
            updated += 1

    db.commit()

    return ApiResponse.success(
        data={
            "action": action,
            "updated_count": updated,
            "total_processed": len(images),
        },
        message=f"全{action}完成"
    )
