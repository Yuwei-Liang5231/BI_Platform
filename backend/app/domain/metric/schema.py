"""calc_rule 受限结构校验（架构 7.2）。

指标计算规则不写自然语言，用受限结构表达；超出 v1 算子清单的一律在保存时拒绝
（CalcRuleError），并给出面向用户的明确报错。

两种合法形态（二选一）：

1. 扁平聚合：
   {"base_aggregation": "sum",
    "source": {"table": "orders", "column": "amount", "filter": "status = 'paid'"},
    "time_field": "order_date"}          # 可选：多个日期列时必须显式指定

2. 子聚合四则运算（比率类）：
   {"expression": "A / B",
    "operands": {"A": {"table": "orders", "column": "amount", "aggregation": "sum",
                        "filter": "status = 'paid'"},
                  "B": {"table": "orders", "column": "user_id", "aggregation": "count_distinct"}}}

time_field 语义（2026-09-14 起「日期可选」）：
- 规则级 "time_field"（顶层）：扁平形态下必须为目标数据集的日期列（严格）；
  表达式形态下作为默认值——仅应用于拥有该列的数据集，其余数据集自动解析唯一日期列。
- 规则级 "time_field": null（显式置空）：声明「全期常数指标」——所有操作数
  不按时间过滤，任意查询区间返回同一全期汇总值（与 operand 级 time_field 互斥）。
- operand 级 "time_field"：严格指定该 operand 数据集的日期列（跨表比率中
  各表日期列名不同时使用）；置 null 表示该操作数不按时间过滤（全期常数）。
- 数据集没有任何日期列：自动视为全期常数（不报错）；
  多个日期列且未显式指定时仍编译拒绝，避免口径歧义。

filter 文法（合取范式，v1 算子清单）：
    condition (AND condition)*
    condition := column ( = | != | <> | > | >= | < | <= ) literal
               | column IS [NOT] NULL
    literal   := 数字 | '字符串' | true | false | today | today±N d
    column    := 标识符 | "带空格/特殊字符的列名"（双引号，"" 转义）

相对日期字面量（2026-09-16）：today（计算当天）、today+N d / today-N d
（偏移 N 天，如 today+15d）。不加引号（与字符串区分）。编译为 $__today__
命名参数，执行时绑定计算当天日期——值随自然日滚动（时点性指标）。
它不是 SQL 函数/表达式，只是受限文法内的相对字面量，红线不破。

明确不支持（保存即拒绝）：OR / NOT / 括号分组、嵌套聚合、窗口函数、
自定义 SQL 片段、group_by/cohort 等任何结构外字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

AGGREGATIONS = ("sum", "count", "count_distinct", "avg", "max", "min")
NUMERIC_ONLY = ("sum", "avg")

ALLOWED_TOP_KEYS = {"base_aggregation", "source", "expression", "operands", "time_field"}
ALLOWED_SOURCE_KEYS = {"table", "column", "filter", "time_field"}
ALLOWED_OPERAND_KEYS = {"table", "column", "aggregation", "filter", "time_field"}

_COMPARISON_OPS = ("<=", ">=", "!=", "<>", "=", ">", "<")


class CalcRuleError(ValueError):
    """calc_rule 结构/文法非法。message 面向用户，保存即拒绝。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class RelativeDate:
    """相对日期字面量：today（计算当天）或 today±N d（偏移 N 天）。

    编译为 $__today__ 命名参数（执行时绑定计算当天日期），指标值随自然日
    滚动（时点性指标）。它不是 SQL 函数/表达式，只是受限文法内的相对
    字面量——保存即拒绝的红线（禁函数/自定义 SQL）不破。"""

    offset_days: int = 0

    def render(self) -> str:
        """还原为文法文本（round-trip：FilterCondition → 字符串）。"""
        if self.offset_days == 0:
            return "today"
        sign = "+" if self.offset_days > 0 else "-"
        return f"today{sign}{abs(self.offset_days)}d"


@dataclass
class FilterCondition:
    column: str
    op: str  # = / != / > / >= / < / <= / is_null / is_not_null
    literal: str | int | float | bool | RelativeDate | None = None


@dataclass
class Operand:
    table: str
    column: str | None
    aggregation: str
    filter: list[FilterCondition] = field(default_factory=list)
    time_field: str | None = None
    no_time: bool = False  # time_field 显式置 null：该操作数不按时间过滤（全期常数）


# 表达式 AST：("name", "A") 或 ("binop", op, left, right)
ExprNode = tuple


@dataclass
class CalcRule:
    mode: str  # flat / expression
    base_aggregation: str | None = None
    source: Operand | None = None
    expression: ExprNode | None = None
    operands: dict[str, Operand] = field(default_factory=dict)
    time_field: str | None = None
    no_time: bool = False  # 规则级 time_field 显式置 null：整条指标为全期常数


# ---------------------------------------------------------------- 词法


_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>'(?:[^']|'')*')
      | (?P<qident>"(?:[^"]|"")*")
      | (?P<ident>[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)
      | (?P<op><>|!=|>=|<=|=|>|<|\+|\-|\*|/|\(|\))
    )""",
    re.VERBOSE,
)

_KEYWORDS = {"and", "is", "not", "null", "true", "false"}


def _tokenize(text: str, context: str) -> list[tuple[str, str]]:
    """返回 [(kind, value)]，kind ∈ number/string/qident/ident/op。
    qident 为双引号列名（Excel/CSV 接入的列常含空格，如 "Project Name"）。"""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == m.start():
            rest = text[pos:].strip()
            raise CalcRuleError(
                f"{context} 存在无法解析的内容：{rest[:40]!r}。"
                f"filter 仅支持『字段 比较符 值』用 AND 连接；不支持 OR / NOT / 括号 / 函数"
            )
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "qident":
            tokens.append(("ident", value[1:-1].replace('""', '"')))
        elif kind == "ident" and value.lower() in _KEYWORDS:
            tokens.append(("keyword", value.lower()))
        else:
            tokens.append((kind, value))
    return tokens


def _unquote_string(raw: str) -> str:
    return raw[1:-1].replace("''", "'")


def _parse_relative_date(
    tokens: list[tuple[str, str]], i: int, column: str
) -> tuple[RelativeDate, int]:
    """解析相对日期字面量。tokens[i] 为 today 记号（大小写不敏感）。
    三种形态：today / today+N d（op+number）/ today-N d（词法把无空格的
    "-N" 并进 number，故负偏移直接表现为负数 number）。返回
    (RelativeDate, 下一个未消费索引)；写法不完整即 CalcRuleError。"""
    err = (
        f"字段 {column} 的相对日期写法不完整：应为 today、today+N d 或 today-N d"
        f"（N 为整数天数，如 today+15d）"
    )
    j = i + 1
    if j >= len(tokens):
        return RelativeDate(0), j
    k2, v2 = tokens[j]
    if k2 == "number":
        if not v2.startswith("-"):
            raise CalcRuleError(err)  # `today 15 d`：缺符号
        offset = int(v2)  # today-15d（无空格，词法合并为负数 number）
        if "." in v2:
            raise CalcRuleError(err)
        j += 1
    elif k2 == "op" and v2 in ("+", "-"):
        j += 1
        if j >= len(tokens) or tokens[j][0] != "number":
            raise CalcRuleError(err)
        num = tokens[j][1]
        if "." in num or int(num) < 0:
            raise CalcRuleError(err)
        offset = -int(num) if v2 == "-" else int(num)
        j += 1
    else:
        return RelativeDate(0), j  # 纯 today（后接 AND / 条件结束等）
    if j >= len(tokens) or tokens[j][0] != "ident" or tokens[j][1].lower() != "d":
        raise CalcRuleError(err)
    return RelativeDate(offset), j + 1


# ---------------------------------------------------------------- filter 文法


def parse_filter(text: str) -> list[FilterCondition]:
    """解析合取范式过滤条件。空串/纯空白 → 无条件。"""
    if text is None or not text.strip():
        return []
    tokens = _tokenize(text, "filter")
    conditions: list[FilterCondition] = []
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind != "ident":
            raise CalcRuleError(f"filter 第 {i + 1} 个记号应为字段名，实际是 {value!r}")
        column = value
        i += 1
        if i < len(tokens) and tokens[i] == ("keyword", "is"):
            i += 1
            negated = False
            if i < len(tokens) and tokens[i] == ("keyword", "not"):
                negated = True
                i += 1
            if i >= len(tokens) or tokens[i] != ("keyword", "null"):
                raise CalcRuleError(f"IS 后应为 NULL（字段 {column}）")
            i += 1
            conditions.append(FilterCondition(column, "is_not_null" if negated else "is_null"))
        else:
            if i >= len(tokens) or tokens[i][0] != "op" or tokens[i][1] not in _COMPARISON_OPS:
                raise CalcRuleError(
                    f"字段 {column} 后应为比较符（= != <> > >= < <= 或 IS NULL），"
                    f"实际是 {tokens[i][1] if i < len(tokens) else '条件结束'!r}"
                )
            op = tokens[i][1]
            i += 1
            if i >= len(tokens):
                raise CalcRuleError(f"比较符 {op} 后缺少比较值（字段 {column}）")
            lkind, lvalue = tokens[i]
            if lkind == "ident" and lvalue.lower() == "today":
                literal, i = _parse_relative_date(tokens, i, column)
            elif lkind == "number":
                literal: str | float = float(lvalue) if "." in lvalue else int(lvalue)
                i += 1
            elif lkind == "string":
                literal = _unquote_string(lvalue)
                i += 1
            elif lkind == "keyword" and lvalue in ("true", "false"):
                literal = lvalue == "true"
                i += 1
            elif lkind == "keyword" and lvalue == "null":
                raise CalcRuleError(f"不支持 {column} = NULL 写法，请使用 {column} IS NULL")
            else:
                raise CalcRuleError(
                    f"字段 {column} 的比较值应为数字、'字符串'、true/false 或 today±N d，"
                    f"实际是 {lvalue!r}"
                )
            conditions.append(FilterCondition(column, "!=" if op == "<>" else op, literal))
        if i < len(tokens):
            if tokens[i] == ("keyword", "and"):
                i += 1
            else:
                raise CalcRuleError(
                    f"多个过滤条件只能用 AND 连接，{tokens[i][1]!r} 不被支持"
                    f"（v1 不支持 OR / NOT / 括号分组）"
                )
    if not conditions:
        raise CalcRuleError("filter 不能为空")
    return conditions


# ---------------------------------------------------------------- expression 文法


def _parse_expression_tokens(tokens: list[tuple[str, str]]) -> ExprNode:
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def parse_additive() -> ExprNode:
        nonlocal pos
        node = parse_multiplicative()
        while peek() is not None and peek()[0] == "op" and peek()[1] in ("+", "-"):
            op = tokens[pos][1]
            pos += 1
            node = ("binop", op, node, parse_multiplicative())
        return node

    def parse_multiplicative() -> ExprNode:
        nonlocal pos
        node = parse_factor()
        while peek() is not None and peek()[0] == "op" and peek()[1] in ("*", "/"):
            op = tokens[pos][1]
            pos += 1
            node = ("binop", op, node, parse_factor())
        return node

    def parse_factor() -> ExprNode:
        nonlocal pos
        if peek() is None:
            raise CalcRuleError("expression 不完整")
        kind, value = tokens[pos]
        if kind == "ident":
            pos += 1
            return ("name", value)
        if kind == "op" and value == "(":
            pos += 1
            node = parse_additive()
            if peek() is None or peek() != ("op", ")"):
                raise CalcRuleError("expression 括号不匹配")
            pos += 1
            return node
        raise CalcRuleError(
            f"expression 中出现非法记号 {value!r}：只能引用 operands 中定义的操作数名，"
            f"用 + - * / ( ) 组合；不支持数字常量、函数与嵌套聚合"
        )

    node = parse_additive()
    if pos != len(tokens):
        raise CalcRuleError(f"expression 存在无法解析的多余内容：{tokens[pos][1]!r}")
    return node


# ---------------------------------------------------------------- 结构解析


def _parse_operand(raw: dict, context: str) -> Operand:
    if not isinstance(raw, dict):
        raise CalcRuleError(f"{context} 必须是对象")
    unknown = set(raw) - ALLOWED_OPERAND_KEYS
    if unknown:
        raise CalcRuleError(
            f"{context} 含不支持的字段：{sorted(unknown)}。"
            f"允许字段：{sorted(ALLOWED_OPERAND_KEYS)}。"
            f"若需要分组/cohort/窗口等能力，属于阶段 4a 扩展算子，v1 明确不支持"
        )
    table = raw.get("table")
    if not isinstance(table, str) or not table.strip():
        raise CalcRuleError(f"{context}.table 必须是非空字符串")
    column = raw.get("column")
    if column is not None and (not isinstance(column, str) or not column.strip()):
        raise CalcRuleError(f"{context}.column 必须是非空字符串或省略")
    aggregation = raw.get("aggregation")
    if aggregation not in AGGREGATIONS:
        raise CalcRuleError(
            f"{context}.aggregation 必须是 {list(AGGREGATIONS)} 之一，实际是 {aggregation!r}"
        )
    tf = raw.get("time_field")
    if tf is not None and (not isinstance(tf, str) or not tf.strip()):
        raise CalcRuleError(
            f"{context}.time_field 必须是非空字符串或 null（null = 该操作数不按时间过滤，全期常数）"
        )
    return Operand(
        table=table.strip(),
        column=column.strip() if isinstance(column, str) else None,
        aggregation=aggregation,
        filter=parse_filter(raw.get("filter") or ""),
        time_field=tf.strip() if isinstance(tf, str) else None,
        no_time="time_field" in raw and raw["time_field"] is None,
    )


def parse_calc_rule(raw: dict) -> CalcRule:
    """解析并校验 calc_rule 结构。结构非法抛 CalcRuleError。"""
    if not isinstance(raw, dict):
        raise CalcRuleError("calc_rule 必须是 JSON 对象")
    unknown = set(raw) - ALLOWED_TOP_KEYS
    if unknown:
        raise CalcRuleError(
            f"calc_rule 含不支持的字段：{sorted(unknown)}。允许字段：{sorted(ALLOWED_TOP_KEYS)}。"
            f"若需要分组/cohort/窗口函数等能力，属于阶段 4a 扩展算子，v1 明确不支持"
        )

    time_field = raw.get("time_field")
    if time_field is not None and (not isinstance(time_field, str) or not time_field.strip()):
        raise CalcRuleError(
            "time_field 必须是非空字符串或 null（null = 全期常数指标，不按时间过滤）"
        )
    # time_field 键显式存在且为 null → 全期常数声明；键缺失 → 维持自动解析语义
    rule_no_time = "time_field" in raw and raw["time_field"] is None

    has_flat = "base_aggregation" in raw or "source" in raw
    has_expr = "expression" in raw or "operands" in raw
    if has_flat and has_expr:
        raise CalcRuleError("calc_rule 不能同时是扁平聚合（base_aggregation/source）与表达式（expression/operands）")
    if not has_flat and not has_expr:
        raise CalcRuleError(
            "calc_rule 必须提供 base_aggregation + source（扁平聚合）或 expression + operands（比率类）"
        )

    if has_flat:
        if "base_aggregation" not in raw or "source" not in raw:
            raise CalcRuleError("扁平聚合形态必须同时提供 base_aggregation 与 source")
        if raw["base_aggregation"] not in AGGREGATIONS:
            raise CalcRuleError(
                f"base_aggregation 必须是 {list(AGGREGATIONS)} 之一，实际是 {raw['base_aggregation']!r}"
            )
        source = raw["source"]
        if not isinstance(source, dict):
            raise CalcRuleError("source 必须是对象")
        unknown = set(source) - ALLOWED_SOURCE_KEYS
        if unknown:
            raise CalcRuleError(f"source 含不支持的字段：{sorted(unknown)}。允许字段：{sorted(ALLOWED_SOURCE_KEYS)}")
        # 仅当 source 显式携带 time_field 时透传（键缺失 ≠ 显式 null，
        # 否则所有扁平指标都会被误判为全期常数）
        source_operand_raw = {
            "table": source.get("table"), "column": source.get("column"),
            "aggregation": raw["base_aggregation"], "filter": source.get("filter"),
        }
        if "time_field" in source:
            source_operand_raw["time_field"] = source.get("time_field")
        operand = _parse_operand(source_operand_raw, "source")
        if operand.column is None:
            raise CalcRuleError("source.column 必填（count 需要计数目标；如需 COUNT(*) 请用 operand 形态的 count 且省略 column）")
        return CalcRule(
            mode="flat",
            base_aggregation=raw["base_aggregation"],
            source=operand,
            time_field=time_field.strip() if isinstance(time_field, str) else None,
            no_time=rule_no_time,
        )

    if "expression" not in raw or "operands" not in raw:
        raise CalcRuleError("表达式形态必须同时提供 expression 与 operands")
    if not isinstance(raw["operands"], dict) or not raw["operands"]:
        raise CalcRuleError("operands 必须是非空对象")
    operands = {
        name: _parse_operand(defn, f"operands.{name}") for name, defn in raw["operands"].items()
    }
    expr = _parse_expression_tokens(_tokenize(raw["expression"], "expression"))

    # 表达式引用校验 + 引用图完整性（防止漏定义/漏使用造成语义歧义）
    referenced: set[str] = set()

    def walk(node: ExprNode) -> None:
        if node[0] == "name":
            if node[1] not in operands:
                raise CalcRuleError(f"expression 引用了未定义的操作数 {node[1]!r}（已定义：{list(operands)}）")
            referenced.add(node[1])
        else:
            walk(node[2])
            walk(node[3])

    walk(expr)
    unused = set(operands) - referenced
    if unused:
        raise CalcRuleError(f"operands 中定义了 expression 未引用的操作数：{sorted(unused)}")

    return CalcRule(
        mode="expression",
        expression=expr,
        operands=operands,
        time_field=time_field.strip() if isinstance(time_field, str) else None,
        no_time=rule_no_time,
    )
