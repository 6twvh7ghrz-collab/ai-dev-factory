"""API 设计模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class ApiDefinition(Base):
    __tablename__ = "api_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    method = Column(String(10), nullable=False, comment="HTTP方法：GET/POST/PUT/DELETE")
    path = Column(String(500), nullable=False, comment="API路径")
    description = Column(Text, nullable=True, comment="接口描述")
    request_schema = Column(Text, nullable=True, comment="请求参数JSON")
    response_schema = Column(Text, nullable=True, comment="响应格式JSON")
    error_codes = Column(Text, nullable=True, comment="错误码JSON")
    status = Column(String(20), default="designed", comment="接口状态")

    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="api_definitions")
