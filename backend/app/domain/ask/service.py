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


def parse_topn_order(question: str) -> dict:
    """从问句解析 TopN 与排序倾向（规则通道的确定性映射）。"""
    out: dict = {}
    topn = re.search(r"前\s*(\d{1,2})\s*[名个位]?", question)
    if topn:
        out["top_n"] = min(int(topn.group(1)), 50)
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


def build_card(db: Session, user, question: str) -> dict:
    """问句 → 理解卡（只解析意图，不计算数值）。

    B9.2-2 扩展：拆解维度/筛选/排序/TopN 进意图（全部锚定真实数据候选）；
    意图解析不出可执行结构时走「逃生舱」纯对话兜底（mode=help，无数字）。
    """
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
        metric = hits[0]  # 最具体匹配为默认主指标，其余可勾选「同时计算」（B9.2-3）
        ambiguous.append(
            {
                "field": "metric",
                "options": [{"code": m.code, "name": m.name} for m in hits[:5]],
                "default": metric.code,
                "reason": "问题命中多个指标：已默认取最具体的一个，其余可在「同时计算」中勾选",
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

    # 拆解意图（B9.2-2）：先取该指标的候选维度列（权限同源、真实数据锚点）
    dimension: str | None = None
    dim_candidates: list[dict] = []
    dim_values_cache: dict[str, list[str]] = {}
    if metric is not None:
        from app.domain.query.service import list_breakdown_dimensions

        try:
            dim_ctx = list_breakdown_dimensions(db, metric_ref=metric.code, user=user)
            dim_candidates = [d for d in dim_ctx["dimensions"] if d["low_cardinality"]]
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
                    dim_candidates = [d for d in dim_ctx["dimensions"] if d["low_cardinality"]]
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

    card = {
        "question": question,
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
        "dimension_options": [
            {"dataset": c["dataset"], "column": c["column"], "distinct_count": c["distinct_count"]}
            for c in dim_candidates[:10]
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
    if metric is None:
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
    return card


# ---------------------------------------------------------------- 执行（口径同源）


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
) -> dict:
    """理解卡确认后执行。与看板完全同一计算出口（query.service），权限同源。

    B9.2-2：带 dimension 时走拆解出口（compute_metric_breakdown），
    否则单值出口（compute_metric_value）——两者共用同一编译器与缓存纪律。
    """
    from app.domain.query import service as query_service

    if dimension:
        data = query_service.compute_metric_breakdown(
            db, metric_ref=metric, start=start, end=end, compare=compare,
            dimension=dimension, filters=filters or [],
            order_by=order_by, order=order, top_n=top_n, user=user,
        )
        data["kind"] = "breakdown"
        return data
    data = query_service.compute_metric_value(
        db, metric_ref=metric, start=start, end=end, compare=compare, user=user
    )
    data["kind"] = "value"
    return data


# ---------------------------------------------------------------- 空态推荐问题（B9.2-3）


def build_suggestions(db: Session, user, limit: int = 5) -> list[str]:
    """空态推荐问题：从登录用户可见的 active 指标自动生成示例问法。

    行业无关：只用指标名 + 真实低基数维度列名拼模板（不硬编码任何业务词）；
    权限同源（restricted_metric_ids）；维度候选获取失败仅少拆解示例，不阻塞。
    """
    hidden = restricted_metric_ids(db, user)
    visible = [
        m for m in db.query(Metric).filter(Metric.status == "active").all()
        if m.id not in hidden
    ]
    if not visible:
        return []
    out: list[str] = []
    for m in sorted(visible, key=lambda x: -x.id):  # 新建的指标排前面，建议随目录更新
        name = m.name
        out.append(f"上个月{name}是多少")
        out.append(f"上个月{name}环比如何")
        try:
            from app.domain.query.service import list_breakdown_dimensions

            dims = [
                d for d in list_breakdown_dimensions(db, metric_ref=m.code, user=user)["dimensions"]
                if d["low_cardinality"]
            ]
            if dims:
                out.append(f"按{dims[0]['column']}拆解上个月{name}的环比")
        except Exception:
            pass  # 维度候选失败不阻塞推荐
        if len(out) >= limit:
            break
    return out[:limit]
