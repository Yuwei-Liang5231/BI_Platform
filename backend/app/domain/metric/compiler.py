"""指标编译器（架构 7.2，B2 核心难点）。

compile(calc_rule, datasets, relations) -> CompiledQuery

- 输入：指标定义（受限 calc_rule）+ datasets（数据集列与覆盖区间）+ relations（显式表关系）
- 输出：DuckDB 方言参数化 SQL（时间范围为 $__start__ / $__end__ 绑定参数）+ 周期元信息
- 关系解析只允许 dataset_relations 显式注册的星型结构（不变式 6）：
  过滤列不在本表时，查「本表 → 维度表」一跳关系；找不到即拒绝并明确提示，禁止隐式推断
- 时间对齐：所有 operand 强制按各自数据集的 time_field 加同一时间范围条件，
  避免分子分母口径漂移
- 行业无关：本模块无任何行业字段/分支（不变式 7），A/B 两套数据集交叉验证

不支持（保存即拒绝，见 schema.py）：cohort、窗口函数、嵌套聚合、自定义 SQL、
多跳/多事实表关联（阶段 4a 扩展）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.domain.metric.schema import (
    AGGREGATIONS,
    NUMERIC_ONLY,
    CalcRule,
    CalcRuleError,
    FilterCondition,
    Operand,
    parse_calc_rule,
)

# 编译期语义错误（针对当前数据集/表关系状态），保存即拒绝
_RESERVED_OPERAND_NAMES = {"__start__", "__end__"}


class CompileError(ValueError):
    """编译失败：数据集/列/表关系不满足。message 面向用户。"""


@dataclass
class DatasetInfo:
    """编译输入：一个数据集的列类型与日期覆盖区间（服务层从元数据表装载）。"""

    id: int
    name: str
    columns: dict[str, str]                 # 列名 -> 逻辑类型 int/float/date/datetime/bool/string/mixed
    coverage: dict[str, tuple[date, date]]  # 日期列 -> (最小, 最大)


@dataclass
class RelationInfo:
    """编译输入：显式注册的表关系（事实表 → 维度表，一跳）。"""

    from_dataset: str
    from_column: str
    to_dataset: str
    to_column: str


@dataclass
class CompiledQuery:
    """编译产物：参数化 SQL + 周期元信息（B3 统一计算服务消费）。"""

    sql: str
    primary_dataset: str
    primary_dataset_id: int
    dataset_names: list[str]                # 参与的数据集（去重，按出现顺序）
    time_fields: dict[str, str]             # 数据集名 -> 时间字段
    coverage_start: date | None             # 合并覆盖区间（周期完整性判定用）
    coverage_end: date | None
    grain: str = "day"                      # v1 编译粒度固定为 day（最细粒度）
    used_relations: list[dict] = field(default_factory=list)  # 可解释性：用到的表关系


# ---------------------------------------------------------------- 渲染辅助


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal_sql(value) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _condition_sql(column_ref: str, cond: FilterCondition) -> str:
    if cond.op == "is_null":
        return f"{column_ref} IS NULL"
    if cond.op == "is_not_null":
        return f"{column_ref} IS NOT NULL"
    return f"{column_ref} {cond.op} {_literal_sql(cond.literal)}"


def _agg_sql(operand: Operand, column_ref: str) -> str:
    agg = operand.aggregation
    if agg == "count":
        return f"COUNT(*)" if operand.column is None else f"COUNT({column_ref})"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {column_ref})"
    func = {"sum": "SUM", "avg": "AVG", "max": "MAX", "min": "MIN"}[agg]
    return f"{func}({column_ref})"


def _render_expr(node, operand_sql: dict[str, str]) -> str:
    """表达式 AST → SQL。除法用 NULLIF 包裹，分母为 0 时返回 NULL 而非报错。"""
    if node[0] == "name":
        return operand_sql[node[1]]
    _, op, left, right = node
    l_sql = _render_expr(left, operand_sql)
    r_sql = _render_expr(right, operand_sql)
    if op == "/":
        return f"(({l_sql}) / NULLIF(({r_sql}), 0))"
    return f"(({l_sql}) {op} ({r_sql}))"


# ---------------------------------------------------------------- 语义解析


def _require_dataset(datasets: dict[str, DatasetInfo], name: str, context: str) -> DatasetInfo:
    ds = datasets.get(name)
    if ds is None:
        known = ", ".join(sorted(datasets)) or "（当前无数据集）"
        raise CompileError(f"{context} 引用的数据集 {name!r} 不存在。已注册的数据集：{known}")
    return ds


def _require_column(ds: DatasetInfo, column: str | None, context: str, *, required: bool) -> str | None:
    if column is None:
        if required:
            raise CompileError(f"{context} 缺少 column")
        return None
    if column not in ds.columns:
        raise CompileError(
            f"{context} 引用的列 {column!r} 在数据集 {ds.name!r} 中不存在。"
            f"现有列：{sorted(ds.columns)}"
        )
    return column


def _check_aggregation_type(operand: Operand, ds: DatasetInfo, context: str) -> None:
    if operand.aggregation in NUMERIC_ONLY and operand.column is not None:
        col_type = ds.columns.get(operand.column, "")
        if col_type not in ("int", "float"):
            raise CompileError(
                f"{context} 的 {operand.aggregation} 只能用于数值列（int/float），"
                f"而 {ds.name}.{operand.column} 的类型是 {col_type!r}"
            )


def _resolve_filter_columns(
    operand: Operand,
    ds: DatasetInfo,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    context: str,
    joins: list[tuple[str, str, str]],
    used_relations: list[dict],
) -> list[str]:
    """把 filter 条件解析为全限定列引用；跨表列走显式一跳关系，必要时补 JOIN。"""
    clauses: list[str] = []
    joined: set[str] = set()
    for cond in operand.filter:
        if cond.column in ds.columns:
            clauses.append(_condition_sql(f"{_q(ds.name)}.{_q(cond.column)}", cond))
            continue
        # 一跳星型解析：本表(事实) → 维度表
        candidates = [
            r for r in relations
            if r.from_dataset == ds.name
            and r.to_dataset in datasets
            and cond.column in datasets[r.to_dataset].columns
        ]
        if not candidates:
            raise CompileError(
                f"{context} 的过滤列 {cond.column!r} 不在数据集 {ds.name!r} 中，"
                f"且找不到 {ds.name} → 维度表 的显式表关系。"
                f"请先在数据集管理中登记表关系（编译器禁止隐式推断关联键）"
            )
        if len(candidates) > 1:
            dup = ", ".join(sorted({r.to_dataset for r in candidates}))
            raise CompileError(
                f"{context} 的过滤列 {cond.column!r} 可通过多个表关系取得（{dup}），语义歧义，请拆分指标"
            )
        rel = candidates[0]
        if rel.to_dataset not in joined:
            joins.append((rel.to_dataset, rel.from_column, rel.to_column))
            joined.add(rel.to_dataset)
            used_relations.append(
                {"from_dataset": rel.from_dataset, "from_column": rel.from_column,
                 "to_dataset": rel.to_dataset, "to_column": rel.to_column}
            )
        clauses.append(_condition_sql(f"{_q(rel.to_dataset)}.{_q(cond.column)}", cond))
    return clauses


def _resolve_time_field(
    ds: DatasetInfo,
    explicit: str | None,
    context: str,
) -> str:
    """时间字段解析：显式指定（operand 级，或规则级命中本数据集）优先且严格校验；
    否则该数据集恰好只有一个日期列时自动采用，多个日期列即拒绝。"""
    if explicit is not None:
        if explicit not in ds.columns:
            raise CompileError(
                f"{context} 指定的 time_field {explicit!r} 在数据集 {ds.name!r} 中不存在"
            )
        if ds.columns[explicit] not in ("date", "datetime"):
            raise CompileError(
                f"{context} 指定的 time_field {explicit!r} 不是日期列（实际类型 "
                f"{ds.columns[explicit]!r}）"
            )
        if explicit not in ds.coverage:
            raise CompileError(
                f"{context} 指定的 time_field {explicit!r} 没有覆盖区间登记"
                f"（数据集接入时未识别到该列的日期区间）"
            )
        return explicit
    candidates = sorted(ds.coverage)
    if not candidates:
        raise CompileError(
            f"{context}：数据集 {ds.name!r} 没有日期列覆盖区间登记，无法按周期过滤。"
            f"请重新接入数据集或为日期列补充覆盖信息"
        )
    if len(candidates) > 1:
        raise CompileError(
            f"{context}：数据集 {ds.name!r} 存在多个日期列 {candidates}，"
            f"请在 calc_rule 中显式指定 time_field（operand 级或规则级），避免口径歧义"
        )
    return candidates[0]


def _operand_subquery(
    operand: Operand,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    rule_time_field: str | None,
    context: str,
    used_relations: list[dict],
) -> tuple[str, str, str, tuple[date, date] | None, list[str]]:
    """生成单个 operand 的聚合子查询。返回 (sql, dataset_name, time_field, coverage, joined_names)。

    时间字段优先级：operand 级 > 规则级（仅当该列存在于本数据集）> 自动解析唯一日期列。
    joined_names 为跨表过滤列经关系 JOIN 的维度表名（无时间绑定，调用方须将其
    纳入 dataset_names，否则执行时不会创建对应视图）。
    """
    ds = _require_dataset(datasets, operand.table, context)
    column = _require_column(ds, operand.column, context, required=operand.aggregation != "count")
    _check_aggregation_type(operand, ds, context)

    joins: list[tuple[str, str, str]] = []
    where_clauses = _resolve_filter_columns(
        operand, ds, datasets, relations, context, joins, used_relations
    )

    explicit = operand.time_field or (
        rule_time_field if rule_time_field in ds.columns else None
    )
    time_field = _resolve_time_field(ds, explicit, context)
    start, end = ds.coverage[time_field]
    where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} >= $__start__")
    where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} <= $__end__")

    join_sql = "".join(
        f" JOIN {_q(dim)} ON {_q(ds.name)}.{_q(fk)} = {_q(dim)}.{_q(pk)}"
        for dim, fk, pk in joins
    )
    sql = (
        f"SELECT {_agg_sql(operand, f'{_q(ds.name)}.{_q(column)}' if column else None)} AS value "
        f"FROM {_q(ds.name)}{join_sql} "
        f"WHERE {' AND '.join(where_clauses)}"
    )
    return sql, ds.name, time_field, (start, end), [dim for dim, _, _ in joins]


def _validate_operand_names(calc: CalcRule, datasets: dict[str, DatasetInfo]) -> None:
    for name in calc.operands:
        if name.lower() in _RESERVED_OPERAND_NAMES:
            raise CalcRuleError(f"操作数名 {name!r} 是编译器保留字，请改名")
        if name in datasets:
            raise CalcRuleError(
                f"操作数名 {name!r} 与数据集名冲突，会导致 SQL 中 CTE 与表名歧义，请改名"
            )


def compile_metric(
    calc_rule: dict,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    time_field: str | None = None,
) -> CompiledQuery:
    """编译指标定义为 DuckDB 参数化 SQL。结构错误抛 CalcRuleError，语义错误抛 CompileError。"""
    calc = parse_calc_rule(calc_rule)
    if time_field is not None and calc.time_field is not None:
        raise CalcRuleError("time_field 在 calc_rule 内已指定，不要重复传入")
    effective_time_field = calc.time_field or time_field

    used_relations: list[dict] = []
    coverages: list[tuple[date, date]] = []
    time_fields: dict[str, str] = {}
    dataset_names: list[str] = []

    def track(name: str, tf: str, cov: tuple[date, date] | None) -> None:
        if name not in dataset_names:
            dataset_names.append(name)
        prev = time_fields.get(name)
        if prev is not None and prev != tf:
            raise CompileError(
                f"数据集 {name} 在同一指标中被要求使用两个时间字段（{prev} / {tf}），口径歧义"
            )
        time_fields[name] = tf
        if cov is not None:
            coverages.append(cov)

    def track_joined(joined: list[str]) -> None:
        """跨表过滤 JOIN 的维度表：纳入 dataset_names（供执行时建视图），无时间绑定。"""
        for name in joined:
            if name not in dataset_names:
                dataset_names.append(name)

    if calc.mode == "flat":
        src_ds = _require_dataset(datasets, calc.source.table, "source")
        # 扁平形态下规则级 time_field 是唯一指定途径，必须严格命中，防止拼写错误被静默忽略
        if calc.time_field is not None and calc.time_field not in src_ds.columns:
            raise CompileError(
                f"source 指定的 time_field {calc.time_field!r} 在数据集 {calc.source.table!r} 中不存在"
            )
        sql, ds_name, tf, cov, joined = _operand_subquery(
            calc.source, datasets, relations, effective_time_field, "source", used_relations
        )
        track(ds_name, tf, cov)
        track_joined(joined)
        final_sql = sql
        primary = ds_name
    else:
        _validate_operand_names(calc, datasets)
        ctes: list[str] = []
        operand_sql: dict[str, str] = {}
        for name, operand in calc.operands.items():
            sub, ds_name, tf, cov, joined = _operand_subquery(
                operand, datasets, relations, effective_time_field, f"operands.{name}", used_relations
            )
            track(ds_name, tf, cov)
            track_joined(joined)
            ctes.append(f"{_q(name)} AS ({sub})")
            operand_sql[name] = f"{_q(name)}.value"
        final_sql = (
            f"WITH {', '.join(ctes)} "
            f"SELECT {_render_expr(calc.expression, operand_sql)} AS value "
            f"FROM {', '.join(_q(n) for n in calc.operands)}"
        )
        primary = next(iter(calc.operands.values())).table

    primary_ds = datasets[primary]
    coverage_start = min(c[0] for c in coverages) if coverages else None
    coverage_end = max(c[1] for c in coverages) if coverages else None

    return CompiledQuery(
        sql=final_sql,
        primary_dataset=primary,
        primary_dataset_id=primary_ds.id,
        dataset_names=dataset_names,
        time_fields=time_fields,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        used_relations=used_relations,
    )


def calc_rule_signature(calc_rule: dict) -> str:
    """归一化签名校验辅助：去除空白差异，用于变更检测。"""
    import json

    return json.dumps(calc_rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AGGREGATIONS",
    "CompileError",
    "CompiledQuery",
    "DatasetInfo",
    "RelationInfo",
    "calc_rule_signature",
    "compile_metric",
]
