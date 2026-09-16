"""单层归因分解（B10-2）：把指标变化量按单一维度拆贡献度。

独立成服务（架构 7.x 扩展）：B10 异动结论与 B12 报告中心共用同一归因出口。

算法（加性守恒）：
- 本期 vs 基期（上一等长周期，与环比口径一致）按维度分组求值；
- 每组贡献 = 本期组值 - 基期组值（新组全额计入、消失组 v1 不计入——
  按"本期存在的组"拆解，守恒偏差在响应中如实给出 conservation 字段）；
- 贡献按绝对值降序取 TopN，剩余合并为「其他」桶——Top 来源 + 其他 = 总变化。

数值纪律：组值走 compute_metric_breakdown 单点出口、总变化走
compute_metric_value 单点出口——本模块零自产数字。
比率类指标不支持（组值是"率"，率的变化不守恒，按组差贡献无业务意义）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.query.service import (
    MAX_BREAKDOWN_TOP_N,
    _resolve_metric,
    compute_metric_breakdown,
    compute_metric_value,
)
from app.infra.models import Metric, User


def attribute_delta(
    db: Session,
    user: User,
    *,
    metric_ref,
    start: str,
    end: str,
    dimension: str,
    compare: str = "mom",
    top_n: int = 10,
) -> dict:
    """单层归因：总变化按维度拆贡献（加性指标）。

    返回 {metric, period, compare, current_total, prev_total, delta,
    dimension, top_dimensions:[{value, current, prev, contribution, contribution_pct}],
    others_contribution, conservation_deviation_pct}。
    """
    metric: Metric = _resolve_metric(db, metric_ref)
    from app.domain.auth.service import ensure_metric_visible

    ensure_metric_visible(db, user, metric)
    _ensure_additive(metric)

    # 总变化走单值出口（与看板完全同口径）
    current = compute_metric_value(
        db, metric_ref=metric.id, start=start, end=end, compare="none", user=user
    )
    from app.domain.query.service import _previous_range

    prev_start, prev_end = _previous_range(
        _parse_iso(start), _parse_iso(end), compare
    )
    prev = compute_metric_value(
        db,
        metric_ref=metric.id,
        start=prev_start.isoformat(),
        end=prev_end.isoformat(),
        compare="none",
        user=user,
    )
    current_total = current["value"]
    prev_total = prev["value"]
    if current_total is None or prev_total is None:
        raise BusinessError("本期或基期无数据，无法归因（请调整区间至数据覆盖范围内）", 40000)
    total_delta = current_total - prev_total

    # 组值走拆解出口（top_n 拉满：归因需要全量组，TopN 截断由本服务负责）
    breakdown = compute_metric_breakdown(
        db,
        metric_ref=metric.id,
        start=start,
        end=end,
        compare=compare,
        dimension=dimension,
        top_n=MAX_BREAKDOWN_TOP_N,
        user=user,
    )
    contributions = []
    for row in breakdown["rows"]:
        contribution = (row["value"] or 0) - (row["prev_value"] or 0)
        contributions.append(
            {
                "value": row["dimension"],
                "current": row["value"],
                "prev": row["prev_value"],
                "contribution": contribution,
            }
        )
    contributions.sort(key=lambda c: -abs(c["contribution"]))

    picked = contributions[:top_n]
    others = contributions[top_n:]
    others_contribution = sum(c["contribution"] for c in others)
    denom = total_delta if total_delta != 0 else None
    for c in picked:
        c["contribution_pct"] = (
            round(c["contribution"] / total_delta * 100, 4) if denom is not None else None
        )
    if others:
        picked.append(
            {
                "value": "（其他）",
                "current": None,
                "prev": None,
                "contribution": others_contribution,
                "contribution_pct": (
                    round(others_contribution / total_delta * 100, 4)
                    if denom is not None
                    else None
                ),
            }
        )

    # 守恒校验（信息性）：各组贡献合计 vs 单值口径总变化
    explained = sum(c["contribution"] for c in contributions)
    deviation = (
        round((explained - total_delta) / abs(total_delta) * 100, 4)
        if total_delta
        else None
    )
    return {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "name": metric.name,
        "dimension": dimension,
        "compare": compare,
        "start": start,
        "end": end,
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
        "current_total": current_total,
        "prev_total": prev_total,
        "delta": total_delta,
        "top_dimensions": picked,
        "explained_delta": explained,
        "conservation_deviation_pct": deviation,
    }


def _ensure_additive(metric: Metric) -> None:
    """比率类（expression 形态）不支持单层归因：组值是率，率差不守恒。"""
    import json

    calc = json.loads(metric.calc_rule_json or "{}")
    if "expression" in calc or "operands" in calc:
        raise BusinessError(
            f"指标 {metric.code} 为比率类指标：率的变化不具可加性，单层归因仅支持加性指标",
            40000,
        )


def _parse_iso(raw: str):
    from datetime import date

    return date.fromisoformat(str(raw).strip())
