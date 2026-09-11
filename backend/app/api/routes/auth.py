"""auth 路由：登录、当前用户、用户管理（admin）、指标可见性登记（admin）。

登录接口公开；其余全部需要登录态。401/403 与 body code 对齐（D11）由
deps + BusinessError 统一保证。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.auth import service

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "viewer"
    department: str = ""


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    department: str | None = None
    status: str | None = None
    password: str | None = None


class RestrictionsPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict]


@router.post("/login")
def login(db: DbDep, body: LoginRequest):
    """登录：返回 JWT 与用户信息。前端存 token，后续请求带 Authorization: Bearer <token>。"""
    user, token = service.authenticate(db, body.username, body.password)
    user.last_login_at = datetime.now()
    db.commit()
    return ok_response(
        {"token": token, "token_type": "bearer", "user": service.user_to_dict(user)},
        message="登录成功",
    )


@router.get("/me")
def me(user: CurrentUser):
    return ok_response(service.user_to_dict(user))


@router.get("/users")
def list_users(db: DbDep, _: AdminUser):
    from app.infra.models import User

    users = db.query(User).order_by(User.id).all()
    return ok_response([service.user_to_dict(u) for u in users])


@router.post("/users")
def create_user(db: DbDep, body: UserCreateRequest, operator: AdminUser):
    user = service.create_user(
        db,
        username=body.username,
        password=body.password,
        role=body.role,
        department=body.department,
    )
    _ = operator
    return ok_response(service.user_to_dict(user), message="用户已创建")


@router.patch("/users/{user_id}")
def update_user(db: DbDep, user_id: int, body: UserUpdateRequest, operator: AdminUser):
    changes = body.model_dump(exclude_none=True)
    user = service.update_user(db, user_id, changes, operator=operator)
    return ok_response(service.user_to_dict(user), message="用户已更新")


# ---------------------------------------------------------------- 可见性登记


@router.get("/metrics/{metric_id}/restrictions")
def get_restrictions(db: DbDep, metric_id: int, _: AdminUser):
    from app.domain.metric import service as metric_service

    metric_service.get_metric(db, metric_id)  # 不存在 → 404
    return ok_response(service.list_restrictions(db, metric_id))


@router.put("/metrics/{metric_id}/restrictions")
def put_restrictions(db: DbDep, metric_id: int, body: RestrictionsPutRequest, operator: AdminUser):
    """整组替换可见性登记（items=[] 即清空、恢复全员可见）。"""
    from app.domain.metric import service as metric_service

    metric = metric_service.get_metric(db, metric_id)
    items = service.replace_restrictions(db, metric, body.items, operator=operator)
    return ok_response(items, message="可见性登记已更新")
