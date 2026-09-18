"""指标编译器（架构 7.2，B2 核心难点）。

compile(calc_rule, datasets, relations) -> CompiledQuery

- 输入：指标定义（受限 calc_rule）+ datasets（数据集列与覆盖区间）+ relations（显式表关系）
- 输出：DuckDB 方言参数化 SQL（时间范围为 $__start__ / $__end__ 绑定参数）+ 周期元信息
- 关系解析只允许 dataset_relations 显式注册的星型结构（不变式 6）：
  过滤列不在本表时，查「本表 → 维度表」一跳关系；找不到即拒绝并明确提示，禁止隐式推断
- 时间对齐：所有 operand 强制按各自数据集的 time_field 加同一时间范围条件，
  避免分子分母口径漂移；数据集无日期列或 time_field 显式置 null 的操作数
  不加时间过滤（全期常数），coverage 计入 None（2026-09-14「日期可选」）
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
    RelativeDate,
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
    """编译输入：显式注册的表关系（事实表 → 维度表，一跳）。

    P2 复合键：column_pairs 为完整列对列表 [[from, target], ...]（含首对），
    为空元组时退化为单列键（from_column/to_column）——旧数据零迁移兼容。
    """

    from_dataset: str
    from_column: str
    to_dataset: str
    to_column: str
    column_pairs: tuple[tuple[str, str], ...] = ()

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        return self.column_pairs or ((self.from_column, self.to_column),)


@dataclass
class CompiledQuery:
    """编译产物：参数化 SQL + 周期元信息（B3 统一计算服务消费）。"""

    sql: str
    primary_dataset: str
    primary_dataset_id: int
    dataset_names: list[str]                # 参与的数据集（去重，按出现顺序）
    time_fields: dict[str, str]             # 数据集名 -> 时间字段（空 = 全期常数指标）
    coverage_start: date | None             # 合并覆盖区间（周期完整性判定用）
    coverage_end: date | None
    grain: str = "day"                      # v1 编译粒度固定为 day（最细粒度）
    used_relations: list[dict] = field(default_factory=list)  # 可解释性：用到的表关系

    @property
    def is_constant(self) -> bool:
        """全期常数指标：无任何时间绑定——SQL 不含时间过滤，任意查询区间
        返回同一全期汇总值（数据集无日期列，或 time_field 显式置 null）。"""
        return not self.time_fields


# ---------------------------------------------------------------- 渲染辅助


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal_sql(value) -> str:
    if isinstance(value, RelativeDate):
        # $__today__ 由执行侧绑定计算当天日期；偏移渲染为固定 INTERVAL 文本，
        # SQL 仍确定性（缓存键另含 today 锚点，见 query 服务）
        if value.offset_days == 0:
            return "$__today__"
        sign = "+" if value.offset_days > 0 else "-"
        return f"($__today__ {sign} INTERVAL {abs(value.offset_days)} DAY)"
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


def _uses_today(operand: Operand) -> bool:
    """口径 filter 是否引用相对日期字面量（today）——缓存键须含当天锚点。"""
    return any(isinstance(c.literal, RelativeDate) for c in operand.filter)


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


def _resolve_cross_column(
    ds: DatasetInfo,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    column: str,
    context: str,
    what: str,
    joins: list[RelationInfo],
    joined: set[str],
    used_relations: list[dict],
) -> str:
    """跨表列解析（过滤列/拆解维度列共用，P1+P2）：

    1. 本表列直取（含列名本身带点的情形——精确匹配优先，限定名不抢）；
    2. `表名.列名` 限定名：在所有可能的拆分点中找「一跳可达维表 + 其列」的
       唯一解释（表名/列名含点时也能正确拆分）；多个解释 → 歧义拒绝；
    3. 纯列名一跳解析（原行为）：本表 → 维度表显式关系，必要时补 JOIN
       （与过滤 JOIN 共享 joined 去重）。多路径命中即歧义拒绝；
       找不到给出可行动提示。
    """
    if column in ds.columns:
        return f"{_q(ds.name)}.{_q(column)}"

    def _append_join(rel: RelationInfo) -> None:
        if rel.to_dataset not in joined:
            joins.append(rel)
            joined.add(rel.to_dataset)
            rec = {
                "from_dataset": rel.from_dataset, "to_dataset": rel.to_dataset,
                "from_column": rel.from_column, "to_column": rel.to_column,
            }
            if rel.column_pairs:
                rec["column_pairs"] = [list(p) for p in rel.column_pairs]
            used_relations.append(rec)

    # 限定名：`表名.列名`（一跳维表）
    if "." in column:
        qualified: list[tuple[RelationInfo, str, str]] = []  # (关系, 表名, 列名)
        for i in range(1, len(column)):
            left, right = column[:i], column[i + 1:]
            if not left or not right:
                continue
            for rel in relations:
                if rel.from_dataset != ds.name or rel.to_dataset != left:
                    continue
                dim = datasets.get(rel.to_dataset)
                if dim is not None and right in dim.columns:
                    qualified.append((rel, left, right))
        if len(qualified) == 1:
            rel, dname, cname = qualified[0]
            _append_join(rel)
            return f"{_q(dname)}.{_q(cname)}"
        if len(qualified) > 1:
            dup = ", ".join(sorted({d for _, d, _ in qualified}))
            raise CompileError(
                f"{context} 的{what} {column!r} 限定名有多种解释（{dup}），语义歧义，请改用不含点的列名或调整表名"
            )
        # 限定名无匹配：不静默退回纯列名（用户显式指定了表），直接报错
        raise CompileError(
            f"{context} 的{what} {column!r} 是限定名，但找不到 {ds.name} → 对应维表 的显式表关系"
            f"（或维表中无该列）。请先在数据集管理中登记表关系"
        )

    candidates = [
        r for r in relations
        if r.from_dataset == ds.name
        and r.to_dataset in datasets
        and column in datasets[r.to_dataset].columns
    ]
    if not candidates:
        raise CompileError(
            f"{context} 的{what} {column!r} 不在数据集 {ds.name!r} 中，"
            f"且找不到 {ds.name} → 维度表 的显式表关系。"
            f"请先在数据集管理中登记表关系，或改用本表文本列"
        )
    if len(candidates) > 1:
        dup = ", ".join(sorted({r.to_dataset for r in candidates}))
        raise CompileError(
            f"{context} 的{what} {column!r} 可通过多个表关系取得（{dup}），"
            f"语义歧义，请用 维表名.{column} 限定或拆分指标"
        )
    rel = candidates[0]
    _append_join(rel)
    return f"{_q(rel.to_dataset)}.{_q(column)}"


def _resolve_condition_columns(
    cond: FilterCondition,
    ds: DatasetInfo,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    context: str,
    joins: list[RelationInfo],
    joined: set[str],
    used_relations: list[dict],
) -> str:
    """单个过滤条件解析为全限定列引用；跨表列走显式一跳关系，必要时补 JOIN。
    joined 为同一子查询内共享的已 JOIN 维度表集合（过滤列与拆解维度列共用，
    防止同一维表被 JOIN 两次导致 SQL 表别名冲突）。"""
    ref = _resolve_cross_column(
        ds, datasets, relations, cond.column, context, "过滤列", joins, joined, used_relations
    )
    return _condition_sql(ref, cond)


def _resolve_filter_columns(
    operand: Operand,
    ds: DatasetInfo,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    context: str,
    joins: list[RelationInfo],
    used_relations: list[dict],
    joined: set[str] | None = None,
) -> list[str]:
    """把 filter 条件解析为全限定列引用；跨表列走显式一跳关系，必要时补 JOIN。
    joined 传入时与调用方共享（拆解子查询中维度列 JOIN 与过滤 JOIN 共用去重）。"""
    if joined is None:
        joined = set()
    return [
        _resolve_condition_columns(cond, ds, datasets, relations, context, joins, joined, used_relations)
        for cond in operand.filter
    ]


def _resolve_time_field(
    ds: DatasetInfo,
    explicit: str | None,
    context: str,
) -> str | None:
    """时间字段解析：显式指定（operand 级，或规则级命中本数据集）优先且严格校验；
    否则该数据集恰好只有一个日期列时自动采用；数据集没有任何日期列时返回 None
    （该操作数为全期常数，不按时间过滤）；多个日期列即拒绝（口径歧义，须显式
    指定日期列或以 time_field: null 声明全期常数）。"""
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
        return None  # 日期可选（2026-09-14）：无日期列 → 全期常数，不再拒绝
    if len(candidates) > 1:
        raise CompileError(
            f"{context}：数据集 {ds.name!r} 存在多个日期列 {candidates}，"
            f"请在 calc_rule 中显式指定 time_field（operand 级或规则级），"
            f"或置 time_field 为 null 声明全期常数（不按时间过滤），避免口径歧义"
        )
    return candidates[0]


_NUMERIC_TYPES = {"int", "float"}
_TEXTY_TYPES = {"string", "mixed"}


def _join_on_sql(
    rel: RelationInfo,
    datasets: dict[str, DatasetInfo],
) -> str:
    """关系 JOIN 的 ON 条件（P2 复合键：全部列对 AND 连接）。单列对两侧键类型
    不匹配（文本键 vs 数值键，常见于 Excel/CSV 接入）时对文本侧做 TRY_CAST——
    脏值转 NULL 自然不匹配，而不是整条 SQL 因 Binder 错误不可用。"""
    conds: list[str] = []
    for fk, pk in rel.pairs:
        left_ref = f"{_q(rel.from_dataset)}.{_q(fk)}"
        right_ref = f"{_q(rel.to_dataset)}.{_q(pk)}"
        lt = datasets[rel.from_dataset].columns.get(fk, "")
        rt = datasets[rel.to_dataset].columns.get(pk, "")
        if lt in _TEXTY_TYPES and rt in _NUMERIC_TYPES:
            left_ref = f"TRY_CAST({left_ref} AS DOUBLE)"
        elif rt in _TEXTY_TYPES and lt in _NUMERIC_TYPES:
            right_ref = f"TRY_CAST({right_ref} AS DOUBLE)"
        conds.append(f"{left_ref} = {right_ref}")
    return " AND ".join(conds)


def _operand_subquery(    operand: Operand,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    rule_time_field: str | None,
    context: str,
    used_relations: list[dict],
    force_no_time: bool = False,
) -> tuple[str, str, str | None, tuple[date, date] | None, list[str]]:
    """生成单个 operand 的聚合子查询。返回 (sql, dataset_name, time_field, coverage, joined_names)。

    时间字段优先级：规则级全期常数声明（force_no_time）> operand 级 no_time > operand 级
    time_field > 规则级 time_field（仅当该列存在于本数据集）> 自动解析唯一日期列；
    数据集无日期列或显式声明常数时 time_field 为 None——不加时间 WHERE，coverage 返回 None。
    joined_names 为跨表过滤列经关系 JOIN 的维度表名（无时间绑定，调用方须将其
    纳入 dataset_names，否则执行时不会创建对应视图）。
    """
    ds = _require_dataset(datasets, operand.table, context)
    column = _require_column(ds, operand.column, context, required=operand.aggregation != "count")
    _check_aggregation_type(operand, ds, context)

    joins: list[RelationInfo] = []
    where_clauses = _resolve_filter_columns(
        operand, ds, datasets, relations, context, joins, used_relations
    )

    cov: tuple[date, date] | None = None
    if force_no_time or operand.no_time:
        time_field = None  # 全期常数：不按时间过滤（规则级声明 / operand 级显式置 null）
    else:
        explicit = operand.time_field or (
            rule_time_field if rule_time_field in ds.columns else None
        )
        time_field = _resolve_time_field(ds, explicit, context)
    if time_field is not None:
        start, end = ds.coverage[time_field]
        cov = (start, end)
        # 时间过滤用半开区间 [__start__, __end__+1d)：datetime 列下 `<= __end__`
        # 会被解释为 `<= 当日 00:00:00`，把端点日的全部日间记录截掉（卡片整段
        # 聚合有值、逐日序列几乎全 null 的"卡片有值折线无端"矛盾即源于此）；
        # 纯 date 列下 < end+1d 与 <= end 语义等价，两种类型统一正确。
        where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} >= $__start__")
        where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} < ($__end__ + INTERVAL 1 DAY)")

    join_sql = "".join(
        f" JOIN {_q(rel.to_dataset)} ON {_join_on_sql(rel, datasets)}"
        for rel in joins
    )
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = (
        f"SELECT {_agg_sql(operand, f'{_q(ds.name)}.{_q(column)}' if column else None)} AS value "
        f"FROM {_q(ds.name)}{join_sql} {where_sql}"
    )
    return sql, ds.name, time_field, cov, [rel.to_dataset for rel in joins]


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

    # 规则级 time_field: null（全期常数声明）与 operand 级显式 time_field 互斥，
    # 同时出现说明口径自相矛盾，保存即拒绝
    if calc.no_time:
        offenders = (
            [calc.source] if calc.mode == "flat" else list(calc.operands.values())
        )
        conflicting = [op for op in offenders if op.time_field]
        if conflicting:
            raise CalcRuleError(
                "规则级 time_field 已置 null（全期常数，不按时间过滤），"
                "与操作数级 time_field 冲突；请二选一：删除操作数级 time_field，"
                "或去掉规则级 null 声明"
            )

    used_relations: list[dict] = []
    coverages: list[tuple[date, date]] = []
    time_fields: dict[str, str] = {}
    dataset_names: list[str] = []

    def track(name: str, tf: str | None, cov: tuple[date, date] | None) -> None:
        if name not in dataset_names:
            dataset_names.append(name)
        if tf is None:
            return  # 全期常数操作数：无时间绑定，不计覆盖区间
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
            calc.source, datasets, relations, effective_time_field, "source", used_relations,
            force_no_time=calc.no_time,
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


# ---------------------------------------------------------------- 拆解编译（B9.2-1）


@dataclass
class CompiledBreakdown:
    """拆解编译产物：每组一行 (dimension, value)，供维度拆解计算出口消费。

    与 CompiledQuery 同源（同一 parse/operand 解析/时间对齐/关系解析），
    仅把"整表聚合"换成"按维度列分组聚合"。比率类指标外层按组组合
    操作数值——每组 = 该组分子/该组分母（禁止对"率"求平均）。
    """

    sql: str
    primary_dataset: str
    primary_dataset_id: int
    dataset_names: list[str]
    time_fields: dict[str, str]
    coverage_start: date | None
    coverage_end: date | None
    dimension_column: str
    used_relations: list[dict] = field(default_factory=list)

    @property
    def is_constant(self) -> bool:
        return not self.time_fields


def _resolve_dimension_ref(
    ds: DatasetInfo,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    dimension_column: str,
    context: str,
    joins: list[RelationInfo],
    joined: set[str],
    used_relations: list[dict],
) -> str:
    """拆解维度列解析（P1）：本表文本列直取；`表名.列名` 限定名指定维表列；
    纯列名退回一跳关系解析。统一走 _resolve_cross_column（与过滤列同栈）。"""
    return _resolve_cross_column(
        ds, datasets, relations, dimension_column, context,
        "拆解维度列", joins, joined, used_relations,
    )


def _operand_group_subquery(
    operand: Operand,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    rule_time_field: str | None,
    dimension_column: str,
    extra_filters: list[FilterCondition],
    context: str,
    used_relations: list[dict],
    force_no_time: bool = False,
) -> tuple[str, str, str | None, tuple[date, date] | None, list[str]]:
    """分组版 operand 子查询：SELECT <维度> AS dimension, <聚合> AS value
    ... GROUP BY 维度。请求级过滤（extra_filters）与口径过滤同栈生效，且
    必须能被本 operand 的数据集解析（解析不了 → 拒绝，防止比率分子分母
    过滤口径漂移）。返回 (sql, dataset_name, time_field, coverage, joined_names)。"""
    ds = _require_dataset(datasets, operand.table, context)
    column = _require_column(ds, operand.column, context, required=operand.aggregation != "count")
    _check_aggregation_type(operand, ds, context)

    joins: list[RelationInfo] = []
    joined: set[str] = set()
    where_clauses = _resolve_filter_columns(
        operand, ds, datasets, relations, context, joins, used_relations, joined
    )
    for cond in extra_filters:
        where_clauses.append(
            _resolve_condition_columns(
                cond, ds, datasets, relations, context, joins, joined, used_relations
            )
        )

    dim_ref = _resolve_dimension_ref(
        ds, datasets, relations, dimension_column, context, joins, joined, used_relations
    )

    cov: tuple[date, date] | None = None
    if force_no_time or operand.no_time:
        time_field = None
    else:
        explicit = operand.time_field or (
            rule_time_field if rule_time_field in ds.columns else None
        )
        time_field = _resolve_time_field(ds, explicit, context)
    if time_field is not None:
        start, end = ds.coverage[time_field]
        cov = (start, end)
        # 半开区间（B7-fix6 契约）：与 _operand_subquery 完全一致
        where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} >= $__start__")
        where_clauses.append(f"{_q(ds.name)}.{_q(time_field)} < ($__end__ + INTERVAL 1 DAY)")

    join_sql = "".join(
        f" JOIN {_q(rel.to_dataset)} ON {_join_on_sql(rel, datasets)}"
        for rel in joins
    )
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = (
        f"SELECT {dim_ref} AS dimension, "
        f"{_agg_sql(operand, f'{_q(ds.name)}.{_q(column)}' if column else None)} AS value "
        f"FROM {_q(ds.name)}{join_sql} {where_sql} GROUP BY {dim_ref}"
    )
    return sql, ds.name, time_field, cov, [rel.to_dataset for rel in joins]


def compile_metric_breakdown(
    calc_rule: dict,
    datasets: dict[str, DatasetInfo],
    relations: list[RelationInfo],
    dimension_column: str,
    time_field: str | None = None,
    extra_filters: list[FilterCondition] | None = None,
) -> CompiledBreakdown:
    """编译维度拆解查询。与 compile_metric 同一受限文法与语义校验，差别仅在：
    operand 子查询按维度列分组聚合；表达式形态外层按组组合操作数值
    （比率 = 每组分子/分母，天然规避"对率求平均"）。结构错误抛
    CalcRuleError，语义错误抛 CompileError。"""
    if not isinstance(dimension_column, str) or not dimension_column.strip():
        raise CalcRuleError("拆解维度列 dimension 必须是非空字符串")
    dimension_column = dimension_column.strip()
    extra_filters = list(extra_filters or [])

    calc = parse_calc_rule(calc_rule)
    if time_field is not None and calc.time_field is not None:
        raise CalcRuleError("time_field 在 calc_rule 内已指定，不要重复传入")
    effective_time_field = calc.time_field or time_field
    if calc.no_time:
        offenders = (
            [calc.source] if calc.mode == "flat" else list(calc.operands.values())
        )
        conflicting = [op for op in offenders if op.time_field]
        if conflicting:
            raise CalcRuleError(
                "规则级 time_field 已置 null（全期常数，不按时间过滤），"
                "与操作数级 time_field 冲突；请二选一"
            )

    used_relations: list[dict] = []
    coverages: list[tuple[date, date]] = []
    time_fields: dict[str, str] = {}
    dataset_names: list[str] = []

    def track(name: str, tf: str | None, cov: tuple[date, date] | None) -> None:
        if name not in dataset_names:
            dataset_names.append(name)
        if tf is None:
            return
        prev = time_fields.get(name)
        if prev is not None and prev != tf:
            raise CompileError(
                f"数据集 {name} 在同一指标中被要求使用两个时间字段（{prev} / {tf}），口径歧义"
            )
        time_fields[name] = tf
        if cov is not None:
            coverages.append(cov)

    def track_joined(joined: list[str]) -> None:
        for name in joined:
            if name not in dataset_names:
                dataset_names.append(name)

    if calc.mode == "flat":
        sql, ds_name, tf, cov, joined = _operand_group_subquery(
            calc.source, datasets, relations, effective_time_field, dimension_column,
            extra_filters, "source", used_relations, force_no_time=calc.no_time,
        )
        track(ds_name, tf, cov)
        track_joined(joined)
        final_sql = sql
        primary = ds_name
    else:
        _validate_operand_names(calc, datasets)
        ctes: list[str] = []
        operand_sql: dict[str, str] = {}
        first_name = next(iter(calc.operands))
        for name, operand in calc.operands.items():
            sub, ds_name, tf, cov, joined = _operand_group_subquery(
                operand, datasets, relations, effective_time_field, dimension_column,
                extra_filters, f"operands.{name}", used_relations,
            )
            track(ds_name, tf, cov)
            track_joined(joined)
            ctes.append(f"{_q(name)} AS ({sub})")
            operand_sql[name] = f"{_q(name)}.value"
        # 每组组合（NULL 安全连接：维度值为 NULL 的组也能正确配对）；
        # 除法仍由 _render_expr 包 NULLIF——某组分母为 0 → 该组 value=null
        null_safe_ons = " AND ".join(
            f"{_q(first_name)}.dimension IS NOT DISTINCT FROM {_q(n)}.dimension"
            for n in calc.operands
            if n != first_name
        )
        final_sql = (
            f"WITH {', '.join(ctes)} "
            f"SELECT {_q(first_name)}.dimension AS dimension, "
            f"{_render_expr(calc.expression, operand_sql)} AS value "
            f"FROM {', '.join(_q(n) for n in calc.operands)}"
            + (f" WHERE {null_safe_ons}" if null_safe_ons else "")
        )
        primary = next(iter(calc.operands.values())).table

    primary_ds = datasets[primary]
    coverage_start = min(c[0] for c in coverages) if coverages else None
    coverage_end = max(c[1] for c in coverages) if coverages else None

    return CompiledBreakdown(
        sql=final_sql,
        primary_dataset=primary,
        primary_dataset_id=primary_ds.id,
        dataset_names=dataset_names,
        time_fields=time_fields,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        dimension_column=dimension_column,
        used_relations=used_relations,
    )


def calc_rule_signature(calc_rule: dict) -> str:
    """归一化签名校验辅助：去除空白差异，用于变更检测。"""
    import json

    return json.dumps(calc_rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AGGREGATIONS",
    "CompileError",
    "CompiledBreakdown",
    "CompiledQuery",
    "DatasetInfo",
    "RelationInfo",
    "calc_rule_signature",
    "compile_metric",
    "compile_metric_breakdown",
]
