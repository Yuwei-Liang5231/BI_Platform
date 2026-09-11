"""路由级依赖注入（统一入口，路由模块只从这里取）。

B4 认证依赖：
- CurrentUser：必须持有有效 JWT（未登录/过期/无效 → 40100；账号停用 → 40300）；
- AdminUser：当前用户必须是 admin（否则 40300）；
- WriterUser：admin 或 analyst（指标口径维护类写操作）。
"""

from __future__ import annotations

from typing import Annotated

import jwt as pyjwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.response import BusinessError
from app.domain.auth import security
from app.infra.database import get_db
from app.infra.models import User

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]

_bearer_scheme = HTTPBearer(auto_error=False, description="JWT: Bearer <token>")


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbDep,
) -> User:
    if credentials is None or not credentials.credentials:
        raise BusinessError("未登录或缺少凭证", 40100)
    try:
        payload = security.decode_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (pyjwt.PyJWTError, KeyError, ValueError) as exc:
        raise BusinessError("凭证无效或已过期，请重新登录", 40100) from exc
    user = db.get(User, user_id)
    if user is None:
        raise BusinessError("用户不存在或已删除", 40100)
    if user.status != "active":
        raise BusinessError("账号已停用，请联系管理员", 40300)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise BusinessError("需要管理员权限", 40300)
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_writer(user: CurrentUser) -> User:
    if user.role not in ("admin", "analyst"):
        raise BusinessError("需要 analyst 或 admin 权限", 40300)
    return user


WriterUser = Annotated[User, Depends(require_writer)]

__all__ = ["SettingsDep", "DbDep", "CurrentUser", "AdminUser", "WriterUser"]
