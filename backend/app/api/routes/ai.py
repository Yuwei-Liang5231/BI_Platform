"""AI 能力路由（P1：看板速览 / 异动假设 / 口径助手）。

全部接口走算写分离 + 降级：LLM 未配置或失败 → 返回规则兜底 / 空字段，
绝不 500、绝不自产数字。响应统一经 ok_response 包装。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.core.response import ok_response
from app.domain.ai import (
    anomaly_hypothesis,
    attribute_interpretation,
    calc_notes,
    dashboard_summary,
    metric_linkage,
    semantic_annotations,
)
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


# ---------------------------------------------------------------- 字段语义标注（P2 #4）


@router.post("/dataset-semantic-annotations")
def post_semantic_annotations(
    db: DbDep,
    user: CurrentUser,
    payload: SemanticAnnotationsRequest,
):
    """生成字段语义标注建议（键白名单防幻觉；无 LLM → 空标注）。"""
    data = semantic_annotations.suggest_annotations(db, payload.dataset_id)
    return ok_response(data)


class SemanticAnnotationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: int


class SemanticSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotations: dict[str, str]


# ---------------------------------------------------------------- 归因下钻 AI 解读（P2 #6）


class AttributeInterpretationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: int | str
    start: str
    end: str
    dimensions: list[str]
    compare: str = "mom"


@router.post("/attribute-interpretation")
def post_attribute_interpretation(
    db: DbDep,
    user: CurrentUser,
    payload: AttributeInterpretationRequest,
):
    """归因根节点一句话 AI 解读（权限/口径同源 attribute-tree；降级 → null）。"""
    data = attribute_interpretation.build_interpretation(
        db, user,
        metric_id=payload.metric_id, start=payload.start, end=payload.end,
        dimensions=payload.dimensions, compare=payload.compare,
    )
    return ok_response(data)


# ---------------------------------------------------------------- 多指标联动归因（A4/P7）


@router.get("/metric-linkage")
def get_metric_linkage(
    db: DbDep,
    user: CurrentUser,
    metric_id: int = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    top_n: int = Query(3, ge=1, le=5),
):
    """同期联动指标分析：同项目内与本指标相关性最高的 TOP N（权限同源，
    受限候选剔除；r 为统计量按 2 位小数展示；AI 传播假设算写分离、可降级）。"""
    data = metric_linkage.build_metric_linkage(
        db, user, metric_id=metric_id, start=start, end=end, top_n=top_n
    )
    return ok_response(data)


# ---------------------------------------------------------------- AI 反馈闭环（P4-2）


_AI_KINDS = (
    "dashboard_summary",
    "anomaly_hypothesis",
    "attribute_interpretation",
    "calc_notes",
    "semantic_annotations",
    "ask",
    "daily_insight",
    "metric_linkage",
)


class AiFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    target: str = ""
    rating: str          # up / down
    correction: str = ""
    project_id: int | None = None


@router.post("/feedback")
def post_ai_feedback(db: DbDep, user: CurrentUser, payload: AiFeedbackIn):
    """记录一次 AI 输出反馈（有用/无用 + 可选人工修正）。

    零阻塞：反馈失败不影响任何 AI 主流程；kind 白名单防脏数据。
    """
    from app.infra.models import AiFeedback

    kind = (payload.kind or "").strip()
    rating = (payload.rating or "").strip().lower()
    if kind not in _AI_KINDS:
        return ok_response({"recorded": False, "reason": "unknown_kind"})
    if rating not in ("up", "down"):
        return ok_response({"recorded": False, "reason": "bad_rating"})

    row = AiFeedback(
        user_id=user.id,
        project_id=payload.project_id,
        kind=kind,
        target=(payload.target or "")[:200],
        rating=rating,
        correction=(payload.correction or "")[:2000],
    )
    db.add(row)
    db.commit()
    return ok_response({"recorded": True, "id": row.id})


@router.get("/feedback/summary")
def get_ai_feedback_summary(db: DbDep, _admin: AdminUser, project_id: int | None = None):
    """AI 质量概览（admin）：各功能赞踩、差评率与最近 bad case（按项目过滤）。

    项目归属：入参缺省 → 默认项目；NULL project_id 的存量反馈行视为默认项目。
    """
    from sqlalchemy import func, or_

    from app.domain.project.service import default_project_id, resolve_project_id
    from app.infra.models import AiFeedback, User

    pid = resolve_project_id(db, project_id)
    conds = [AiFeedback.project_id == pid]
    if pid == default_project_id(db):
        # NULL = 未指定项目的存量反馈，归默认项目（与 B9.3「NULL=默认项目」约定一致）
        conds = [or_(AiFeedback.project_id == pid, AiFeedback.project_id.is_(None))]

    rows = (
        db.query(
            AiFeedback.kind,
            AiFeedback.rating,
            func.count(AiFeedback.id),
        )
        .filter(*conds)
        .group_by(AiFeedback.kind, AiFeedback.rating)
        .all()
    )
    stat: dict[str, dict[str, int]] = {}
    for kind, rating, cnt in rows:
        s = stat.setdefault(kind, {"up": 0, "down": 0})
        s[rating] = cnt
    by_kind = [
        {
            "kind": k,
            "up": v["up"],
            "down": v["down"],
            "total": v["up"] + v["down"],
            "down_rate": round(v["down"] / (v["up"] + v["down"]) * 100, 1)
            if (v["up"] + v["down"])
            else 0.0,
        }
        for k, v in sorted(stat.items(), key=lambda kv: -kv[1]["down"])
    ]

    bad = (
        db.query(AiFeedback, User.username)
        .outerjoin(User, User.id == AiFeedback.user_id)
        .filter(AiFeedback.rating == "down", *conds)
        .order_by(AiFeedback.id.desc())
        .limit(20)
        .all()
    )
    recent_bad = [
        {
            "kind": f.kind,
            "target": f.target,
            "correction": f.correction,
            "username": uname or "",
            "created_at": f.created_at,
        }
        for f, uname in bad
    ]
    return ok_response({"by_kind": by_kind, "recent_bad": recent_bad})


# ---------------------------------------------------------------- 每日洞察（P5/A1）


@router.post("/insight/run")
def post_insight_run(db: DbDep, _admin: AdminUser):
    """手动触发一次每日洞察（幂等；调度器每日自动执行，此接口用于验收/补跑）。"""
    from app.domain.insight.service import run_daily_insight

    data = run_daily_insight(db)
    return ok_response(data)


@router.get("/insights")
def get_ai_insights(
    db: DbDep,
    user: CurrentUser,
    project_id: int | None = None,
    limit: int = Query(3, ge=1, le=10),
    days: int = Query(3, ge=1, le=30),
):
    """洞察条数据源（A2 常驻 AI 洞察条）：最近 N 天的每日洞察，按指标去重取最新。

    - 权限同源：受限指标（role+department 登记）的洞察对当前用户隐藏；
    - 软删指标不再展示（join metrics.status == active）；
    - 纯读接口无副作用：空结果由前端决定是否提示 admin 手动生成。
    """
    from datetime import date, timedelta

    from sqlalchemy import exists

    from app.domain.auth.service import build_user_hidden_map
    from app.domain.project.service import resolve_project_id
    from app.infra.models import AiInsight, Metric

    pid = resolve_project_id(db, project_id)
    hidden = build_user_hidden_map(db).get(user.id, set())
    today = date.today()
    since = today - timedelta(days=days - 1)

    rows = (
        db.query(AiInsight, Metric.name)
        .join(Metric, Metric.id == AiInsight.metric_id)
        .filter(
            AiInsight.project_id == pid,
            AiInsight.insight_date >= since,
            Metric.status == "active",
        )
        .order_by(AiInsight.insight_date.desc(), AiInsight.id.desc())
        .all()
    )
    items: list[dict] = []
    seen: set[int] = set()
    for ins, metric_name in rows:
        if ins.metric_id in hidden or ins.metric_id in seen:
            continue
        seen.add(ins.metric_id)
        items.append(
            {
                "id": ins.id,
                "insight_date": ins.insight_date.isoformat(),
                "metric_id": ins.metric_id,
                "metric_code": ins.metric_code,
                "metric_name": metric_name or ins.metric_code,
                "direction": ins.direction,
                "title": ins.title,
                "body": ins.body,
                "source": ins.source,
                "current": ins.current,
                "baseline_mean": ins.baseline_mean,
                "abnormality": ins.abnormality,
            }
        )
        if len(items) >= limit:
            break

    generated_today = bool(
        db.query(exists().where(AiInsight.project_id == pid, AiInsight.insight_date == today)).scalar()
    )
    return ok_response(
        {
            "items": items,
            "generated_today": generated_today,
            "insight_date": today.isoformat(),
        }
    )
