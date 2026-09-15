"""LLM 模型管理路由（阶段 2）：多模型登记 / 切换启用 / 连通性测试。

权限：全部管理员专用（D12 同数据集管理口径）；普通用户不需要感知模型配置。
配置优先级：is_active=1 的数据库记录 > env 兜底（见 infra/llm.resolve_llm_config）。
api_key 只在创建时写入、编辑时空值表示保持不变；列表/详情一律脱敏返回。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, DbDep, SettingsDep
from app.core.response import BusinessError, ok_response
from app.infra.llm import mask_llm_config, resolve_llm_config, test_connection
from app.infra.models import LlmModel
from app.infra.repository import Repository

logger = logging.getLogger("app.api.llm")

router = APIRouter(prefix="/llm", tags=["llm"])


class LlmModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=300)
    model: str = Field(min_length=1, max_length=200)
    remark: str = Field(default="", max_length=300)
    activate: bool = False  # 创建后是否立即启用


class LlmModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=300)  # 空/缺省 = 保持原值
    model: str | None = Field(default=None, min_length=1, max_length=200)
    remark: str | None = Field(default=None, max_length=300)


class LlmTestIn(BaseModel):
    """连通性测试入参：传字段 = 测试未保存的表单值；传 model_id = 测试已存记录。"""

    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)
    model_id: int | None = None


def _to_dict(m: LlmModel) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "base_url": m.base_url,
        "api_key_masked": mask_llm_config({"api_key": m.api_key})["api_key"],
        "model": m.model,
        "is_active": bool(m.is_active),
        "remark": m.remark,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _get_model(db, model_id: int) -> LlmModel:
    m = Repository(LlmModel, db).get(model_id)
    if m is None:
        raise BusinessError(f"模型 {model_id} 不存在", 40400)
    return m


def _check_base_url(base_url: str) -> None:
    if not base_url.strip().startswith(("http://", "https://")):
        raise BusinessError("base_url 必须以 http:// 或 https:// 开头", 40000)


# ---------------------------------------------------------------- 查询


@router.get("/models")
def list_models(db: DbDep, settings: SettingsDep, user: AdminUser):
    """模型列表 + 当前生效配置来源（admin）。"""
    _ = user
    rows = Repository(LlmModel, db).list(order_by=LlmModel.id)
    effective = mask_llm_config(resolve_llm_config(db, settings))
    return ok_response({"models": [_to_dict(m) for m in rows], "effective": effective})


# ---------------------------------------------------------------- 写操作


@router.post("/models")
def create_model(body: LlmModelIn, db: DbDep, user: AdminUser):
    """登记新模型（admin）。activate=true 时启用并互斥掉其他记录。"""
    _ = user
    repo = Repository(LlmModel, db)
    _check_base_url(body.base_url)
    if repo.count(name=body.name.strip()):
        raise BusinessError(f"模型名称「{body.name.strip()}」已存在", 40000)
    m = repo.add(
        LlmModel(
            name=body.name.strip(),
            base_url=body.base_url.strip(),
            api_key=body.api_key.strip(),
            model=body.model.strip(),
            remark=body.remark.strip(),
            is_active=0,
        )
    )
    if body.activate:
        _activate(db, m.id)
    db.commit()
    return ok_response(_to_dict(_get_model(db, m.id)))


@router.put("/models/{model_id}")
def update_model(model_id: int, body: LlmModelUpdate, db: DbDep, user: AdminUser):
    """编辑模型（admin）。api_key 留空 = 保持原值。"""
    _ = user
    m = _get_model(db, model_id)
    repo = Repository(LlmModel, db)
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in fields:
        name = fields["name"].strip()
        dup = db.query(LlmModel).filter(LlmModel.name == name, LlmModel.id != model_id).first()
        if dup:
            raise BusinessError(f"模型名称「{name}」已存在", 40000)
        fields["name"] = name
    if "base_url" in fields:
        _check_base_url(fields["base_url"])
        fields["base_url"] = fields["base_url"].strip()
    if "api_key" in fields and not fields["api_key"].strip():
        fields.pop("api_key")  # 空值 = 不修改凭据
    for key in ("model", "remark"):
        if key in fields:
            fields[key] = fields[key].strip()
    repo.update(m, **fields)
    db.commit()
    return ok_response(_to_dict(_get_model(db, model_id)))


@router.delete("/models/{model_id}")
def delete_model(model_id: int, db: DbDep, user: AdminUser):
    """删除模型（admin）。删除启用中的记录后自动回退 env 兜底配置。"""
    _ = user
    m = _get_model(db, model_id)
    was_active = bool(m.is_active)
    Repository(LlmModel, db).delete(model_id)
    db.commit()
    return ok_response({"deleted": model_id, "was_active": was_active})


@router.post("/models/{model_id}/activate")
def activate_model(model_id: int, db: DbDep, user: AdminUser):
    """切换启用（admin）：全局唯一启用一条，事务内互斥。"""
    _ = user
    _get_model(db, model_id)
    _activate(db, model_id)
    db.commit()
    return ok_response(_to_dict(_get_model(db, model_id)))


def _activate(db, model_id: int) -> None:
    db.query(LlmModel).filter(LlmModel.is_active == 1).update({LlmModel.is_active: 0})
    db.query(LlmModel).filter(LlmModel.id == model_id).update({LlmModel.is_active: 1})


# ---------------------------------------------------------------- 连通性测试


@router.post("/models/test")
def test_model(body: LlmTestIn, db: DbDep, settings: SettingsDep, user: AdminUser):
    """连通性测试（admin）。model_id 优先（测试已存记录），否则测表单值。

    SSL 校验策略与运行时一致（全局 settings：企业内网自签证书可关闭或指定 CA 包）。
    """
    _ = user
    verify = {"verify_ssl": settings.llm_verify_ssl, "ca_bundle": settings.llm_ca_bundle}
    if body.model_id is not None:
        m = _get_model(db, body.model_id)
        config = {"base_url": m.base_url, "api_key": m.api_key, "model": m.model, **verify}
    else:
        config = {"base_url": body.base_url, "api_key": body.api_key, "model": body.model, **verify}
    ok, message = test_connection(config)
    return ok_response({"ok": ok, "message": message})
