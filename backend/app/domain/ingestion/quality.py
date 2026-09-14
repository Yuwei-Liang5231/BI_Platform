"""数据集质检报告（B8，阶段 2"完整质检"）。

行列级定位：逐列给出类型/空值/空列/常量列/混杂偏离（行号+原始内容），
非字符串列额外给出空值行号样本。按需从 Parquet 现算（本地数据毫秒级），
不落库——数据集每次增量导入后报告自然反映最新数据。

行业无关（不变式 7）：本模块只认识行、列、类型与空值，不认识业务语义。
"""

from __future__ import annotations

import json

import pyarrow.compute as pc
from pyarrow import Table

from app.core.response import BusinessError
from app.domain.ingestion.types import classify_value
from app.infra.models import DatasetCoverage

_NULL_RATIO_HIGH = 0.5
_DEVIATION_LIMIT = 20
_NULL_SAMPLE_LIMIT = 10


def _col_stat(columns: list[dict], name: str) -> dict:
    for c in columns:
        if c["name"] == name:
            return c
    raise BusinessError(f"列 {name!r} 不存在")


def quality_report(dataset, coverages: list[DatasetCoverage], table: Table) -> dict:
    """生成数据集质检报告。dataset 仅用于读 schema_json。"""
    columns_meta = json.loads(dataset.schema_json)
    total_rows = table.num_rows
    col_reports: list[dict] = []

    for meta in columns_meta:
        name, ctype = meta["name"], meta["type"]
        col = table.column(name)
        null_count = col.null_count
        non_null = col.drop_null()
        non_null_count = non_null.length()
        issues: list[dict] = []

        distinct = pc.count_distinct(col).as_py() if non_null_count else 0
        is_empty = non_null_count == 0 and total_rows > 0
        is_constant = (not is_empty) and distinct == 1

        report = {
            "name": name,
            "type": ctype,
            "mixed": bool(meta.get("mixed")),
            "null_count": null_count,
            "null_ratio": round(null_count / total_rows, 4) if total_rows else 0.0,
            "distinct_count": distinct,
            "is_empty": is_empty,
            "is_constant": is_constant,
            "issues": [],
        }

        if is_empty:
            issues.append({"level": "high", "code": "empty_column", "message": "全空列：无任何有效值，不参与计算"})
        if total_rows and null_count / total_rows > _NULL_RATIO_HIGH and not is_empty:
            issues.append(
                {
                    "level": "medium",
                    "code": "high_null_ratio",
                    "message": f"空值占比 {null_count}/{total_rows} 超过 50%，使用该列计算的口径可能失真",
                }
            )
        if is_constant and not is_empty:
            issues.append({"level": "info", "code": "constant_column", "message": "常量列：全部非空值相同，区分度低"})

        # 混杂/字符串列：按原始内容做类型分布检测（B8 增量导入后，schema 类型
        # 可能仍是 string——但内容混杂同样要暴露，如数字落进字符串列）
        if report["mixed"] or ctype == "string":
            if non_null_count:
                counter: dict[str, int] = {}
                parsed: list[tuple[int, str, str]] = []
                col_strings = ["" if v is None else str(v) for v in col.to_pylist()]
                for i, v in enumerate(col_strings):
                    kind = classify_value(v)
                    if kind == "":
                        continue
                    counter[kind] = counter.get(kind, 0) + 1
                    parsed.append((i + 1, v, kind))
                if len(counter) > 1:
                    dominant = max(counter, key=counter.get)
                    report["dominant_type"] = dominant
                    report["type_counts"] = counter
                    report["deviations"] = [
                        {"row": row_no, "value": v, "value_type": kind}
                        for row_no, v, kind in parsed
                        if kind != dominant
                    ][:_DEVIATION_LIMIT]
                    report["deviation_total"] = sum(1 for _, _, k in parsed if k != dominant)
                    if report["mixed"]:
                        code, message = "mixed_types", "类型混杂：已按字符串存储，聚合计算前建议修正源数据并全量重导"
                    else:
                        code = "mixed_content"
                        message = (
                            f"内容类型不一：字符串列中混有 {dict(counter)} 形态的值"
                            "（已按字符串存储，不影响聚合，但常意味着源数据口径不齐）"
                        )
                    issues.append({"level": "high", "code": code, "message": message})

        # 非字符串列：空值行号样本（字符串列空值常见，不列）
        if null_count and ctype not in ("string", "mixed"):
            null_rows = [
                i + 1 for i, v in enumerate(col.to_pylist()) if v is None
            ][:_NULL_SAMPLE_LIMIT]
            report["null_sample_rows"] = null_rows
            report["null_total"] = null_count

        report["issues"] = issues
        col_reports.append(report)

    return {
        "dataset_id": dataset.id,
        "name": dataset.name,
        "dataset_ver": dataset.dataset_ver,
        "row_count": total_rows,
        "column_count": len(columns_meta),
        "columns": col_reports,
        "coverage": [
            {
                "column": c.column_name,
                "period_start": c.period_start.isoformat(),
                "period_end": c.period_end.isoformat(),
                "non_null_count": c.non_null_count,
            }
            for c in coverages
        ],
    }
