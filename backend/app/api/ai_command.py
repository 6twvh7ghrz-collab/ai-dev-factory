"""自然语言指令 API

POST /api/ai-command/preview          - 预览自然语言指令（V1）
POST /api/ai-command/execute-readonly - 执行只读指令（V1.1）
POST /api/ai-command/execute          - 安全执行写指令（V1.2，需确认令牌）

只读白名单：show_status, diagnose_blocker
已启用写意图：start_development
禁止：generate_plan / pause / resume / stop / unknown
"""
from pydantic import BaseModel, Field
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.response import ApiResponse
from app.executor.command_normalizer import CommandNormalizer
from app.executor.ai_brain_controller import AIBrainController

router = APIRouter()

# 全局单例
_normalizer = CommandNormalizer()
_controller = AIBrainController(_normalizer)


class PreviewRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    text: str = Field(..., description="用户自然语言输入", max_length=1000)


class ExecuteReadonlyRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    text: str = Field(..., description="用户自然语言输入", max_length=1000)
    confirmed_intent: str = Field(..., description="前端确认的意图，必须与重新解析结果一致")


class ExecuteRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    text: str = Field(..., description="用户自然语言输入", max_length=1000)
    confirmed_intent: str = Field(..., description="前端确认的意图，必须与重新解析结果一致")
    confirmation_token: str = Field(..., description="一次性确认令牌")


def _get_db_path() -> str:
    """获取数据库绝对路径"""
    from app.core.config import settings
    from pathlib import Path

    db_url = settings.DATABASE_URL
    if db_url.startswith("sqlite:///"):
        return db_url.replace("sqlite:///", "")
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db")


def _check_project_exists(db_path: str, project_id: int) -> bool:
    """检查项目是否存在"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE id = ?", (project_id,))
    proj = cur.fetchone()
    conn.close()
    return proj is not None


@router.post("/ai-command/preview", summary="预览自然语言指令")
async def preview_command(req: PreviewRequest):
    """将用户自然语言输入标准化为指令，仅返回预览结果。

    本轮禁止执行任何实际操作：
    - 不启动 Worker
    - 不调用 DeepSeek
    - 不创建或修改任务
    - 不修改数据库
    """
    # 基本校验
    if not req.text or not req.text.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "data": None,
                "message": "输入文本不能为空",
                "error": {"code": "EMPTY_INPUT", "detail": "text 字段不能为空"},
            },
        )

    db_path = _get_db_path()
    if not _check_project_exists(db_path, req.project_id):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "data": None,
                "message": f"项目 #{req.project_id} 不存在",
                "error": {"code": "PROJECT_NOT_FOUND", "detail": f"项目 #{req.project_id} 不存在"},
            },
        )

    # 预览指令
    result = _controller.preview(req.text, req.project_id)

    # V1.2: 为需要确认的写意图生成一次性确认令牌
    confirmation_token = None
    from app.executor.ai_brain_controller import _ENABLED_WRITE_INTENTS
    from app.executor.command_normalizer import CommandIntent
    if result["ok"]:
        intent_value = result["intent"]
        # 检查是否为已启用的写意图
        try:
            intent_enum = CommandIntent(intent_value)
            if intent_enum in _ENABLED_WRITE_INTENTS:
                from app.executor.confirmation_token import get_token_manager
                token_mgr = get_token_manager()
                confirmation_token = token_mgr.generate(
                    project_id=req.project_id,
                    intent=intent_value,
                    text=req.text.strip(),
                )
        except ValueError:
            pass  # 非标准意图，不生成令牌

    return JSONResponse(
        status_code=200,
        content={
            "ok": result["ok"],
            "data": {
                "project_id": result["project_id"],
                "intent": result["intent"],
                "confidence": result["confidence"],
                "requires_confirmation": result["requires_confirmation"],
                "action": result["action"],
                "message": result["message"],
                "executed": result["executed"],
                "confirmation_token": confirmation_token,
            },
            "message": result["message"],
            "error": None if result["ok"] else {
                "code": result.get("error", "UNKNOWN"),
                "detail": result["message"],
            },
        },
    )


@router.post("/ai-command/execute-readonly", summary="执行只读自然语言指令")
async def execute_readonly_command(req: ExecuteReadonlyRequest):
    """执行只读自然语言指令（show_status / diagnose_blocker）。

    执行前验证：
    1. 重新解析文本，确认 intent == confirmed_intent
    2. intent 属于只读白名单（show_status / diagnose_blocker）
    3. project_id 存在

    拒绝所有写意图（start_development / generate_plan / pause / resume / stop / unknown）。

    禁止：
    - 启动 Worker
    - 调用 DeepSeek
    - 创建或修改任务
    - 修改数据库
    """
    # 基本校验
    if not req.text or not req.text.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "code": "EMPTY_INPUT",
                "message": "输入文本不能为空",
                "executed": False,
                "data": None,
                "error": {"code": "EMPTY_INPUT", "detail": "text 字段不能为空"},
            },
        )

    if not req.confirmed_intent or not req.confirmed_intent.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "code": "MISSING_INTENT",
                "message": "confirmed_intent 不能为空",
                "executed": False,
                "data": None,
                "error": {"code": "MISSING_INTENT", "detail": "confirmed_intent 字段不能为空"},
            },
        )

    db_path = _get_db_path()
    if not _check_project_exists(db_path, req.project_id):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "code": "PROJECT_NOT_FOUND",
                "message": f"项目 #{req.project_id} 不存在",
                "executed": False,
                "data": None,
                "error": {"code": "PROJECT_NOT_FOUND", "detail": f"项目 #{req.project_id} 不存在"},
            },
        )

    # 执行只读指令
    result = _controller.execute_readonly(
        user_input=req.text.strip(),
        project_id=req.project_id,
        confirmed_intent=req.confirmed_intent.strip(),
    )

    status_code = 200 if result.get("ok") else 200  # 拒绝执行也用200，用code区分
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": result["ok"],
            "code": result.get("code", "OK"),
            "project_id": result.get("project_id"),
            "intent": result.get("intent"),
            "executed": result.get("executed", False),
            "action": result.get("action"),
            "data": result.get("data"),
            "message": result.get("message", ""),
            "error": None if result.get("ok") else {
                "code": result.get("code", "UNKNOWN"),
                "detail": result.get("message", ""),
            },
        },
    )


@router.post("/ai-command/execute", summary="安全执行自然语言写指令（需确认令牌）")
async def execute_command(req: ExecuteRequest):
    """安全执行自然语言写指令（V1.2，当前仅 start_development）。

    执行前验证：
    1. 重新解析 text，确认 intent == confirmed_intent
    2. intent 属于已启用的写意图白名单
    3. confirmation_token 有效且未使用
    4. project_id 存在
    5. 项目绑定了允许工作区
    6. 工作区存在且为有效 Git 仓库
    7. working tree clean
    8. 真实调度器 runnable_tasks > 0
    9. 不存在活跃 run
    10. 不存在冲突 Lease 或资源锁

    禁止自动执行正式电商项目。
    """
    # 基本校验
    if not req.text or not req.text.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "code": "EMPTY_INPUT",
                "message": "输入文本不能为空",
                "executed": False,
                "data": None,
                "error": {"code": "EMPTY_INPUT", "detail": "text 字段不能为空"},
            },
        )

    if not req.confirmed_intent or not req.confirmed_intent.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "code": "MISSING_INTENT",
                "message": "confirmed_intent 不能为空",
                "executed": False,
                "data": None,
                "error": {"code": "MISSING_INTENT", "detail": "confirmed_intent 字段不能为空"},
            },
        )

    if not req.confirmation_token or not req.confirmation_token.strip():
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "code": "MISSING_TOKEN",
                "message": "confirmation_token 不能为空",
                "executed": False,
                "data": None,
                "error": {"code": "MISSING_TOKEN", "detail": "confirmation_token 字段不能为空"},
            },
        )

    # 执行写指令
    result = _controller.execute_write(
        user_input=req.text.strip(),
        project_id=req.project_id,
        confirmed_intent=req.confirmed_intent.strip(),
        confirmation_token=req.confirmation_token.strip(),
    )

    return JSONResponse(
        status_code=200,
        content={
            "ok": result["ok"],
            "code": result.get("code", "OK"),
            "project_id": result.get("project_id"),
            "intent": result.get("intent"),
            "executed": result.get("executed", False),
            "action": result.get("action"),
            "data": result.get("data"),
            "run_id": result.get("run_id"),
            "task_id": result.get("task_id"),
            "message": result.get("message", ""),
            "error": None if result.get("ok") else {
                "code": result.get("code", "UNKNOWN"),
                "detail": result.get("message", ""),
            },
        },
    )
