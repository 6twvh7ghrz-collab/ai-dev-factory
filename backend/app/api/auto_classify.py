"""图片自动分类逻辑 API

根据图片特征（尺寸、宽高比、格式、文件大小等）自动分配分类标签。
支持内置规则和用户自定义规则。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.database.engine import get_db
from app.models import Project, Image, Category, ImageCategory
from app.core.response import ApiResponse

router = APIRouter()


class ClassifyRequest(BaseModel):
    """触发自动分类请求"""
    image_ids: Optional[List[int]] = None  # 为空则分类该项目下所有图片
    rules: Optional[List[str]] = None  # 指定启用的规则，为空则全部启用


class ClassifyRule:
    """单个分类规则的基类，实现 check(image) -> category_name 或 None"""

    name: str = ""
    description: str = ""

    def check(self, image: Image) -> Optional[str]:
        raise NotImplementedError


# ═══════════════════════════════════════
#  内置分类规则
# ═══════════════════════════════════════

class SizeRule(ClassifyRule):
    """按图片尺寸分类"""
    name = "size"
    description = "按图片尺寸（像素总数）分类"

    def check(self, image: Image) -> Optional[str]:
        if not image.width or not image.height:
            return None
        pixels = image.width * image.height
        if pixels < 100_000:  # < 100k pixels (e.g. ~300x300)
            return "小尺寸"
        elif pixels < 1_000_000:  # < 1M pixels (e.g. ~1000x1000)
            return "中等尺寸"
        else:
            return "大尺寸"


class AspectRatioRule(ClassifyRule):
    """按宽高比分类"""
    name = "aspect_ratio"
    description = "按图片宽高比分类"

    def check(self, image: Image) -> Optional[str]:
        if not image.width or not image.height:
            return None
        ratio = image.width / image.height
        if ratio > 1.3:
            return "横图"
        elif ratio < 0.77:
            return "竖图"
        else:
            return "方图"


class FormatRule(ClassifyRule):
    """按文件格式分类"""
    name = "format"
    description = "按图片文件格式分类"

    def check(self, image: Image) -> Optional[str]:
        fmt = (image.format or "").lower()
        mapping = {
            "jpeg": "JPEG格式",
            "jpg": "JPEG格式",
            "png": "PNG格式",
            "webp": "WebP格式",
            "gif": "GIF格式",
            "bmp": "BMP格式",
        }
        return mapping.get(fmt)


class FileSizeRule(ClassifyRule):
    """按文件大小分类"""
    name = "file_size"
    description = "按图片文件大小分类"

    def check(self, image: Image) -> Optional[str]:
        if not image.file_size:
            return None
        kb = image.file_size / 1024
        if kb < 50:
            return "小文件(<50KB)"
        elif kb < 500:
            return "中等文件(50-500KB)"
        else:
            return "大文件(>500KB)"


class OrientationRule(ClassifyRule):
    """按方向分类"""
    name = "orientation"
    description = "图片方向（横/竖/方）"

    def check(self, image: Image) -> Optional[str]:
        if not image.width or not image.height:
            return None
        if image.width > image.height:
            return "横向"
        elif image.height > image.width:
            return "竖向"
        else:
            return "正方形"


# ═══════════════════════════════════════
#  规则注册表
# ═══════════════════════════════════════

BUILTIN_RULES: dict[str, ClassifyRule] = {
    r.name: r for r in [
        SizeRule(),
        AspectRatioRule(),
        FormatRule(),
        FileSizeRule(),
        OrientationRule(),
    ]
}


# ═══════════════════════════════════════
#  分类执行引擎
# ═══════════════════════════════════════

def auto_classify_images(
    images: List[Image],
    db: Session,
    project_id: int,
    rules: Optional[List[str]] = None,
) -> dict:
    """
    对一批图片执行自动分类，返回统计信息。
    
    算法：
    1. 遍历每张图片
    2. 对每个启用的规则执行 check()
    3. 如果返回分类名，则确保该分类存在并与图片关联
    """
    # 确定启用的规则
    if rules:
        active_rules = {name: BUILTIN_RULES[name] for name in rules if name in BUILTIN_RULES}
    else:
        active_rules = dict(BUILTIN_RULES)

    if not active_rules:
        return {"classified_count": 0, "rules_applied": [], "details": "没有启用的规则"}

    # 缓存已有分类，避免重复查询
    existing_categories: dict[str, Category] = {}
    for cat in db.query(Category).filter(Category.project_id == project_id).all():
        existing_categories[cat.name] = cat

    # 缓存已有关联，避免重复建立
    existing_links = set()
    for link in db.query(ImageCategory).filter(
        ImageCategory.image_id.in_([img.id for img in images])
    ).all():
        existing_links.add((link.image_id, link.category_id))

    classified_count = 0
    assigned_count = 0
    details_by_image = {}

    for img in images:
        assigned_categories = []
        for rule_name, rule in active_rules.items():
            cat_name = rule.check(img)
            if not cat_name:
                continue

            # 获取或创建分类
            if cat_name not in existing_categories:
                cat = Category(
                    project_id=project_id,
                    name=cat_name,
                    description=rule.description,
                    sort_order=len(existing_categories),
                )
                db.add(cat)
                db.flush()
                existing_categories[cat_name] = cat
            else:
                cat = existing_categories[cat_name]

            # 检查是否已有关联
            if (img.id, cat.id) not in existing_links:
                link = ImageCategory(image_id=img.id, category_id=cat.id)
                db.add(link)
                existing_links.add((img.id, cat.id))
                assigned_count += 1

            assigned_categories.append(cat_name)

        if assigned_categories:
            classified_count += 1
            details_by_image[img.id] = {
                "filename": img.original_filename,
                "categories": assigned_categories,
            }

    db.commit()

    return {
        "classified_count": classified_count,
        "total_images": len(images),
        "assigned_count": assigned_count,
        "rules_applied": list(active_rules.keys()),
        "total_categories": len(existing_categories),
        "details": details_by_image,
    }


# ═══════════════════════════════════════
#  API 端点
# ═══════════════════════════════════════

@router.get("/projects/{project_id}/classify-rules", summary="获取可用分类规则")
async def list_classify_rules(project_id: int, db: Session = Depends(get_db)):
    """返回所有内置分类规则，供前端展示"""
    rules = []
    for name, rule in BUILTIN_RULES.items():
        rules.append({
            "name": rule.name,
            "description": rule.description,
        })
    return ApiResponse.success(data=rules)


@router.post("/projects/{project_id}/classify", summary="执行图片自动分类")
async def execute_classify(
    project_id: int,
    data: ClassifyRequest,
    db: Session = Depends(get_db),
):
    """
    对指定图片执行自动分类。

    - **image_ids**: 要分类的图片ID列表，为空则分类该项目下所有 cleaned 状态的图片
    - **rules**: 启用的规则名称列表，为空则启用所有规则
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ApiResponse.not_found("项目")

    # 获取要分类的图片
    if data.image_ids:
        images = db.query(Image).filter(
            Image.project_id == project_id,
            Image.id.in_(data.image_ids),
        ).all()
    else:
        # 默认对所有已上传/已清洗的图片分类
        images = db.query(Image).filter(
            Image.project_id == project_id,
            Image.status.in_(["uploaded", "cleaned", "selected"]),
        ).all()

    if not images:
        return ApiResponse.validation_error("没有可分类的图片，请先上传图片")

    # 执行自动分类
    result = auto_classify_images(
        images=images,
        db=db,
        project_id=project_id,
        rules=data.rules,
    )

    return ApiResponse.success(
        data=result,
        message=f"自动分类完成：{result['classified_count']}/{result['total_images']} 张图片已分类，新增 {result['assigned_count']} 条分类关联"
    )
