"""归因下钻 AI 解读（AI 能力 P2 功能 6）。

对归因树根节点（第一层 TopN 贡献）生成一句算写分离的 AI 解读：
- 复用 `anomaly.attribution.attribute_tree_node`（path=[]，权限/口径同源）；
- LLM 只见 `{{ref:KEY}}` 占位符（贡献分组名 + 贡献占比 + 方向词），
  数字服务端 `fmt_pct` 预格式化回填，三道审计与全局引擎一致；
- 降级：LLM 未配置/失败/审计全剔 → `interpretation=null`，前端不展示该句
  （下钻结果本身照常展示，零阻塞）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.ai_narrative import fmt_pct, llm_narrative
from app.domain.anomaly.attribution import attribute_tree_node
from app.infra.llm import resolve_llm_config

# 参与解读的第一层贡献项个数
_TOP_N = 3

_INTERPRET_SYSTEM = """你是经营归因解读助手。给定某指标在一段时期内变化的方向，以及按某一维度
拆解后贡献最大的分组清单，用一句话解读主要变化来源。

铁律（违反任何一条的句子都会被系统剔除）：
1. 任何数字（含百分比、序号、数量）都必须用占位符 {{ref:KEY}} 引用，KEY 必须严格来自
   给定清单；
2. 占位符之外禁止出现任何数字字符；
3. 只能基于给定分组与占比做中性陈述（如"主要由 XX 贡献"），不编造清单外事实、
   不评价好坏、不提供建议；
4. 只输出 JSON：{"sections":[{"section":"interpretation","sentences":["一句话", ...]}]}——
   给出 1~3 个候选句子（同一含义的不同写法），系统会逐句审计并取第一句合格的；
   每个候选都必须独立完整且遵守上述全部铁律。"""


def build_interpretation(
    db: Session,
    user,
    *,
    metric_id: int | str,
    start: str,
    end: str,
    dimensions: list[str],
    compare: str = "mom",
) -> dict[str, Any]:
    """生成归因根节点一句话解读（算写分离；降级 → interpretation=null）。"""
    settings = get_settings()
    llm_configured = resolve_llm_config(db, settings) is not None

    tree = attribute_tree_node(
        db, user,
        metric_ref=metric_id, start=start, end=end,
        dimensions=dimensions, path=[], compare=compare,
    )
    children = [c for c in (tree.get("children") or []) if c.get("contribution_pct") is not None]
    top = children[:_TOP_N]
    if not top:
        return {
            "interpretation": None,
            "source": "rule",
            "llm_configured": llm_configured,
            "reason": "no_groups",
        }

    delta = tree.get("delta") or 0
    direction_text = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")

    # 贡献分组名可能含数字（如"华东区"安全、"TOP1门店"不安全）——
    # 分组名原样作回填文本（服务端数据，非 LLM 产出，审计不拦）
    ref_table: dict[str, tuple[str, str]] = {
        "metric_name": (f"{tree['name']}指标名称", tree["name"]),
        "dim_name": ("拆解维度名", tree.get("children_dimension") or ""),
        "direction": ("变化方向", direction_text),
    }
    for i, c in enumerate(top, 1):
        ref_table[f"g{i}_name"] = (f"贡献第{i}的分组名", str(c.get("value")))
        ref_table[f"g{i}_pct"] = (
            f"贡献第{i}的分组贡献占比",
            fmt_pct(c.get("contribution_pct")),
        )

    if not llm_configured:
        return {
            "interpretation": None,
            "source": "rule",
            "llm_configured": False,
            "reason": "llm_not_configured",
        }

    user_payload = {
        "metric": tree["name"],
        "period": {"start": start, "end": end},
        "direction": direction_text,
        "dimension": tree.get("children_dimension"),
        "top_groups": [
            {"name_ref": f"g{i}_name", "pct_ref": f"g{i}_pct"} for i in range(1, len(top) + 1)
        ],
    }
    result = llm_narrative(
        db, settings,
        ref_table=ref_table,
        sections_to_write=["interpretation"],
        system_prompt=_INTERPRET_SYSTEM,
        user_payload=user_payload,
        timeout=60.0,
        retries=1,  # 单句场景脆弱：网络抖动重试一次
    )
    if result is None:
        return {
            "interpretation": None,
            "source": "rule",
            "llm_configured": True,
            "reason": "llm_failed",
        }
    sentences = [s for sec in result["sections"] for s in sec.get("sentences", [])]
    return {
        "interpretation": sentences[0] if sentences else None,
        "source": "llm",
        "llm_configured": True,
        "reason": None if sentences else "llm_failed",
    }
