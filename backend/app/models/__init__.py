"""数据模型统一导入，确保所有模型被注册"""
from app.models.project import Project
from app.models.analysis import RequirementAnalysis
from app.models.module import Module, Feature
from app.models.database_design import DatabaseTable
from app.models.api_design import ApiDefinition
from app.models.task import DevelopmentTask
from app.models.bug import Bug, BugStatusLog
from app.models.ai_config import AiConfig, AiGenerationLog
from app.models.image import Image, Category, ImageCategory, PendingRelease

__all__ = [
    "Project",
    "RequirementAnalysis",
    "Module",
    "Feature",
    "DatabaseTable",
    "ApiDefinition",
    "DevelopmentTask",
    "Bug",
    "BugStatusLog",
    "AiConfig",
    "AiGenerationLog",
    "Image",
    "Category",
    "ImageCategory",
    "PendingRelease",
]
