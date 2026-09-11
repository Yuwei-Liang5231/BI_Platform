"""统一计算服务（架构不变式 1：唯一取数出口，B3）。

看板 / 问数 / 报告 / 预警的所有数值只能经本模块计算：
- 周期完整性判定：请求范围超出数据覆盖区间 → value=null 且
  period_complete=false（前端显示"—"），**不返回部分值冒充完整周期**；
- LRU 缓存：键含指标 ver + dataset_ver + 时间范围 + SQL 摘要；
  口径变更 / 数据重导入后旧键自然失效；不完整周期不入缓存；
- 环比/同比在查询层基于同一编译产物计算（架构 7.2：不属于编译器表达式）；
- 执行时实时重编译：保证使用当前数据集覆盖区间与表关系（元数据规模小，
  编译为毫秒级）；存档 SQL 仅用于展示与历史回看（B2 语义不变）。

权限（B4 落地）：user 为必填参数；受限指标在取数出口强制 403（不变式 2，
与 metric 元数据接口共用 auth.service 的同一套判定——权限双出口）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.metric.compiler import CompiledQuery, compile_metric
from app.domain.metric.schema import CalcRuleError
from app.infra.cache import metric_cache
from app.infra.duckdb_session import duckdb_views
from app.infra.models import Dataset, Metric, User
from app.domain.metric.service import load_relations

# 导出与序列的最大天数（防滥用；按天循环执行，10 万行/天在 DuckDB 下毫秒级）
MAX_EXPORT_DAYS = 1095

VALID_COMPARE = ("none", "mom", "yoy")


# ---------------------------------------------------------------- 入参解析


def _parse_date(raw: str, field_name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (ValueError, TypeError):
        raise BusinessError(f"{field_name} 日期格式应为 YYYY-MM-DD，收到 {raw!r}", 40000) from None


def parse_range(start: str, end: str) -> tuple[date, date]:
    s = _parse_date(start, "start")
    e = _parse_date(end, "end")
    if s > e:
        raise BusinessError(f"start ({s}) 不能晚于 end ({e})", 40000)
    return s, e


def _ensure_visible(db: Session, user: User, metric: Metric) -> None:
    """权限双出口之数值侧：受限指标 403 拒算（名称也不回显）。"""
    from app.domain.auth.service import ensure_metric_visible

    ensure_metric_visible(db, user, metric)


def _resolve_metric(db: Session, metric_ref) -> Metric:
    """metric 入参兼容 id（int / 数字字符串）与 code（字符串）。"""
    metric = None
    if isinstance(metric_ref, int) or (isinstance(metric_ref, str) and metric_ref.strip().isdigit()):
        metric = db.get(Metric, int(metric_ref))
    if metric is None and isinstance(metric_ref, str):
        metric = db.query(Metric).filter(Metric.code == metric_ref.strip()).first()
    if metric is None or metric.status == "deleted":
        raise BusinessError(f"指标 {metric_ref!r} 不存在", 40400)
    if metric.status == "disabled":
        raise BusinessError(f"指标 {metric.code} 已停用，无法计算", 40000)
    if metric.status == "pending":
        # B5 模板导入态：口径已登记但数据集未绑定/编译未通过
        raise BusinessError(
            f"指标 {metric.code} 尚未绑定数据集，暂不可计算（上传对应数据后可重新导入模板激活）",
            40000,
        )
    return metric


# ---------------------------------------------------------------- 编译与执行


def _compile_metric(db: Session, metric: Metric) -> CompiledQuery:
    try:
        return compile_metric(
            json.loads(metric.calc_rule_json),
            _load_datasets_for_query(db),
            load_relations(db),
        )
    except (CalcRuleError, ValueError) as exc:
        # 定义合法但数据集状态已变（如列被重接后消失）：给出可行动的报错
        raise BusinessError(f"指标 {metric.code} 编译失败：{exc}", 40000) from exc


def _load_datasets_for_query(db: Session):
    """复用 metric.service 的装载逻辑，避免两套类型推断读取路径。"""
    from app.domain.metric.service import load_datasets

    return load_datasets(db)


def _dataset_runtime(db: Session, compiled: CompiledQuery) -> dict[str, dict]:
    """参与数据集的运行时信息：{name: {ver, parquet_path}}（缓存键因子 + 执行视图）。"""
    rows = db.query(Dataset).filter(Dataset.name.in_(compiled.dataset_names)).all()
    by_name = {r.name: r for r in rows}
    missing = [n for n in compiled.dataset_names if n not in by_name]
    if missing:
        raise BusinessError(f"数据集 {missing} 已不存在，请重新接入或修正指标口径", 40000)
    return {
        name: {"ver": by_name[name].dataset_ver, "parquet_path": by_name[name].parquet_path}
        for name in compiled.dataset_names
    }


def _coerce_value(raw):
    """DuckDB 聚合结果统一为 JSON 安全类型（HUGEINT/Decimal → int/float）。"""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return int(raw) if raw == raw.to_integral_value() else float(raw)
    return raw


def _cache_key(metric: Metric, compiled: CompiledQuery, runtime: dict, start: date, end: date) -> str:
    dataset_vers = ",".join(f"{n}:{runtime[n]['ver']}" for n in sorted(runtime))
    sql_digest = hashlib.sha1(compiled.sql.encode("utf-8")).hexdigest()[:12]
    return metric_cache.make_key(
        "metric", metric.id, metric.ver, dataset_vers,
        start.isoformat(), end.isoformat(), sql_digest,
    )


def _compute_range(
    db: Session,
    metric: Metric,
    compiled: CompiledQuery,
    runtime: dict,
    start: date,
    end: date,
) -> dict:
    """计算单个时间范围。返回 {value, period_complete, cache}。

    周期不完整 → value=None 且不缓存（补录数据后结果会变，缓存会固化 null）。
    完整但区间内无数据 / 比率分母为 0 → value=None 但 period_complete=True。
    """
    cov_start, cov_end = compiled.coverage_start, compiled.coverage_end
    if cov_start is None or cov_end is None or start < cov_start or end > cov_end:
        return {"value": None, "period_complete": False, "cache": "bypass"}

    key = _cache_key(metric, compiled, runtime, start, end)
    cached = metric_cache.get(key)
    if cached is not None:
        return {**cached, "cache": "hit"}

    views = {name: info["parquet_path"] for name, info in runtime.items()}
    with duckdb_views(views) as con:
        row = con.execute(compiled.sql, {"__start__": start, "__end__": end}).fetchone()
    result = {"value": _coerce_value(row[0] if row else None), "period_complete": True, "cache": "miss"}
    metric_cache.put(key, result)
    return result


# ---------------------------------------------------------------- 对外能力


def _previous_range(start: date, end: date, compare: str) -> tuple[date, date]:
    if compare == "mom":
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        return prev_end - timedelta(days=span - 1), prev_end
    # yoy：去年同起止；2/29 场景回退为 2/28
    try:
        prev_start = start.replace(year=start.year - 1)
    except ValueError:
        prev_start = start.replace(year=start.year - 1, day=28)
    try:
        prev_end = end.replace(year=end.year - 1)
    except ValueError:
        prev_end = end.replace(year=end.year - 1, day=28)
    return prev_start, prev_end


def _change_pct(current, previous):
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / abs(previous) * 100, 4)


def compute_metric_value(
    db: Session,
    *,
    metric_ref,
    start: str,
    end: str,
    compare: str = "none",
    user: User,
) -> dict:
    """单值计算（唯一取数出口核心方法）。"""
    if compare not in VALID_COMPARE:
        raise BusinessError(f"compare 只支持 {' / '.join(VALID_COMPARE)}，收到 {compare!r}", 40000)
    start_d, end_d = parse_range(start, end)
    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)

    compiled = _compile_metric(db, metric)
    runtime = _dataset_runtime(db, compiled)
    current = _compute_range(db, metric, compiled, runtime, start_d, end_d)

    payload = {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "name": metric.name,
        "ver": metric.ver,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "value": current["value"],
        "period_complete": current["period_complete"],
        "cache": current["cache"],
        "coverage": {
            "start": compiled.coverage_start.isoformat() if compiled.coverage_start else None,
            "end": compiled.coverage_end.isoformat() if compiled.coverage_end else None,
        },
        "grain": compiled.grain,
        "compare": None,
    }

    if compare != "none":
        prev_start, prev_end = _previous_range(start_d, end_d, compare)
        prev = _compute_range(db, metric, compiled, runtime, prev_start, prev_end)
        payload["compare"] = {
            "type": compare,
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "value": prev["value"],
            "period_complete": prev["period_complete"],
            "change_pct": _change_pct(current["value"], prev["value"]),
        }
    return payload


def compute_metric_series(
    db: Session,
    *,
    metric_ref,
    start: str,
    end: str,
    user: User,
) -> list[dict]:
    """按日序列（导出与小趋势共用）。逐日走 _compute_range，天然复用缓存。"""
    start_d, end_d = parse_range(start, end)
    days = (end_d - start_d).days + 1
    if days > MAX_EXPORT_DAYS:
        raise BusinessError(f"导出范围过长（{days} 天），单次最多 {MAX_EXPORT_DAYS} 天", 40000)

    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)
    compiled = _compile_metric(db, metric)
    runtime = _dataset_runtime(db, compiled)

    series = []
    cursor = start_d
    while cursor <= end_d:
        point = _compute_range(db, metric, compiled, runtime, cursor, cursor)
        series.append(
            {
                "date": cursor.isoformat(),
                "value": point["value"],
                "period_complete": point["period_complete"],
            }
        )
        cursor += timedelta(days=1)
    return series


def export_metric_csv(
    db: Session,
    *,
    metric_ref,
    start: str,
    end: str,
    user: User,
) -> tuple[str, str]:
    """导出 CSV（export 挂载于 query，执行方案 B3）。

    返回 (csv_text, filename)。UTF-8 带 BOM——Excel 打开中文不乱码
    （与 B1 PowerShell 脚本同款教训前置规避）。
    """
    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)
    series = compute_metric_series(db, metric_ref=metric.id, start=start, end=end, user=user)

    lines = ["date,value,period_complete"]
    for point in series:
        value = "" if point["value"] is None else point["value"]
        lines.append(f"{point['date']},{value},{'1' if point['period_complete'] else '0'}")
    csv_text = "\ufeff" + "\n".join(lines) + "\n"
    filename = f"{metric.code}_{start}_{end}.csv"
    return csv_text, filename
