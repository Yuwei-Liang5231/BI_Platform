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
import logging
import re
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.auth.service import restricted_metric_ids
from app.infra.llm import chat_json, resolve_llm_config
from app.infra.models import Metric

logger = logging.getLogger("app.domain.ask")


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

    # 相对年份 + N月：去年11月 / 前年3月 / 今年6月 → 具体某年某月
    # （必须先于下方整年匹配，否则「去年11月」会错落进「去年→整年」分支）
    rel_month = re.search(r"(前年|去年|今年)\s*(\d{1,2})\s*月", question)
    if rel_month:
        y = today.year + {"前年": -2, "去年": -1, "今年": 0}[rel_month.group(1)]
        m = int(rel_month.group(2))
        if 1 <= m <= 12:
            return date(y, m, 1), date(y, m, monthrange(y, m)[1]), rel_month.group(0)

    if re.search(r"上上(个)?月", question):
        # 上上个完整自然月（必须先于「上个月」判断：r"上(一)?个?月" 会命中"上上月"尾部）
        first_of_this = today.replace(day=1)
        last_prev = first_of_this - timedelta(days=1)
        last_prev2 = last_prev.replace(day=1) - timedelta(days=1)
        return last_prev2.replace(day=1), last_prev2, "上上月"
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


# ---------------------------------------------------------------- 拆解意图解析（B9.2-2）


def parse_dimensions(question: str, dim_candidates: list[dict]) -> list[str]:
    """从问句解析拆解维度：候选维度列名出现在问句中即命中（列名是锚点，
    行业无关）。按列名长度降序（越具体越优先），最多取 3 个，多出的进歧义提示。"""
    q = question.lower()
    hits = [
        c["column"] for c in sorted(dim_candidates, key=lambda d: -len(d["column"]))
        if c["column"].lower() in q
    ]
    return hits[:3]


_CN_DIGIT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num_to_int(text: str) -> int | None:
    """中文数字 → 整数（支持 一~九十九，如「五」=5、「二十」=20）。"""
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGIT.get(text)


def parse_topn_order(question: str) -> dict:
    """从问句解析 TopN 与排序倾向（规则通道的确定性映射；支持中文数字「前五」）。"""
    out: dict = {}
    topn = re.search(r"前\s*(\d{1,2})\s*[名个位]?", question)
    if topn:
        out["top_n"] = min(int(topn.group(1)), 50)
    else:
        cn = re.search(r"前\s*([一两二三四五六七八九十]{1,3})\s*[名个位条]?", question)
        if cn:
            n = _cn_num_to_int(cn.group(1))
            if n:
                out["top_n"] = min(n, 50)
    if re.search(r"(掉|跌|降)[得的最]{1,2}(厉害|最多|最狠|最大)|跌幅最大|下滑最", question):
        out["order_by"], out["order"] = "change_pct", "asc"   # 跌幅最深在前
    elif re.search(r"(涨|增|升)[得的最]{1,2}(厉害|最多|最快|最大)|增幅最大|增长最快", question):
        out["order_by"], out["order"] = "change_pct", "desc"
    elif re.search(r"(贡献|变化|差异|影响)(最大|最多|最明显)|波动最大", question):
        out["order_by"], out["order"] = "change_abs", "desc"
    elif re.search(r"(最高|最多|最大|卖得最好|排行)", question) and "top_n" not in out:
        out["order_by"], out["order"] = "value", "desc"
    elif re.search(r"(最低|最少|最小|卖得最差)", question):
        out["order_by"], out["order"] = "value", "asc"
    if "top_n" not in out and re.search(r"最[高低多大小厉害快差]", question):
        out["top_n"] = 10
    return out


def parse_filter_words(question: str) -> list[dict]:
    """从问句解析「只看X / 排除X」筛选意图（词面捕获，值合法性由维度值清单校验）。"""
    out: list[dict] = []
    for m in re.finditer(r"(只看|仅看|筛选)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})", question):
        out.append({"op": "=", "raw": m.group(2)})
    for m in re.finditer(r"(排除|去掉|不含|剔除)\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})", question):
        out.append({"op": "!=", "raw": m.group(2)})
    return out


# 追问语气/指代/相对时间词（B9.2-4）：问句无指标词面但含这些信号 → 继承上一轮意图
_FOLLOWUP_RE = re.compile(
    r"那|再|还|就|只|仅|它|他|她|这|呢|吧$|同比|环比|上(一)?个?月|上上月|去年|今年|本月|上月|最近\d+天"
)


def _looks_like_followup(question: str) -> bool:
    """追问判定辅助：短问句 + 追问语气/指代/时间词。长问句（>30 字）视为新问题。"""
    return len(question) <= 30 and bool(_FOLLOWUP_RE.search(question))


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


_FUZZY_TAIL_CHARS = "额量数值费金"  # 业务后缀：词干近似匹配时剥离（销售额→销售）


def _candidate_stem(candidate: str) -> str:
    """候选文本剥掉尾部业务后缀成词干（至少保留 2 字，避免过度泛化）。"""
    stem = (candidate or "").strip()
    while len(stem) > 2 and stem[-1] in _FUZZY_TAIL_CHARS:
        stem = stem[:-1]
    return stem if len(stem) >= 2 else ""


def match_metrics(question: str, metrics: list[Metric]) -> tuple[list[Metric], dict[Metric, str]]:
    """指标匹配：先精确（问句包含候选全名/别名），无精确命中再做**词干近似**。

    返回 (精确命中列表, 近似命中 {指标: 命中词干})——精确命中优先，
    近似仅作兜底（如「销售情况」词干「销售」≈「销售额」），调用方须在
    理解卡标注"语义近似匹配"提示用户确认。各自按匹配长度降序（越具体越优先）。
    """
    q = question.lower()
    exact: list[tuple[int, Metric]] = []
    approx: dict[Metric, str] = {}
    for m in metrics:
        best = 0
        stem_best = ""
        for cand in _metric_candidates(m):
            c = cand.lower().strip()
            if c and c in q and len(c) > best:
                best = len(c)
            if not stem_best:
                stem = _candidate_stem(c)
                if stem and stem in q:
                    stem_best = stem
        if best:
            exact.append((best, m))
        elif stem_best:
            approx[m] = stem_best
    exact.sort(key=lambda t: -t[0])
    return [m for _, m in exact], approx


# ---------------------------------------------------------------- LLM 意图


def _llm_intent(
    config: dict, question: str, metrics: list[Metric], today: date,
    dim_candidates: list[dict],
) -> tuple[dict | None, str | None]:
    """LLM 意图解析。输出经严格校验：指标必须在候选清单内、维度列/筛选值必须在
    真实数据候选内、枚举字段在白名单内——任何越纲/非法字段一律丢弃（宁缺毋滥，防幻觉）。

    返回 (意图, 失败原因)：意图为 None 时失败原因非空，供理解卡向用户解释降级。
    """
    metric_list = "\n".join(
        f"- code={m.code} | name={m.name} | aliases={json.dumps(_metric_candidates(m)[2:], ensure_ascii=False)}"
        for m in metrics[:200]
    )
    dim_list = "\n".join(
        f"- {c['column']}（数据集 {c['dataset']}，基数 {c['distinct_count']}）"
        for c in dim_candidates[:30]
    ) or "（无可用维度列）"
    system = (
        "你是 BI 平台的问数意图解析器。只输出一个 JSON 对象，禁止计算任何数值。\n"
        "字段名必须与下面完全一致（禁止自创字段名或换名字）：\n"
        '{"metric_code": "指标code或null", "start": "YYYY-MM-DD或null", "end": "YYYY-MM-DD或null", '
        '"compare": "none|mom|yoy", "dimension": "维度列名或null", '
        '"filters": [{"column": "维度列名", "op": "=或!=", "value": "筛选值"}]或[], '
        '"order_by": "value|change_abs|change_pct或null", "order": "asc|desc或null", "top_n": 整数或null}\n'
        "示例输出：{\"metric_code\": \"gmv_paid\", \"start\": \"2026-08-01\", \"end\": \"2026-08-31\", "
        "\"compare\": \"mom\", \"dimension\": \"channel\", \"filters\": [], "
        "\"order_by\": \"change_pct\", \"order\": \"asc\", \"top_n\": 5}\n"
        "metric_code 必须原样取自候选清单的 code，找不到填 null；"
        "禁止编造候选清单之外的指标、维度列或筛选值；用户问题与指标数据无关时不要硬套字段。"
        "若问题提到某维度列的具体取值（如地区名、渠道名、状态词），dimension 填该列并在 filters 中加对应筛选（op=\"=\"）。"
    )
    user = (
        f"今天：{today.isoformat()}\n候选指标清单：\n{metric_list or '（无）'}\n"
        f"候选维度列（当前用户可见、可拆解）：\n{dim_list}\n"
        f"用户问题：{question}"
    )
    intent = chat_json(config, system, user)
    if not isinstance(intent, dict):
        logger.info("LLM 意图解析：模型未返回有效 JSON（降级规则解析器），question=%r", question[:80])
        return None, "模型返回格式异常（可能为推理型模型输出被截断），已用规则解析兜底"

    valid_cols = {c["column"] for c in dim_candidates}
    validated: dict = {}
    dropped: list[str] = []
    code = intent.get("metric_code")
    if isinstance(code, str) and any(m.code == code for m in metrics):
        validated["metric_code"] = code
    else:
        dropped.append(f"metric_code={code!r}")
    for field in ("start", "end"):
        raw = intent.get(field)
        if isinstance(raw, str):
            try:
                validated[field] = date.fromisoformat(raw.strip()).isoformat()
            except ValueError:
                dropped.append(f"{field}={raw!r}")
    if intent.get("compare") in ("none", "mom", "yoy"):
        validated["compare"] = intent["compare"]
    else:
        dropped.append(f"compare={intent.get('compare')!r}")
    # 维度：单一列（编译器一次只支持一个拆解维度），越纲丢弃
    dim = intent.get("dimension")
    if isinstance(dim, str) and dim.strip() in valid_cols:
        validated["dimension"] = dim.strip()
    elif dim:
        dropped.append(f"dimension={dim!r}")
    # 筛选：列必须在候选内、op 白名单、value 非空（值合法性由 build_card 对真实数据校验）
    filters = intent.get("filters")
    if isinstance(filters, list):
        kept = []
        for f in filters[:5]:
            if not isinstance(f, dict):
                continue
            col, op = f.get("column"), f.get("op")
            if isinstance(col, str) and col.strip() in valid_cols and op in ("=", "!=") \
                    and f.get("value") not in (None, ""):
                kept.append({"column": col.strip(), "op": op, "value": f["value"]})
        if kept:
            validated["filters"] = kept
    if intent.get("order_by") in ("value", "change_abs", "change_pct"):
        validated["order_by"] = intent["order_by"]
    if intent.get("order") in ("asc", "desc"):
        validated["order"] = intent["order"]
    topn = intent.get("top_n")
    if isinstance(topn, int) and 1 <= topn <= 50:
        validated["top_n"] = topn
    if dropped:
        logger.info("LLM 意图校验丢弃越纲字段（防幻觉降级）: %s", "; ".join(dropped))
    logger.info(
        "LLM 意图解析结果: question=%r, validated=%s",
        question[:80], json.dumps(validated, ensure_ascii=False),
    )
    if not validated:
        return None, "模型返回的意图字段均不在候选范围内（防幻觉机制拦截），已用规则解析兜底"
    return validated, None


def _llm_help_reply(config: dict, question: str, metric_names: list[str]) -> str | None:
    """逃生舱（B9.2-2）：意图解析不出可执行结构时，LLM 纯对话兜底——
    只做引导与建议改写，禁止出现任何数值（含 % 的行一律拒收降级）。"""
    system = (
        "你是 BI 平台的问数助手。用户的问题超出了平台当前能力（平台只能回答："
        "已有指标的数值、按维度拆解、时间环比/同比对比）。请用不超过 3 句中文回复："
        "1) 说明无法直接回答该问题；2) 结合指标目录建议一个可执行的改写问法"
        "（必须引用目录中的指标名称）。严禁编造任何数值、百分比或计算结果。"
    )
    user = f"指标目录：{json.dumps(metric_names[:50], ensure_ascii=False)}\n用户问题：{question}"
    try:
        reply = chat_json(config, system + '\n输出格式：{"reply": "中文回复"}', user)
        if isinstance(reply, dict):
            reply = reply.get("reply")
        if isinstance(reply, str) and reply.strip() and not re.search(r"\d+(\.\d+)?%", reply):
            return reply.strip()[:500]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 理解卡


def build_card(db: Session, user, question: str, conversation_id: int | None = None) -> dict:
    """问句 → 理解卡（只解析意图，不计算数值）。

    B9.2-2 扩展：拆解维度/筛选/排序/TopN 进意图（全部锚定真实数据候选）；
    意图解析不出可执行结构时走「逃生舱」纯对话兜底（mode=help，无数字）。
    B9.2-4 多轮追问：问句无指标词面且带追问语气/指代/时间词时，继承上一轮
    意图（metric/dimension/filters/排序/对比），时间按新词重算、无新词则继承；
    权限每轮重校验（继承指标已受限 → 提示无权限，不泄露名称）。
    """
    question = (question or "").strip()
    if not question:
        raise BusinessError("请输入问题", 40000)

    from app.core.config import get_settings
    from app.domain.ask import session as ask_session
    from app.domain.ask import conversations as ask_conversations
    from app.domain.query.service import BREAKDOWN_MAX_CARDINALITY

    settings = get_settings()
    # LLM 配置：模型管理页启用的记录优先，env 兜底；都无则走关键词解析器
    llm_cfg = resolve_llm_config(db, settings)
    today = date.today()

    # B9.2-6 会话持久化：首问自动建会话；意图继承键 = 会话 id（TTL 语义不变）
    conv = ask_conversations.get_or_create_conversation(db, user, conversation_id)
    session_key = f"conv-{conv.id}"

    hidden = restricted_metric_ids(db, user)
    all_active = db.query(Metric).filter(Metric.status == "active").all()
    # 权限继承：候选只含登录用户可见的指标（与看板同一套判定）
    visible = [m for m in all_active if m.id not in hidden]
    restricted_named = [m for m in all_active if m.id in hidden]

    prev_intent = ask_session.get_intent(session_key)
    hits, approx_hits = match_metrics(question, visible)
    restricted_hits, restricted_approx = match_metrics(question, restricted_named)

    metric: Metric | None = None
    ambiguous: list[dict] = []
    no_metric_reason: str | None = None
    inherited = False       # B9.2-4：本轮为追问轮且成功继承上一轮指标

    if len(hits) == 1:
        metric = hits[0]
    elif len(hits) > 1:
        metric = hits[0]  # 最具体匹配为默认主指标，其余可勾选「同时计算」（B9.2-3）
        ambiguous.append(
            {
                "field": "metric",
                "options": [{"code": m.code, "name": m.name} for m in hits[:5]],
                "default": metric.code,
                "reason": "问题命中多个指标：已默认取最具体的一个，其余可在「同时计算」中勾选",
            }
        )
    elif restricted_hits or restricted_approx:
        # 精确/近似命中受限指标：与 exact 行为一致，提示无权限不泄露名称
        no_metric_reason = "该指标无权限"
    elif approx_hits:
        # 语义近似兜底（B9.2-5 后补）：口语变体如「销售情况」词干「销售」≈「销售额」
        approx_sorted = sorted(approx_hits.items(), key=lambda t: -len(t[1]))
        metric = approx_sorted[0][0]
        ambiguous.append(
            {
                "field": "metric",
                "options": [{"code": m.code, "name": m.name} for m, _ in approx_sorted[:5]],
                "default": metric.code,
                "reason": (
                    f"未命中指标全名，已按语义近似匹配（「{approx_sorted[0][1]}」≈「{metric.name}」）；"
                    "如不对请在理解卡中修改"
                ),
            }
        )
    elif prev_intent is not None and _looks_like_followup(question):
        # 追问轮：继承上一轮指标；权限每轮重校验（继承指标已受限 → 提示无权限，不泄露名称）
        prev_code = prev_intent.get("metric_code")
        prev_metric = next((m for m in visible if m.code == prev_code), None)
        if prev_metric is not None:
            metric = prev_metric
            inherited = True
        elif any(m.code == prev_code for m in restricted_named):
            no_metric_reason = "该指标无权限"
        # 继承指标已被删除：落回下方「无匹配指标」首问处理
    if metric is None and no_metric_reason is None:
        no_metric_reason = "没有找到匹配的指标（可先在指标目录确认指标名称或别名）"

    start, end = previous_complete_month(today)
    time_is_explicit = False
    time_inherited = False
    time_hit = parse_time_range(question, today)
    if time_hit:
        start, end, _ = time_hit
        time_is_explicit = True
    elif inherited and prev_intent.get("start") and prev_intent.get("end"):
        # 追问轮无新时间词 → 继承上一轮时间（如「那上个月呢」之外的纯指代）
        start = date.fromisoformat(prev_intent["start"])
        end = date.fromisoformat(prev_intent["end"])
        time_inherited = True

    parsed_compare = parse_compare(question)
    if parsed_compare:
        compare = parsed_compare
    elif inherited and prev_intent.get("compare"):
        compare = prev_intent["compare"]  # 追问轮无对比词 → 沿用上一轮口径
    else:
        compare = "none"

    # 拆解意图（B9.2-2）：先取该指标的候选维度列（权限同源、真实数据锚点）
    dimension: str | None = None
    dim_candidates: list[dict] = []
    dim_values_cache: dict[str, list[str]] = {}
    if metric is not None:
        from app.domain.query.service import list_breakdown_dimensions

        try:
            dim_ctx = list_breakdown_dimensions(db, metric_ref=metric.code, user=user)
            # V1.3.2：候选不再限低基数——业务从哪个维度拆只有业务知道；
            # 仅拦 ID 类极端高基数（>5000 值，拆解无洞察且拖垮聚合），高基数列由前端标注提示
            dim_candidates = [d for d in dim_ctx["dimensions"] if d["distinct_count"] <= BREAKDOWN_MAX_CARDINALITY]
        except Exception:
            dim_candidates = []  # 维度候选获取失败不阻塞理解卡，仅少拆解能力

    def _dimension_values(col: str) -> list[str]:
        """维度列真实取值（覆盖区间内），带会话内缓存——LLM/规则筛选值校验共用。"""
        if col not in dim_values_cache:
            try:
                from app.domain.query.service import list_dimension_values

                dim_values_cache[col] = list_dimension_values(
                    db, metric_ref=metric.code, column=col, user=user
                )["values"]
            except Exception:
                dim_values_cache[col] = []
        return dim_values_cache[col]

    def _ground_filters(raw_filters: list[dict]) -> list[dict]:
        """筛选值落地校验：value 必须出现在该维度列真实取值中（防幻觉硬筛）。"""
        grounded = []
        for f in raw_filters[:3]:
            val = str(f.get("value", "")).strip()
            if not val:
                continue
            values = _dimension_values(f["column"])
            exact = next((v for v in values if v == val), None)
            if exact is None:
                fuzzy = next((v for v in values if val in v or v in val), None)
                exact = fuzzy
            if exact is not None:
                grounded.append({"column": f["column"], "op": f.get("op", "="), "value": exact})
        return grounded

    # 规则通道：维度/TopN/排序/「只看X」词面
    dim_hits = parse_dimensions(question, dim_candidates)
    if dim_hits:
        dimension = dim_hits[0]
        if len(dim_hits) > 1:
            ambiguous.append({
                "field": "dimension",
                "options": [{"column": c} for c in dim_hits],
                "default": dimension,
                "reason": "命中多个维度列，一次仅支持按一个维度拆解，请确认",
            })
    topn_intent = parse_topn_order(question)
    rule_filters: list[dict] = []
    if dim_candidates:  # 「只看X / 排除X」不依赖已选维度：词面挂到任一候选列即可
        for f in parse_filter_words(question):
            for col in [dimension] + [c["column"] for c in dim_candidates if c["column"] != dimension]:
                grounded = _ground_filters([{**f, "column": col, "value": f.get("raw")}])
                if grounded:
                    rule_filters.extend(grounded)
                    break

    # 追问轮补全（B9.2-4）：本轮词面未命中的意图字段继承上一轮
    if inherited:
        valid_cols = {c["column"] for c in dim_candidates}
        if dimension is None and prev_intent.get("dimension") in valid_cols:
            dimension = prev_intent["dimension"]  # 上一轮维度仍在本轮候选内才继承
        if not rule_filters and prev_intent.get("filters"):
            prev_filters = [f for f in prev_intent["filters"] if f.get("column") in valid_cols]
            if prev_filters == prev_intent.get("filters"):
                rule_filters = [dict(f) for f in prev_filters]  # 列全部仍有效才整体继承
        for k in ("order_by", "order", "top_n"):
            if topn_intent.get(k) is None and prev_intent.get(k) is not None:
                topn_intent[k] = prev_intent[k]

    # LLM 通道：在候选清单内改选指标/时间/比较/维度/筛选，越纲字段丢弃
    source = "fallback"
    source_note: str | None = None
    if llm_cfg:
        intent, source_note = _llm_intent(llm_cfg, question, visible, today, dim_candidates)
        if intent:
            source = "llm"
            source_note = None
            if intent.get("metric_code"):
                metric = next(m for m in visible if m.code == intent["metric_code"])
                # 指标被 LLM 改选后维度候选需跟随（列名匹配保持真实锚点）
                try:
                    from app.domain.query.service import list_breakdown_dimensions

                    dim_ctx = list_breakdown_dimensions(db, metric_ref=metric.code, user=user)
                    dim_candidates = [
                        d for d in dim_ctx["dimensions"]
                        if d["distinct_count"] <= BREAKDOWN_MAX_CARDINALITY
                    ]
                except Exception:
                    dim_candidates = []
            if not time_is_explicit and intent.get("start") and intent.get("end"):
                s, e = date.fromisoformat(intent["start"]), date.fromisoformat(intent["end"])
                if s <= e:
                    start, end, time_is_explicit = s, e, True
            if intent.get("compare"):
                compare = intent["compare"]
            if not dimension and intent.get("dimension"):
                dimension = intent["dimension"]
            if not rule_filters and intent.get("filters"):
                rule_filters = _ground_filters(intent["filters"])
            for k in ("order_by", "order", "top_n"):
                if intent.get(k) is not None and k not in topn_intent:
                    topn_intent[k] = intent[k]

    # 维度值语义匹配（2026-09-15 用户反馈：问句含「华南地区」却未选维度）：
    # 未解析出拆解维度/筛选时，扫描维度列**真实取值**——问句包含某取值（长度≥2，
    # 取最长命中）→ 自动按该列拆解并加筛选（值锚定真实数据，防幻觉红线不破）
    if metric is not None and dimension is None and not rule_filters and dim_candidates:
        vdim, vval = None, ""
        for c in dim_candidates[:15]:
            for v in (_dimension_values(c["column"]) or [])[:50]:
                if isinstance(v, str) and len(v) >= 2 and v in question and len(v) > len(vval):
                    vdim, vval = c["column"], v
        if vdim:
            dimension = vdim
            rule_filters = [{"column": vdim, "op": "=", "value": vval}]
            ambiguous.append(
                {
                    "field": "dimension",
                    "options": [{"column": vdim, "is_default": True, "name": f"{vdim} = {vval}"}],
                    "default": vdim,
                    "reason": f"检测到问题中包含维度「{vdim}」的取值「{vval}」，已按 {vdim} 拆解并筛选；如不需要可在理解卡中取消",
                }
            )

    disambiguation_note = None
    if metric is not None:
        try:
            dis = json.loads(metric.disambiguation_json or "{}")
        except json.JSONDecodeError:
            dis = {}
        # 口径分歧登记（指标管理页，责任人维护）真实 schema：
        #   {question: str, options: [{name, description} | str], default: str}
        # 兼容历史误存格式 {scenario: 说明}（纯 str→str 映射）。
        if isinstance(dis, dict) and dis:
            if "question" in dis or "options" in dis:
                dis_question = str(dis.get("question") or "").strip()
                raw_opts = dis.get("options") or []
                dis_options: list[dict] = []
                for raw in raw_opts[:6]:
                    o = raw if isinstance(raw, dict) else {"name": str(raw)}
                    name = str(o.get("name") or o.get("label") or "").strip()
                    if not name:
                        continue
                    dis_options.append({
                        "name": name,
                        "description": str(o.get("description") or "").strip() or None,
                        "is_default": name == str(dis.get("default") or "").strip(),
                    })
            else:
                dis_question = "该指标存在多种可能口径"
                dis_options = [
                    {"name": str(k), "description": str(v).strip() or None, "is_default": False}
                    for k, v in list(dis.items())[:6]
                ]
            dis_options = [o for o in dis_options if o["name"]]
            if dis_question or dis_options:
                disambiguation_note = {
                    "metric_code": metric.code,
                    "question": dis_question or None,
                    "options": dis_options,
                    "hint": "该指标责任人登记了口径分歧说明（仅为口径提示，理解卡可修改计算方式）",
                }
                ambiguous.append(
                    {
                        "field": "disambiguation",
                        "question": dis_question or None,
                        "options": dis_options,
                        "reason": "指标责任人登记的口径分歧说明（来源：指标管理页「口径分歧」登记）",
                    }
                )

    card = {
        "question": question,
        # B9.2-6 会话持久化：conversation_id 标识多轮对话（首问自动建会话），
        # 前端每轮携带；历史列表/恢复走 conversations API
        "conversation_id": conv.id,
        "inherited": inherited,
        "time_inherited": time_inherited,
        "llm_configured": llm_cfg is not None,
        "source": source,
        # LLM 降级原因（source=fallback 且已配置 LLM 时非空，供前端向用户解释）
        "source_note": source_note if (source == "fallback" and llm_cfg is not None) else None,
        "metric": ({"id": metric.id, "code": metric.code, "name": metric.name} if metric else None),
        # B9.2-3 多指标并列：问句命中的其余可见指标（默认主指标之外，最多 2 个），
        # 前端勾选后逐指标独立走 execute——数值仍全部由 compute 单点出口计算
        "multi_metrics": (
            [
                {"code": m.code, "name": m.name}
                for m in hits
                if metric is not None and m.id != metric.id and m.code != metric.code
            ][:2]
            if metric is not None else []
        ),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "compare": compare,
        "time_is_explicit": time_is_explicit,
        # B9.2-2 拆解意图
        "dimension": dimension,
        # 全量输出（V1.3.2 拆解维度全量开放后不再 [:15] 截断——高基数列排在
        # distinct 升序尾部，截断会系统性隐藏业务要拆的列；前端 select 可搜索）
        "dimension_options": [
            {"dataset": c["dataset"], "column": c["column"], "distinct_count": c["distinct_count"]}
            for c in dim_candidates
        ],
        "filters": rule_filters,
        "order_by": topn_intent.get("order_by", "value"),
        "order": topn_intent.get("order", "desc"),
        "top_n": topn_intent.get("top_n"),
        "ambiguous": ambiguous,
        "disambiguation_note": disambiguation_note,
        "can_compute": metric is not None,
        "no_metric_reason": no_metric_reason,
    }
    card["mode"] = "analysis" if metric is not None else "help"
    if metric is not None:
        # 会话意图留存（B9.2-4）：仅存意图，数值每轮由 compute 单点出口现算
        ask_session.save_intent(session_key, {
            "metric_code": metric.code,
            "dimension": dimension,
            "filters": rule_filters,
            "order_by": card["order_by"],
            "order": card["order"],
            "top_n": card["top_n"],
            "compare": compare,
            "start": card["start"],
            "end": card["end"],
        })
    else:
        # 逃生舱：意图解析不出可执行结构 → 纯对话兜底（LLM 生成引导，规则通道给固定文案；都无数字）
        help_reply = None
        if llm_cfg:
            help_reply = _llm_help_reply(
                llm_cfg, question, [m.name for m in visible]
            )
        card["help_reply"] = help_reply or (
            "我暂时理解不了这个问题。本平台可以回答已有指标的数值、按维度拆解和时间对比，"
            "例如「上个月按地区拆解销售额的环比」——您可以参考指标目录改写一下问法。"
        )
    logger.info(
        "理解卡生成: source=%s mode=%s metric=%s range=%s~%s compare=%s dimension=%s filters=%s",
        source, card["mode"],
        card["metric"]["code"] if card["metric"] else None,
        card["start"], card["end"], compare, dimension, rule_filters,
    )
    # B9.2-6：本轮对话落库（问句 + 理解卡快照），历史列表/恢复可查
    ask_conversations.append_turn(db, conv, question, card)
    return card


# ---------------------------------------------------------------- 执行（口径同源）


def _metric_profile(metric: Metric) -> dict:
    """指标口径摘要（B9.2-5 ⑥口径透明）：业务可读的 calc_rule 摘要，兼容 flat/expression 两形态。"""
    try:
        rule = json.loads(metric.calc_rule_json or "{}")
    except json.JSONDecodeError:
        rule = {}
    profile: dict = {
        "name": metric.name,
        "definition": metric.definition or "",
    }
    if "base_aggregation" in rule and "source" in rule:
        src = rule.get("source") or {}
        profile.update(
            {
                "kind": "flat",
                "aggregation": rule.get("base_aggregation", ""),
                "source_table": src.get("table", ""),
                "source_column": src.get("column", ""),
                "filter": src.get("filter") or "",
            }
        )
    else:
        operands = rule.get("operands") or {}
        profile.update(
            {
                "kind": "expression",
                "expression": rule.get("expression", ""),
                "operands": [
                    {
                        "name": k,
                        "table": v.get("table", ""),
                        "column": v.get("column") or "",
                        "aggregation": v.get("aggregation", ""),
                        "filter": v.get("filter") or "",
                    }
                    for k, v in operands.items()
                ],
            }
        )
    return profile


_AGG_CN = {"sum": "求和", "avg": "平均", "count": "计数", "max": "最大", "min": "最小", "count_distinct": "去重计数"}


def _fmt_num(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _build_conclusion(data: dict) -> str | None:
    """一句话结论（B9.2-5 ④）：**纯模板拼装**，数字全部来自 compute 单点出口的返回值
    —— LLM 不参与结论生成，算写分离红线不破。解析不出有效数字时返回 None。"""
    kind = data.get("kind")
    if kind == "breakdown":
        rows = [r for r in data.get("rows") or [] if r.get("value") is not None]
        if not rows:
            return None
        top, second = rows[0], rows[1] if len(rows) > 1 else None
        parts = [f"按「{data['dimension']}」拆解共 {data.get('total_groups', len(rows))} 组"]
        head = f"{top['dimension']} 以 {_fmt_num(top['value'])} 排名第一"
        if top.get("share") is not None:
            head += f"，占比 {top['share'] * 100:.1f}%"
        parts.append(head)
        if second is not None:
            tail = f"其后为 {second['dimension']}（{_fmt_num(second['value'])}"
            if second.get("share") is not None:
                tail += f"，占比 {second['share'] * 100:.1f}%"
            tail += "）"
            parts.append(tail)
        cmp = data.get("compare")
        if cmp and top.get("change_pct") is not None:
            arrow = "▲" if top["change_pct"] >= 0 else "▼"
            parts.append(f"{top['dimension']} {cmp['type']}{arrow}{abs(top['change_pct']):.2f}%")
        return "：".join([parts[0], "；".join(parts[1:])]) + "。"
    if kind == "value":
        value = data.get("value")
        if value is None:
            return None
        if data.get("constant"):
            base = f"「{data.get('name')}」为全期常数 {_fmt_num(value)}（与查询区间无关）"
            return base
        span = f"{data['start']} ~ {data['end']}"
        base = f"{span} 「{data.get('name')}」为 {_fmt_num(value)}"
        if not data.get("period_complete") and data.get("data_through"):
            base += f"（数据截至 {data['data_through']}）"
        cmp = data.get("compare")
        if cmp and cmp.get("change_pct") is not None:
            arrow = "▲" if cmp["change_pct"] >= 0 else "▼"
            base += f"，{cmp['type']}{arrow}{abs(cmp['change_pct']):.2f}%（基期 {cmp['start']} ~ {cmp['end']}）"
        return base + "。"
    return None


def execute_card(
    db: Session,
    user,
    *,
    metric: str | int,
    start: str,
    end: str,
    compare: str = "none",
    dimension: str | None = None,
    filters: list[dict] | None = None,
    order_by: str = "value",
    order: str = "desc",
    top_n: int | None = None,
    conversation_id: int | None = None,
) -> dict:
    """理解卡确认后执行。与看板完全同一计算出口（query.service），权限同源。

    B9.2-2：带 dimension 时走拆解出口（compute_metric_breakdown），
    否则单值出口（compute_metric_value）——两者共用同一编译器与缓存纪律。
    B9.2-6：conversation_id 非空时把结果快照回写会话最新一轮（历史恢复可看当时真值）。
    """
    from app.domain.query import service as query_service

    if dimension:
        data = query_service.compute_metric_breakdown(
            db, metric_ref=metric, start=start, end=end, compare=compare,
            dimension=dimension, filters=filters or [],
            order_by=order_by, order=order, top_n=top_n, user=user,
        )
        data["kind"] = "breakdown"
    else:
        data = query_service.compute_metric_value(
            db, metric_ref=metric, start=start, end=end, compare=compare, user=user
        )
        data["kind"] = "value"
    # B9.2-5：一句话结论（模板拼装，数字全部来自上方 compute 返回）+ 口径摘要
    data["conclusion"] = _build_conclusion(data)
    data["metric_profile"] = _metric_profile(query_service._resolve_metric(db, metric))
    _persist_results(db, user, conversation_id, data)
    return data


def _persist_results(db: Session, user, conversation_id: int | None, data: dict) -> None:
    """结果快照回写会话（B9.2-6）：失败不阻塞计算返回（留档尽力而为）。

    存储形态与前端渲染约定一致：[{label, data}]。
    """
    if conversation_id is None:
        return
    try:
        from app.domain.ask import conversations as ask_conversations

        snapshot = [{"label": data.get("name") or "", "data": data}]
        ask_conversations.attach_results(db, user, conversation_id, snapshot)
    except Exception:  # noqa: BLE001 - 留档失败不影响计算结果返回
        logger.warning("问数结果回写会话失败: conversation_id=%s", conversation_id, exc_info=True)


# ---------------------------------------------------------------- 空态推荐问题（B9.2-3）


def build_suggestions(db: Session, user, limit: int = 5) -> list[str]:
    """空态推荐问题：从登录用户可见的 active 指标自动生成示例问法。

    行业无关：只用指标名 + 真实维度列名拼模板（不硬编码任何业务词）；
    权限同源（restricted_metric_ids）；维度候选获取失败仅少拆解示例，不阻塞。
    生成策略（2026-09-15 优化）：跨指标轮转（避免 5 条全是同一指标）、
    业务可读名优先（含中文的指标/维度列排前，代码风命名如 Sum_Saving 降权）。
    """
    hidden = restricted_metric_ids(db, user)
    visible = [
        m for m in db.query(Metric).filter(Metric.status == "active").all()
        if m.id not in hidden
    ]
    if not visible:
        return []

    def _readable(s: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))

    # 业务可读性优先，同级内新建指标排前（建议随目录更新）
    visible.sort(key=lambda m: (-int(_readable(m.name)), -m.id))

    from app.domain.query.service import list_breakdown_dimensions

    per_metric: list[list[str]] = []
    for m in visible[:limit]:
        name = m.name
        items = [f"上个月{name}是多少", f"上个月{name}环比如何"]
        try:
            dims = list_breakdown_dimensions(db, metric_ref=m.code, user=user)["dimensions"]
            # 拆解示例的列名同样业务可读优先，同级取低基数（拆出来更好读）
            dims.sort(key=lambda d: (-int(_readable(d["column"])), d["distinct_count"]))
            if dims:
                items.append(f"按「{dims[0]['column']}」拆解上个月{name}的环比")
        except Exception:
            pass  # 维度候选失败不阻塞推荐
        per_metric.append(items)

    # 轮转交错：第 1 条取指标1、第 2 条取指标2…让 5 条覆盖多个指标
    out: list[str] = []
    idx = 0
    while len(out) < limit and idx < limit:
        for items in per_metric:
            if idx < len(items):
                out.append(items[idx])
                if len(out) >= limit:
                    break
        idx += 1
    return out[:limit]
