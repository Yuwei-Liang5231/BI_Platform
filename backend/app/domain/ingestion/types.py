"""类型推断与值分类（B1 最小质检）。

判定规则（对每个去空格后的值）：
- 空串 / 字面量 NULL（大小写不敏感，ETL 导出常见）→ null
- ISO 日期时间 yyyy-MM-dd[ T]HH:MM(:SS(.ffffff)?)（可解析）→ datetime
- ISO 日期 yyyy-MM-dd（可解析）→ date
- 整数（**前导零视为字符串**，保护工单号/编码类 ID）→ int
- 浮点 → float
- true/false（大小写不敏感）→ bool
- 其余 → string

列级：非空值类型集合 > 1 → mixed（存储为 string，并标记 mixed=true）；
date/datetime 同列视为同族，统一为 datetime；int/float 同列统一为 float。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.response import BusinessError

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")
# 日期/时间：ISO（2025-09-30）与中文 Excel 常见斜杠格式（2025/9/30）都支持
DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
DATETIME_RE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(:(\d{2})(\.\d{1,6})?)?$"
)

# ETL 导出中常见的空值字面量（大小写不敏感）。仅收 null；
# "N/A"/"NA" 等可能是合法业务码（如地区码），不纳入。
NULL_WORDS = {"null"}

# 值类型（分类粒度）
T_INT = "int"
T_FLOAT = "float"
T_DATE = "date"
T_DATETIME = "datetime"
T_BOOL = "bool"
T_STRING = "string"

# 列级对外类型（含 mixed）
T_MIXED = "mixed"


def is_null_value(raw: str) -> bool:
    """空串或 NULL 字面量 → null。"""
    v = raw.strip()
    return not v or v.lower() in NULL_WORDS


def parse_date_value(v: str) -> date:
    """把 classify 判为 date 的字符串转 date（ISO 与斜杠格式）。"""
    m = DATE_RE.match(v.strip())
    if not m:
        raise ValueError(f"非法日期: {v!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_datetime_value(v: str) -> datetime:
    """把 classify 判为 datetime 的字符串转 datetime（ISO 与斜杠格式）。"""
    m = DATETIME_RE.match(v.strip())
    if not m:
        raise ValueError(f"非法日期时间: {v!r}")
    y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    s = int(m.group(7)) if m.group(7) else 0
    us = int(float(m.group(8)) * 1_000_000) if m.group(8) else 0
    return datetime(y, mo, d, h, mi, s, us)


def classify_value(raw: str) -> str:
    """单值分类，返回 int/float/date/datetime/bool/string；null 返回空串。"""
    v = raw.strip()
    if not v or v.lower() in NULL_WORDS:
        return ""
    if DATETIME_RE.match(v):
        try:
            parse_datetime_value(v)
            return T_DATETIME
        except ValueError:
            pass
    if DATE_RE.match(v):
        try:
            parse_date_value(v)
            return T_DATE
        except ValueError:
            pass
    if INT_RE.match(v):
        digits = v.lstrip("+-")
        if len(digits) > 1 and digits.startswith("0"):
            return T_STRING  # 前导零：保留为字符串，避免 ID 语义丢失
        return T_INT
    if FLOAT_RE.match(v):
        return T_FLOAT
    if v.lower() in ("true", "false"):
        return T_BOOL
    return T_STRING


@dataclass
class ColumnStat:
    """单列推断结果。"""

    name: str
    types: Counter = field(default_factory=Counter)
    null_count: int = 0
    sample: list[str] = field(default_factory=list)
    min_date: date | None = None
    max_date: date | None = None
    non_null_count: int = 0

    # ---- 汇总输出 ----

    def final_type(self) -> str:
        non_null = {t for t in self.types if t}
        if not non_null:
            return T_STRING
        if len(non_null) == 1:
            return next(iter(non_null))
        if non_null <= {T_INT, T_FLOAT}:
            # 数值列内部的整数/小数混合只是精度问题，统一为 float，不算混杂
            return T_FLOAT
        if non_null <= {T_DATE, T_DATETIME}:
            # date 与 datetime 同列视为同族时间类型，统一为 datetime
            return T_DATETIME
        return T_MIXED

    def to_dict(self) -> dict:
        col_type = self.final_type()
        return {
            "name": self.name,
            "type": col_type,
            "mixed": col_type == T_MIXED,
            "null_count": self.null_count,
            "non_null_count": self.non_null_count,
            "sample": self.sample[:3],
        }


def normalize_header(raw_header: list[str]) -> list[str]:
    """表头清洗：去 BOM/空白；空列名补 col_N；重名追加序号。"""
    header: list[str] = []
    seen: Counter = Counter()
    for i, raw in enumerate(raw_header):
        name = raw.strip().lstrip("\ufeff") or f"col_{i + 1}"
        seen[name] += 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
        header.append(name)
    return header


def scan_rows(
    header: list[str],
    rows: Iterator[list[str]],
) -> tuple[list[ColumnStat], int, list[int]]:
    """第一遍扫描：推断列统计 + 计数 + 定位列数不一致的行。

    返回 (列统计列表, 数据行数, 异常行号列表[1-based 数据行])。
    调用方对异常行必须明确报错，不得静默修正。
    """
    stats = [ColumnStat(name=name) for name in header]
    row_count = 0
    ragged: list[int] = []
    n_cols = len(header)

    try:
        for row in rows:
            row_count += 1
            if len(row) != n_cols:
                if len(ragged) < 10:
                    ragged.append(row_count)
                continue
            for stat, raw in zip(stats, row):
                kind = classify_value(raw)
                stat.types[kind] += 1
                if kind == "":
                    stat.null_count += 1
                    continue
                stat.non_null_count += 1
                v = raw.strip()
                if len(stat.sample) < 3 and v not in stat.sample:
                    stat.sample.append(v)
                if kind == T_DATE:
                    d = parse_date_value(v)
                    stat.min_date = d if stat.min_date is None else min(stat.min_date, d)
                    stat.max_date = d if stat.max_date is None else max(stat.max_date, d)
                elif kind == T_DATETIME:
                    d = parse_datetime_value(v).date()
                    stat.min_date = d if stat.min_date is None else min(stat.min_date, d)
                    stat.max_date = d if stat.max_date is None else max(stat.max_date, d)
    except BaseException:
        # 提前退出（如质检报错）时关闭底层迭代器，避免 Windows 文件句柄泄漏
        close = getattr(rows, "close", None)
        if close is not None:
            close()
        raise

    return stats, row_count, ragged
