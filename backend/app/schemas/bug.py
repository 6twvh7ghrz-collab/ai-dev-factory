"""Bug Schema"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class BugCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="Bug标题")
    description: Optional[str] = None
    error_message: Optional[str] = None
    reproduction_steps: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    related_code: Optional[str] = None
    backend_logs: Optional[str] = None
    console_errors: Optional[str] = None
    network_requests: Optional[str] = None
    task_id: Optional[int] = Field(None, description="关联任务ID（去重用）")
    execution_id: Optional[int] = Field(None, description="关联执行记录ID（去重用）")


class BugUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None


class BugStatusUpdate(BaseModel):
    """Bug 状态更新"""
    status: str = Field(..., description="目标状态")
    reason: Optional[str] = Field(None, description="状态变更原因")


class BugExecutionResult(BaseModel):
    """CODEX 执行结果"""
    execution_result: str = Field(..., description="CODEX执行结果")
    files_changed: Optional[str] = Field(None, description="修改文件清单，逗号分隔")
    test_result: Optional[str] = Field(None, description="测试结果")
    remaining_issues: Optional[str] = Field(None, description="未解决问题")


class BugTestResult(BaseModel):
    """回归测试结果"""
    passed: bool = Field(..., description="是否通过")
    test_notes: Optional[str] = Field(None, description="测试说明")
    checklist: Optional[list[str]] = Field(None, description="测试清单结果")


class BugOut(BaseModel):
    id: int
    project_id: int
    title: str
    description: Optional[str] = None
    error_message: Optional[str] = None
    reproduction_steps: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    bug_type: Optional[str] = None
    severity: Optional[str] = None
    probable_cause: Optional[str] = None
    affected_module: Optional[str] = None
    affected_files: Optional[str] = None
    fix_plan: Optional[str] = None
    regression_risks: Optional[str] = None
    fix_prompt: Optional[str] = None
    test_steps: Optional[str] = None
    is_blocking: Optional[str] = None
    execution_result: Optional[str] = None
    files_changed: Optional[str] = None
    test_result: Optional[str] = None
    remaining_issues: Optional[str] = None
    executed_at: Optional[datetime] = None
    status: str
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
