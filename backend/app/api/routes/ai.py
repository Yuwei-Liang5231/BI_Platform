"""AI 能力路由（P1：看板速览 / 异动假设 / 口径助手）。

全部接口走算写分离 + 降级：LLM 未配置或失败 → 返回规则兜底 / 空字段，
绝不 500、绝不自产数字。响应统一经 ok_response 包装。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.ai import anomaly_hypothesis, calc_notes, dashboard_summary
from app.domain.project.service import resolve_project_id

router = APIRouter(prefix="/ai", tags=["ai"])


# ---------------------------------------------------------------- 看板速览


@router.get("/dashboard-summary")
def get_dashboard_summary(
    db: DbDep,
    user: CurrentUser,
    project_id: int | None = None,
    start: str = Query(...),
    end: str = Query(...),
    metric_ids: list[int] | None = Query(None),
):
    pid = resolve_project_id(db, project_id)
    data = dashboard_summary.build_dashboard_summary(
        db, user, pid, start, end, metric_ids
    )
    return ok_response(data)


# ---------------------------------------------------------------- 异动假设


@router.get("/anomaly-hypothesis")
def get_anomaly_hypothesis(
    db: DbDep,
    user: CurrentUser,
    metric_id: int = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    compare: str = "mom",
    dimensions: list[str] | None = Query(None),
):
    data = anomaly_hypothesis.build_anomaly_hypothesis(
        db, user, metric_id, start, end, compare=compare, dimensions=dimensions
    )
    return ok_response(data)


# ---------------------------------------------------------------- 口径助手


class CalcNotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: int
    column: str
    aggregation: str
    alias: str | None = None
    sample_values: list[Any] | None = None


@router.post("/metric-calc-notes")
def post_metric_calc_notes(
    payload: CalcNotesRequest,
    db: DbDep,
    user: CurrentUser,
):
    data = calc_notes.suggest_calc_notes(
        db,
        dataset_id=payload.dataset_id,
        column=payload.column,
        aggregation=payload.aggregation,
        alias=payload.alias,
        sample_values=payload.sample_values,
    )
    return ok_response(data)
