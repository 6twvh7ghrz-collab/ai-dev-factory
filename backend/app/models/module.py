"""模块和功能模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False, comment="模块名称")
    description = Column(Text, nullable=True, comment="模块描述")
    input_definition = Column(Text, nullable=True, comment="输入定义")
    process_definition = Column(Text, nullable=True, comment="处理逻辑")
    output_definition = Column(Text, nullable=True, comment="输出定义")
    priority = Column(String(20), default="medium", comment="优先级：high/medium/low")
    version_stage = Column(String(20), default="mvp", comment="版本阶段：mvp/phase2/later")
    status = Column(String(20), default="planned", comment="模块状态")
    sort_order = Column(Integer, default=0, comment="排序")

    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="modules")
    features = relationship("Feature", back_populates="module", cascade="all, delete-orphan")


class Feature(Base):
    __tablename__ = "features"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False, comment="功能名称")
    description = Column(Text, nullable=True, comment="功能描述")
    priority = Column(String(20), default="medium", comment="优先级")
    version_stage = Column(String(20), default="mvp", comment="版本阶段：mvp/phase2/later")
    acceptance_criteria = Column(Text, nullable=True, comment="验收标准JSON")
    status = Column(String(20), default="planned", comment="功能状态")

    module = relationship("Module", back_populates="features")
