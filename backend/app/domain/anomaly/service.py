"""异动检测服务（B10-1 反常性引擎）。"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.query.service import compute_metric_series
from app.infra.models import AnomalyConfig, Metric, User
from app.infra.repository import Repository

# 保守档默认值（指标级可配覆盖）
DEFAULT_Z_THRESHOLD = 3.0
DEFAULT_MIN_SAMPLES = 8
DEFAULT_MATERIALITY_PCT = 5.0
MAX_BASELINE_WEEKS = 52  # 同星期几基准最多回看周数（min_samples 上限）


def get_config(db: Session, metric_id: int) -> AnomalyConfig:
    """指标配置（无记录 → 默认值对象，不落库）。"""
    cfg = (
        db.query(AnomalyConfig)
        .filter(AnomalyConfig.metric_id == metric_id)
        .first()
    )
    return cfg


def get_effective_config(db: Session, metric_id: int) -> dict:
    """生效配置（无记录回默认档）。"""
    cfg = get_config(db, metric_id)
    return {
        "z_threshold": cfg.z_threshold if cfg else DEFAULT_Z_THRESHOLD,
        "min_samples": cfg.min_samples if cfg else DEFAULT_MIN_SAMPLES,
        "materiality_pct": cfg.materiality_pct if cfg else DEFAULT_MATERIALITY_PCT,
        "enabled": bool(cfg.enabled) if cfg else True,
        "customized": cfg is not None,
    }


def upsert_config(
    db: Session,
    metric: Metric,
    *,
    z_threshold: float | None = None,
    min_samples: int | None = None,
    materiality_pct: float | None = None,
    enabled: bool | None = None,
) -> AnomalyConfig:
    """创建/更新指标级配置。"""
    if z_threshold is not None and not 1.0 <= z_threshold <= 10.0:
        raise BusinessError("z_threshold 须在 1.0~10.0 之间（越大越保守）", 40000)
    if min_samples is not None and not 1 <= min_samples <= MAX_BASELINE_WEEKS:
        raise BusinessError(f"min_samples 须在 1~{MAX_BASELINE_WEEKS} 之间", 40000)
    if materiality_pct is not None and not 0 <= materiality_pct <= 100000:
        raise BusinessError("materiality_pct 须在 0~100000 之间（变化率百分比）", 40000)
    cfg = get_config(db, metric.id)
    if cfg is None:
        cfg = AnomalyConfig(
            metric_id=metric.id,
            project_id=metric.project_id,
            z_threshold=z_threshold if z_threshold is not None else DEFAULT_Z_THRESHOLD,
            min_samples=min_samples if min_samples is not None else DEFAULT_MIN_SAMPLES,
            materiality_pct=(
                materiality_pct if materiality_pct is not None else DEFAULT_MATERIALITY_PCT
            ),
            enabled=int(enabled) if enabled is not None else 1,
        )
        return Repository(AnomalyConfig, db).add(cfg)
    if z_threshold is not None:
        cfg.z_threshold = z_threshold
    if min_samples is not None:
        cfg.min_samples = min_samples
    if materiality_pct is not None:
        cfg.materiality_pct = materiality_pct
    if enabled is not None:
        cfg.enabled = int(enabled)
    Repository(AnomalyConfig, db).update(cfg)
    return cfg


def _judge(
    current: float, samples: list[float], z_threshold: float
) -> tuple[bool, float | None, float, float]:
    """反常性判定（纯函数，便于对账测试）。返回 (abnormal, abnormality, mean, stdev)。

    - 常规：z = (current-mean)/stdev，|z| >= z_threshold 判反常；
    - 基准恒定（stdev=0）：z 无定义——任何非零偏离都判反常（恒定基线的
      任何变化都是真异变），abnormality 返回 None（无标准差倍数可言），
      阈值不参与（恒定基准下阈值再大也不该把 1000→5000 判为正常）。
    """
    mean = statistics.fmean(samples)
    stdev = statistics.pstdev(samples) if len(samples) > 1 else 0.0
    delta = current - mean
    if stdev == 0:
        return (delta != 0, None, mean, stdev)
    z = delta / stdev
    return (abs(z) >= z_threshold, round(abs(z), 4), mean, stdev)


def detect_for_metric(
    db: Session,
    user: User,
    metric: Metric,
    *,
    detect_date: date | None = None,
) -> dict:
    """单指标单日反常性检测。

    detect_date 缺省 = 数据覆盖末日（最新有数据的一天）。基准 = 该日往前
    1..MAX_BASELINE_WEEKS 周的同星期几日值（有效样本按配置 min_samples 截取）。
    """
    from app.domain.query.service import _resolve_metric

    resolved = metric if isinstance(metric, Metric) else _resolve_metric(db, metric)
    if resolved.status == "pending":
        raise BusinessError(f"指标 {resolved.code} 尚未绑定数据集，无法检测", 40000)

    cfg = get_effective_config(db, resolved.id)
    compiled = _compile_for_anomaly(db, resolved)
    if compiled.coverage_end is None:
        return _no_data_result(resolved, cfg, "指标为全期常数或无时间字段，不适用日粒度异动检测")

    # 时点性指标（today 过滤）序列退化为同值，基准分布无意义——结果中标注
    today_anchor = "__today__" in compiled.sql

    target = detect_date or compiled.coverage_end
    weeks = max(cfg["min_samples"], 4)  # 至少拉 4 周窗口，配置更大时拉更多
    start = target - timedelta(days=7 * weeks)
    series = compute_metric_series(
        db, metric_ref=resolved.id, start=start.isoformat(), end=target.isoformat(), user=user
    )
    by_date = {point["date"]: point["value"] for point in series}
    current = by_date.get(target.isoformat())
    if current is None:
        return _no_data_result(resolved, cfg, f"{target.isoformat()} 无数据（未覆盖或聚合为空）", target)

    samples = []
    for i in range(1, weeks + 1):
        day = target - timedelta(days=7 * i)
        v = by_date.get(day.isoformat())
        if v is not None:
            samples.append(v)
    if len(samples) < cfg["min_samples"]:
        return {
            "metric_id": resolved.id,
            "metric_code": resolved.code,
            "name": resolved.name,
            "date": target.isoformat(),
            "current": current,
            "abnormal": False,
            "verdict": "insufficient_baseline",
            "reason": f"同星期几基准样本不足（{len(samples)}/{cfg['min_samples']}），不判断",
            "config": cfg,
        }

    abnormal, abnormality, mean, stdev = _judge(current, samples, cfg["z_threshold"])
    delta = current - mean
    # B10-2 要紧度：第二道判断——反常但幅度小于 materiality_pct 的不构成结论
    delta_pct = round(delta / abs(mean) * 100, 4) if mean else None
    material = (
        (abs(delta_pct) >= cfg["materiality_pct"])
        if (abnormal and delta_pct is not None)
        else None
    )
    reason = None
    if abnormal:
        if abnormality is not None:
            reason = f"偏离同星期几基准 {abnormality:.2f} 倍标准差（阈值 {cfg['z_threshold']}）"
        else:
            reason = "基准恒定（方差 0），出现偏离即反常"
        if material is False:
            reason += f"；但变化幅度 {abs(delta_pct):.1f}% 低于要紧度阈值 {cfg['materiality_pct']}%，不构成异动结论"
    return {
        "metric_id": resolved.id,
        "metric_code": resolved.code,
        "name": resolved.name,
        "date": target.isoformat(),
        "current": current,
        "baseline": {
            "mean": mean,
            "stdev": stdev,
            "samples": len(samples),
            "kind": "same_weekday",
        },
        "delta": delta,
        "delta_pct": delta_pct,
        "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
        "abnormality": abnormality,
        "abnormal": abnormal,
        "material": material,  # B10-2 要紧度：三道判断之二（None=无法判定）
        "verdict": "abnormal" if abnormal else "normal",
        "reason": reason,
        "config": cfg,
        "time_point_metric": bool(today_anchor),
    }


def _no_data_result(metric: Metric, cfg: dict, reason: str, target: date | None = None) -> dict:
    return {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "name": metric.name,
        "date": target.isoformat() if target else None,
        "current": None,
        "abnormal": False,
        "verdict": "no_data",
        "reason": reason,
        "config": cfg,
    }


def detect_for_project(db: Session, user: User, project_id: int | None = None) -> list[dict]:
    """项目内全部启用指标的批量检测（总览页/看板黄条数据源）。

    只返回反常项 + 无数据/样本不足的摘要计数——宁缺毋滥。
    """
    from app.domain.metric.service import list_metrics
    from app.domain.project.service import resolve_project_id

    pid = resolve_project_id(db, project_id)
    metrics = [
        m for m in list_metrics(db, status="active", project_id=pid)
        if m.status == "active"
    ]
    results = []
    counts = {"abnormal": 0, "normal": 0, "insufficient_baseline": 0, "no_data": 0, "disabled": 0}
    for m in metrics:
        cfg = get_config(db, m.id)
        if cfg is not None and not cfg.enabled:
            counts["disabled"] += 1
            continue
        try:
            r = detect_for_metric(db, user, m)
        except BusinessError:
            counts["no_data"] += 1
            continue
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        if r["verdict"] == "abnormal":
            results.append(r)
    results.sort(key=lambda r: -(r.get("abnormality") or 0))
    return {"project_id": pid, "counts": counts, "anomalies": results[:5]}  # 限量 5 条宁缺毋滥


def _compile_for_anomaly(db: Session, metric: Metric):
    from app.domain.query.service import _compile_metric

    return _compile_metric(db, metric)
