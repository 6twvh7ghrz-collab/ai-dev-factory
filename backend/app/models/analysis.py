"""需求分析模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class RequirementAnalysis(Base):
    __tablename__ = "requirement_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)

    product_definition = Column(Text, nullable=True, comment="产品一句话定义")
    problem = Column(Text, nullable=True, comment="软件解决的问题")
    target_users = Column(Text, nullable=True, comment="目标用户JSON")
    usage_scenarios = Column(Text, nullable=True, comment="使用场景JSON")
    core_value = Column(Text, nullable=True, comment="核心价值JSON")
    business_flow = Column(Text, nullable=True, comment="主要业务流程JSON")
    risks = Column(Text, nullable=True, comment="项目风险JSON")
    technical_notes = Column(Text, nullable=True, comment="技术难点JSON")
    third_party_deps = Column(Text, nullable=True, comment="第三方依赖JSON")
    data_security = Column(Text, nullable=True, comment="数据安全要求JSON")
    mvp_success_criteria = Column(Text, nullable=True, comment="MVP成功标准JSON")

    # 原始AI返回
    raw_result = Column(Text, nullable=True, comment="AI原始返回内容")

    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="requirement_analysis")
