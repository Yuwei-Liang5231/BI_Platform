"""统一计算服务（架构不变式 1：唯一取数出口，B3）。

看板 / 问数 / 报告 / 预警的所有数值只能经本模块计算：
- 周期完整性判定：请求范围超出数据覆盖区间 → value=null 且
  period_complete=false（前端显示"—"），**不返回部分值冒充完整周期**；
- LRU 缓存：键含指标 ver + dataset_ver + 时间范围 + SQL 摘要；
  口径变更 / 数据重导入后旧键自然失效；不完整周期不入缓存；
- 环比/同比在查询层基于同一编译产物计算（架构 7.2：不属于编译器表达式）；
- 执行时实时重编译：保证使用当前数据集覆盖区间与表关系（元数据规模小，
  编译为毫秒级）；存档 SQL 仅用于展示与历史回看（B2 语义不变）；
- 全期常数指标（2026-09-14「日期可选」）：未绑定时间字段的指标与区间无关，
  任意范围返回同一全期值；不参与环比，按日序列退化为单点。

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
from app.domain.ingestion.service import view_target
from app.domain.metric.compiler import (
    CompiledBreakdown,
    CompiledQuery,
    compile_metric,
    compile_metric_breakdown,
)
from app.domain.metric.compiler import _q as _quote_ident
from app.domain.metric.schema import CalcRuleError, FilterCondition
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


def _cache_key(
    metric: Metric,
    compiled: CompiledQuery,
    runtime: dict,
    start: date,
    end: date,
    cov_start: date | None = None,
    cov_end: date | None = None,
) -> str:
    dataset_vers = ",".join(f"{n}:{runtime[n]['ver']}" for n in sorted(runtime))
    sql_digest = hashlib.sha1(compiled.sql.encode("utf-8")).hexdigest()[:12]
    # 覆盖端点参与键：部分周期值入库后，数据补录（覆盖变化）→ 键变化 → 自然失效
    cov_tag = f"{cov_start.isoformat()}~{cov_end.isoformat()}" if cov_start and cov_end else "nocov"
    return metric_cache.make_key(
        "metric", metric.id, metric.ver, dataset_vers,
        start.isoformat(), end.isoformat(), sql_digest, cov_tag,
    )


def _compute_range(
    db: Session,
    metric: Metric,
    compiled: CompiledQuery,
    runtime: dict,
    start: date,
    end: date,
) -> dict:
    """计算单个时间范围。返回 {value, period_complete, data_through, cache}。

    契约 v2（2026-09-11，替代"不完整周期显示—"旧约）：真实业务逻辑是
    「实际是多少就是多少」——覆盖区间与请求区间相交即计算真实值，部分周期
    不虚报也不隐瞒：值照常返回，period_complete=false + data_through
    （数据实际截止日）供前端标注"数据截至"。
    - 覆盖未知 / 区间与覆盖无交集 → value=None（区间无数据，不缓存）
    - 区间有数据但聚合结果为 null（如比率分母为 0）→ value=None
    - 缓存键含覆盖端点：数据补录（覆盖变化）后键变化，不会返回陈旧部分值。
    - 全期常数指标（2026-09-14「日期可选」）：编译产物无时间过滤，与请求区间
      无关——不做覆盖判定，period_complete=True、data_through=None，
      缓存键不含覆盖端点（cov_tag=nocov）。
    """
    cov_start, cov_end = compiled.coverage_start, compiled.coverage_end
    is_constant = compiled.is_constant
    if not is_constant and (
        cov_start is None or cov_end is None or start > cov_end or end < cov_start
    ):
        return {"value": None, "period_complete": False, "data_through": None, "cache": "bypass"}

    key = _cache_key(metric, compiled, runtime, start, end, cov_start, cov_end)
    cached = metric_cache.get(key)
    if cached is not None:
        return {**cached, "cache": "hit"}

    # B8：分片目录数据集展开为 glob（存量单文件原样）
    views = {name: view_target(info["parquet_path"]) for name, info in runtime.items()}
    # 常数指标 SQL 无 $__start__/__end__ 占位符，绑定空参数（DuckDB 对未使用
    # 的命名参数不保证容忍，缺参/多参都不冒这个险）
    params: dict = {} if is_constant else {"__start__": start, "__end__": end}
    with duckdb_views(views) as con:
        row = con.execute(compiled.sql, params).fetchone()
    if is_constant:
        result = {
            "value": _coerce_value(row[0] if row else None),
            "period_complete": True,
            "data_through": None,
            "cache": "miss",
        }
    else:
        result = {
            "value": _coerce_value(row[0] if row else None),
            "period_complete": start >= cov_start and end <= cov_end,
            "data_through": min(end, cov_end).isoformat(),
            "cache": "miss",
        }
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
    is_constant = compiled.is_constant
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
        "data_through": current["data_through"],
        "cache": current["cache"],
        # 全期常数指标：未绑定时间字段，任意区间返回同一全期汇总值
        "constant": is_constant,
        "coverage": {
            "start": compiled.coverage_start.isoformat() if compiled.coverage_start else None,
            "end": compiled.coverage_end.isoformat() if compiled.coverage_end else None,
        },
        "grain": compiled.grain,
        "compare": None,
    }

    if compare != "none" and not is_constant:
        # 契约 v2：当前周期不完整（数据未覆盖到区间尾）时，先截出「有效数据窗口」
        # [start, data_through]，再对其同长度前移一期作基期。旧实现 prev_end =
        # prev_start + (data_through - start) 只对齐窗口末端、起点仍锚定完整区间
        # 前移，跨月长区间会把基期推到覆盖边缘甚至之外——如 12-01~01-29 数据截至
        # 12-31 时基期漂成 10-02~11-01（仅 11-01 一天有数据 → +640909% 假涨幅），
        # 或 12-01~01-31 时漂到 10-01~10-31（覆盖外 → 直接无环比）。
        # 有效窗口对齐后，长区间与「12 月整月」的环比一致（都是 vs 11 月同长度窗口），
        # 短区间场景（08-01~08-31 截至至 08-10 → 基期 07-01~07-10）行为不变。
        eff_end_d = end_d
        if not current["period_complete"] and current["data_through"]:
            eff_end_d = date.fromisoformat(current["data_through"])
        prev_start, prev_end = _previous_range(start_d, eff_end_d, compare)
        prev = _compute_range(db, metric, compiled, runtime, prev_start, prev_end)
        payload["compare"] = {
            "type": compare,
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "value": prev["value"],
            "period_complete": prev["period_complete"],
            "data_through": prev["data_through"],
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

    # 全期常数指标与时间无关：序列退化为单点（逐日循环只会重复同一值，
    # 既浪费执行也会在导出 CSV 里制造误导性的"每天一个相同值"）
    if compiled.is_constant:
        point = _compute_range(db, metric, compiled, runtime, start_d, start_d)
        return [
            {
                "date": start_d.isoformat(),
                "value": point["value"],
                "period_complete": point["period_complete"],
            }
        ]

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


# ---------------------------------------------------------------- 维度拆解（B9.2-1）

VALID_ORDER_BY = ("value", "change_abs", "change_pct")
VALID_ORDER = ("asc", "desc")
VALID_BREAKDOWN_FILTER_OPS = ("<=", ">=", "!=", "=", ">", "<", "is_null", "is_not_null")
DEFAULT_BREAKDOWN_TOP_N = 10
MAX_BREAKDOWN_TOP_N = 50
MAX_BREAKDOWN_ROWS = 2000        # SQL 端拉回上限：防高基数列把结果集拖爆
LOW_CARDINALITY_THRESHOLD = 200  # 「低基数」提示线：≤200 前端正常展示；超过标注「拆解较慢」（V1.3.2 由 50 放宽）
BREAKDOWN_MAX_CARDINALITY = 5000  # 拆解硬上限：ID 类极端高基数列（每行一值、无洞察且拖垮聚合）不开放拆解


def _parse_breakdown_filters(raw) -> list[FilterCondition]:
    """请求级拆解过滤解析：[{column, op, value}]。op 白名单与口径 filter 文法
    一致；列越纲（不在数据集/无显式关系）由编译器按 operand 逐个判定（400）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BusinessError("filters 必须是数组", 40000)
    conds: list[FilterCondition] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("column") or "").strip():
            raise BusinessError(f"filters[{i}] 必须是含 column 的对象", 40000)
        column = str(item["column"]).strip()
        op = item.get("op")
        if op not in VALID_BREAKDOWN_FILTER_OPS:
            raise BusinessError(
                f"filters[{i}].op 仅支持 {' / '.join(VALID_BREAKDOWN_FILTER_OPS)}，收到 {op!r}",
                40000,
            )
        if op in ("is_null", "is_not_null"):
            conds.append(FilterCondition(column=column, op=op))
            continue
        if "value" not in item or item["value"] is None:
            raise BusinessError(f"filters[{i}]（{column}）缺少比较值 value", 40000)
        conds.append(FilterCondition(column=column, op=op, literal=item["value"]))
    return conds


def _compile_breakdown(
    db: Session, metric: Metric, dimension: str, extra_filters: list[FilterCondition]
) -> CompiledBreakdown:
    try:
        return compile_metric_breakdown(
            json.loads(metric.calc_rule_json),
            _load_datasets_for_query(db),
            load_relations(db),
            dimension,
            extra_filters=extra_filters,
        )
    except (CalcRuleError, ValueError) as exc:
        # CalcRuleError 是 ValueError 子类，统一给出 400 可行动报错
        raise BusinessError(f"指标 {metric.code} 拆解编译失败：{exc}", 40000) from exc


def _breakdown_cache_key(
    metric: Metric,
    compiled: CompiledBreakdown,
    runtime: dict,
    start: date,
    end: date,
    cov_start: date | None,
    cov_end: date | None,
    compare: str,
    filters: list[FilterCondition],
) -> str:
    dataset_vers = ",".join(f"{n}:{runtime[n]['ver']}" for n in sorted(runtime))
    sql_digest = hashlib.sha1(compiled.sql.encode("utf-8")).hexdigest()[:12]
    filt_json = json.dumps(
        [{"column": c.column, "op": c.op, "literal": c.literal} for c in filters],
        ensure_ascii=False, sort_keys=True,
    )
    filt_digest = hashlib.sha1(filt_json.encode("utf-8")).hexdigest()[:12]
    cov_tag = f"{cov_start.isoformat()}~{cov_end.isoformat()}" if cov_start and cov_end else "nocov"
    # compare 必须参与键：缓存内容含基期组值，none/mom/yoy 三种请求互不串值
    return metric_cache.make_key(
        "brkdwn", metric.id, metric.ver, dataset_vers,
        start.isoformat(), end.isoformat(), sql_digest, compare, filt_digest, cov_tag,
    )


def _exec_breakdown(
    db: Session, compiled: CompiledBreakdown, runtime: dict, start: date, end: date
) -> dict[str, object]:
    """执行拆解 SQL → {维度值(str): 组值}。SQL 端 LIMIT 防高基数拖爆内存。"""
    views = {name: view_target(info["parquet_path"]) for name, info in runtime.items()}
    params: dict = {} if compiled.is_constant else {"__start__": start, "__end__": end}
    with duckdb_views(views) as con:
        rows = con.execute(compiled.sql + f" LIMIT {MAX_BREAKDOWN_ROWS}", params).fetchall()
    # 统一列序：dimension, value（flat 与 expression 两种形态均如此）
    return {str(r[0]): _coerce_value(r[1]) for r in rows}


def compute_metric_breakdown(
    db: Session,
    *,
    metric_ref,
    start: str,
    end: str,
    compare: str = "none",
    dimension: str,
    filters: list | None = None,
    order_by: str = "value",
    order: str = "desc",
    top_n: int | None = None,
    user: User,
) -> dict:
    """维度拆解计算（B9.2-1）：按维度列分组聚合 + 请求级过滤 + 排序 + TopN。

    - 与 metric-value 完全同源：同一编译器/权限判定/周期完整性契约 v2/缓存失效机制；
    - 比率类指标按组重算分子分母（编译器 expression 形态天然保证，禁止对率求平均）；
    - compare 的组内环比/同比 = 该组基期值对比（基期区间与单值口径一致，
      不完整周期同样按「有效数据窗口」对齐）；全期常数指标 compare=null；
    - 排序字段 value / change_abs / change_pct；空值组恒排末尾（与方向无关）。
    """
    if compare not in VALID_COMPARE:
        raise BusinessError(f"compare 只支持 {' / '.join(VALID_COMPARE)}，收到 {compare!r}", 40000)
    if order_by not in VALID_ORDER_BY:
        raise BusinessError(f"order_by 只支持 {' / '.join(VALID_ORDER_BY)}，收到 {order_by!r}", 40000)
    if order not in VALID_ORDER:
        raise BusinessError(f"order 只支持 {' / '.join(VALID_ORDER)}，收到 {order!r}", 40000)
    if top_n is None:
        top_n = DEFAULT_BREAKDOWN_TOP_N
    if not isinstance(top_n, int) or not 1 <= top_n <= MAX_BREAKDOWN_TOP_N:
        raise BusinessError(f"top_n 须在 1~{MAX_BREAKDOWN_TOP_N} 之间", 40000)
    if not isinstance(dimension, str) or not dimension.strip():
        raise BusinessError("dimension（拆解维度列）必填", 40000)
    dimension = dimension.strip()
    conds = _parse_breakdown_filters(filters)

    start_d, end_d = parse_range(start, end)
    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)
    compiled = _compile_breakdown(db, metric, dimension, conds)
    runtime = _dataset_runtime(db, compiled)
    is_constant = compiled.is_constant

    cov_start, cov_end = compiled.coverage_start, compiled.coverage_end
    if not is_constant and (
        cov_start is None or cov_end is None or start_d > cov_end or end_d < cov_start
    ):
        return {
            "metric_id": metric.id, "metric_code": metric.code, "name": metric.name,
            "ver": metric.ver, "dimension": dimension, "start": start_d.isoformat(),
            "end": end_d.isoformat(), "constant": False, "coverage": {"start": None, "end": None},
            "rows": [], "total_groups": 0, "period_complete": False, "data_through": None,
            "compare": None, "order_by": order_by, "order": order, "top_n": top_n,
            "cache": "bypass",
        }

    key = _breakdown_cache_key(metric, compiled, runtime, start_d, end_d, cov_start, cov_end, compare, conds)
    cached = metric_cache.get(key)
    if cached is not None:
        payload = _build_breakdown_payload(
            metric, dimension, start_d, end_d, compiled, order_by, order, top_n,
            cached["current"], cached["prev_map"], cached["prev_meta"],
            cached["period_complete"], cached["data_through"],
        )
        payload["cache"] = "hit"
        return payload

    current: dict[str, object] = _exec_breakdown(db, compiled, runtime, start_d, end_d)
    period_complete = True if is_constant else (start_d >= cov_start and end_d <= cov_end)
    data_through = None if is_constant else min(end_d, cov_end).isoformat()

    prev_map: dict[str, object] | None = None
    prev_meta: dict | None = None
    if compare != "none" and not is_constant:
        eff_end_d = end_d
        if not period_complete and data_through:
            eff_end_d = date.fromisoformat(data_through)
        prev_start, prev_end = _previous_range(start_d, eff_end_d, compare)
        prev_map = _exec_breakdown(db, compiled, runtime, prev_start, prev_end)
        prev_meta = {
            "type": compare,
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "period_complete": prev_start >= cov_start and prev_end <= cov_end,
            "data_through": min(prev_end, cov_end).isoformat(),
        }

    metric_cache.put(key, {
        "current": current, "prev_map": prev_map, "prev_meta": prev_meta,
        "period_complete": period_complete, "data_through": data_through,
    })
    payload = _build_breakdown_payload(
        metric, dimension, start_d, end_d, compiled, order_by, order, top_n,
        current, prev_map, prev_meta, period_complete, data_through,
    )
    payload["cache"] = "miss"
    return payload


def _build_breakdown_payload(
    metric: Metric,
    dimension: str,
    start_d: date,
    end_d: date,
    compiled: CompiledBreakdown,
    order_by: str,
    order: str,
    top_n: int,
    current: dict[str, object],
    prev_map: dict[str, object] | None,
    prev_meta: dict | None,
    period_complete: bool,
    data_through: str | None,
) -> dict:
    """组内对比计算 + 排序 + TopN（缓存命中路径同样走这里，纯内存计算）。"""
    rows = []
    for dim, value in current.items():
        prev_value = prev_map.get(dim) if prev_map is not None else None
        if value is None or prev_value is None:
            change_abs = None
        else:
            change_abs = round(value - prev_value, 6)
        rows.append({
            "dimension": dim,
            "value": value,
            "prev_value": prev_value,
            "change_abs": change_abs,
            "change_pct": _change_pct(value, prev_value),
        })
    total_groups = len(rows)
    # B9.2-5 占比：分母 = 截断前全部非空组值之和（TopN 截断后占比仍以全体为准）；
    # 全部非正/无值 → share=None（无占比语义）
    total_sum = sum(r["value"] for r in rows if r["value"] is not None)
    for r in rows:
        r["share"] = (
            round(r["value"] / total_sum, 4)
            if r["value"] is not None and total_sum > 0
            else None
        )
    # 空值组恒排末尾（与方向无关），其余按所选字段升/降序
    nonnull = [r for r in rows if r[order_by] is not None]
    nulls = [r for r in rows if r[order_by] is None]
    nonnull.sort(key=lambda r: r[order_by], reverse=(order == "desc"))
    rows = (nonnull + nulls)[:top_n]
    return {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "name": metric.name,
        "ver": metric.ver,
        "dimension": dimension,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "constant": compiled.is_constant,
        "coverage": {
            "start": compiled.coverage_start.isoformat() if compiled.coverage_start else None,
            "end": compiled.coverage_end.isoformat() if compiled.coverage_end else None,
        },
        "period_complete": period_complete,
        "data_through": data_through,
        "compare": prev_meta,
        "order_by": order_by,
        "order": order,
        "top_n": top_n,
        "total_groups": total_groups,
        "rows": rows,
    }


def list_breakdown_dimensions(db: Session, *, metric_ref, user: User) -> dict:
    """候选拆解维度列（B9.2-1）：主数据集与一跳可达维度表的文本列（string/mixed），
    类型驱动、行业无关；基数经 DuckDB 现算 COUNT(DISTINCT) 供前端判定「低基数」。"""
    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)
    compiled = _compile_metric(db, metric)
    # load_datasets 返回 {name: DatasetInfo}（编译器同款装载路径）
    datasets = _load_datasets_for_query(db)
    primary = datasets[compiled.primary_dataset]
    relations = load_relations(db)

    involved: dict[str, set[str]] = {primary.name: set()}
    for col, typ in primary.columns.items():
        if typ in ("string", "mixed"):
            involved[primary.name].add(col)
    for rel in relations:
        if rel.from_dataset != primary.name:
            continue
        dim = datasets.get(rel.to_dataset)
        if dim is None:
            continue
        cols = involved.setdefault(dim.name, set())
        for col, typ in dim.columns.items():
            if typ in ("string", "mixed"):
                cols.add(col)

    involved = {name: cols for name, cols in involved.items() if cols}
    if not involved:
        return {
            "metric_id": metric.id, "metric_code": metric.code,
            "primary_dataset": primary.name, "dimensions": [],
        }

    rows = db.query(Dataset).filter(Dataset.name.in_(involved)).all()
    by_name = {r.name: r for r in rows}
    views = {name: view_target(by_name[name].parquet_path) for name in involved if name in by_name}
    out: list[dict] = []
    with duckdb_views(views) as con:
        for name, cols in involved.items():
            if name not in by_name:
                continue  # 关系指向的数据集已被删除：跳过该候选，不阻塞整表
            ordered = sorted(cols)
            sel = ", ".join(f"COUNT(DISTINCT {_quote_ident(c)})" for c in ordered)
            counts = con.execute(f"SELECT {sel} FROM {_quote_ident(name)}").fetchone()
            for col, cnt in zip(ordered, counts):
                cnt = int(cnt or 0)
                if cnt == 0:
                    continue  # 全空列（整列 NULL/空串）拆解无意义，不出现在候选中
                out.append({
                    "dataset": name,
                    "column": col,
                    "distinct_count": cnt,
                    "low_cardinality": cnt <= LOW_CARDINALITY_THRESHOLD,
                })
    out.sort(key=lambda d: (d["dataset"] != primary.name, d["distinct_count"], d["column"]))
    return {
        "metric_id": metric.id,
        "metric_code": metric.code,
        "primary_dataset": primary.name,
        "dimensions": out,
    }


MAX_DIMENSION_VALUES = 50   # 筛选值候选上限（超出截断并置 truncated=true）


def list_dimension_values(db: Session, *, metric_ref, column: str, user: User) -> dict:
    """指定维度列的去重值候选（B9.2-2）：供问数理解卡筛选值下拉与 LLM 筛选值校验。

    列越纲（不在数据集/无显式关系）由拆解编译器判定 400；值集合取自该指标
    覆盖区间内的真实数据（与拆解同一条 SQL，天然过滤不可解析组合）。
    """
    metric = _resolve_metric(db, metric_ref)
    _ensure_visible(db, user, metric)
    column = (column or "").strip()
    if not column:
        raise BusinessError("column（维度列名）必填", 40000)
    compiled = _compile_breakdown(db, metric, column, [])  # 兼作列合法性校验
    base = {"metric_id": metric.id, "metric_code": metric.code, "column": column}
    if compiled.is_constant or compiled.coverage_start is None or compiled.coverage_end is None:
        return {**base, "values": [], "truncated": False}

    runtime = _dataset_runtime(db, compiled)
    views = {name: view_target(info["parquet_path"]) for name, info in runtime.items()}
    params = {"__start__": compiled.coverage_start, "__end__": compiled.coverage_end}
    values: list[str] = []
    seen: set[str] = set()
    with duckdb_views(views) as con:
        rows = con.execute(compiled.sql + f" LIMIT {MAX_BREAKDOWN_ROWS}", params).fetchall()
        for r in rows:
            v = str(r[0])
            if v not in seen:
                seen.add(v)
                values.append(v)
                if len(values) > MAX_DIMENSION_VALUES:
                    break
    truncated = len(values) > MAX_DIMENSION_VALUES
    return {**base, "values": values[:MAX_DIMENSION_VALUES], "truncated": truncated}
