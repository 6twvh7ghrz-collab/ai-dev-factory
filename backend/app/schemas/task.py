"""任务 Schema"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    execution_result: Optional[str] = None
    sort_order: Optional[int] = None


class TaskOut(BaseModel):
    id: int
    project_id: int
    module_id: Optional[int] = None
    feature_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    task_type: str
    priority: str
    dependencies: Optional[str] = None
    files_to_check: Optional[str] = None
    files_to_modify: Optional[str] = None
    codex_prompt: Optional[str] = None
    test_steps: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status: str
    execution_result: Optional[str] = None
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
