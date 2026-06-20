"""统一响应格式"""
from typing import Any, Optional


class ApiResponse:
    """所有 API 返回统一格式"""

    @staticmethod
    def success(data: Any = None, message: str = "操作成功") -> dict:
        return {
            "ok": True,
            "data": data,
            "message": message,
            "error": None,
        }

    @staticmethod
    def error(
        code: str = "INTERNAL_ERROR",
        detail: str = "操作失败",
        message: str = "操作失败",
    ) -> dict:
        return {
            "ok": False,
            "data": None,
            "message": message,
            "error": {
                "code": code,
                "detail": detail,
            },
        }

    @staticmethod
    def not_found(resource: str = "资源") -> dict:
        return ApiResponse.error(
            code="NOT_FOUND",
            detail=f"{resource}不存在",
            message=f"{resource}不存在",
        )

    @staticmethod
    def validation_error(detail: str = "输入数据验证失败") -> dict:
        return ApiResponse.error(
            code="VALIDATION_ERROR",
            detail=detail,
            message="输入数据验证失败",
        )

    @staticmethod
    def ai_error(detail: str = "AI 服务调用失败") -> dict:
        return ApiResponse.error(
            code="AI_SERVICE_ERROR",
            detail=detail,
            message="AI 服务调用失败",
        )
