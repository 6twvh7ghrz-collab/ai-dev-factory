"""安全工具：加密解密 API Key"""
import base64
from cryptography.fernet import Fernet
from app.core.config import settings


def _get_fernet() -> Fernet:
    """根据配置密钥生成 Fernet 实例"""
    key = settings.ENCRYPTION_KEY.encode()
    # 确保密钥长度为32字节，然后base64编码为Fernet所需格式
    padded = key.ljust(32, b"0")[:32]
    return Fernet(base64.urlsafe_b64encode(padded))


def encrypt_value(plain_text: str) -> str:
    """加密明文"""
    if not plain_text:
        return ""
    f = _get_fernet()
    return f.encrypt(plain_text.encode()).decode()


def decrypt_value(cipher_text: str) -> str:
    """解密密文"""
    if not cipher_text:
        return ""
    f = _get_fernet()
    return f.decrypt(cipher_text.encode()).decode()


def mask_key(key: str) -> str:
    """遮盖 API Key，只显示前后4位"""
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
