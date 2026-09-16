"""报告中心路由（B12-1）：模板 CRUD + 报告预览（无 LLM 完整可用版）。

数字纪律：预览返回的 narrative/conclusions/refs 全部来自平台计算出口；
B12-2 的 LLM 叙述层只允许引用 refs，禁止自产数字。
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.project.service import resolve_project_id
from app.domain.report import service as report_service

router = APIRouter(prefix="/reports", tags=["reports"])


class TemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    period_type: str  # daily / weekly / monthly
    metric_ids: list[int] = Field(min_length=1)
    sections: dict[str, Any] | None = None
    project_id: int | None = None  # 缺省=当前项目上下文（query 传参）


class TemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    period_type: str | None = None
    metric_ids: list[int] | None = None
    sections: dict[str, Any] | None = None


class ReportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: int | None = None       # 与临时参数二选一
    period_type: str | None = None
    metric_ids: list[int] | None = None
    sections: dict[str, Any] | None = None
    as_of: str | None = None             # YYYY-MM-DD，缺省=今天锚点
    project_id: int | None = None


@router.get("/templates")
def list_templates(db: DbDep, user: CurrentUser, project_id: int | None = None):
    pid = resolve_project_id(db, project_id)
    return ok_response(
        [report_service.template_to_dict(t) for t in report_service.list_templates(db, pid)]
    )


@router.post("/templates")
def create_template(db: DbDep, body: TemplateCreateRequest, user: CurrentUser):
    pid = resolve_project_id(db, body.project_id)
    t = report_service.create_template(
        db,
        project_id=pid,
        name=body.name,
        period_type=body.period_type,
        metric_ids=body.metric_ids,
        sections=body.sections,
        created_by=user.username,
    )
    return ok_response(report_service.template_to_dict(t), message="报告模板已创建")


@router.put("/templates/{template_id}")
def update_template(db: DbDep, template_id: int, body: TemplateUpdateRequest, _: CurrentUser):
    t = report_service.get_template(db, template_id)
    t = report_service.update_template(db, t, body=body.model_dump(exclude_unset=True))
    return ok_response(report_service.template_to_dict(t), message="报告模板已更新")


@router.delete("/templates/{template_id}")
def delete_template(db: DbDep, template_id: int, _: AdminUser):
    report_service.delete_template(db, report_service.get_template(db, template_id))
    return ok_response(None, message="报告模板已删除")


@router.post("/preview")
def preview_report(db: DbDep, body: ReportPreviewRequest, user: CurrentUser):
    """生成报告预览（无 LLM 完整可用版）：结论集 + 规则化叙述 + refs 对账表。"""
    return ok_response(
        report_service.generate_report(
            db,
            user,
            template_id=body.template_id,
            period_type=body.period_type,
            metric_ids=body.metric_ids,
            sections=body.sections,
            as_of=body.as_of,
            project_id=body.project_id,
        )
    )
