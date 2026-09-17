"""自动建模建议（B13-1 预研 spike）：对项目内数据集自动**建议**表关系与指标候选。

铁律：只出建议、绝不自动入库——入库必须走人工确认（B13-2 确认流）。

B13-2 修正（预研结论落地）：
- 高基数兜底：护栏外列不再整列排除，改截断采样（各取前 N 个 distinct 估算
  重叠率），evidence.sampled=true 置信降级（score × 0.95）——修复 user_id 类漏检；
- 孤立巧合降权：同数据集对之间仅一条建议时（无其他关联佐证）score × 0.9 并
  标注 isolated_pair——抑制跨表枚举巧合误报（如两个不同业务的 region）；
- LLM 语义复审（可选）：配置了 LLM 时对关系建议批量判断"两列是否业务同一
  实体键"，结果附 llm_review.verdict（likely/unlikely/uncertain），仅供参考；
- 指标候选噪音：布尔列（distinct ≤ 2）与枚举/等级类数值列（distinct ≤ 12 且
  列名含等级/状态等词）排除 sum 候选。

表关系建议算法（行业无关、可解释）：
- 候选列：文本列（string/mixed），护栏内全量值域、护栏外截断采样；
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
SAMPLE_RELATION_LIMIT = 50000  # 关系建议截断采样上限（护栏外列的值域采样条数）
ENUM_NAME_RE = re.compile(
    r"(level|grade|tier|type|status|state|flag|is_|has_|gender|sex|category|rank"
    r"|等级|级别|类型|状态|标志|类别|性别|星)",
    re.IGNORECASE,
)


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


def _sample_distinct(
    views: dict, text_cols: dict[str, list[str]]
) -> tuple[dict[tuple[str, str], set], set[tuple[str, str]]]:
    """逐列取 distinct 值集合（B13-2：护栏外列截断采样，不再整列排除）。

    返回 (samples, sampled_keys)：sampled_keys 中的列基数超过护栏
    （BREAKDOWN_MAX_CARDINALITY），以扩展窗口（SAMPLE_RELATION_LIMIT）截断采样，
    重叠率为估算值，evidence.sampled=true 置信降级。
    """
    samples: dict[tuple[str, str], set] = {}
    sampled_keys: set[tuple[str, str]] = set()
    limit = SAMPLE_RELATION_LIMIT + 1
    with duckdb_views(views) as con:
        for name, cols in text_cols.items():
            for col in cols:
                rows = con.execute(
                    f"SELECT DISTINCT {_quote_ident(col)} FROM {_quote_ident(name)} "
                    f"LIMIT {limit}"
                ).fetchall()
                if len(rows) > BREAKDOWN_MAX_CARDINALITY:
                    sampled_keys.add((name, col))
                vals = {str(r[0]) for r in rows if r[0] is not None and str(r[0]).strip() != ""}
                if vals:
                    samples[(name, col)] = vals
    return samples, sampled_keys


def suggest_relations(db: Session, project_id: int) -> tuple[list[dict], list[dict]]:
    """表关系建议：值域重叠 + 命名相似，双向去重、已登记关系标注 existing。

    B13-2：护栏外列截断采样参与（sampled 标注置信降级）；同数据集对间孤立
    建议降权警示（抑制枚举巧合误报）；配置 LLM 时附语义复审结论。
    返回 (建议列表, 每表参与说明)——说明用于向导明示"为什么某张表没有建议"。
    """
    datasets = _datasets_of(db, project_id)

    def _base_note(r) -> dict:
        cols = r.columns_map or {}
        return {
            "name": r.name,
            "text_columns": sum(1 for t in cols.values() if t in ("string", "mixed")),
            "date_columns": sum(1 for t in cols.values() if t in ("date", "datetime")),
            "relation_note": "",
        }

    if len(datasets) < 2:
        notes = []
        for r in datasets:
            note = _base_note(r)
            note["relation_note"] = "项目内数据集不足 2 张，无法建议跨表关系"
            notes.append(note)
        return [], notes
    views = {r.name: view_target(r.parquet_path) for r in datasets}
    text_cols = {
        r.name: [c for c, t in r.columns_map.items() if t in ("string", "mixed")]
        for r in datasets
    }
    samples, sampled_keys = _sample_distinct(views, text_cols)

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
                    is_sampled = (na, ca) in sampled_keys or (nb, cb) in sampled_keys
                    if is_sampled:
                        score *= 0.95  # 截断采样估算，置信降级
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
                            "sampled": is_sampled,
                        },
                        "existing": key in existing,
                    }
                    prev = raw.get(key)
                    if prev is None or item["score"] > prev["score"]:
                        raw[key] = item

    # B13-2 孤立巧合降权：同数据集对之间仅此一条建议（无其他关联佐证）时，
    # 大概率是跨表枚举值域巧合（如两个不同业务的 region），降权并警示。
    by_pair: dict[frozenset, list[dict]] = {}
    for item in raw.values():
        by_pair.setdefault(frozenset((item["from_dataset"], item["to_dataset"])), []).append(item)
    for group in by_pair.values():
        if len(group) == 1:
            item = group[0]
            item["score"] = round(item["score"] * 0.9, 4)
            item["evidence"]["isolated_pair"] = True

    out = sorted(raw.values(), key=lambda x: -x["score"])[:MAX_SUGGEST_RELATIONS]
    _llm_relation_review(db, out)

    # 每表参与说明：让"某张表为什么没有建议"在向导里可见可解释
    participating = {d for it in out for d in (it["from_dataset"], it["to_dataset"])}
    notes: list[dict] = []
    for r in datasets:
        note = _base_note(r)
        tcols = text_cols.get(r.name, [])
        if not tcols:
            note["relation_note"] = "无文本列（纯数值/日期表），没有可关联的键列"
        elif not any(k[0] == r.name for k in samples):
            note["relation_note"] = "文本列均无有效非空值，无法参与匹配"
        elif r.name not in participating:
            note["relation_note"] = "文本列与其他表无 ≥50% 值域重叠且无同名列，未产生建议"
        notes.append(note)
    return out, notes


def _llm_relation_review(db: Session, suggestions: list[dict]) -> None:
    """可选 LLM 语义复审：批量判断每对列是否业务同一实体键（就地附 llm_review）。

    未配置 LLM / 调用失败 → 静默跳过（建议引擎本身不依赖 LLM）。
    verdict：likely（同一实体键）/ unlikely（不同实体或非键列）/ uncertain。
    """
    if not suggestions:
        return
    from app.infra.llm import chat_json, resolve_llm_config

    config = resolve_llm_config(db, get_settings())
    if not config:
        return
    lines = [
        f"{i}. {s['from_dataset']}.{s['from_column']} ↔ {s['to_dataset']}.{s['to_column']}"
        f"（值域重叠 {s['evidence']['overlap_ratio']}）"
        for i, s in enumerate(suggestions)
    ]
    system = (
        "你是数据建模评审员。判断下列候选表关系里，两列是否为业务上同一实体的连接键"
        "（如订单表的 user_id 与用户表的 user_id → likely；两个不同业务表的同名枚举列"
        "如 region/等级 → unlikely；无法判断 → uncertain）。"
        '只输出 JSON {"reviews":[{"index":序号,"verdict":"likely|unlikely|uncertain",'
        '"reason":"一句话"}]}，序号必须来自给定清单。'
    )
    obj = chat_json(config, system, "\n".join(lines), timeout=60.0)
    reviews = obj.get("reviews") if isinstance(obj, dict) else None
    if not isinstance(reviews, list):
        return
    for r in reviews:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(suggestions):
            continue
        verdict = r.get("verdict")
        if verdict not in ("likely", "unlikely", "uncertain"):
            continue
        suggestions[idx]["llm_review"] = {
            "verdict": verdict,
            "reason": str(r.get("reason") or "")[:200],
        }


# ---------------------------------------------------------------- 指标候选


def _heuristic_metric_candidates(db: Session, project_id: int) -> list[dict]:
    """启发式指标候选：日期列 × 数值列 → sum；高基数文本列 → count_distinct。

    B13-2 噪音过滤：布尔数值列（distinct ≤ 2）与枚举/等级类数值列
    （distinct ≤ 12 且列名含等级/状态等词）排除 sum 候选（对 0/1、等级求和无业务意义）。
    """
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
                distinct = con.execute(
                    f"SELECT COUNT(DISTINCT {_quote_ident(col)}) FROM {_quote_ident(name)}"
                ).fetchone()[0]
                if distinct <= 2 or (distinct <= 12 and ENUM_NAME_RE.search(col)):
                    continue  # 布尔/枚举列：sum 无业务意义（B13-2 噪音过滤）
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
    """指标候选 = 启发式 + 可选 LLM 提议（去重：同表同列同聚合留启发式在前）。

    B13-2.1 修复：候选统一附 time_field/date_columns——多日期列数据集
    （快照表常见）未显式 time_field 会被「保存即编译拒绝」400，向导入库
    必须带上；date_columns 供向导下拉调整（默认取首个日期列）。
    """
    heur = _heuristic_metric_candidates(db, project_id)
    llm = _llm_metric_candidates(db, _datasets_of(db, project_id))
    seen = {(h["dataset"], h["column"], h["aggregation"]) for h in heur}
    extra = [c for c in llm if (c["dataset"], c["column"], c["aggregation"]) not in seen]
    out = [*heur, *extra]

    date_map: dict[str, list[str]] = {}
    for r in _datasets_of(db, project_id):
        cols = [c for c, t in r.columns_map.items() if t in ("date", "datetime")]
        if cols:
            date_map[r.name] = cols
    for c in out:
        cols = date_map.get(c["dataset"], [])
        c["date_columns"] = cols
        if cols:
            c.setdefault("time_field", cols[0])
    return out


def all_suggestions(db: Session, project_id: int | None = None) -> dict:
    pid = resolve_project_id(db, project_id)
    relations, dataset_notes = suggest_relations(db, pid)
    return {
        "project_id": pid,
        "relations": relations,
        "datasets": dataset_notes,
        "metrics": suggest_metrics(db, pid),
        "disclaimer": "以上为自动建议，未经人工确认不会写入任何配置（B13 铁律）",
    }
