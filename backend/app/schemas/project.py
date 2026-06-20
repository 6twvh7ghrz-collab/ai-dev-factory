"""项目管理 Schema"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="项目名称")
    idea: Optional[str] = Field(None, max_length=2000, description="软件想法")
    description: Optional[str] = Field(None, max_length=2000, description="项目详细说明")


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    idea: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    current_stage: Optional[str] = None


class RequirementInput(BaseModel):
    """需求输入 Schema"""
    name: Optional[str] = Field(None, description="软件名称")
    idea: Optional[str] = Field(None, max_length=2000, description="软件想法/一句话描述")
    description: Optional[str] = Field(None, max_length=2000, description="项目详细说明")
    target_users: Optional[str] = Field(None, max_length=2000, description="目标用户")
    core_features: Optional[str] = Field(None, max_length=2000, description="核心功能")
    scenarios: Optional[str] = Field(None, max_length=2000, description="使用场景")
    platform: Optional[str] = Field(None, max_length=100, description="平台类型")
    need_login: Optional[str] = Field(None, max_length=20, description="是否需要登录")
    need_database: Optional[str] = Field(None, max_length=20, description="是否需要数据库")
    need_ai: Optional[str] = Field(None, max_length=20, description="是否需要AI")
    need_third_party: Optional[str] = Field(None, max_length=20, description="是否需要第三方接口")
    need_upload: Optional[str] = Field(None, max_length=20, description="是否需要上传文件")
    need_export: Optional[str] = Field(None, max_length=20, description="是否需要导出结果")
    tech_requirements: Optional[str] = Field(None, max_length=2000, description="技术要求")
    exclude_features: Optional[str] = Field(None, max_length=2000, description="不希望出现的功能")
    additional_notes: Optional[str] = Field(None, max_length=2000, description="其他补充说明")


class ProjectOut(BaseModel):
    id: int
    name: str
    idea: Optional[str] = None
    description: Optional[str] = None
    status: str
    current_stage: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
