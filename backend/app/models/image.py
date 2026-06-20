"""图片与分类模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    original_filename = Column(String(500), nullable=False, comment="原始文件名")
    stored_path = Column(String(500), nullable=False, comment="存储路径")
    thumbnail_path = Column(String(500), nullable=True, comment="缩略图路径")
    cleaned_path = Column(String(500), nullable=True, comment="清洗后路径")
    file_size = Column(Integer, nullable=True, comment="文件大小(字节)")
    width = Column(Integer, nullable=True, comment="宽度")
    height = Column(Integer, nullable=True, comment="高度")
    format = Column(String(20), nullable=True, comment="格式")
    status = Column(String(20), default="uploaded", comment="状态: uploaded/cleaning/cleaned/selected/released")
    sort_order = Column(Integer, default=0, comment="排序")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 多对多分类
    categories = relationship("ImageCategory", back_populates="image", cascade="all, delete-orphan")


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False, comment="分类名称")
    description = Column(Text, nullable=True, comment="分类描述")
    sort_order = Column(Integer, default=0, comment="排序")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    images = relationship("ImageCategory", back_populates="category", cascade="all, delete-orphan")


class ImageCategory(Base):
    """图片与分类的关联表"""
    __tablename__ = "image_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_id = Column(Integer, ForeignKey("images.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    image = relationship("Image", back_populates="categories")
    category = relationship("Category", back_populates="images")


class PendingRelease(Base):
    """待发布资料库"""
    __tablename__ = "pending_release"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=True, comment="上架标题")
    description = Column(Text, nullable=True, comment="上架描述")
    price = Column(Float, nullable=True, comment="价格")
    platform = Column(String(50), nullable=True, comment="目标平台")
    sort_order = Column(Integer, default=0, comment="排序")
    status = Column(String(20), default="draft", comment="状态: draft/ready/released/failed")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    image = relationship("Image")
