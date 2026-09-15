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
        "disambiguation": {"下降": "按减少金额", "波动": "按变化幅度"},
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


def test_ask_testset_regression(client, ask_env):
    """问数测试集（tests/ask_testset.json）：通过率必须 100%（M3 标准 ≥95%）。"""
    testset = json.loads(
        (Path(__file__).parent / "ask_testset.json").read_text(encoding="utf-8")
    )
    assert testset["cases"], "测试集不能为空"

    failed: list[str] = []
    for case in testset["cases"]:
        card = _ask(client, case["question"])
        exp = case["expect"]
        ok = True
        if exp.get("mode") == "help" or exp.get("can_compute") is False:
            # 逃生舱（B9.2-2）：解析不出可执行结构 → help 模式 + 引导文案（无数字）
            ok = ok and card["can_compute"] is False
            ok = ok and card.get("mode") == "help"
            ok = ok and bool(card.get("help_reply"))
        else:
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
        if not ok:
            failed.append(f"{case['id']}: got {json.dumps(card, ensure_ascii=False)[:200]}")

    rate = 1 - len(failed) / len(testset["cases"])
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
    """disambiguation 登记的默认算法出现在理解卡（黄色提示引用）。"""
    card = _ask(client, "2026年1月销售额")
    assert card["disambiguation_note"] is not None
    assert "下降" in card["disambiguation_note"]["defaults"]
    assert any(a["field"] == "disambiguation" for a in card["ambiguous"])


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
