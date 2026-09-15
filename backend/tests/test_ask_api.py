"""B9 问数 Agent API 集成测试。

验收主线（计划 3.3 M3）：
- 问数测试集回归（关键词兜底解析器，离线确定性）：问题 → 期望理解卡
- 口径一致性：ask/execute 结果与看板 metric-value 完全一致
- 权限继承：受限指标问数 → "该指标无权限"；执行 403（与看板行为一致）
- "算不了"：无匹配指标明确返回原因，不现场拼装查询
- 歧义：多指标命中黄提示候选；disambiguation 默认算法引用
- LLM 边界：test 环境 LLM 未配置 → source=fallback、llm_configured=false
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.infra.cache import metric_cache

DATASET_NAME = "ask_sales_2026"
METRIC_CODE = "ask_gmv"

SALES_ROWS = [
    ["2025-01-10", "100", "paid"],
    ["2026-01-10", "300", "paid"],
    ["2026-01-20", "70", "paid"],
    ["2026-02-05", "80", "paid"],
]

GMV_RULE = {
    "base_aggregation": "sum",
    "source": {"table": DATASET_NAME, "column": "amount", "filter": "status = 'paid'"},
}


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def ask_env(client):
    metric_cache.clear()
    resp = client.post(
        "/api/datasets/upload",
        files={"file": ("ask_sales.csv", io.BytesIO(_csv_bytes(
            ["sale_date", "amount", "status"], SALES_ROWS
        )), "text/csv")},
        data={"name": DATASET_NAME},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": METRIC_CODE,
        "name": "问数销售额",
        "aliases": ["销售额", "营业额", "GMV"],
        "definition": "已支付订单金额合计",
        "disambiguation": {
            "question": "口径下降时按哪个口径计算？",
            "options": [
                {"name": "下降", "description": "按减少金额"},
                {"name": "波动", "description": "按变化幅度"},
            ],
            "default": "下降",
        },
        "calc_rule": GMV_RULE,
    })
    assert resp.status_code == 200, resp.text
    return client.post("/api/metrics", json={}).status_code  # 占位，返回无用


def _ask(client, question: str):
    return client.post("/api/query/ask", json={"question": question}).json()["data"]


# ---------------------------------------------------------------- 测试集回归


def _relative_range(kind: str, today: date) -> tuple[str, str]:
    if kind == "prev_month":
        last_prev = today.replace(day=1) - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    if kind == "this_month":
        end = today.replace(day=28)
        import calendar

        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        return today.replace(day=1).isoformat(), end.isoformat()
    if kind == "this_year":
        return date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()
    if kind == "last_year":
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat()
    if kind.startswith("last_n_days_"):
        n = int(kind.rsplit("_", 1)[1])
        return (today - timedelta(days=n - 1)).isoformat(), today.isoformat()
    raise ValueError(f"未知 relative_time: {kind}")


def _assert_card(card: dict, exp: dict) -> bool:
    """单轮理解卡断言（主问与追问共用）。"""
    ok = True
    if exp.get("mode") == "help" or exp.get("can_compute") is False:
        # 逃生舱（B9.2-2）：解析不出可执行结构 → help 模式 + 引导文案（无数字）
        ok = ok and card["can_compute"] is False
        ok = ok and card.get("mode") == "help"
        ok = ok and bool(card.get("help_reply"))
        return ok
    ok = ok and card["can_compute"] is True
    ok = ok and card.get("mode") == "analysis"
    ok = ok and card["metric"]["code"] == exp["metric_code"]
    if exp.get("start"):
        ok = ok and card["start"] == exp["start"]
    if exp.get("end"):
        ok = ok and card["end"] == exp["end"]
    if exp.get("relative_time"):
        s, e = _relative_range(exp["relative_time"], date.today())
        ok = ok and card["start"] == s and card["end"] == e
    if exp.get("compare"):
        ok = ok and card["compare"] == exp["compare"]
    if exp.get("dimension"):
        ok = ok and card["dimension"] == exp["dimension"]
    if "filters" in exp:
        ok = ok and card["filters"] == exp["filters"]
    if "top_n" in exp:
        ok = ok and card["top_n"] == exp["top_n"]
    if "inherited" in exp:
        ok = ok and card.get("inherited") is exp["inherited"]
    return ok


def _ask_card(client, question: str, conversation_id: int | None = None):
    body = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/api/query/ask", json=body).json()["data"]


def test_execute_conclusion_and_profile(client, ask_env):
    """B9.2-5：一句话结论（数字与 compute 返回对账）+ 口径摘要。"""
    resp = client.post(
        "/api/query/ask/execute",
        json={"metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-31"},
    )
    d = resp.json()["data"]
    assert d["kind"] == "value"
    assert d["conclusion"] and f"{d['value']:,.2f}" in d["conclusion"]
    assert d["metric_profile"]["kind"] == "flat"
    assert d["metric_profile"]["aggregation"] == "sum"
    assert d["metric_profile"]["source_column"] == "amount"

    # 拆解：结论含第一组维度值与占比；占比合计 = 100%
    resp2 = client.post(
        "/api/query/ask/execute",
        json={
            "metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-31",
            "dimension": "status",
        },
    )
    d2 = resp2.json()["data"]
    assert d2["kind"] == "breakdown"
    assert d2["conclusion"] and d2["rows"][0]["dimension"] in d2["conclusion"]
    assert f"{d2['rows'][0]['value']:,.2f}" in d2["conclusion"]
    shares = [r["share"] for r in d2["rows"] if r["share"] is not None]
    assert sum(shares) == pytest.approx(1.0)


def test_fuzzy_stem_match_covers_colloquial_variant(client, ask_env):
    """口语变体「销售情况」不含别名全词「销售额」→ 词干「销售」近似命中，
    理解卡可计算并带语义近似提示（用户实测：同会话上一问成功、此问曾走逃生舱）。

    注意：client 是 session 级共享库，本测试不做 role 限制类写操作，
    受限场景由 test_permission_inheritance / test_followup_permission_rechecked_per_turn 覆盖。
    """
    card = _ask(client, "华南地区5月的销售情况怎么样啊")
    assert card["can_compute"] is True
    assert card["mode"] == "analysis"
    assert card["metric"]["code"] == METRIC_CODE
    approx = next(a for a in card["ambiguous"] if a["field"] == "metric")
    assert "近似" in approx["reason"]


def test_parse_topn_chinese_numerals():
    """中文数字 TopN（用户实测 bug：说「前五」但显示条数默认 10）。"""
    from app.domain.ask.service import parse_topn_order

    assert parse_topn_order("跌幅最厉害的前五列出来")["top_n"] == 5
    assert parse_topn_order("按地区拆解，前二十名")["top_n"] == 20
    assert parse_topn_order("前十")["top_n"] == 10
    assert parse_topn_order("前3")["top_n"] == 3
    assert parse_topn_order("前两名的销售额")["top_n"] == 2
    assert "top_n" not in parse_topn_order("销售额环比如何")


def test_ask_testset_regression(client, ask_env):
    """问数测试集（tests/ask_testset.json）：通过率必须 100%（M3 标准 ≥95%）。

    B9.2-4：case 带 followups 时逐轮携带上一轮返回的 session_id 链式断言。
    """
    testset = json.loads(
        (Path(__file__).parent / "ask_testset.json").read_text(encoding="utf-8")
    )
    assert testset["cases"], "测试集不能为空"

    failed: list[str] = []
    for case in testset["cases"]:
        card = _ask(client, case["question"])
        ok = _assert_card(card, case["expect"])
        if not ok:
            failed.append(f"{case['id']}: got {json.dumps(card, ensure_ascii=False)[:200]}")
        for fi, fu in enumerate(case.get("followups") or []):
            fu_card = _ask_card(client, fu["question"], card.get("conversation_id"))
            if not _assert_card(fu_card, fu["expect"]):
                failed.append(
                    f"{case['id']}#followup{fi} ({fu['question']}): "
                    f"got {json.dumps(fu_card, ensure_ascii=False)[:200]}"
                )

    total = sum(1 + len(c.get("followups") or []) for c in testset["cases"])
    rate = 1 - len(failed) / total
    assert not failed, f"通过率 {rate:.0%} < 100%：\n" + "\n".join(failed)


# ---------------------------------------------------------------- 口径一致性 / 执行


def test_execute_matches_dashboard_metric_value(client, ask_env):
    """口径一致性（M3 铁律）：问数执行结果与看板 metric-value 完全一致。"""
    card = _ask(client, "2026年1月销售额")
    body = {
        "metric": card["metric"]["code"],
        "start": card["start"],
        "end": card["end"],
        "compare": card["compare"],
    }
    via_ask = client.post("/api/query/ask/execute", json=body).json()["data"]
    via_dash = client.post("/api/query/metric-value", json=body).json()["data"]
    # 数值口径完全一致；且看板同参请求直接命中问数执行写入的同一缓存条目
    # （cache: miss → hit）——口径同源的最强证据
    assert via_ask.pop("kind") == "value"  # B9.2-2：执行结果带出口类型标注
    via_ask.pop("cache"), via_dash.pop("cache")
    # B9.2-5：问数层附加结论/口径摘要（计算数值口径同源，不参与同源比对）
    via_ask.pop("conclusion"), via_ask.pop("metric_profile")
    assert via_ask == via_dash
    assert via_ask["value"] == pytest.approx(370.0)


def test_empty_question_rejected(client, ask_env):
    resp = client.post("/api/query/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_llm_not_configured_flag(client, ask_env):
    """test 环境 LLM 未配置：理解卡明确标注，走确定性兜底解析器。"""
    card = _ask(client, "2026年1月销售额")
    assert card["llm_configured"] is False
    assert card["source"] == "fallback"


# ---------------------------------------------------------------- 歧义与默认算法


def test_disambiguation_note_surfaced(client, ask_env):
    """disambiguation（口径分歧登记，真实 schema question/options/default）出现在理解卡。"""
    card = _ask(client, "2026年1月销售额")
    note = card["disambiguation_note"]
    assert note is not None
    assert note["question"] == "口径下降时按哪个口径计算？"
    names = [o["name"] for o in note["options"]]
    assert "下降" in names and "波动" in names
    default_opts = [o for o in note["options"] if o["is_default"]]
    assert len(default_opts) == 1 and default_opts[0]["name"] == "下降"
    amb = next(a for a in card["ambiguous"] if a["field"] == "disambiguation")
    assert amb["question"] and amb["options"]


def test_no_metric_clear_reason(client, ask_env):
    card = _ask(client, " quantum_flux_vortex 是多少")
    assert card["can_compute"] is False
    assert "没有找到匹配的指标" in card["no_metric_reason"]


# ---------------------------------------------------------------- 拆解意图（B9.2-2）


def test_execute_breakdown_via_ask(client, ask_env):
    """问数执行拆解：与直连 /query/breakdown 完全同一出口（逐字段一致）。"""
    card = _ask(client, "按status拆解2026年1月销售额")
    assert card["dimension"] == "status"
    body = {
        "metric": card["metric"]["code"], "start": card["start"], "end": card["end"],
        "compare": "none", "dimension": "status",
    }
    via_ask = client.post("/api/query/ask/execute", json=body).json()["data"]
    via_direct = client.post("/api/query/breakdown", json=body).json()["data"]
    assert via_ask.pop("kind") == "breakdown"
    via_direct.pop("cache"), via_ask.pop("cache")
    # B9.2-5：问数层附加结论/口径摘要（计算口径同源，不参与同源比对）
    via_ask.pop("conclusion"), via_ask.pop("metric_profile")
    assert via_ask == via_direct
    rows = {r["dimension"]: r["value"] for r in via_ask["rows"]}
    assert rows["paid"] == pytest.approx(370.0)


def test_execute_breakdown_with_filter_and_topn(client, ask_env):
    """筛选 + TopN + 同比基期：理解卡参数逐项生效（2025-01 基期 paid=100）。"""
    body = {
        "metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-31",
        "compare": "yoy", "dimension": "status",
        "filters": [{"column": "status", "op": "=", "value": "paid"}],
        "order_by": "value", "order": "desc", "top_n": 5,
    }
    resp = client.post("/api/query/ask/execute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["kind"] == "breakdown"
    assert data["top_n"] == 5
    row = data["rows"][0]
    assert row["dimension"] == "paid"
    assert row["value"] == pytest.approx(370.0)
    assert row["prev_value"] == pytest.approx(100.0)
    assert row["change_pct"] == pytest.approx(270.0)


def test_dimension_values_endpoint(client, ask_env):
    """筛选值候选：真实取值清单；越纲列 400（编译器判定）。"""
    resp = client.post(
        "/api/query/ask/dimension-values", json={"metric": METRIC_CODE, "column": "status"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["values"] == ["paid"]
    assert data["truncated"] is False

    bad = client.post(
        "/api/query/ask/dimension-values", json={"metric": METRIC_CODE, "column": "not_a_column"}
    )
    assert bad.status_code == 400


def test_help_mode_reply_without_numbers(client, ask_env):
    """逃生舱：解析不出可执行结构 → help 模式 + 引导文案；规则兜底文案不含数字。"""
    card = _ask(client, "今天天气怎么样适合郊游吗")
    assert card["mode"] == "help"
    assert card["can_compute"] is False
    assert card["help_reply"]
    assert "%" not in card["help_reply"]  # 兜底文案绝不出现数值


# ---------------------------------------------------------------- 多轮追问（B9.2-4）


def test_followup_inherits_metric_and_time(client, ask_env):
    """追问「那上个月呢」：继承指标与对比方式，时间按新词重算。"""
    first = _ask(client, "2026年1月销售额")
    assert first["conversation_id"]
    fu = _ask_card(client, "那上个月呢", first["conversation_id"])
    assert fu["inherited"] is True
    assert fu["metric"]["code"] == METRIC_CODE
    s, e = _relative_range("prev_month", date.today())
    assert fu["start"] == s and fu["end"] == e


def test_followup_inherits_dimension(client, ask_env):
    """追问继承拆解维度：「按status拆解2026年1月销售额」→「那上个月呢」。"""
    first = _ask(client, "按status拆解2026年1月销售额")
    assert first["dimension"] == "status"
    fu = _ask_card(client, "那上个月呢", first["conversation_id"])
    assert fu["inherited"] is True
    assert fu["dimension"] == "status"
    s, e = _relative_range("prev_month", date.today())
    assert fu["start"] == s and fu["end"] == e


def test_followup_bogus_conversation_rejected(client, ask_env):
    """伪造 conversation_id（不存在/他人会话）：明确报「会话不存在」，绝不降级继承。"""
    r = client.post("/api/query/ask", json={"question": "那上个月呢", "conversation_id": 999999})
    assert r.json()["code"] != 0
    assert "会话不存在" in r.json()["message"]


def test_conversation_persistence_lifecycle(client, ask_env):
    """B9.2-6 会话持久化：首问自动建会话 → 落库 → 列表可见 → 恢复消息 → 执行回写结果快照。"""
    card = _ask(client, "2026年1月销售额按status拆解")
    conv_id = card["conversation_id"]
    assert conv_id

    # 列表可见且标题取首问前缀
    convs = client.get("/api/query/ask/conversations").json()["data"]
    target = next(c for c in convs if c["id"] == conv_id)
    assert target["title"].startswith("2026年1月销售额")

    # 执行 → 结果快照回写最新 assistant 消息
    exec_resp = client.post(
        "/api/query/ask/execute",
        json={
            "metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-31",
            "dimension": "status", "conversation_id": conv_id,
        },
    )
    assert exec_resp.status_code == 200, exec_resp.text

    # 恢复消息：user/assistant 成对，assistant 结果快照非空
    msgs = client.get(f"/api/query/ask/conversations/{conv_id}/messages").json()["data"]
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[0]["text"] == "2026年1月销售额按status拆解"
    assert msgs[1]["readonly"] is True
    assert isinstance(msgs[1]["results"], list) and len(msgs[1]["results"]) == 1
    assert msgs[1]["results"][0]["data"]["kind"] == "breakdown"
    assert "label" in msgs[1]["results"][0]
    assert msgs[1]["card"]["metric"]["code"] == METRIC_CODE

    # 重命名：列表标题更新；空标题拒绝；他人改报「会话不存在」
    assert client.patch(
        f"/api/query/ask/conversations/{conv_id}", json={"title": "华东GMV环比分析"}
    ).json()["code"] == 0
    convs2 = client.get("/api/query/ask/conversations").json()["data"]
    assert any(c["id"] == conv_id and c["title"] == "华东GMV环比分析" for c in convs2)
    r_empty = client.patch(f"/api/query/ask/conversations/{conv_id}", json={"title": "   "})
    assert r_empty.json()["code"] != 0
    from tests.conftest import create_test_user

    other = create_test_user(client, "ask_rename_other", role="analyst")
    r_other = client.patch(
        f"/api/query/ask/conversations/{conv_id}", json={"title": "偷改"}, headers=other
    )
    assert "会话不存在" in r_other.json()["message"]

    # 删除 → 列表与消息均不可见（他人/不存在同报错）
    assert client.delete(f"/api/query/ask/conversations/{conv_id}").json()["code"] == 0
    assert all(c["id"] != conv_id for c in client.get("/api/query/ask/conversations").json()["data"])
    r = client.get(f"/api/query/ask/conversations/{conv_id}/messages")
    assert r.json()["code"] != 0


def test_conversation_isolated_per_user(client, ask_env):
    """会话按用户隔离：B 用户读/删 A 用户会话 → 「会话不存在」（不泄露存在性）。"""
    from tests.conftest import create_test_user

    card = _ask(client, "2026年1月销售额")
    other = create_test_user(client, "ask_conv_other", role="analyst")
    r1 = client.get(
        f"/api/query/ask/conversations/{card['conversation_id']}/messages", headers=other
    )
    assert "会话不存在" in r1.json()["message"]
    r2 = client.delete(
        f"/api/query/ask/conversations/{card['conversation_id']}", headers=other
    )
    assert "会话不存在" in r2.json()["message"]
    # 归属人仍可读
    r3 = client.get(f"/api/query/ask/conversations/{card['conversation_id']}/messages")
    assert r3.json()["code"] == 0


def test_followup_with_metric_word_is_new_question(client, ask_env):
    """追问句含指标词面 → 按新问题解析（重置话题），不继承。"""
    first = _ask(client, "2026年1月销售额")
    fu = _ask_card(client, "2026年2月营业额是多少", first["conversation_id"])
    assert fu["inherited"] is False
    assert fu["metric"]["code"] == METRIC_CODE
    assert fu["start"] == "2026-02-01" and fu["end"] == "2026-02-28"


def test_followup_permission_rechecked_per_turn(client, ask_env):
    """权限每轮重校验：viewer 首问建会话（指标未受限）→ 中途受限 → 追问继承时提示无权限。

    跨用户直接续他人会话已被会话隔离拦截（见 test_conversation_isolated_per_user）。
    """
    from tests.conftest import create_test_user

    viewer = create_test_user(client, "ask_fu_viewer", role="viewer")
    first = _ask_card(client, "2026年1月销售额")
    # 用 viewer 身份重问一轮建立 viewer 自己的会话
    first = client.post(
        "/api/query/ask", json={"question": "2026年1月销售额"}, headers=viewer
    ).json()["data"]
    assert first["can_compute"] is True

    metric_id = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"][0]["id"]
    resp = client.put(
        f"/api/auth/metrics/{metric_id}/restrictions",
        json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
    )
    assert resp.status_code == 200, resp.text

    fu = client.post(
        "/api/query/ask", json={"question": "那上个月呢", "conversation_id": first["conversation_id"]},
        headers=viewer,
    ).json()["data"]
    assert fu["can_compute"] is False
    assert fu["no_metric_reason"] == "该指标无权限"
    assert fu["metric"] is None


# ---------------------------------------------------------------- 权限继承


def test_permission_inheritance(client, ask_env):
    """受限指标：问数理解卡提示"该指标无权限"；执行 403——与看板行为一致。"""
    metric_id = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"][0]["id"]
    from tests.conftest import create_test_user

    viewer = create_test_user(client, "ask_viewer", role="viewer")
    resp = client.put(
        f"/api/auth/metrics/{metric_id}/restrictions",
        json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
    )
    assert resp.status_code == 200, resp.text

    card_resp = client.post(
        "/api/query/ask", json={"question": "2026年1月销售额"}, headers=viewer
    )
    card = card_resp.json()["data"]
    assert card["can_compute"] is False
    assert card["no_metric_reason"] == "该指标无权限"
    assert card["metric"] is None  # 不泄露名称与口径

    exec_resp = client.post(
        "/api/query/ask/execute",
        json={"metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-31"},
        headers=viewer,
    )
    assert exec_resp.status_code == 403

    # admin 不受影响
    admin_card = _ask(client, "2026年1月销售额")
    assert admin_card["can_compute"] is True


# ---------------------------------------------------------------- 时间解析（规则通道）


def test_parse_time_range_relative_month():
    """相对年份+N月：去年/今年/前年M月 → 具体某年某月，不得落进整年分支。"""
    from app.domain.ask.service import parse_time_range

    today = date(2026, 9, 15)
    # 回归（用户实测 bug）：「去年11月」曾被解析为去年整年
    assert parse_time_range("帮我看看去年11月运行次数前三的工具", today) == (
        date(2025, 11, 1), date(2025, 11, 30), "去年11月",
    )
    assert parse_time_range("今年2月怎么样", today) == (
        date(2026, 2, 1), date(2026, 2, 28), "今年2月",
    )
    assert parse_time_range("前年3月的销售额", today) == (
        date(2024, 3, 1), date(2024, 3, 31), "前年3月",
    )
    # 上上月（必须先于「上个月」词面匹配）
    assert parse_time_range("上上月环比如何", today) == (
        date(2026, 7, 1), date(2026, 7, 31), "上上月",
    )
    # 原有语义不回退
    assert parse_time_range("去年整体怎么样", today) == (
        date(2025, 1, 1), date(2025, 12, 31), "去年",
    )
    assert parse_time_range("上个月销售额", today) == (
        date(2026, 8, 1), date(2026, 8, 31), "上个月",
    )


# ---------------------------------------------------------------- 易用性三件套（B9.2-3）


def test_alias_matching(client, ask_env):
    """B9.2-3 ①：别名（GMV/营业额）与指标主名等价命中。"""
    for alias in ("GMV", "营业额"):
        card = _ask(client, f"2026年1月{alias}")
        assert card["can_compute"] is True
        assert card["metric"]["code"] == METRIC_CODE


def test_multi_metric_parallel(client, ask_env):
    """B9.2-3 ③：问句命中多个可见指标 → multi_metrics 并列清单 + 各指标独立执行。"""
    resp = client.post("/api/metrics", json={
        "code": "ask_profit",
        "name": "问数毛利",
        "aliases": ["毛利"],
        "calc_rule": GMV_RULE,
    })
    assert resp.status_code == 200, resp.text

    card = _ask(client, "2026年1月销售额和毛利")
    assert card["can_compute"] is True
    assert card["metric"]["code"] == METRIC_CODE  # 最具体命中为主指标
    codes = {m["code"] for m in card["multi_metrics"]}
    assert "ask_profit" in codes

    # 并列指标独立走同一 execute 出口（口径同源）
    data = client.post(
        "/api/query/ask/execute",
        json={"metric": "ask_profit", "start": "2026-01-01", "end": "2026-01-31", "compare": "none"},
    ).json()["data"]
    assert data["kind"] == "value"
    assert data["value"] == pytest.approx(370.0)


def test_ask_suggestions_visible_only(client, ask_env):
    """B9.2-3 ②：空态推荐问题来自可见指标；受限指标不出现在受限用户建议中。"""
    data = client.get("/api/query/ask/suggestions").json()["data"]
    assert isinstance(data, list) and data
    assert any("问数销售额" in s for s in data)
    assert all(len(s) <= 60 for s in data)  # 模板句保持短句

    from tests.conftest import create_test_user

    viewer = create_test_user(client, "ask_sug_viewer", role="viewer")
    metric_id = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"][0]["id"]
    resp = client.put(
        f"/api/auth/metrics/{metric_id}/restrictions",
        json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
    )
    assert resp.status_code == 200, resp.text
    data_v = client.get("/api/query/ask/suggestions", headers=viewer).json()["data"]
    assert all("问数销售额" not in s for s in data_v)  # 不泄露受限指标名称
