"""开发任务模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class DevelopmentTask(Base):
    __tablename__ = "development_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    feature_id = Column(Integer, ForeignKey("features.id", ondelete="SET NULL"), nullable=True)

    title = Column(String(500), nullable=False, comment="任务标题")
    description = Column(Text, nullable=True, comment="任务描述")
    task_type = Column(String(30), default="backend", comment="任务类型：frontend/backend/database/test/documentation")
    priority = Column(String(20), default="medium", comment="优先级：high/medium/low")
    dependencies = Column(Text, nullable=True, comment="依赖任务ID列表JSON")
    files_to_check = Column(Text, nullable=True, comment="需检查的文件JSON")
    files_to_modify = Column(Text, nullable=True, comment="需修改的文件JSON")
    codex_prompt = Column(Text, nullable=True, comment="CODEX执行指令")
    test_steps = Column(Text, nullable=True, comment="测试步骤JSON")
    acceptance_criteria = Column(Text, nullable=True, comment="验收标准JSON")
    implementation_steps = Column(Text, nullable=True, comment="实现步骤JSON")

    status = Column(String(20), default="pending", comment="任务状态：pending/analyzing/executing/waiting_test/test_failed/completed/paused/cancelled")
    readiness_status = Column(String(20), default="draft", comment="任务准备状态：draft/needs_planning/ready/executing/testing/completed/blocked")
    execution_result = Column(Text, nullable=True, comment="执行结果")
    sort_order = Column(Integer, default=0, comment="排序")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", back_populates="development_tasks")
