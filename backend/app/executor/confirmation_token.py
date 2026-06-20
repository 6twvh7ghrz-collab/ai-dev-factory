"""一次性确认令牌管理器

用于自然语言指令执行的安全确认：
- Preview 成功后生成短期令牌
- 有效期 60 秒
- 只能使用一次
- 绑定 project_id、intent、原始文本摘要
"""
import uuid
import time
import hashlib
import threading
from typing import Optional, Dict, Any


class TokenRecord:
    """令牌记录"""
    __slots__ = ('token', 'project_id', 'intent', 'text_hash', 'created_at', 'used')

    def __init__(self, token: str, project_id: int, intent: str, text_hash: str):
        self.token = token
        self.project_id = project_id
        self.intent = intent
        self.text_hash = text_hash
        self.created_at = time.time()
        self.used = False


class ConfirmationTokenManager:
    """一次性确认令牌管理器（内存存储，进程内有效）"""

    TTL_SECONDS = 60  # 令牌有效期

    def __init__(self):
        self._tokens: Dict[str, TokenRecord] = {}
        self._lock = threading.Lock()

    def _hash_text(self, text: str) -> str:
        """生成文本摘要（SHA256前16位）"""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

    def generate(self, project_id: int, intent: str, text: str) -> str:
        """生成一次性确认令牌

        Args:
            project_id: 项目ID
            intent: 确认的意图
            text: 原始用户输入文本

        Returns:
            str: 令牌字符串
        """
        token = f"confirm-{uuid.uuid4().hex[:24]}"
        text_hash = self._hash_text(text)

        record = TokenRecord(
            token=token,
            project_id=project_id,
            intent=intent,
            text_hash=text_hash,
        )

        with self._lock:
            # 清理过期令牌
            self._cleanup_expired()
            self._tokens[token] = record

        return token

    def validate_and_consume(
        self,
        token: str,
        project_id: int,
        intent: str,
        text: str,
    ) -> Dict[str, Any]:
        """验证并消耗一次性令牌

        成功消耗后令牌立即失效。

        Returns:
            dict: {"valid": bool, "code": str, "message": str}
        """
        with self._lock:
            record = self._tokens.get(token)

            # 令牌不存在
            if record is None:
                return {
                    "valid": False,
                    "code": "INVALID_TOKEN",
                    "message": "确认令牌无效或不存在",
                }

            # 令牌已使用
            if record.used:
                return {
                    "valid": False,
                    "code": "TOKEN_ALREADY_USED",
                    "message": "确认令牌已被使用，每次确认需要新的令牌",
                }

            # 令牌过期（先检查过期再清理，保证能返回TOKEN_EXPIRED而非INVALID_TOKEN）
            if time.time() - record.created_at > self.TTL_SECONDS:
                del self._tokens[token]
                return {
                    "valid": False,
                    "code": "TOKEN_EXPIRED",
                    "message": f"确认令牌已过期（有效期{self.TTL_SECONDS}秒），请重新解析指令",
                }

            # 清理其他过期令牌
            self._cleanup_expired()

            # 验证绑定信息
            if record.project_id != project_id:
                return {
                    "valid": False,
                    "code": "TOKEN_PROJECT_MISMATCH",
                    "message": f"令牌绑定的项目({record.project_id})与请求项目({project_id})不匹配",
                }

            if record.intent != intent:
                return {
                    "valid": False,
                    "code": "TOKEN_INTENT_MISMATCH",
                    "message": f"令牌绑定的意图({record.intent})与请求意图({intent})不匹配",
                }

            text_hash = self._hash_text(text)
            if record.text_hash != text_hash:
                return {
                    "valid": False,
                    "code": "TOKEN_TEXT_MISMATCH",
                    "message": "令牌绑定的文本与请求文本不匹配，请重新解析指令",
                }

            # 消耗令牌
            record.used = True
            return {
                "valid": True,
                "code": "TOKEN_VALID",
                "message": "令牌验证通过",
            }

    def revoke(self, token: str):
        """撤销令牌（用户取消时调用）"""
        with self._lock:
            if token in self._tokens:
                del self._tokens[token]

    def _cleanup_expired(self):
        """清理过期令牌"""
        now = time.time()
        expired = [
            t for t, r in self._tokens.items()
            if now - r.created_at > self.TTL_SECONDS
        ]
        for t in expired:
            del self._tokens[t]

    def get_active_count(self) -> int:
        """获取当前有效令牌数（用于监控）"""
        with self._lock:
            self._cleanup_expired()
            return len([r for r in self._tokens.values() if not r.used])


# 全局单例
_token_manager: Optional[ConfirmationTokenManager] = None
_token_lock = threading.Lock()


def get_token_manager() -> ConfirmationTokenManager:
    """获取全局令牌管理器单例"""
    global _token_manager
    if _token_manager is None:
        with _token_lock:
            if _token_manager is None:
                _token_manager = ConfirmationTokenManager()
    return _token_manager
