"""LLM 叙述层（B12-2，算写分离核心）：结论 JSON → LLM 组织章节文字。

红线（执行方案 B12-2）：
- LLM 拿到的是**占位符清单**（ref key + 业务含义标签），不含任何原始数值——
  想写数字只能引用 {{ref:KEY}}，无数字可抄；
- 渲染时平台按 refs 表回填真值（display 文本在服务端预格式化，LLM 无法篡改）；
- 三道审计：
  ① 占位符引用了清单外的 KEY → 整句剔除；
  ② 去掉占位符后残留任何数字（裸数字，含日期/数量）→ 整句剔除（正则审计）；
  ③ 章节内无一幸存 / LLM 失败 / 未配置 → 该章降级为规则句（B12-1 句式库）。
- 归因章节保留规则句（贡献占比表不适合散文化，且数字粒度过细易诱发幻觉）。

测试注入口：chat_json 按模块导入（from app.infra.llm import chat_json），
测试 monkeypatch 本模块命名空间即可，无需起 mock HTTP 服务。
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infra.llm import chat_json, resolve_llm_config

PLACEHOLDER_RE = re.compile(r"\{\{ref:([A-Za-z0-9_]+)\}\}")
BARE_DIGIT_RE = re.compile(r"\d")  # 去占位符后不允许任何数字残留

# LLM 可叙述的章节（attribution 保留规则句）
LLM_SECTIONS = ("overview", "metrics", "anomaly")


def _build_ref_table(result: dict, sections: dict, period: dict) -> dict[str, tuple[str, str]]:
    """占位符清单：key → (业务含义标签, 服务端预格式化的显示文本)。

    LLM 只见标签（无数值）；回填文本在本模块生成——LLM 无法影响任何数字。
    """
    # 局部导入避免环（service 不依赖本模块的运行时值）
    from app.domain.report.service import _fmt_pct, _n

    table: dict[str, tuple[str, str]] = {
        "p_start": ("报告周期开始日期", period["start"]),
        "p_end": ("报告周期结束日期", period["end"]),
    }
    for c in result["conclusions"]:
        mid = c["metric_id"]
        name = f"{c['name']}（{c['code']}）"
        table[f"m{mid}_value"] = (f"{name}本期值", _n(c["value"]))
        if sections.get("mom"):
            table[f"m{mid}_mom"] = (f"{name}环比变化", _fmt_pct(c["mom_pct"]))
        if sections.get("yoy"):
            table[f"m{mid}_yoy"] = (f"{name}同比变化", _fmt_pct(c["yoy_pct"]))
    for r in result["anomalies"]:
        mid = r["metric_id"]
        name = f"{r['name']}（{r['metric_code']}）"
        table[f"a{mid}_value"] = (f"{name}异动日值", _n(r["current"]))
        table[f"a{mid}_mean"] = (f"{name}正常水平（同星期几基准均值）", _n(r["baseline"]["mean"]))
        abn = (
            f"{r['abnormality']:.2f} 倍标准差"
            if r.get("abnormality") is not None
            else "恒定基准偏离"
        )
        table[f"a{mid}_abn"] = (f"{name}偏离幅度", abn)
    return table


def _audit_sentence(sentence: str, ref_keys: set[str]) -> bool:
    """三道审计之①②：未知 ref / 裸数字 → 句子不合格。"""
    keys = PLACEHOLDER_RE.findall(sentence)
    if any(k not in ref_keys for k in keys):
        return False
    without = PLACEHOLDER_RE.sub("", sentence)
    return BARE_DIGIT_RE.search(without) is None


def _backfill(sentence: str, table: dict[str, tuple[str, str]]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: table[m.group(1)][1], sentence)


_SYSTEM_PROMPT = """你是企业经营报告的撰写助手，把给定的结论清单组织成流畅的中文报告段落。

铁律（违反任何一条的句子都会被系统剔除）：
1. 任何数字（含百分比、日期、序号、数量）都必须用占位符 {{ref:KEY}} 引用，KEY 必须严格来自给定清单；
2. 占位符之外禁止出现任何数字字符——包括日期（用 {{ref:p_start}}、{{ref:p_end}}）、"前3名"这类数量词；
3. 不编造清单外的事实；不评价好坏只陈述变化；
4. 只输出 JSON：{"sections":[{"section":"<章节键>","sentences":["句子1","句子2"]}]}，
   章节键只能用给定清单中的键；句子要完整、专业、避免模板腔。"""


def llm_narrative(
    db: Session,
    settings: Settings,
    result: dict,
    sections: dict,
    period: dict,
) -> dict | None:
    """尝试用 LLM 生成章节叙述。可用 → {"sections", "source", "degraded"}；
    未配置/失败/全部被剔除 → None（调用方整段走规则句）。"""
    config = resolve_llm_config(db, settings)
    if not config:
        return None

    table = _build_ref_table(result, sections, period)
    allowed = [k for k in LLM_SECTIONS if (k != "metrics" or (sections.get("mom") or sections.get("yoy"))) and (k != "anomaly" or sections.get("anomaly"))]
    if not allowed:
        return None

    user_payload = {
        "report_period": {"start": period["start"], "end": period["end"]},
        "available_refs": {k: v[0] for k, v in table.items()},
        "sections_to_write": allowed,
    }
    obj = chat_json(config, _SYSTEM_PROMPT, json.dumps(user_payload, ensure_ascii=False), timeout=60.0)
    if not obj or not isinstance(obj.get("sections"), list):
        return None

    ref_keys = set(table)
    rule_fallback = {sec["section"]: sec["sentences"] for sec in result.get("rule_narrative", [])}
    out_sections: list[dict] = []
    degraded: list[str] = []
    used_llm = False

    for key in allowed:
        llm_sec = next(
            (s for s in obj["sections"] if isinstance(s, dict) and s.get("section") == key),
            None,
        )
        sentences = [
            s.strip()
            for s in (llm_sec or {}).get("sentences", [])
            if isinstance(s, str) and s.strip()
        ]
        kept = [_backfill(s, table) for s in sentences if _audit_sentence(s, ref_keys)]
        if kept:
            out_sections.append({"section": key, "sentences": kept, "source": "llm"})
            used_llm = True
        elif key in rule_fallback:
            out_sections.append(
                {"section": key, "sentences": rule_fallback[key], "source": "rule"}
            )
            degraded.append(key)

    if not used_llm:
        return None
    return {"sections": out_sections, "source": "llm", "degraded": degraded}


def try_narrative(db: Session, result: dict, sections: dict, period: dict) -> dict:
    """preview 入口：LLM 可用则叙述层接管（章节级降级到规则句），否则整段规则句。

    返回 {"narrative", "narrative_source", "llm_degraded"}。
    """
    settings = get_settings()
    result.setdefault("rule_narrative", [])  # 供降级取句（由调用方塞入）
    try:
        out = llm_narrative(db, settings, result, sections, period)
    except Exception:  # 叙述层任何意外都不阻塞报告
        out = None
    if out is not None:
        return {
            "narrative": out["sections"],
            "narrative_source": "llm",
            "llm_degraded": out["degraded"],
        }
    return {
        "narrative": [{**s, "source": "rule"} for s in result["rule_narrative"]],
        "narrative_source": "rule",
        "llm_degraded": [],
    }
