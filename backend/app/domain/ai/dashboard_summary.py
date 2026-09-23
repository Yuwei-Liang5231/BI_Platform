"""看板智能摘要（AI 能力 P1 功能 1：看板速览）。

读 `period` 内当前项目（或指定指标集）的活跃指标，统一经指标中心计算本期值
与环比，再以算写分离引擎组织自然语言摘要。LLM 未配置/失败时整体降级为规则句
（计数概览），绝不 500、绝不自产数字。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.ai_narrative import fmt_metric_value, fmt_pct, llm_narrative
from app.domain.metric.service import list_metrics
from app.domain.project.service import resolve_project_id
from app.domain.query.service import compute_metric_value
from app.infra.llm import resolve_llm_config

# 单次摘要最多覆盖指标数（避免 prompt 过长 / 计算开销过大）
MAX_METRICS = 15

_DASHBOARD_SYSTEM_PROMPT = """你是企业看板的智能摘要助手，把给定指标清单的本期表现组织成
简短的中文概览段落。

铁律（违反任何一条的句子都会被系统剔除）：
1. 任何数字（含百分比、日期、序号、数量）都必须用占位符 {{ref:KEY}} 引用，KEY 必须严格
   来自给定清单；
2. 占位符之外禁止出现任何数字字符——包括日期、"前3名"这类数量词；
3. 不编造清单外的事实，不评价指标好坏，只客观陈述升降与幅度；
4. 只输出 JSON：{"sections":[{"section":"overview","sentences":["句子1","句子2"]}]}。

写作要求：overview 2~4 句，先讲整体态势（多少指标环比上升/下降），再挑 1~3 个变化最
显著的指标点出其方向与幅度，末尾预告可下钻的异动。"""


def build_dashboard_summary(
    db: Session,
    user,
    project_id: int | None,
    start: str,
    end: str,
    metric_ids: list[int] | None = None,
) -> dict[str, Any]:
    """构建看板速览摘要（算写分离 + 降级）。

    返回 {"llm_configured", "source", "sections", "degraded", "rule_text"}。
    source ∈ {"llm","rule","none"}；无可用指标时为 "none"。
    """
    settings = get_settings()
    pid = resolve_project_id(db, project_id)

    from app.domain.auth.service import restricted_metric_ids

    project_metrics = list_metrics(db, project_id=pid, status="active")
    if metric_ids:
        wanted = set(metric_ids[:MAX_METRICS])
        metrics = [m for m in project_metrics if m.id in wanted][:MAX_METRICS]
    else:
        metrics = project_metrics[:MAX_METRICS]
    # 纵深防御：受限指标在源头排除（不依赖 compute 抛异常兜底）
    restricted = restricted_metric_ids(db, user)
    metrics = [m for m in metrics if m.id not in restricted]

    rows: list[dict] = []
    up = down = flat = 0
    for m in metrics:
        try:
            calc = compute_metric_value(
                db, metric_ref=m.id, start=start, end=end, compare="mom", user=user
            )
        except Exception:
            # 受限指标/计算失败：跳过，不参与摘要（不阻塞整页）
            continue
        if calc.get("value") is None:
            continue
        pct = (calc.get("compare") or {}).get("change_pct")
        rows.append(
            {
                "id": m.id,
                "name": m.name,
                "code": m.code,
                "value": calc["value"],
                "mom_pct": pct,
            }
        )
        if pct is None:
            flat += 1
        elif pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1

    rule_text = (
        f"本期共 {len(rows)} 个指标，{up} 个环比上升、{down} 个环比下降"
        + (f"、{flat} 个环比持平" if flat else "")
        + "。"
    )

    if not rows:
        return {
            "llm_configured": resolve_llm_config(db, settings) is not None,
            "source": "none",
            "sections": [],
            "degraded": [],
            "rule_text": "（当前范围内暂无可汇总的指标数据）",
        }

    # ref_table：key -> (业务含义标签, 服务端预格式化显示文本)
    ref_table: dict[str, tuple[str, str]] = {}
    for i, r in enumerate(rows, 1):
        ref_table[f"m{i}_name"] = (f"指标{i}名称", r["name"])
        ref_table[f"m{i}_value"] = (f"{r['name']}本期值", fmt_metric_value(r["value"]))
        ref_table[f"m{i}_mom"] = (
            f"{r['name']}环比变化",
            fmt_pct(r["mom_pct"]) if r["mom_pct"] is not None else "—",
        )

    llm_configured = resolve_llm_config(db, settings) is not None
    if not llm_configured:
        return {
            "llm_configured": False,
            "source": "rule",
            "sections": [{"section": "overview", "sentences": [rule_text], "source": "rule"}],
            "degraded": [],
            "rule_text": rule_text,
        }

    user_payload = {
        "period": {"start": start, "end": end},
        "rule_narrative": [{"section": "overview", "sentences": [rule_text]}],
    }
    result = llm_narrative(
        db,
        settings,
        ref_table=ref_table,
        sections_to_write=["overview"],
        system_prompt=_DASHBOARD_SYSTEM_PROMPT,
        user_payload=user_payload,
        timeout=60.0,
        retries=0,
        kind="dashboard_summary",
        project_id=pid,
    )
    if result is None:
        return {
            "llm_configured": True,
            "source": "rule",
            "sections": [{"section": "overview", "sentences": [rule_text], "source": "rule"}],
            "degraded": ["overview"],
            "rule_text": rule_text,
        }
    return {
        "llm_configured": True,
        "source": result["source"],
        "sections": result["sections"],
        "degraded": result["degraded"],
        "rule_text": rule_text,
    }
