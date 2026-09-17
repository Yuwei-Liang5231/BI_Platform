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


# ---------------------------------------------------------------- 多层归因（B14）

MAX_TREE_DEPTH = 4


def attribute_tree_node(
    db: Session,
    user: User,
    *,
    metric_ref,
    start: str,
    end: str,
    dimensions: list[str],
    path: list[dict] | None = None,
    compare: str = "mom",
    top_n: int = 10,
) -> dict:
    """逐层下钻归因（B14）：返回指定节点的子层贡献拆解。

    - dimensions：有序维度层级（如 品类→区域→渠道），2~4 层、不重复；
    - path：已下钻路径 [{dimension, value}, ...]，必须与 dimensions 严格前缀对齐；
    - 根节点（path 空）总值走 compute_metric_value 单点出口；
    - 子层贡献：compute_metric_breakdown（top_n 拉满）叠加 path 请求级过滤——
      完全复用既有编译/权限/周期完整性契约/缓存，零改动 query 服务；
    - 守恒：Σ子贡献 = 节点 delta（组数超 MAX_BREAKDOWN_TOP_N 截断时偏差如实上报）。
    """
    metric: Metric = _resolve_metric(db, metric_ref)
    from app.domain.auth.service import ensure_metric_visible

    ensure_metric_visible(db, user, metric)
    _ensure_additive(metric)

    dims = [str(d or "").strip() for d in (dimensions or [])]
    dims = [d for d in dims if d]
    if len(dims) < 2:
        raise BusinessError("dimensions（层级维度）至少 2 层——单层归因请用 /query/attribute", 40000)
    if len(dims) > MAX_TREE_DEPTH:
        raise BusinessError(f"dimensions 最多 {MAX_TREE_DEPTH} 层", 40000)
    if len(set(dims)) != len(dims):
        raise BusinessError("dimensions 内存在重复维度", 40000)

    norm_path: list[dict] = []
    for i, step in enumerate(path or []):
        if not isinstance(step, dict) or not str(step.get("value") or "").strip():
            raise BusinessError(f"path[{i}] 须为 {{dimension, value}} 且 value 非空", 40000)
        dim = str(step.get("dimension") or "").strip()
        if dim != dims[i]:
            raise BusinessError(
                f"path[{i}].dimension={dim!r} 与层级第 {i + 1} 层 {dims[i]!r} 不一致（须严格前缀对齐）",
                40000,
            )
        norm_path.append({"dimension": dim, "value": str(step["value"]).strip()})
    if len(norm_path) >= len(dims):
        raise BusinessError("已到叶子层：path 深度须小于层级数（叶子无下一维可拆）", 40000)

    if compare not in ("mom", "yoy"):
        raise BusinessError("归因基期 compare 仅支持 mom / yoy", 40000)

    # 节点过滤条件（父路径 → breakdown 请求级 filters）
    conds = [
        {"column": s["dimension"], "op": "=", "value": s["value"]} for s in norm_path
    ]

    from app.domain.query.service import _previous_range

    prev_start, prev_end = _previous_range(_parse_iso(start), _parse_iso(end), compare)

    # 下一层（子层）维度 = path 深度；节点 current/prev = 子层拆解合计
    # （compute_metric_value 不支持请求级过滤；根节点用单值出口保证精确）
    child_dim = dims[len(norm_path)]
    child_rows = compute_metric_breakdown(
        db,
        metric_ref=metric.id,
        start=start,
        end=end,
        compare=compare,
        dimension=child_dim,
        filters=conds or None,
        top_n=MAX_BREAKDOWN_TOP_N,
        user=user,
    )

    contributions = []
    for row in child_rows["rows"]:
        contributions.append(
            {
                "value": row["dimension"],
                "current": row["value"],
                "prev": row["prev_value"],
                "contribution": (row["value"] or 0) - (row["prev_value"] or 0),
            }
        )
    contributions.sort(key=lambda c: -abs(c["contribution"]))

    node_current = sum((c["current"] or 0) for c in contributions)
    node_prev = sum((c["prev"] or 0) for c in contributions)
    if not norm_path:
        # 根节点：单值出口对账（口径与看板完全一致）
        cur = compute_metric_value(
            db, metric_ref=metric.id, start=start, end=end, compare="none", user=user
        )
        prv = compute_metric_value(
            db,
            metric_ref=metric.id,
            start=prev_start.isoformat(),
            end=prev_end.isoformat(),
            compare="none",
            user=user,
        )
        if cur["value"] is not None:
            node_current = cur["value"]
        if prv["value"] is not None:
            node_prev = prv["value"]

    node_delta = node_current - node_prev

    picked = contributions[:top_n]
    others = contributions[top_n:]
    others_contribution = sum(c["contribution"] for c in others)
    denom = node_delta if node_delta != 0 else None
    for c in picked:
        c["contribution_pct"] = (
            round(c["contribution"] / node_delta * 100, 4) if denom is not None else None
        )
    if others:
        picked.append(
            {
                "value": "（其他）",
                "current": None,
                "prev": None,
                "contribution": others_contribution,
                "contribution_pct": (
                    round(others_contribution / node_delta * 100, 4)
                    if denom is not None
                    else None
                ),
            }
        )

    explained = sum(c["contribution"] for c in contributions)
    deviation = (
        round((explained - node_delta) / abs(node_delta) * 100, 4)
        if node_delta
        else None
    )
    has_next = len(norm_path) + 1 < len(dims)
    return {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "name": metric.name,
        "dimensions": dims,
        "path": norm_path,
        "compare": compare,
        "start": start,
        "end": end,
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
        "current_total": node_current,
        "prev_total": node_prev,
        "delta": node_delta,
        "next_dimension": None if not has_next else dims[len(norm_path) + 1],
        "has_next": has_next,
        "children_dimension": child_dim,
        "children": picked,
        "children_total_count": len(contributions),
        "explained_delta": explained,
        "conservation_deviation_pct": deviation,
    }


def _parse_iso(raw: str):
    from datetime import date

    return date.fromisoformat(str(raw).strip())
