"""智能追问 API - 基于用户自然语言描述生成关键问题"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database.engine import get_db
from app.core.response import ApiResponse
from app.api.ai_config import get_active_provider

router = APIRouter()

# 软件需求描述最大字符数限制（在调用AI之前校验）
MAX_INPUT_LENGTH = 2000

SMART_QUESTION_PROMPT = """你是一位资深的软件产品经理，正在帮助一位不懂编程的用户明确他们的软件需求。

用户已经用自然语言描述了他们想做的软件。请根据用户的描述，生成最多3个最关键的问题来帮助用户进一步明确需求。

规则：
1. 只问最关键的问题，最多3个
2. 问题必须是用户能轻松回答的，不要使用技术术语
3. 每个问题提供2-4个常见选项供用户快速选择
4. 问题应该聚焦在：是否需要登录、数据存储、平台类型、是否需要第三方服务等
5. 如果用户描述已经很清楚，可以少问或不问

请严格按照以下 JSON 格式输出，不要输出任何其他内容：
{
  "questions": [
    {
      "question": "问题文本",
      "hint": "简短提示说明为什么这个问题重要",
      "options": ["选项A", "选项B", "选项C"]
    }
  ],
  "summary": "对用户需求的一句话理解"
}"""


@router.post("/smart-questions", summary="生成智能追问")
async def generate_smart_questions(data: dict, db: Session = Depends(get_db)):
    """根据用户的自然语言描述，生成最多3个关键追问问题。

    请求体：
    {
        "user_input": "用户用自然语言描述的软件想法",
        "previous_answers": {"之前的回答key": "回答内容"}  // 可选，第二轮追问时使用
    }
    """
    user_input = data.get("user_input", "").strip()
    if not user_input:
        return ApiResponse.validation_error("请输入软件想法描述")

    if len(user_input) > MAX_INPUT_LENGTH:
        return ApiResponse.error(
            code="INPUT_TOO_LONG",
            detail=f"软件需求描述最多允许 {MAX_INPUT_LENGTH} 个字符，当前输入 {len(user_input)} 个字符",
            message="输入内容过长",
        )

    try:
        provider = get_active_provider(db)
    except ValueError as e:
        return ApiResponse.ai_error(detail=str(e))

    # 构建用户消息
    user_msg = f"用户的软件想法描述：\n{user_input}"

    previous_answers = data.get("previous_answers", {})
    if previous_answers:
        answers_text = "\n".join(f"- {k}: {v}" for k, v in previous_answers.items())
        user_msg += f"\n\n用户对之前问题的回答：\n{answers_text}"

    try:
        result = await provider.async_chat_json(
            system_prompt=SMART_QUESTION_PROMPT,
            user_message=user_msg,
            max_tokens=1000,
        )
        return ApiResponse.success(data=result, message="智能追问生成成功")

    except Exception as e:
        return ApiResponse.ai_error(detail=str(e))
