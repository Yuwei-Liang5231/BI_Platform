"""多指标联动归因（A4，第一梯队收尾）。

一指标异动时，自动在**同项目**内寻找与它同期联动的其他指标：
- 取目标指标在「统计区间 + 等长前区间」的日序列（口径同源 compute_metric_series，
  权限同源 _ensure_visible）；
- 候选 = 同项目 active、当前用户可见的其余指标（限量扫描，按 id 稳定排序）；
- 对每个候选用对齐日值算 Pearson 相关系数（|r| ≥ 0.6 才算联动），并按
  「本期窗口合计 vs 前等长窗口合计」判定该候选自身的方向与变化幅度；
- 按 |r| 倒序取 TOP N；AI 只做传播假设的措辞（算写分离：数字一律服务端
  预格式化经 {{ref:KEY}} 回填，三道审计一致）；无 LLM/失败 → 规则句兜底，
  候选清单本身是确定性结果，不受降级影响。

相关系数 r 是统计量非指标值：按 z-score 同款例外保留 2 位小数展示。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.ai_narrative import fmt_pct, llm_narrative
from app.domain.query.service import compute_metric_series
from app.infra.llm import resolve_llm_config
from app.infra.models import Metric

logger = get_logger("app.domain.ai.metric_linkage")

# 联动判定与扫描上限
CORR_THRESHOLD = 0.6   # |r| ≥ 0.6 才视为联动（保守档，宁缺毋滥）
MIN_PAIRS = 5          # 对齐日值不足 5 对不判定（样本太少无意义）
DEFAULT_TOP_N = 3
MAX_SCAN = 12          # 候选扫描上限（防指标很多时拖垮首屏；按 id 稳定排序）
MIN_WINDOW_DAYS = 7    # 窗口过短（如详情页选单日）无法算相关：以 end 为锚向前扩到 7 天
MAX_WINDOW_DAYS = 31   # 窗口过长（前区间翻倍后易超导出上限）：截取最近 31 天

_DIRECTION_WORD = {"up": "上升", "down": "下降"}

_LINKAGE_SYSTEM = """你是经营指标联动分析助手。给定目标指标在一段时期的变化方向，以及同期与它
相关性最高的若干指标（含各自方向与变化幅度），用一句话给出可能的联动/传导假设。

铁律（违反任何一条的句子都会被系统剔除）：
1. 任何数字（含百分比、序号、相关系数）都必须用占位符 {{ref:KEY}} 引用，KEY 必须严格来自
   给定清单；
2. 占位符之外禁止出现任何数字字符；
3. 只能基于给定指标与方向做中性假设（如"可能与 XX 同步变化"），必须使用"可能"等
   非确定性措辞，不编造因果机制、不评价好坏、不提供建议；
4. 只输出 JSON：{"sections":[{"section":"interpretation","sentences":["一句话", ...]}]}——
   给出 1~3 个候选句子（同一含义的不同写法），系统会逐句审计并取第一句合格的；
   每个候选都必须独立完整且遵守上述全部铁律。"""


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    """Pearson 相关系数（对齐日值）；样本不足或零方差 → None。"""
    n = len(pairs)
    if n < MIN_PAIRS:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    r = sxy / ((sxx ** 0.5) * (syy ** 0.5))
    return max(-1.0, min(1.0, r))


def _window_sum(by_date: dict[str, float | None], s: date, e: date) -> float | None:
    """窗口内非空值合计；整窗无数据 → None。"""
    total = 0.0
    has = False
    cur = s
    while cur <= e:
        v = by_date.get(cur.isoformat())
        if v is not None:
            total += v
            has = True
        cur += timedelta(days=1)
    return total if has else None


def _direction_word(cur: float | None, prev: float | None) -> str | None:
    if cur is None or prev is None:
        return None
    if cur > prev:
        return "up"
    if cur < prev:
        return "down"
    return "flat"


def _change_pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _series_map(db: Session, user, metric_id: int, start: str, end: str) -> dict[str, float | None]:
    rows = compute_metric_series(db, metric_ref=metric_id, start=start, end=end, user=user)
    return {r["date"]: r["value"] for r in rows}


def build_metric_linkage(
    db: Session,
    user,
    *,
    metric_id: int,
    start: str,
    end: str,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, Any]:
    """目标指标 × 同期候选指标的联动分析（权限同源；降级不阻塞候选清单）。"""
    from app.domain.auth.service import build_user_hidden_map
    from app.domain.query.service import parse_range

    settings = get_settings()
    llm_configured = resolve_llm_config(db, settings) is not None

    start_d, end_d = parse_range(start, end)
    requested = (end_d - start_d).days + 1
    # 窗口归一：过短（详情页单日区间）扩到最少 7 天、过长截到 31 天，均以 end 为锚
    # （异动日通常靠近 end）。不归一的话单日窗口对齐日值 ≤ 2 对，相关系数永远算不出来，
    # 用户会看到"静默无清单"。
    window = min(max(requested, MIN_WINDOW_DAYS), MAX_WINDOW_DAYS)
    window_expanded = window != requested
    eff_start = end_d - timedelta(days=window - 1)
    eff_end = end_d
    prev_end = eff_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window - 1)

    target = db.get(Metric, int(metric_id))
    if target is None or target.status != "active":
        from app.core.response import BusinessError

        raise BusinessError(f"指标 {metric_id} 不存在或已删除", 40400)

    # 目标序列：前区间 + 本区间一次取回（逐日走缓存，天然复用）
    target_map = _series_map(db, user, target.id, prev_start.isoformat(), eff_end.isoformat())
    t_cur = _window_sum(target_map, eff_start, eff_end)
    t_prev = _window_sum(target_map, prev_start, prev_end)
    target_direction = _direction_word(t_cur, t_prev)
    target_change_pct = _change_pct(t_cur, t_prev)

    # 候选：同项目 active、当前用户可见、非目标本身；限量扫描（按 id 稳定）
    hidden = build_user_hidden_map(db).get(user.id, set())
    cand_rows = (
        db.query(Metric)
        .filter(
            Metric.project_id == target.project_id,
            Metric.status == "active",
            Metric.id != target.id,
        )
        .order_by(Metric.id)
        .limit(MAX_SCAN)
        .all()
    )

    # 目标在本区间的日值序列（对齐用）
    target_daily = {
        d: v
        for d, v in target_map.items()
        if eff_start.isoformat() <= d <= eff_end.isoformat() and v is not None
    }

    candidates: list[dict[str, Any]] = []
    for m in cand_rows:
        if m.id in hidden:
            continue
        try:
            cmap = _series_map(db, user, m.id, prev_start.isoformat(), eff_end.isoformat())
        except Exception:  # noqa: BLE001 - 单个候选失败不拖垮整体（无权限/口径异常等）
            logger.warning("linkage: 候选指标 %s 序列获取失败，跳过", m.id, exc_info=True)
            continue
        pairs = [
            (target_daily[d], cmap[d])
            for d in target_daily
            if d in cmap and cmap[d] is not None
        ]
        r = _pearson(pairs)
        if r is None or abs(r) < CORR_THRESHOLD:
            continue
        c_cur = _window_sum(cmap, eff_start, eff_end)
        c_prev = _window_sum(cmap, prev_start, prev_end)
        direction = _direction_word(c_cur, c_prev)
        pct = _change_pct(c_cur, c_prev)
        candidates.append(
            {
                "metric_id": m.id,
                "metric_code": m.code,
                "metric_name": m.name,
                "correlation": round(r, 2),
                "linkage": "inverse" if r < 0 else "same",
                "direction": direction,
                "change_pct": pct,
                "note": (
                    f"与「{target.name}」{'反向' if r < 0 else '同向'}联动"
                    f"（相关系数 {r:.2f}）：本期"
                    + ("无明显变化" if direction in (None, "flat") else _DIRECTION_WORD.get(direction, "变化"))
                    + (f" {fmt_pct(pct)}" if pct is not None else "")
                ),
            }
        )

    candidates.sort(key=lambda c: -abs(c["correlation"]))
    candidates = candidates[:top_n]

    # 规则句（确定性兜底，始终可展示）
    if candidates:
        bits = [
            f"{c['metric_name']}（{'反向' if c['linkage'] == 'inverse' else '同向'}，r={c['correlation']:.2f}）"
            for c in candidates
        ]
        rule_text = f"同期有 {len(candidates)} 个指标与本指标联动：{'、'.join(bits)}。"
    else:
        rule_text = f"同期未发现明显联动的指标（|相关系数| ≥ {CORR_THRESHOLD:.1f} 才纳入）。"

    out: dict[str, Any] = {
        "metric_id": target.id,
        "metric_name": target.name,
        "metric_code": target.code,
        "start": start,
        "end": end,
        "window": {
            "start": eff_start.isoformat(),
            "end": eff_end.isoformat(),
            "days": window,
            "expanded": window_expanded,
        },
        "target_direction": target_direction,
        "target_change_pct": target_change_pct,
        "candidates": candidates,
        "rule_text": rule_text,
        "interpretation": None,
        "source": "rule",
        "llm_configured": llm_configured,
        "reason": "no_candidates" if not candidates else None,
    }
    if not candidates or not llm_configured:
        if candidates and not llm_configured:
            out["reason"] = "llm_not_configured"
        return out

    # AI 传播假设：数字全部服务端预格式化进 ref_table（算写分离）
    ref_table: dict[str, tuple[str, str]] = {
        "metric_name": ("目标指标名称", target.name),
        "direction": ("目标指标本期方向", _DIRECTION_WORD.get(target_direction or "flat", "持平")),
    }
    if target_change_pct is not None:
        ref_table["target_pct"] = ("目标指标本期变化幅度", fmt_pct(target_change_pct))
    for i, c in enumerate(candidates, 1):
        ref_table[f"c{i}_name"] = (f"联动指标{i}的名称", str(c["metric_name"]))
        ref_table[f"c{i}_corr"] = (f"联动指标{i}与目标指标的相关系数（2 位小数）", f"{c['correlation']:.2f}")
        ref_table[f"c{i}_dir"] = (
            f"联动指标{i}本期方向",
            _DIRECTION_WORD.get(c["direction"] or "flat", "持平"),
        )
        ref_table[f"c{i}_pct"] = (
            f"联动指标{i}本期变化幅度",
            fmt_pct(c["change_pct"]),
        )

    user_payload = {
        "metric": target.name,
        "period": {"start": eff_start.isoformat(), "end": eff_end.isoformat()},
        "direction": _DIRECTION_WORD.get(target_direction or "flat", "持平"),
        "candidates": [
            {
                "name_ref": f"c{i}_name",
                "corr_ref": f"c{i}_corr",
                "dir_ref": f"c{i}_dir",
                "pct_ref": f"c{i}_pct",
                "linkage": c["linkage"],
            }
            for i, c in enumerate(candidates, 1)
        ],
    }
    result = llm_narrative(
        db, settings,
        ref_table=ref_table,
        sections_to_write=["interpretation"],
        system_prompt=_LINKAGE_SYSTEM,
        user_payload=user_payload,
        timeout=60.0,
        retries=1,  # 单句场景脆弱：网络抖动重试一次
        kind="metric_linkage",
        project_id=target.project_id,
    )
    if result is None:
        out["reason"] = "llm_failed"
        return out
    sentences = [s for sec in result["sections"] for s in sec.get("sentences", [])]
    out["interpretation"] = sentences[0] if sentences else None
    out["source"] = "llm"
    out["reason"] = None if sentences else "llm_failed"
    return out
