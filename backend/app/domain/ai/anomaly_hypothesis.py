"""异动 AI 假设解释（AI 能力 P1 功能 2：异动解释）。

对检测为「反常且要紧」的指标，按常用维度做单层归因，再用算写分离引擎把
Top 贡献维度组织成自然语言假设。LLM 未配置/失败时降级为固定建议动作
（_action_hint），绝不 500、绝不自产数字。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.ai_narrative import fmt_metric_value, fmt_pct, llm_narrative
from app.domain.anomaly.attribution import attribute_delta
from app.domain.anomaly.service import _action_hint, detect_for_metric
from app.domain.metric.service import get_metric
from app.infra.llm import resolve_llm_config

# 单次解释最多使用的归因维度个数
MAX_DIMENSIONS = 2
# 每个维度取 Top 贡献项个数
TOP_N = 3

_HYPOTHESIS_SYSTEM_PROMPT = """你是经营分析的假设生成助手，基于给定指标的异动方向与各维度
Top 贡献项，提出可能造成该异动的业务假设。

铁律（违反任何一条的句子都会被系统剔除）：
1. 任何数字（含百分比、日期、序号、数量）都必须用占位符 {{ref:KEY}} 引用，KEY 必须严格
   来自给定清单；
2. 占位符之外禁止出现任何数字字符；
3. 只能就"哪些维度/分组贡献最大"提出**中性假设**（可能由于 XX 变化、季节性、一次性事件等），
   不武断下结论、不编造清单外事实；
4. 只输出 JSON：{"sections":[{"section":"hypothesis","sentences":["句子1","句子2"]}]}。

写作要求：hypothesis 2~3 句，先点出异动方向（上升/下降）由哪些维度的哪些分组主要驱动，
再对最突出的 1~2 个分组给出中性、可核查的假设方向。"""


def _load_dimensions(metric) -> list[str]:
    try:
        dims = json.loads(metric.dimensions_json or "[]")
    except (ValueError, TypeError):
        dims = []
    return [d for d in dims if isinstance(d, str)] if isinstance(dims, list) else []


def build_anomaly_hypothesis(
    db: Session,
    user,
    metric_id: int,
    start: str,
    end: str,
    compare: str = "mom",
    dimensions: list[str] | None = None,
) -> dict[str, Any]:
    """构建异动 AI 假设解释（算写分离 + 降级）。

    返回 {"has_anomaly","direction","hypothesis","source","fallback_action_hint",
    "llm_configured","degraded"}。
    """
    settings = get_settings()
    metric = get_metric(db, metric_id)
    # B9 契约：受限指标不泄露名称/方向——与 /query/anomaly 端点同源校验
    from app.domain.auth.service import ensure_metric_visible

    ensure_metric_visible(db, user, metric)
    llm_configured = resolve_llm_config(db, settings) is not None

    detection = detect_for_metric(db, user, metric, detect_date=date.fromisoformat(end))
    # 简化判定：abnormal 且 material=True 才算"异动结论"
    has_anomaly = bool(detection.get("abnormal")) and detection.get("material") is True
    direction = detection.get("direction", "flat")

    if not has_anomaly:
        return {
            "has_anomaly": False,
            "direction": direction,
            "hypothesis": [],
            "source": "none",
            "fallback_action_hint": "",
            "llm_configured": llm_configured,
            "degraded": [],
        }

    action_hint = _action_hint(direction)

    dims = dimensions or _load_dimensions(metric)
    dims = dims[:MAX_DIMENSIONS]
    if not dims:
        # 无可用维度：直接走固定建议动作，不调 LLM
        return {
            "has_anomaly": True,
            "direction": direction,
            "hypothesis": [],
            "source": "rule",
            "fallback_action_hint": action_hint,
            "llm_configured": llm_configured,
            "degraded": ["hypothesis"],
        }

    ref_table: dict[str, tuple[str, str]] = {
        "metric_name": (f"{metric.name}指标名称", metric.name),
        "direction_text": ("异动方向", "上升" if direction == "up" else "下降"),
    }
    attr_summaries: list[dict] = []
    di = 0
    for dim in dims:
        try:
            attr = attribute_delta(
                db, user,
                metric_ref=metric.id, start=start, end=end,
                dimension=dim, compare=compare, top_n=TOP_N,
            )
        except Exception:
            continue
        di += 1
        top = attr.get("top_dimensions", [])[:TOP_N]
        attr_summaries.append({"dimension": dim, "top": top})
        for j, row in enumerate(top, 1):
            ref_table[f"d{di}_top{j}_val"] = (f"{dim}维度第{j}贡献分组", str(row.get("value")))
            ref_table[f"d{di}_top{j}_pct"] = (
                f"{dim}维度第{j}贡献分组贡献占比",
                fmt_pct(row.get("contribution_pct")),
            )

    if not llm_configured:
        return {
            "has_anomaly": True,
            "direction": direction,
            "hypothesis": [],
            "source": "rule",
            "fallback_action_hint": action_hint,
            "llm_configured": False,
            "degraded": ["hypothesis"],
        }

    user_payload = {
        "metric_name": metric.name,
        "direction": direction,
        "compare": compare,
        "dimension_summaries": [
            {
                "dimension": s["dimension"],
                "top": [
                    {"value_ref": f"d{idx}_top{j}_val", "pct_ref": f"d{idx}_top{j}_pct"}
                    for j, _ in enumerate(s["top"], 1)
                ],
            }
            for idx, s in enumerate(attr_summaries, 1)
        ],
        "rule_narrative": [
            {"section": "hypothesis", "sentences": [action_hint]}
        ],
    }
    result = llm_narrative(
        db, settings,
        ref_table=ref_table,
        sections_to_write=["hypothesis"],
        system_prompt=_HYPOTHESIS_SYSTEM_PROMPT,
        user_payload=user_payload,
        timeout=60.0,
        retries=0,
    )
    if result is None:
        return {
            "has_anomaly": True,
            "direction": direction,
            "hypothesis": [],
            "source": "rule",
            "fallback_action_hint": action_hint,
            "llm_configured": True,
            "degraded": ["hypothesis"],
        }
    hypothesis = []
    for sec in result["sections"]:
        hypothesis.extend(sec.get("sentences", []))
    return {
        "has_anomaly": True,
        "direction": direction,
        "hypothesis": hypothesis,
        "source": result["source"],
        "fallback_action_hint": action_hint,
        "llm_configured": True,
        "degraded": result["degraded"],
    }
