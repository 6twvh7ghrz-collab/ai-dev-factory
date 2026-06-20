"""AI 配置模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class AiConfig(Base):
    __tablename__ = "ai_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(100), nullable=False, comment="AI提供商：openai/deepseek/moonshot等")
    model = Column(String(100), nullable=False, comment="模型名称")
    api_key_encrypted = Column(Text, nullable=True, comment="加密后的API Key")
    base_url = Column(String(500), nullable=True, comment="API基础URL")
    is_active = Column(Boolean, default=False, comment="是否为当前激活配置")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AiGenerationLog(Base):
    __tablename__ = "ai_generation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="关联项目ID")
    generation_type = Column(String(50), nullable=False, comment="生成类型：analysis/mvp/architecture/database/api/task/bug")
    model = Column(String(100), nullable=True, comment="使用的模型")
    input_summary = Column(Text, nullable=True, comment="输入摘要")
    output_summary = Column(Text, nullable=True, comment="输出摘要")
    success = Column(Boolean, default=False, comment="是否成功")
    error_message = Column(Text, nullable=True, comment="错误信息")

    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="ai_generation_logs")
