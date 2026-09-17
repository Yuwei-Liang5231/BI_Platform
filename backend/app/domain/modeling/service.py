"""自动建模建议（B13-1 预研 spike）：对项目内数据集自动**建议**表关系与指标候选。

铁律：只出建议、绝不自动入库——入库必须走人工确认（B13-2 确认流）。

表关系建议算法（行业无关、可解释）：
- 候选列：文本列且基数 ≤ 5000（与拆解维度同一硬护栏，ID 类极端列排除）；
- 值域重叠：两列 distinct 值交集 / 较小基数（dim 侧覆盖率）；
- 命名相似：归一化后相等 1.0 / 包含 0.7 / 词元 Jaccard；
- 综合分 = 0.6×值域重叠 + 0.4×命名相似；方向按基数定（大→小 = many_to_one）；
- 每对列组合只留最高分，A→B 与 B→A 去重。

指标候选启发（无日期列的数据集跳过——指标必须有时间维度）：
- 数值列 → sum 候选；
- 高基数文本列（>100）→ count_distinct 候选（ID 类）；
- 可选 LLM 辅助：列名+类型+样例 → 提议指标名与聚合方式（输出校验锚定真实
  表列，非法提议丢弃防幻觉）；未配置 LLM 时纯启发。

证据全部随建议输出（overlap/name/card），供人工核对而非黑盒打分。
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infra.duckdb_session import duckdb_views
from app.infra.models import DatasetRelation
from app.infra.repository import Repository
from app.domain.ingestion.service import view_target
from app.domain.metric.compiler import _q as _quote_ident
from app.domain.query.service import BREAKDOWN_MAX_CARDINALITY
from app.domain.project.service import resolve_project_id
from app.infra.models import Dataset, DatasetRelation


def _datasets_of(db: Session, project_id: int) -> list[Dataset]:
    """项目内数据集（列类型来自 schema_json）。"""
    rows = (
        db.query(Dataset)
        .filter(Dataset.project_id == project_id)
        .order_by(Dataset.id)
        .all()
    )
    for r in rows:
        r.columns_map = {
            c["name"]: c["type"] for c in json.loads(r.schema_json or "[]")
        }
    return rows

MAX_SUGGEST_RELATIONS = 20
HIGH_CARD_DISTINCT = 100  # 文本列基数超过此值 → count_distinct 指标候选


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (name or "").lower())


def _name_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.7
    toks_a = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", (a or "").lower()))
    toks_b = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", (b or "").lower()))
    if not toks_a or not toks_b:
        return 0.0
    return len(toks_a & toks_b) / len(toks_a | toks_b)


def _sample_distinct(views: dict, text_cols: dict[str, list[str]]) -> dict[tuple[str, str], set]:
    """逐列取 distinct 值集合（上限 = 护栏 +1，超出标记不参与）。"""
    samples: dict[tuple[str, str], set] = {}
    with duckdb_views(views) as con:
        for name, cols in text_cols.items():
            for col in cols:
                rows = con.execute(
                    f"SELECT DISTINCT {_quote_ident(col)} FROM {_quote_ident(name)} "
                    f"LIMIT {BREAKDOWN_MAX_CARDINALITY + 1}"
                ).fetchall()
                if len(rows) > BREAKDOWN_MAX_CARDINALITY:
                    continue  # 超护栏列（如外部引用码）不参与关系建议
                vals = {str(r[0]) for r in rows if r[0] is not None and str(r[0]).strip() != ""}
                if vals:
                    samples[(name, col)] = vals
    return samples


def suggest_relations(db: Session, project_id: int) -> list[dict]:
    """表关系建议：值域重叠 + 命名相似，双向去重、已登记关系标注 existing。"""
    datasets = _datasets_of(db, project_id)
    if len(datasets) < 2:
        return []
    views = {r.name: view_target(r.parquet_path) for r in datasets}
    text_cols = {
        r.name: [c for c, t in r.columns_map.items() if t in ("string", "mixed")]
        for r in datasets
    }
    samples = _sample_distinct(views, text_cols)

    id_to_name = {r.id: r.name for r in datasets}
    existing = {
        (
            id_to_name.get(r.dataset_id, r.dataset_id),
            r.from_column,
            id_to_name.get(r.target_dataset_id, r.target_dataset_id),
            r.target_column,
        )
        for r in db.query(DatasetRelation).all()
    }

    raw: dict[tuple, dict] = {}
    names = sorted(datasets, key=lambda r: r.id)
    for i, ds_a in enumerate(names):
        for ds_b in names[i + 1:]:
            a_cols = [(k, samples[k]) for k in samples if k[0] == ds_a.name]
            b_cols = [(k, samples[k]) for k in samples if k[0] == ds_b.name]
            for (na, ca), va in a_cols:
                for (nb, cb), vb in b_cols:
                    denom = min(len(va), len(vb))
                    if denom == 0:
                        continue
                    overlap = len(va & vb) / denom
                    name_sim = _name_similarity(ca, cb)
                    score = 0.6 * overlap + 0.4 * name_sim
                    if overlap < 0.5 and name_sim < 1.0:
                        continue  # 双低：不构成可信建议
                    # 方向：基数大的一侧是事实表（many）
                    if len(va) >= len(vb):
                        key = (na, ca, nb, cb)
                        card_from, card_to = len(va), len(vb)
                    else:
                        key = (nb, cb, na, ca)
                        card_from, card_to = len(vb), len(va)
                    item = {
                        "from_dataset": key[0], "from_column": key[1],
                        "to_dataset": key[2], "to_column": key[3],
                        "relation_type": "many_to_one",
                        "score": round(score, 4),
                        "evidence": {
                            "overlap_ratio": round(overlap, 4),
                            "name_similarity": round(name_sim, 4),
                            "cardinality_from": card_from,
                            "cardinality_to": card_to,
                        },
                        "existing": key in existing,
                    }
                    prev = raw.get(key)
                    if prev is None or item["score"] > prev["score"]:
                        raw[key] = item

    out = sorted(raw.values(), key=lambda x: -x["score"])
    return out[:MAX_SUGGEST_RELATIONS]


# ---------------------------------------------------------------- 指标候选


def _heuristic_metric_candidates(db: Session, project_id: int) -> list[dict]:
    """启发式指标候选：日期列 × 数值列 → sum；高基数文本列 → count_distinct。"""
    datasets = _datasets_of(db, project_id)
    views = {r.name: view_target(r.parquet_path) for r in datasets}
    out: list[dict] = []
    with duckdb_views(views) as con:
        for r in datasets:
            name = r.name
            date_cols = [c for c, t in r.columns_map.items() if t in ("date", "datetime")]
            if not date_cols:
                continue  # 无时间维度：候选无法成指标（明确跳过，报告注明）
            numeric = [c for c, t in r.columns_map.items() if t in ("int", "float")]
            text = [c for c, t in r.columns_map.items() if t in ("string", "mixed")]
            for col in numeric:
                out.append({
                    "dataset": name, "column": col, "aggregation": "sum",
                    "name": f"{col} 合计",
                    "reason": f"数值列 × 时间列 {date_cols[0]}（启发式）",
                    "source": "heuristic",
                })
            for col in text:
                cnt = con.execute(
                    f"SELECT COUNT(DISTINCT {_quote_ident(col)}) FROM {_quote_ident(name)}"
                ).fetchone()[0]
                if cnt > HIGH_CARD_DISTINCT:
                    out.append({
                        "dataset": name, "column": col, "aggregation": "count_distinct",
                        "name": f"{col} 数",
                        "reason": f"高基数文本列（{cnt} 个不同值，ID 类）× 时间列 {date_cols[0]}",
                        "source": "heuristic",
                    })
    return out


def _llm_metric_candidates(db: Session, datasets: list[Dataset]) -> list[dict]:
    """可选 LLM 辅助：提议指标名与聚合方式（输出锚定真实表列，非法丢弃）。"""
    from app.infra.llm import chat_json, resolve_llm_config

    config = resolve_llm_config(db, get_settings())
    if not config or not datasets:
        return []
    summary = {}
    for r in datasets:
        summary[r.name] = {f"{c}({t})": None for c, t in list(r.columns_map.items())[:30]}
    system = (
        "你是数据建模助手。根据数据集与列清单，提议值得建指标的候选（聚合 + 中文指标名）。"
        "规则：dataset/column/aggregation 必须严格来自给定清单（aggregation ∈ sum/avg/"
        "count_distinct/count/max/min）；只提议有业务意义的组合，宁缺毋滥，最多 15 条；"
        '只输出 JSON {"candidates":[{"dataset","column","aggregation","name","reason"}]}。'
    )
    obj = chat_json(config, system, json.dumps(summary, ensure_ascii=False), timeout=60.0)
    if not obj or not isinstance(obj.get("candidates"), list):
        return []
    valid = {}
    for r in datasets:
        valid.setdefault(r.name, set()).update(r.columns_map.keys())
    out = []
    for c in obj["candidates"][:20]:
        if not isinstance(c, dict):
            continue
        ds, col = c.get("dataset"), c.get("column")
        agg = c.get("aggregation")
        if ds in valid and col in valid.get(ds, set()) and agg in (
            "sum", "avg", "count_distinct", "count", "max", "min",
        ):
            out.append({
                "dataset": ds, "column": col, "aggregation": agg,
                "name": str(c.get("name") or f"{col} {agg}")[:100],
                "reason": str(c.get("reason") or "LLM 提议")[:200],
                "source": "llm",
            })
    return out


def suggest_metrics(db: Session, project_id: int) -> list[dict]:
    """指标候选 = 启发式 + 可选 LLM 提议（去重：同表同列同聚合留启发式在前）。"""
    heur = _heuristic_metric_candidates(db, project_id)
    llm = _llm_metric_candidates(db, _datasets_of(db, project_id))
    seen = {(h["dataset"], h["column"], h["aggregation"]) for h in heur}
    extra = [c for c in llm if (c["dataset"], c["column"], c["aggregation"]) not in seen]
    return [*heur, *extra]


def all_suggestions(db: Session, project_id: int | None = None) -> dict:
    pid = resolve_project_id(db, project_id)
    return {
        "project_id": pid,
        "relations": suggest_relations(db, pid),
        "metrics": suggest_metrics(db, pid),
        "disclaimer": "以上为自动建议，未经人工确认不会写入任何配置（B13 铁律）",
    }
