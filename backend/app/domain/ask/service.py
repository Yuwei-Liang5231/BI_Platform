"""问数 Agent（B9，阶段 2"先摊开理解、再给答案"）。

核心语义（计划 3.3 / 架构 6.2）：
- 意图解析双通道：LLM（infra/llm，OpenAI 兼容）→ 结构化意图；LLM 未配置
  或解析失败时降级到**确定性关键词解析器**（离线可回归）。
- 理解卡：展示指标/时间/环比方式，全部可改，改完立即重算；
  歧义（多个候选指标）黄提示列出候选；引用 metrics.disambiguation 登记
  的默认算法说明。
- "算不了"：无可见指标匹配时明确返回原因，绝不现场拼装查询。
- 权限继承：候选指标 = 登录用户可见（restricted_metric_ids）的 active 指标；
  执行走与看板完全相同的 compute_metric_value（数值出口单点）。
- 行业无关（不变式 7）：本模块只认识 code/name/别名/时间词，无业务分支。
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.auth.service import restricted_metric_ids
from app.infra.llm import chat_json, resolve_llm_config
from app.infra.models import Metric


# ---------------------------------------------------------------- 时间解析


def previous_complete_month(today: date) -> tuple[date, date]:
    """上一完整自然月（与前端 usePeriodRange 默认区间同规则）。"""
    last_prev = today.replace(day=1) - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def current_month(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    end = today.replace(day=monthrange(today.year, today.month)[1])
    return start, end


def parse_time_range(question: str, today: date) -> tuple[date, date, str] | None:
    """从问句解析时间区间。返回 (start, end, 命中文本) 或 None（调用方兜底默认区间）。"""
    # 显式日期：YYYY-MM-DD 或 YYYY/MM/DD（单个或范围）
    iso_dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", question)
    if len(iso_dates) >= 2:
        d1, d2 = _to_date(iso_dates[0]), _to_date(iso_dates[1])
        return (min(d1, d2), max(d1, d2), f"{iso_dates[0]}~{iso_dates[1]}")
    if len(iso_dates) == 1:
        d = _to_date(iso_dates[0])
        return (d, d, iso_dates[0])

    # YYYY年M月 → 整月
    cn_month = re.search(r"(\d{4})年(\d{1,2})月", question)
    if cn_month:
        y, m = int(cn_month.group(1)), int(cn_month.group(2))
        return date(y, m, 1), date(y, m, monthrange(y, m)[1]), cn_month.group(0)

    if re.search(r"上(一)?个?月", question):
        return (*previous_complete_month(today), "上个月")
    if re.search(r"(本|这)个?月", question):
        return (*current_month(today), "本月")
    if re.search(r"去(一)?年", question):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "去年"
    if re.search(r"今(一)?年", question):
        return date(today.year, 1, 1), date(today.year, 12, 31), "今年"
    recent = re.search(r"最近(\d{1,3})天", question)
    if recent:
        n = int(recent.group(1))
        return today - timedelta(days=n - 1), today, recent.group(0)
    return None


def parse_compare(question: str) -> str | None:
    if "环比" in question:
        return "mom"
    if "同比" in question:
        return "yoy"
    return None


def _to_date(raw: str) -> date:
    y, m, d = re.split(r"[-/]", raw)
    return date(int(y), int(m), int(d))


# ---------------------------------------------------------------- 指标匹配


def _metric_candidates(metric: Metric) -> list[str]:
    try:
        aliases = json.loads(metric.aliases_json or "[]")
    except json.JSONDecodeError:
        aliases = []
    return [metric.code, metric.name, *aliases]


def match_metrics(question: str, metrics: list[Metric]) -> list[Metric]:
    """按问句包含的候选文本匹配；按匹配文本长度降序（越具体越优先）。"""
    q = question.lower()
    hits: list[tuple[int, Metric]] = []
    for m in metrics:
        best = 0
        for cand in _metric_candidates(m):
            c = cand.lower().strip()
            if c and c in q and len(c) > best:
                best = len(c)
        if best:
            hits.append((best, m))
    hits.sort(key=lambda t: -t[0])
    return [m for _, m in hits]


# ---------------------------------------------------------------- LLM 意图


def _llm_intent(config: dict, question: str, metrics: list[Metric], today: date) -> dict | None:
    """LLM 意图解析。输出经严格校验：指标必须在候选清单内，日期必须合法——
    任何越纲/非法字段一律丢弃（宁缺毋滥，防幻觉）。"""
    metric_list = "\n".join(
        f"- code={m.code} | name={m.name} | aliases={json.dumps(_metric_candidates(m)[2:], ensure_ascii=False)}"
        for m in metrics[:200]
    )
    system = (
        "你是 BI 平台的问数意图解析器。只输出一个 JSON 对象，禁止计算任何数值。"
        "字段：metric_code（必须原样取自候选清单的 code，找不到填 null）、"
        "start、end（YYYY-MM-DD 日期字符串）、compare（none/mom/yoy）。"
        "禁止编造候选清单之外的指标。"
    )
    user = (
        f"今天：{today.isoformat()}\n候选指标清单：\n{metric_list or '（无）'}\n"
        f"用户问题：{question}"
    )
    intent = chat_json(config, system, user)
    if not isinstance(intent, dict):
        return None

    validated: dict = {}
    code = intent.get("metric_code")
    if isinstance(code, str) and any(m.code == code for m in metrics):
        validated["metric_code"] = code
    for field in ("start", "end"):
        raw = intent.get(field)
        if isinstance(raw, str):
            try:
                validated[field] = date.fromisoformat(raw.strip()).isoformat()
            except ValueError:
                pass
    if intent.get("compare") in ("none", "mom", "yoy"):
        validated["compare"] = intent["compare"]
    return validated or None


# ---------------------------------------------------------------- 理解卡


def build_card(db: Session, user, question: str) -> dict:
    """问句 → 理解卡（只解析意图，不计算数值）。"""
    question = (question or "").strip()
    if not question:
        raise BusinessError("请输入问题", 40000)

    from app.core.config import get_settings

    settings = get_settings()
    # LLM 配置：模型管理页启用的记录优先，env 兜底；都无则走关键词解析器
    llm_cfg = resolve_llm_config(db, settings)
    today = date.today()

    hidden = restricted_metric_ids(db, user)
    all_active = db.query(Metric).filter(Metric.status == "active").all()
    # 权限继承：候选只含登录用户可见的指标（与看板同一套判定）
    visible = [m for m in all_active if m.id not in hidden]
    restricted_named = [m for m in all_active if m.id in hidden]

    hits = match_metrics(question, visible)
    restricted_hits = match_metrics(question, restricted_named)

    metric: Metric | None = None
    ambiguous: list[dict] = []
    no_metric_reason: str | None = None

    if len(hits) == 1:
        metric = hits[0]
    elif len(hits) > 1:
        metric = hits[0]  # 最具体匹配为默认，其余列为候选（理解卡可改）
        ambiguous.append(
            {
                "field": "metric",
                "options": [{"code": m.code, "name": m.name} for m in hits[:5]],
                "default": metric.code,
                "reason": "问题命中多个指标，请确认要查询的指标",
            }
        )
    elif restricted_hits:
        no_metric_reason = "该指标无权限"
    else:
        no_metric_reason = "没有找到匹配的指标（可先在指标目录确认指标名称或别名）"

    start, end = previous_complete_month(today)
    time_is_explicit = False
    time_hit = parse_time_range(question, today)
    if time_hit:
        start, end, _ = time_hit
        time_is_explicit = True

    compare = parse_compare(question) or "none"

    # LLM 通道：在候选清单内改选指标/给出合法时间与比较方式，越纲字段丢弃
    source = "fallback"
    if llm_cfg:
        intent = _llm_intent(llm_cfg, question, visible, today)
        if intent:
            source = "llm"
            if intent.get("metric_code"):
                metric = next(m for m in visible if m.code == intent["metric_code"])
            if not time_is_explicit and intent.get("start") and intent.get("end"):
                s, e = date.fromisoformat(intent["start"]), date.fromisoformat(intent["end"])
                if s <= e:
                    start, end, time_is_explicit = s, e, True
            if intent.get("compare"):
                compare = intent["compare"]

    disambiguation_note = None
    if metric is not None:
        try:
            dis = json.loads(metric.disambiguation_json or "{}")
        except json.JSONDecodeError:
            dis = {}
        if dis:
            disambiguation_note = {
                "metric_code": metric.code,
                "defaults": dis,
                "hint": "该指标登记了常见歧义场景的默认算法（仅为口径说明，理解卡可修改计算方式）",
            }
            ambiguous.append(
                {
                    "field": "disambiguation",
                    "options": [{"scenario": k, "default": v} for k, v in dis.items()],
                    "default": None,
                    "reason": "指标责任人登记的默认算法说明",
                }
            )

    return {
        "question": question,
        "llm_configured": llm_cfg is not None,
        "source": source,
        "metric": ({"id": metric.id, "code": metric.code, "name": metric.name} if metric else None),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "compare": compare,
        "time_is_explicit": time_is_explicit,
        "ambiguous": ambiguous,
        "disambiguation_note": disambiguation_note,
        "can_compute": metric is not None,
        "no_metric_reason": no_metric_reason,
    }


# ---------------------------------------------------------------- 执行（口径同源）


def execute_card(db: Session, user, *, metric: str | int, start: str, end: str, compare: str = "none") -> dict:
    """理解卡确认后执行。与看板完全同一计算出口（query.service），权限同源。"""
    from app.domain.query import service as query_service

    return query_service.compute_metric_value(
        db, metric_ref=metric, start=start, end=end, compare=compare, user=user
    )
