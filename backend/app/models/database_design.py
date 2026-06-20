"""数据库设计模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class DatabaseTable(Base):
    __tablename__ = "database_tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    table_name = Column(String(200), nullable=False, comment="表名")
    purpose = Column(Text, nullable=True, comment="表的用途")
    schema_json = Column(Text, nullable=True, comment="表结构JSON（含字段、类型、约束等）")
    relationships_json = Column(Text, nullable=True, comment="表关系JSON")

    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="database_tables")
