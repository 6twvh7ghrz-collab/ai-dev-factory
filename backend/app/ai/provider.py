"""AI Provider 统一接口层

所有 AI 调用通过此层进行，支持 OpenAI 兼容接口（包括 DeepSeek、Moonshot 等）。
不把模型调用代码直接散落在路由中。
"""
import asyncio
import json
from typing import Optional
from openai import OpenAI, APIConnectionError, APITimeoutError, AuthenticationError, RateLimitError
from app.core.logging import get_logger
from app.core.config import settings

logger = get_logger(__name__)

# 默认超时秒数
DEFAULT_TIMEOUT = 60.0


class AiProvider:
    """统一 AI 调用层"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=2,
        )

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[dict] = None,
    ) -> str:
        """发送聊天请求并返回文本结果（同步）"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = self.client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if content is None:
                raise ValueError("AI 返回内容为空")
            return content
        except AuthenticationError:
            raise ValueError("API Key 无效或已过期，请检查配置")
        except APITimeoutError:
            raise ValueError(f"AI 请求超时（{self.timeout}秒），请检查网络或增加超时时间")
        except APIConnectionError as e:
            raise ValueError(f"无法连接到 AI 服务（{self.base_url}），请检查 Base URL 和网络连接。原始错误: {e}")
        except RateLimitError:
            raise ValueError("AI 请求频率超限，请稍后重试")
        except Exception as e:
            logger.error(f"AI 调用失败: {type(e).__name__}: {str(e)}")
            raise ValueError(f"AI 调用失败: {type(e).__name__}: {str(e)}")

    async def async_chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[dict] = None,
    ) -> str:
        """发送聊天请求并返回文本结果（异步，不阻塞事件循环）"""
        return await asyncio.to_thread(
            self.chat,
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict:
        """发送聊天请求并解析 JSON 结果（同步）"""
        # 尝试使用 JSON 模式
        try:
            content = self.chat(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except ValueError:
            # ValueError 是业务级错误（如 Key 无效、超时），不应重试
            raise
        except Exception:
            # 其他异常（如模型不支持 json_object），降级为普通文本模式
            content = self.chat(
                system_prompt=system_prompt + "\n请严格以JSON格式输出，不要输出任何其他内容。",
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        return self._parse_json(content)

    async def async_chat_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict:
        """发送聊天请求并解析 JSON 结果（异步，不阻塞事件循环）"""
        return await asyncio.to_thread(
            self.chat_json,
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _parse_json(content: str) -> dict:
        """解析 AI 返回的 JSON，兼容 Markdown 代码块包裹"""
        text = content.strip()
        # 去除 markdown 代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"AI 返回 JSON 解析失败: {e}\n原始内容: {text[:500]}")
            raise ValueError(f"AI 返回内容无法解析为 JSON: {str(e)}")


def get_provider_from_config(config: "AiConfig") -> AiProvider:
    """从数据库配置创建 Provider 实例"""
    from app.core.security import decrypt_value

    api_key = decrypt_value(config.api_key_encrypted)
    base_url = config.base_url or "https://api.openai.com/v1"
    return AiProvider(api_key=api_key, base_url=base_url, model=config.model)
