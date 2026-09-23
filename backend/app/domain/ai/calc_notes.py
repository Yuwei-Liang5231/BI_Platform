"""指标口径 AI 助手（AI 能力 P1 功能 3：口径助手）。

基于字段（dataset 列名 + 聚合方式 + 样本值）让 LLM 生成指标中文名、别名与
结构化口径说明（rationale / alternatives / pitfalls）。LLM 未配置/失败 → 返回
空字段 + llm_configured=False，由前端保留用户手工填写。

入参白名单校验（算写分离红线之外的前置安全闸）：
- aggregation ∈ {sum, avg, count_distinct, count, max, min}；
- column 必须真实存在于 dataset.schema_json 的字段清单中。
非法 → 抛 40000 业务错误（不进 LLM）。

P4-1 语义标注下游打通：若该字段已有**人工确认**的业务含义（AI 字段语义标注
落库），则作为强约束传给 LLM——口径说明必须以人工语义为准，不得与之矛盾。
无标注时行为与 P1 完全一致（零退化）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BusinessError
from app.infra.llm import chat_json, resolve_llm_config
from app.infra.models import Dataset

# P1 允许的受限聚合（与编译器 calc_rule 白名单一致）
AGGREGATIONS = ("sum", "avg", "count_distinct", "count", "max", "min")

_CALC_NOTES_SYSTEM = """你是企业指标口径编写助手。基于列名、聚合方式与样本值，为一个指标生成
口径说明。

只输出 JSON，结构严格如下：
{"name": "指标中文名（简洁、业务可读）",
 "aliases": ["常用别名1", "常用别名2"],
 "calc_notes": {
   "rationale": "该口径的适用场景与业务含义（1~2 句）",
   "alternatives": ["其他可选聚合方式或口径视角"],
   "pitfalls": "口径易踩坑处（如空值处理、重复计数、单位口径等）"
}}
不要输出任何额外字段或说明文字。

若输入中带有 confirmed_semantics（人工确认的字段业务含义），它是权威口径：
生成的所有内容必须与之保持一致，不得给出与之矛盾的解释或别名。"""


def suggest_calc_notes(
    db: Session,
    dataset_id: int,
    column: str,
    aggregation: str,
    alias: str | None = None,
    sample_values: list | None = None,
) -> dict[str, Any]:
    """生成指标口径建议（含白名单校验 + LLM 可选降级）。

    返回 {"name","aliases","calc_notes":{rationale,alternatives,pitfalls},"llm_configured"}。
    """
    settings = get_settings()

    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise BusinessError(f"数据集 {dataset_id} 不存在", 40400)

    try:
        schema = json.loads(dataset.schema_json or "[]")
    except (ValueError, TypeError):
        schema = []
    valid_cols = [c.get("name") for c in schema if isinstance(c, dict)]

    if column not in valid_cols:
        raise BusinessError(
            f"字段 {column!r} 不在数据集 {dataset_id} 的字段清单中", 40000
        )
    if aggregation not in AGGREGATIONS:
        raise BusinessError(
            f"聚合方式 {aggregation!r} 非法，仅支持 {', '.join(AGGREGATIONS)}", 40000
        )

    llm_configured = resolve_llm_config(db, settings) is not None
    # P4-1：人工确认的字段语义（下游消费 column_semantics，无则空串）
    from app.domain.ai.semantic_annotations import load_annotations

    confirmed = (load_annotations(dataset).get(column) or "").strip()
    empty = {
        "name": "",
        "aliases": [],
        "calc_notes": {"rationale": "", "alternatives": [], "pitfalls": ""},
        "llm_configured": llm_configured,
        "confirmed_semantics": confirmed,
    }
    if not llm_configured:
        return empty

    config = resolve_llm_config(db, settings)
    samples = list(sample_values or [])
    if not samples:
        for c in schema:
            if isinstance(c, dict) and c.get("name") == column:
                samples = c.get("sample") or []
                break
    samples = [str(s) for s in samples][:20]

    user_payload = {
        "column": column,
        "aggregation": aggregation,
        "alias": alias or "",
        "samples": samples,
        "confirmed_semantics": confirmed or None,  # 人工确认语义（权威）
    }
    obj = chat_json(
        config,
        _CALC_NOTES_SYSTEM,
        json.dumps(user_payload, ensure_ascii=False),
        timeout=60.0,
        retries=0,
    )
    if not isinstance(obj, dict):
        return empty

    notes = obj.get("calc_notes") or {}
    return {
        "name": str(obj.get("name") or ""),
        "aliases": [str(a) for a in (obj.get("aliases") or []) if a][:10],
        "calc_notes": {
            "rationale": str(notes.get("rationale") or ""),
            "alternatives": [str(a) for a in (notes.get("alternatives") or []) if a][:10],
            "pitfalls": str(notes.get("pitfalls") or ""),
        },
        "llm_configured": True,
        "confirmed_semantics": confirmed,
    }
