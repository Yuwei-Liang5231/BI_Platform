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
    # 11.9 P1-2 观察期未满：检测日 + 成熟期未到，当日值尚未定型，不检测
    md = getattr(resolved, "maturity_days", None)
    if md and md > 0 and date.today() < target + timedelta(days=md):
        return _no_data_result(
            resolved, cfg,
            f"观察期未满（需 {md} 天，{target.isoformat()} + {md} 天 > 今天），暂不检测",
            target,
        )
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
            reason = f"偏离同星期几基准 {abnormality:.2f} 倍标准差（阈值 {cfg['z_threshold']:g}）"
        else:
            reason = "基准恒定（方差 0），出现偏离即反常"
        if material is False:
            reason += f"；但变化幅度 {abs(delta_pct):.1f}% 低于要紧度阈值 {round(cfg['materiality_pct'])}%，不构成异动结论"
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
    """项目内批量检测（总览页/看板黄条数据源）。

    扫描范围为 **opt-in 语义**：只扫「显式配置过且 enabled=1」的指标——
    全量默认扫描在大目录下不可行（每指标一次序列计算，数十指标即超时），
    且用户只关心自己开启检测的指标。结果只含反常项，限量 5 条宁缺毋滥；
    反常且要紧的结论落库生成站内通知（去重，收件人 = 对该指标可见的全部活跃用户）。
    """
    from app.domain.project.service import resolve_project_id

    pid = resolve_project_id(db, project_id)
    configured = (
        db.query(AnomalyConfig)
        .filter(AnomalyConfig.project_id == pid, AnomalyConfig.enabled == 1)
        .all()
    )
    results = []
    counts = {
        "configured": len(configured), "abnormal": 0, "normal": 0,
        "insufficient_baseline": 0, "no_data": 0, "error": 0,
    }
    for cfg in configured:
        metric = db.get(Metric, cfg.metric_id)
        if metric is None or metric.status != "active":
            continue  # 指标已删/停用：配置随指标生命周期，扫描跳过
        try:
            r = detect_for_metric(db, user, metric)
        except BusinessError:
            counts["no_data"] += 1
            continue
        except Exception:  # noqa: BLE001 - 单指标计算异常不拖垮整批扫描（如数据文件缺失）
            counts["error"] += 1
            continue
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        if r["verdict"] == "abnormal":
            results.append(r)
    results.sort(key=lambda r: -(r.get("abnormality") or 0))
    _persist_notifications(db, [r for r in results if r.get("material") is not False], pid)
    return {"project_id": pid, "counts": counts, "anomalies": results[:5]}  # 限量 5 条宁缺毋滥


def _action_hint(direction: str) -> str:
    """11.9 P1-1 异动通知建议动作：方向驱动的规则模板（纯文案，零查询）。
    算写分离红线不受影响：建议是固定句式，不含任何计算数值。"""
    if direction == "up":
        return (
            "建议动作：① 核对口径与数据完整性（是否有补录/重复计数）；"
            "② 在指标详情按常用维度拆解，定位主要来源；"
            "③ 确认是否存在促销、季节或一次性因素。"
        )
    return (
        "建议动作：① 核查统计口径或采集链路近期是否变更；"
        "② 在指标详情按常用维度拆解，定位下滑来源；"
        "③ 结合业务判断是否需要干预。"
    )


def _persist_notifications(db: Session, abnormal_results: list[dict], project_id: int | None) -> None:
    """异动结论 → 站内通知（同 user+metric+日+方向去重）。

    收件人 = 全部 active 用户中**对该指标可见者**（权限同源：与看板/
    问数同一套受限判定，共用 auth.build_user_hidden_map 批量映射；
    受限用户不收受限指标的异动通知，不泄露名称）。
    """
    from app.domain.auth.service import build_user_hidden_map
    from app.infra.models import Notification

    users = db.query(User).filter(User.status == "active").all()
    user_hidden = build_user_hidden_map(db)
    for r in abnormal_results:
        if r.get("date") is None or r.get("direction") not in ("up", "down"):
            continue
        anomaly_date = date.fromisoformat(r["date"])
        delta_pct = r.get("delta_pct")
        pct_text = f"{round(abs(delta_pct))}%" if delta_pct is not None else ""
        arrow = "↑" if r["direction"] == "up" else "↓"
        title = f"「{r['name']}」异动{arrow} {pct_text}"
        baseline = r.get("baseline") or {}
        body = (
            f"{r['date']} 值为 {round(r['current'], 1):,}，正常水平约 {round(baseline.get('mean', 0), 1):,}；"
            f"{r.get('reason') or ''} {_action_hint(r['direction'])}"
        )
        for u in users:
            if r["metric_id"] in user_hidden[u.id]:
                continue
            dup = (
                db.query(Notification)
                .filter(
                    Notification.user_id == u.id,
                    Notification.metric_id == r["metric_id"],
                    Notification.anomaly_date == anomaly_date,
                    Notification.direction == r["direction"],
                )
                .first()
            )
            if dup is not None:
                continue
            db.add(
                Notification(
                    user_id=u.id,
                    project_id=project_id,
                    metric_id=r["metric_id"],
                    metric_code=r["metric_code"],
                    anomaly_date=anomaly_date,
                    direction=r["direction"],
                    title=title,
                    body=body,
                    current=r.get("current"),
                    baseline_mean=baseline.get("mean"),
                    abnormality=r.get("abnormality"),
                    kind="anomaly",
                )
            )
    db.flush()


def _compile_for_anomaly(db: Session, metric: Metric):
    from app.domain.query.service import _compile_metric

    return _compile_metric(db, metric)
