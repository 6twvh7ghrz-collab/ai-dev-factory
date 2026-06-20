"""项目管理模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="项目名称")
    idea = Column(Text, nullable=True, comment="软件想法/一句话描述")
    description = Column(Text, nullable=True, comment="项目详细说明")

    # 需求字段
    target_users = Column(Text, nullable=True, comment="目标用户")
    core_features = Column(Text, nullable=True, comment="核心功能")
    scenarios = Column(Text, nullable=True, comment="使用场景")
    platform = Column(String(50), nullable=True, comment="平台类型：web/desktop/mobile")
    need_login = Column(String(10), default="unknown", comment="是否需要登录")
    need_database = Column(String(10), default="unknown", comment="是否需要数据库")
    need_ai = Column(String(10), default="unknown", comment="是否需要AI")
    need_third_party = Column(String(10), default="unknown", comment="是否需要第三方接口")
    need_upload = Column(String(10), default="unknown", comment="是否需要上传文件")
    need_export = Column(String(10), default="unknown", comment="是否需要导出结果")
    tech_requirements = Column(Text, nullable=True, comment="用户确定的技术要求")
    exclude_features = Column(Text, nullable=True, comment="用户不希望出现的功能")
    additional_notes = Column(Text, nullable=True, comment="其他补充说明")

    # 状态
    status = Column(String(20), default="draft", comment="项目状态：draft/analyzing/generated/developing/testing/completed/paused")
    current_stage = Column(String(50), default="requirement_input", comment="当前阶段")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 关系
    requirement_analysis = relationship("RequirementAnalysis", back_populates="project", uselist=False, cascade="all, delete-orphan")
    modules = relationship("Module", back_populates="project", cascade="all, delete-orphan")
    database_tables = relationship("DatabaseTable", back_populates="project", cascade="all, delete-orphan")
    api_definitions = relationship("ApiDefinition", back_populates="project", cascade="all, delete-orphan")
    development_tasks = relationship("DevelopmentTask", back_populates="project", cascade="all, delete-orphan")
    bugs = relationship("Bug", back_populates="project", cascade="all, delete-orphan")
    ai_generation_logs = relationship("AiGenerationLog", back_populates="project", cascade="all, delete-orphan")
