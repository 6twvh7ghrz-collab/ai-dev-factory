"""AI 软件开发工厂 V2 - 后端应用 v2.0.1"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.logging import setup_logging
from app.api import projects, ai_config, analysis, modules, tasks, bugs, categories, pending_release, auto_classify, smart_questions, executor, ai_command, planner, v2_worker_api
from app.database.engine import create_tables


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭生命周期"""
    setup_logging()
    create_tables()
    yield


app = FastAPI(
    title="AI 软件开发工厂 V2",
    description="将模糊的软件想法，自动转变为一套清晰、可执行、可测试的软件开发方案",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix="/api", tags=["项目管理"])
app.include_router(ai_config.router, prefix="/api", tags=["AI配置"])
app.include_router(analysis.router, prefix="/api", tags=["AI分析"])
app.include_router(modules.router, prefix="/api", tags=["模块与MVP"])
app.include_router(tasks.router, prefix="/api", tags=["开发任务"])
app.include_router(bugs.router, prefix="/api", tags=["Bug分析"])
app.include_router(categories.router, prefix="/api", tags=["分类与图片"])
app.include_router(pending_release.router, prefix="/api", tags=["待发布资料库"])
app.include_router(auto_classify.router, prefix="/api", tags=["自动分类"])
app.include_router(smart_questions.router, prefix="/api", tags=["智能追问"])
app.include_router(executor.router, prefix="/api", tags=["执行器"])
app.include_router(ai_command.router, prefix="/api", tags=["AI指令"])
app.include_router(planner.router, prefix="/api", tags=["工程规划"])
app.include_router(v2_worker_api.router, tags=["V2 Worker API"])


@app.get("/api/health")
async def health_check():
    return {"ok": True, "data": {"status": "running"}, "message": "服务正常运行"}
