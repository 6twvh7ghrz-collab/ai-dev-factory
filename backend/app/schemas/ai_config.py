"""AI 配置 Schema"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class AiConfigCreate(BaseModel):
    provider: str = Field(..., min_length=1, max_length=100, description="AI提供商")
    model: str = Field(..., min_length=1, max_length=100, description="模型名称")
    api_key: str = Field(..., min_length=1, description="API Key")
    base_url: Optional[str] = Field(None, description="API基础URL")


class AiConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    is_active: Optional[bool] = None


class AiConfigOut(BaseModel):
    id: int
    provider: str
    model: str
    api_key_masked: str = ""
    base_url: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
