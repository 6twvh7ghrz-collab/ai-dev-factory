"""Bug 分析模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.engine import Base


class Bug(Base):
    __tablename__ = "bugs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)

    title = Column(String(500), nullable=False, comment="Bug标题")
    description = Column(Text, nullable=True, comment="Bug描述")
    error_message = Column(Text, nullable=True, comment="错误信息")
    reproduction_steps = Column(Text, nullable=True, comment="复现步骤")
    expected_result = Column(Text, nullable=True, comment="预期结果")
    actual_result = Column(Text, nullable=True, comment="实际结果")
    related_code = Column(Text, nullable=True, comment="相关代码")
    backend_logs = Column(Text, nullable=True, comment="后端日志")
    console_errors = Column(Text, nullable=True, comment="浏览器控制台错误")
    network_requests = Column(Text, nullable=True, comment="网络请求结果")

    # AI 分析结果
    bug_type = Column(String(50), nullable=True, comment="Bug类型")
    severity = Column(String(20), nullable=True, comment="严重等级：critical/high/medium/low")
    probable_cause = Column(Text, nullable=True, comment="最可能的原因JSON")
    affected_module = Column(String(200), nullable=True, comment="受影响模块")
    affected_files = Column(Text, nullable=True, comment="需检查的文件JSON")
    fix_plan = Column(Text, nullable=True, comment="修复计划JSON")
    regression_risks = Column(Text, nullable=True, comment="回归风险JSON")
    fix_prompt = Column(Text, nullable=True, comment="CODEX修复指令")
    test_steps = Column(Text, nullable=True, comment="修复后测试步骤JSON")
    is_blocking = Column(String(10), nullable=True, comment="是否阻塞上线：yes/no/unknown")

    # CODEX 执行结果
    execution_result = Column(Text, nullable=True, comment="CODEX执行结果")
    files_changed = Column(Text, nullable=True, comment="修改文件清单JSON")
    test_result = Column(Text, nullable=True, comment="测试结果")
    remaining_issues = Column(Text, nullable=True, comment="未解决问题")
    executed_at = Column(DateTime, nullable=True, comment="执行时间")

    # 状态与时间
    status = Column(String(20), default="reported", comment="Bug状态：reported/analyzing/analyzed/fix_ready/fixing/waiting_test/resolved/reopened/closed")
    resolved_at = Column(DateTime, nullable=True, comment="解决时间")
    failure_fingerprint = Column(String(32), nullable=True, comment="去重指纹 sha256[:32]")

    # 关联字段（用于执行器闭环）
    task_id = Column(Integer, nullable=True, comment="关联开发任务ID")
    execution_id = Column(Integer, nullable=True, comment="关联执行记录ID")
    repair_attempt = Column(Integer, default=0, comment="修复尝试次数")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", back_populates="bugs")
    status_logs = relationship("BugStatusLog", back_populates="bug", order_by="BugStatusLog.created_at", cascade="all, delete-orphan")


class BugStatusLog(Base):
    """Bug 状态变更记录"""
    __tablename__ = "bug_status_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bug_id = Column(Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False)
    from_status = Column(String(20), nullable=True, comment="原状态")
    to_status = Column(String(20), nullable=False, comment="新状态")
    reason = Column(Text, nullable=True, comment="变更原因")
    operator = Column(String(100), nullable=True, comment="操作人")
    created_at = Column(DateTime, default=datetime.now)

    bug = relationship("Bug", back_populates="status_logs")
