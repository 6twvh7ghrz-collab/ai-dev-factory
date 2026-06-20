"""应用配置管理"""
import os
from pathlib import Path
from typing import List

# 项目根目录（backend/ 的绝对路径）
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings:
    """全局配置，优先从环境变量读取"""

    # 应用
    APP_NAME: str = "AI 软件开发工厂 V2"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # 数据库（使用绝对路径，避免工作目录不同导致连错数据库）
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{_BACKEND_DIR / 'data' / 'ai_factory.db'}"
    )

    # 加密密钥（用于加密 API Key）
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "default-key-change-in-production")

    # CORS
    CORS_ORIGINS: List[str] = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
    ).split(",")

    # ── V2 Feature Flags ──

    # V2.0-B1: 控制平面最小底座（状态机 + 事件服务）
    # 默认关闭，开启后不影响 V1 行为
    V2_CONTROL_PLANE_ENABLED: bool = os.getenv(
        "V2_CONTROL_PLANE_ENABLED", "false"
    ).lower() == "true"

    # V2.0-B2+: Supervisor 模型调用（预留，本轮不实现）
    V2_ENABLED: bool = os.getenv("V2_ENABLED", "false").lower() == "true"

    # V2.0-B2+: Supervisor 模式
    V2_SUPERVISOR_MODE: bool = os.getenv(
        "V2_SUPERVISOR_MODE", "false"
    ).lower() == "true"

    # V2.0-B2+: Agent 并发数
    V2_AGENT_CONCURRENCY: int = int(os.getenv("V2_AGENT_CONCURRENCY", "1"))

    # AI 默认配置
    AI_DEFAULT_TIMEOUT: int = 120  # 秒
    AI_MAX_RETRIES: int = 2
    AI_MAX_TOKENS: int = 4096


settings = Settings()
