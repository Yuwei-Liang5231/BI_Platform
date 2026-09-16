"""模板库 API（B5）。

- GET  /api/templates/industries        行业列表（含指标数）
- GET  /api/templates/{industry}        行业明细（含库内导入状态）
- POST /api/templates/import            勾选批量导入（幂等；analyst/admin）

读接口需登录；导入属指标创建类写操作 → WriterUser（与指标 CRUD 一致）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, DbDep, WriterUser
from app.core.response import ok_response
from app.domain.templates import loader, service

router = APIRouter(prefix="/templates", tags=["templates"])


class TemplateImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    industries: list[str] | None = None   # 缺省 = 全部行业
    codes: list[str] | None = None        # 缺省 = 全部指标
    revalidate: bool = False              # true 时允许把 pending 指标升级为 active
    project_id: int | None = None         # B9.3：导入指标归属项目（缺省=默认项目）


@router.get("/industries")
def list_industries(_: CurrentUser):
    return ok_response(loader.list_packs())


@router.get("/{industry}")
def pack_detail(industry: str, db: DbDep, _: CurrentUser, project_id: int | None = None):
    """行业明细。B9.3：project_id 指定时导入状态按项目判定（向导按当前项目展示）。"""
    return ok_response(service.pack_detail(db, industry, project_id=project_id))


@router.post("/import")
def import_templates(body: TemplateImportRequest, db: DbDep, operator: WriterUser):
    result = service.import_packs(
        db,
        industries=body.industries,
        codes=body.codes,
        revalidate=body.revalidate,
        operator=operator,
        project_id=body.project_id,
    )
    return ok_response(result, message="模板导入完成")
