"""安全原语：口令哈希（pbkdf2_hmac，纯标准库）与 JWT 签发/校验（PyJWT）。

- 口令：pbkdf2_sha256，随机 16 字节盐，格式 `pbkdf2_sha256$iter$salt$hash`；
- JWT：HS256，payload 携带 sub/username/role，exp 由 settings.jwt_expire_minutes 控制；
  签名密钥来自 settings.secret_key（pro 环境必须覆盖默认占位值）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from app.core.config import get_settings

PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。格式不合法一律返回 False（不抛异常，防构造数据攻击面）。"""
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def create_access_token(*, user_id: int, username: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码并校验 JWT。无效/过期抛 pyjwt.PyJWTError，由调用方转 401。"""
    settings = get_settings()
    return pyjwt.decode(
        token, settings.secret_key, algorithms=[settings.jwt_algorithm]
    )
