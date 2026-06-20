"""AI 配置 API"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.models import AiConfig, AiGenerationLog
from app.schemas.ai_config import AiConfigCreate, AiConfigUpdate, AiConfigOut
from app.core.response import ApiResponse
from app.core.security import encrypt_value, decrypt_value, mask_key
from app.ai.provider import AiProvider

router = APIRouter()


@router.get("/settings/ai", summary="获取AI配置列表")
async def list_ai_configs(db: Session = Depends(get_db)):
    configs = db.query(AiConfig).order_by(AiConfig.updated_at.desc()).all()
    result = []
    for c in configs:
        api_key = decrypt_value(c.api_key_encrypted) if c.api_key_encrypted else ""
        result.append({
            "id": c.id,
            "provider": c.provider,
            "model": c.model,
            "api_key_masked": mask_key(api_key),
            "base_url": c.base_url,
            "is_active": c.is_active,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return ApiResponse.success(data=result)


@router.put("/settings/ai", summary="创建或更新AI配置")
async def save_ai_config(data: AiConfigCreate, db: Session = Depends(get_db)):
    # 查找是否已有相同 provider+model 的配置
    existing = db.query(AiConfig).filter(
        AiConfig.provider == data.provider,
        AiConfig.model == data.model,
    ).first()

    if existing:
        existing.api_key_encrypted = encrypt_value(data.api_key)
        existing.base_url = data.base_url
        existing.is_active = True
        # 将其他配置设为非激活
        for c in db.query(AiConfig).filter(AiConfig.id != existing.id).all():
            c.is_active = False
        db.commit()
        db.refresh(existing)
        return ApiResponse.success(data={"id": existing.id}, message="AI配置更新成功")
    else:
        # 将其他配置设为非激活
        for c in db.query(AiConfig).all():
            c.is_active = False
        config = AiConfig(
            provider=data.provider,
            model=data.model,
            api_key_encrypted=encrypt_value(data.api_key),
            base_url=data.base_url,
            is_active=True,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
        return ApiResponse.success(data={"id": config.id}, message="AI配置创建成功")


@router.post("/settings/ai/test", summary="测试AI连接")
async def test_ai_connection(data: AiConfigCreate, db: Session = Depends(get_db)):
    try:
        base_url = data.base_url or "https://api.openai.com/v1"
        provider = AiProvider(
            api_key=data.api_key,
            base_url=base_url,
            model=data.model,
        )
        # 使用 async_chat 避免阻塞事件循环
        result = await provider.async_chat(
            system_prompt="你是一个测试助手。",
            user_message="请回复：连接成功",
            max_tokens=20,
        )
        return ApiResponse.success(data={"response": result[:100]}, message="AI连接测试成功")
    except ValueError as e:
        # AiProvider 内部已转换为用户友好的错误消息
        return ApiResponse.ai_error(detail=str(e))
    except Exception as e:
        logger_msg = f"AI连接测试异常: {type(e).__name__}: {str(e)}"
        from app.core.logging import get_logger
        get_logger(__name__).error(logger_msg)
        return ApiResponse.ai_error(detail=f"连接失败: {type(e).__name__}: {str(e)[:200]}")


def get_active_provider(db: Session = Depends(get_db)) -> AiProvider:
    """获取当前激活的 AI Provider（依赖注入用）"""
    config = db.query(AiConfig).filter(AiConfig.is_active == True).first()
    if not config:
        raise ValueError("未配置AI服务，请先在设置中配置API Key")
    from app.ai.provider import get_provider_from_config
    return get_provider_from_config(config)
