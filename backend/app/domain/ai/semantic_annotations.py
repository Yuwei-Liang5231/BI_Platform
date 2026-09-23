"""数据集字段语义标注（AI 能力 P2 功能 4）。

按列名 + 类型 + 样本值让 LLM 为每列生成一句业务含义备注（semantic_note）。
结构化 JSON 输出（非叙述类），不做占位符审计，但执行**键白名单防幻觉**：
LLM 返回的列键必须 ∈ 数据集真实列名，非法键整条丢弃；备注文本截断限长。

落库（覆盖写 `Dataset.column_semantics_json`）与生成分离：
- 生成 `suggest_annotations`：LLM 未配置/失败 → annotations={}（现状不变，不阻塞）；
- 落库 `save_annotations`：整组替换语义（空 map 即清空），调用方须 admin。

消费（本批）：`dataset_to_dict` 附带 `column_semantics` 供前端展示；
关系向导/指标建议的消费改造留作后续，不阻塞。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BusinessError
from app.infra.llm import chat_json, resolve_llm_config
from app.infra.models import Dataset

# 单条备注最大长度（防 prompt/存储膨胀）
_NOTE_MAX_LEN = 80
# 最多标注列数（超宽数据集只标前 N 列）
_MAX_COLUMNS = 60

_SEMANTIC_SYSTEM = """你是数据表字段语义标注助手。给定一张表的列清单（列名、类型、样本值），
为每个列生成一句中文业务含义备注。

要求：
1. 只输出 JSON：{"annotations": {"<列名>": "<一句业务含义备注>", ...}}；
2. 键必须严格使用给定列名，不得发明不存在的列；
3. 备注一句话（不超过 40 字），说清该列业务含义/单位/粒度，不确定时写"待确认"；
4. 日期列标注格式粒度（如"日粒度日期"），维度列标注业务实体（如"销售渠道"），
   数值列标注度量含义与单位（如"订单金额，单位元"）；
5. 若输入含 confirmed_semantics（人工已确认的语义），它是权威口径：
   同一列的建议不得与之矛盾，术语与风格向其看齐。"""


def _columns_of(dataset: Dataset) -> list[dict]:
    try:
        schema = json.loads(dataset.schema_json or "[]")
    except (ValueError, TypeError):
        schema = []
    return [c for c in schema if isinstance(c, dict) and c.get("name")]


def suggest_annotations(db: Session, dataset_id: int) -> dict[str, Any]:
    """生成字段语义标注建议（键白名单防幻觉；无 LLM → 空标注）。"""
    settings = get_settings()
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise BusinessError(f"数据集 {dataset_id} 不存在", 40400)

    llm_configured = resolve_llm_config(db, settings) is not None
    empty = {"annotations": {}, "llm_configured": llm_configured}
    if not llm_configured:
        return empty

    cols = _columns_of(dataset)[:_MAX_COLUMNS]
    if not cols:
        return empty

    payload_cols = [
        {
            "name": c["name"],
            "type": c.get("type"),
            "sample": [str(s) for s in (c.get("sample") or [])[:3]],
        }
        for c in cols
    ]
    config = resolve_llm_config(db, settings)
    # P4-1：把人工已确认的语义回传，让建议风格一致且不与之矛盾（记忆打通）
    confirmed = load_annotations(dataset)
    obj = chat_json(
        config,
        _SEMANTIC_SYSTEM,
        json.dumps(
            {"columns": payload_cols, "confirmed_semantics": confirmed or None},
            ensure_ascii=False,
        ),
        timeout=60.0,
        retries=0,
    )
    if not isinstance(obj, dict) or not isinstance(obj.get("annotations"), dict):
        return empty

    valid = {c["name"] for c in cols}
    out: dict[str, str] = {}
    for key, note in obj["annotations"].items():
        if key in valid and isinstance(note, str) and note.strip():
            out[str(key)] = note.strip()[:_NOTE_MAX_LEN]
    return {"annotations": out, "llm_configured": True}


def save_annotations(db: Session, dataset_id: int, annotations: dict) -> dict[str, Any]:
    """覆盖写语义标注（整组替换；空 map 清空）。调用方须校验 admin。

    键白名单：不存在的列名直接拒绝（40400），防止脏数据入库。
    """
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise BusinessError(f"数据集 {dataset_id} 不存在", 40400)
    if not isinstance(annotations, dict):
        raise BusinessError("annotations 必须是对象（列名 → 备注）", 40000)

    valid = {c["name"] for c in _columns_of(dataset)}
    clean: dict[str, str] = {}
    for key, note in annotations.items():
        if key not in valid:
            raise BusinessError(f"列 {key!r} 不在数据集 {dataset_id} 的字段清单中", 40000)
        if not isinstance(note, str):
            raise BusinessError(f"列 {key!r} 的备注必须是字符串", 40000)
        clean[str(key)] = note.strip()[:_NOTE_MAX_LEN]

    dataset.column_semantics_json = json.dumps(clean, ensure_ascii=False)
    db.commit()
    return {"dataset_id": dataset_id, "annotations": clean}


def load_annotations(dataset: Dataset) -> dict[str, str]:
    """读取已落库语义标注（dataset_to_dict 消费；空/损坏 → 空对象）。"""
    try:
        raw = json.loads(dataset.column_semantics_json or "{}")
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
