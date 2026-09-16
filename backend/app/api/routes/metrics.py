"""指标路由：CRUD、别名搜索、编译试算、存档 SQL、变更历史。

B4 权限：写操作需 analyst/admin；目录与详情按可见性登记过滤（不变式 2，
与 query 数值出口同一套判定）；operator 缺省取当前登录用户。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, DbDep, WriterUser
from app.core.response import BusinessError, ok_response
from app.domain.auth.service import ensure_metric_visible, filter_visible_metrics
from app.domain.metric import service
from app.infra.models import Metric

logger = logging.getLogger("app.api.metrics")

router = APIRouter(prefix="/metrics", tags=["metrics"])


class MetricCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=99)
    name: str = Field(min_length=1, max_length=200)
    calc_rule: dict[str, Any]
    aliases: list[str] | None = None
    definition: str = ""
    dimensions: list | None = None
    filters: dict | None = None
    topic: str = "general"
    parent_id: int | None = None
    disambiguation: dict | None = None
    owner_department: str = ""
    project_id: int | None = None  # B9.3：归属项目（缺省=默认项目；code 项目内唯一）
    operator_id: str | None = None  # 缺省取当前登录用户


class MetricUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    aliases: list[str] | None = None
    definition: str | None = None
    calc_rule: dict[str, Any] | None = None
    dimensions: list | None = None
    filters: dict | None = None
    topic: str | None = None
    parent_id: int | None = None
    disambiguation: dict | None = None
    owner_department: str | None = None
    status: str | None = None
    reason: str = ""
    operator_id: str | None = None  # 缺省取当前登录用户


class CompileTryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calc_rule: dict[str, Any]
    time_field: str | None = None


def _changes_to_update_payload(body: MetricUpdateRequest) -> dict:
    payload = body.model_dump(exclude_none=True, exclude={"reason", "operator_id"})
    if body.calc_rule is not None:
        payload["calc_rule"] = body.calc_rule  # None 值字段被排除，但显式保留 calc_rule 判断
    return payload


@router.post("")
def create_metric(db: DbDep, body: MetricCreateRequest, user: WriterUser):
    """创建指标：calc_rule 编译不过（超纲算子/缺表关系/列不存在）保存即拒绝。"""
    metric = service.create_metric(
        db,
        code=body.code,
        name=body.name,
        calc_rule=body.calc_rule,
        aliases=body.aliases,
        definition=body.definition,
        dimensions=body.dimensions,
        filters=body.filters,
        topic=body.topic,
        parent_id=body.parent_id,
        disambiguation=body.disambiguation,
        owner_department=body.owner_department,
        operator_id=body.operator_id or user.username,
        project_id=body.project_id,
    )
    return ok_response(service.metric_to_dict(metric), message="指标已创建")


@router.get("")
def list_metrics(
    db: DbDep,
    user: CurrentUser,
    search: str | None = None,
    topic: str | None = None,
    status: str = "active",
    project_id: int | None = None,
):
    """指标目录：按名称/别名/code 模糊搜索；status=active|disabled|all。

    可见性：按当前用户过滤受限指标（不变式 2，与 query 出口同一套判定）。
    B9.3：project_id 缺省=None=全部项目（跨项目浏览）；指定 id 仅返回该项目。
    """
    metrics = service.list_metrics(db, search=search, topic=topic, status=status, project_id=project_id)
    metrics = filter_visible_metrics(db, user, metrics)
    return ok_response([service.metric_to_dict(m) for m in metrics])


@router.get("/{metric_id}")
def get_metric(db: DbDep, metric_id: int, user: CurrentUser):
    metric = service.get_metric(db, metric_id)
    ensure_metric_visible(db, user, metric)
    return ok_response(service.metric_to_dict(metric))


@router.patch("/{metric_id}")
def update_metric(db: DbDep, metric_id: int, body: MetricUpdateRequest, user: WriterUser):
    """部分更新。calc_rule 变更时重编译、ver+1、写存档与留痕（必须给 reason）。"""
    payload = _changes_to_update_payload(body)
    if not payload:
        raise BusinessError("没有提供任何要修改的字段", 40000)
    if payload.get("status") == "deleted":
        raise BusinessError("删除请用 DELETE 接口", 40000)
    metric = service.update_metric(
        db,
        metric_id,
        payload,
        reason=body.reason,
        operator_id=body.operator_id or user.username,
    )
    return ok_response(service.metric_to_dict(metric), message="指标已更新")


@router.delete("/{metric_id}")
def delete_metric(
    db: DbDep, metric_id: int, user: WriterUser, reason: str = "", operator_id: str | None = None
):
    """软删除（status=deleted），目录与搜索不可见，留痕保留。"""
    return ok_response(
        service.delete_metric(
            db, metric_id, reason=reason, operator_id=operator_id or user.username
        ),
        message="指标已删除",
    )


@router.get("/{metric_id}/sql")
def get_metric_sql(db: DbDep, metric_id: int, user: CurrentUser):
    """当前版本的编译存档 SQL（详情页折叠区展示，与实际执行一致）。"""
    ensure_metric_visible(db, user, service.get_metric(db, metric_id))
    return ok_response(service.get_current_sql(db, metric_id))


@router.get("/{metric_id}/changes")
def get_metric_changes(db: DbDep, metric_id: int, user: CurrentUser):
    """口径变更历史（前后快照 + 原因 + 操作人）。"""
    ensure_metric_visible(db, user, service.get_metric(db, metric_id))
    return ok_response(service.get_changes(db, metric_id))


# ---------------------------------------------------------------- 异动检测配置（B10-1）


class AnomalyConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    z_threshold: float | None = None      # 1.0~10.0，缺省 3.0（保守档）
    min_samples: int | None = None        # 同星期几基准样本下限，缺省 8
    materiality_pct: float | None = None  # B10-2 要紧度：|变化率|% 低于此不构成结论，缺省 5.0
    enabled: bool | None = None           # false = 不参与批量异动扫描


@router.get("/{metric_id}/anomaly-config")
def get_anomaly_config(db: DbDep, metric_id: int, user: CurrentUser):
    """指标级异动检测配置（无记录返回默认保守档）。"""
    from app.domain.anomaly import service as anomaly_service

    ensure_metric_visible(db, user, service.get_metric(db, metric_id))
    return ok_response(anomaly_service.get_effective_config(db, metric_id))


@router.put("/{metric_id}/anomaly-config")
def put_anomaly_config(db: DbDep, metric_id: int, body: AnomalyConfigRequest, user: WriterUser):
    """创建/更新指标级异动检测配置（仅 analyst/admin）。"""
    from app.domain.anomaly import service as anomaly_service

    metric = service.get_metric(db, metric_id)
    cfg = anomaly_service.upsert_config(
        db,
        metric,
        z_threshold=body.z_threshold,
        min_samples=body.min_samples,
        materiality_pct=body.materiality_pct,
        enabled=body.enabled,
    )
    return ok_response(
        {
            "metric_id": metric_id,
            "z_threshold": cfg.z_threshold,
            "min_samples": cfg.min_samples,
            "materiality_pct": cfg.materiality_pct,
            "enabled": bool(cfg.enabled),
        },
        message="异动检测配置已保存",
    )


@router.post("/compile")
def compile_try(db: DbDep, body: CompileTryRequest, _: CurrentUser):
    """试编译：不落库，返回参数化 SQL 与周期元信息（Swagger 调试/前端预览用）。"""
    from app.domain.metric.schema import CalcRuleError

    try:
        compiled = service.compile_for_db(db, body.calc_rule)
    except CalcRuleError as exc:  # 防御：service 已转 BusinessError，这里兜底
        raise BusinessError(str(exc), 40000) from exc
    if body.time_field and body.time_field != compiled.time_fields.get(compiled.primary_dataset):
        compiled = service.compile_for_db(db, {**body.calc_rule, "time_field": body.time_field})
    return ok_response(
        {
            "sql": compiled.sql,
            "primary_dataset": compiled.primary_dataset,
            "dataset_names": compiled.dataset_names,
            "time_fields": compiled.time_fields,
            "coverage_start": compiled.coverage_start,
            "coverage_end": compiled.coverage_end,
            "grain": compiled.grain,
            "used_relations": compiled.used_relations,
        }
    )
