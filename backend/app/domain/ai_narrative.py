"""通用算写分离引擎（AI 能力 P1 批核心基础）。

把"计算"与"写作"彻底分离：LLM 只能看到 ``{{ref:KEY}}`` 占位符清单（KEY +
业务含义标签，不含任何原始数值），数值在服务端预格式化后回填，LLM 无法篡改
任何数字。回填后做三道审计：

① 占位符引用了清单外的 KEY → 整句剔除；
② 去掉占位符后残留任何数字（裸数字，含日期/数量）→ 整句剔除；
③ 章节内无一幸存 / LLM 失败 / 未配置 → 该章降级为规则句（调用方提供）。

本模块只提供纯函数原语与通用 ``llm_narrative``，报告叙述层
``app.domain.report.narrative`` 与 AI 速览/异动假设/口径助手均为消费方。
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infra.llm import chat_json, resolve_llm_config

PLACEHOLDER_RE = re.compile(r"\{\{ref:([A-Za-z0-9_]+)\}\}")
BARE_DIGIT_RE = re.compile(r"\d")  # 去占位符后不允许任何数字残留


def audit_sentence(sentence: str, ref_keys: set[str]) -> bool:
    """三道审计之①②：未知 ref / 裸数字 → 句子不合格。

    返回 True 表示这句可以保留（仅引用了清单内 KEY，且去掉占位符后无数字残留）。
    """
    keys = PLACEHOLDER_RE.findall(sentence)
    if any(k not in ref_keys for k in keys):
        return False
    without = PLACEHOLDER_RE.sub("", sentence)
    return BARE_DIGIT_RE.search(without) is None


def backfill(sentence: str, table: dict[str, tuple[str, str]]) -> str:
    """把 {{ref:KEY}} 占位符替换为服务端预格式化的显示文本。

    table: key -> (业务含义标签, 显示文本)；缺失的 KEY 降级为原占位符（不应发生，
    因为审计已过滤未知 KEY）。
    """
    return PLACEHOLDER_RE.sub(
        lambda m: table[m.group(1)][1] if m.group(1) in table else m.group(0),
        sentence,
    )


def fmt_metric_value(v) -> str:
    """数值格式化：千分位 + 1 位小数；None/非法 → 破折号。"""
    try:
        if v is None:
            return "—"
        return f"{float(v):,.1f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(p) -> str:
    """百分比格式化：1 位小数 + %；None/非法 → 破折号。"""
    try:
        if p is None:
            return "—"
        return f"{float(p):.1f}%"
    except (TypeError, ValueError):
        return "—"


def llm_narrative(
    db: Session,
    settings: Settings,
    *,
    ref_table: dict[str, tuple[str, str]],
    sections_to_write: list[str],
    system_prompt: str,
    user_payload: dict,
    timeout: float = 60.0,
    retries: int = 0,
) -> dict | None:
    """通用叙事生成：LLM + 服务端回填 + 审计 + 章节级降级。

    参数：
    - ref_table: key -> (业务含义标签, 显示文本)，由调用方预格式化；
    - sections_to_write: 本次要写的章节键列表；
    - system_prompt / user_payload: 透传给 LLM；user_payload 会追加
      ``available_refs``（KEY→标签）供 LLM 引用；
    - retries: 透传给 chat_json（默认 0；调用方可按需开启重试）。

    返回 ``{"sections":[{section,sentences,source}], "source":"llm",
    "degraded":[...]}``；若 LLM 不可用、返回非法、或全部章节降级 → 返回 None
    （调用方整段走规则句或固定兜底）。
    """
    config = resolve_llm_config(db, settings)
    if not config:
        return None

    send_payload = {
        **user_payload,
        "available_refs": {k: v[0] for k, v in ref_table.items()},
    }
    obj = chat_json(
        config,
        system_prompt,
        json.dumps(send_payload, ensure_ascii=False),
        timeout=timeout,
        retries=retries,
    )
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), list):
        return None

    ref_keys = set(ref_table)
    rule_fallback = {
        s["section"]: s["sentences"]
        for s in send_payload.get("rule_narrative", [])
        if isinstance(s, dict) and isinstance(s.get("sentences"), list)
    }
    out: list[dict] = []
    degraded: list[str] = []
    used_llm = False

    for key in sections_to_write:
        sec = next(
            (s for s in obj["sections"] if isinstance(s, dict) and s.get("section") == key),
            None,
        )
        sentences = [
            s.strip()
            for s in (sec or {}).get("sentences", [])
            if isinstance(s, str) and s.strip()
        ]
        kept = [backfill(s, ref_table) for s in sentences if audit_sentence(s, ref_keys)]
        if kept:
            out.append({"section": key, "sentences": kept, "source": "llm"})
            used_llm = True
        elif key in rule_fallback:
            out.append({"section": key, "sentences": rule_fallback[key], "source": "rule"})
            degraded.append(key)

    if not used_llm:
        return None
    return {"sections": out, "source": "llm", "degraded": degraded}
