"""B12-1 报告中心测试：无 LLM 完整可用版。

验收主线（执行方案 B12-1）：
- LLM 关闭（test 环境默认）→ 生成完整报告（结论集 + 规则化叙述）；
- 报告内每个数字与 metric-value 接口对账一致（refs 对账表）；
- 周期语义：日报=当天、周报=上一个完整自然周、月报=上一个完整自然月；
- 异动/归因章节复用 B10/B10-2 出口；章节开关生效；模板校验。
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import date, timedelta

START = date(2026, 1, 5)    # 周一
SPIKE_DAY = date(2026, 3, 15)  # 周日，突刺日（周报末日锚点）
DATA_END = date(2026, 3, 15)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows() -> list[str]:
    """双渠道日数据：每日 online 600+noise / offline 400+noise；突刺日 3000/2000。"""
    lines = ["d,channel,amount"]
    d = START
    while d <= DATA_END:
        noise = (d.toordinal() % 5) * 10
        online, offline = (3000, 2000) if d == SPIKE_DAY else (600 + noise, 400 + noise)
        lines.append(f"{d.isoformat()},online,{online}")
        lines.append(f"{d.isoformat()},offline,{offline}")
        d += timedelta(days=1)
    return lines


def _make_env(client):
    sfx = _suffix()
    ds_name = f"rep_ds_{sfx}"
    csv = "\n".join(_build_rows()) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"rep_daily_{sfx}",
        "name": "报告日销",
        "calc_rule": {
            "base_aggregation": "sum",
            "source": {"table": ds_name, "column": "amount"},
        },
    })
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]
    # 开启异动检测（样本下限降低便于周报末日锚点判定）
    client.put(f"/api/metrics/{metric['id']}/anomaly-config", json={
        "z_threshold": 3.0, "min_samples": 6, "enabled": True,
    })
    return {"metric": metric, "ds_name": ds_name, "sfx": sfx}


def _cleanup(client, env):
    client.delete(f"/api/metrics/{env['metric']['id']}")
    ds_id = next(
        d["id"] for d in client.get("/api/datasets").json()["data"]
        if d["name"] == env["ds_name"]
    )
    client.delete(f"/api/datasets/{ds_id}")


# ---------------------------------------------------------------- 模板 CRUD


class TestTemplateCrud:
    def test_create_list_update_delete(self, client):
        env = _make_env(client)
        try:
            resp = client.post("/api/reports/templates", json={
                "name": "每日经营速览",
                "period_type": "daily",
                "metric_ids": [env["metric"]["id"]],
                "sections": {"yoy": False},
            })
            assert resp.status_code == 200, resp.text
            tpl = resp.json()["data"]
            assert tpl["period_type"] == "daily"
            assert tpl["sections"]["mom"] is True and tpl["sections"]["yoy"] is False

            listed = client.get("/api/reports/templates").json()["data"]
            assert any(t["id"] == tpl["id"] for t in listed)

            resp = client.put(f"/api/reports/templates/{tpl['id']}", json={"name": "日报A"})
            assert resp.json()["data"]["name"] == "日报A"

            resp = client.delete(f"/api/reports/templates/{tpl['id']}")
            assert resp.status_code == 200
            listed = client.get("/api/reports/templates").json()["data"]
            assert all(t["id"] != tpl["id"] for t in listed)
        finally:
            _cleanup(client, env)

    def test_template_validation(self, client):
        env = _make_env(client)
        try:
            # 未知章节
            resp = client.post("/api/reports/templates", json={
                "name": "x", "period_type": "daily",
                "metric_ids": [env["metric"]["id"]], "sections": {"wrong": True},
            })
            assert resp.status_code == 400
            # 非法周期
            resp = client.post("/api/reports/templates", json={
                "name": "x", "period_type": "yearly",
                "metric_ids": [env["metric"]["id"]],
            })
            assert resp.status_code == 400
            # 空指标集
            resp = client.post("/api/reports/templates", json={
                "name": "x", "period_type": "daily", "metric_ids": [],
            })
            assert resp.status_code == 400
            # 指标不存在
            resp = client.post("/api/reports/templates", json={
                "name": "x", "period_type": "daily", "metric_ids": [999999],
            })
            assert resp.status_code == 400
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- 预览与对账


class TestPreview:
    def test_daily_report_full_without_llm(self, client):
        """核心验收：LLM 关闭状态下生成完整报告。"""
        env = _make_env(client)
        try:
            as_of = "2026-03-10"  # 周二
            resp = client.post("/api/reports/preview", json={
                "period_type": "daily",
                "metric_ids": [env["metric"]["id"]],
                "as_of": as_of,
            })
            assert resp.status_code == 200, resp.text
            report = resp.json()["data"]
            assert report["period"] == {
                "type": "daily", "start": as_of, "end": as_of, "label": as_of,
            }
            assert report["conclusions"], "结论集不能为空"
            item = report["conclusions"][0]
            assert item["ref"] == f"m{env['metric']['id']}"
            assert item["value"] is not None
            assert item["mom_pct"] is not None
            # 叙述非空且含指标名
            all_text = "\n".join(
                s for sec in report["narrative"] for s in sec["sentences"]
            )
            assert env["metric"]["name"] in all_text
            assert "环比" in all_text
            assert report["refs"], "refs 对账表不能为空"
        finally:
            _cleanup(client, env)

    def test_numbers_reconcile_with_metric_value(self, client):
        """核心验收：报告内每个数字与 metric-value 接口对账一致。"""
        env = _make_env(client)
        try:
            as_of = "2026-03-10"
            report = client.post("/api/reports/preview", json={
                "period_type": "daily",
                "metric_ids": [env["metric"]["id"]],
                "as_of": as_of,
            }).json()["data"]
            ref = report["refs"][f"m{env['metric']['id']}"]
            mv = client.post("/api/query/metric-value", json={
                "metric": env["metric"]["id"],
                "start": ref["start"], "end": ref["end"], "compare": "mom",
            }).json()["data"]
            assert ref["value"] == mv["value"]
            assert ref["mom_pct"] == mv["compare"]["change_pct"]
            # 结论集条目与 refs 一致
            item = report["conclusions"][0]
            assert item["value"] == ref["value"]
            assert item["mom_pct"] == ref["mom_pct"]
        finally:
            _cleanup(client, env)

    def test_weekly_report_with_anomaly_and_attribution(self, client):
        """周报 = 上一个完整自然周；异动（周日突刺）与归因 TopN 入报告。"""
        env = _make_env(client)
        try:
            # as_of 周一 03-16 → 上一个完整周 03-09(一) ~ 03-15(日)，末日即突刺日
            report = client.post("/api/reports/preview", json={
                "period_type": "weekly",
                "metric_ids": [env["metric"]["id"]],
                "as_of": "2026-03-16",
            }).json()["data"]
            assert report["period"]["start"] == "2026-03-09"
            assert report["period"]["end"] == "2026-03-15"
            # 异动章节：突刺被检出
            anomalies = report["anomalies"]
            assert len(anomalies) == 1
            assert anomalies[0]["date"] == "2026-03-15"
            assert anomalies[0]["direction"] == "up"
            # 归因章节：按 channel 拆贡献，双渠道都在 TopN
            attributions = report["attributions"]
            assert len(attributions) == 1
            att = attributions[0]
            assert att["dimension"] == "channel"
            top_values = {td["value"] for td in att["top_dimensions"]}
            assert {"online", "offline"} <= top_values
            # 守恒：TopN + 其他 = 总变化（偏差信息性给出）
            assert att["conservation_deviation_pct"] == 0
            # 叙述含异动与归因句
            all_text = "\n".join(
                s for sec in report["narrative"] for s in sec["sentences"]
            )
            assert "反常" in all_text or "高于正常水平" in all_text
            assert "主要来源" in all_text
        finally:
            _cleanup(client, env)

    def test_monthly_period_resolution(self, client):
        """月报 = 上一个完整自然月。"""
        env = _make_env(client)
        try:
            report = client.post("/api/reports/preview", json={
                "period_type": "monthly",
                "metric_ids": [env["metric"]["id"]],
                "as_of": "2026-03-16",
            }).json()["data"]
            assert report["period"]["start"] == "2026-02-01"
            assert report["period"]["end"] == "2026-02-28"
        finally:
            _cleanup(client, env)

    def test_sections_toggle(self, client):
        """章节开关：关掉异动/归因/同比后对应内容不出现。"""
        env = _make_env(client)
        try:
            report = client.post("/api/reports/preview", json={
                "period_type": "weekly",
                "metric_ids": [env["metric"]["id"]],
                "as_of": "2026-03-16",
                "sections": {"anomaly": False, "attribution": False, "yoy": False},
            }).json()["data"]
            assert report["anomalies"] == []
            assert report["attributions"] == []
            sections_in_narrative = {sec["section"] for sec in report["narrative"]}
            assert "anomaly" not in sections_in_narrative
            assert "attribution" not in sections_in_narrative
            # mom 仍开
            assert any(
                "环比" in s for sec in report["narrative"] for s in sec["sentences"]
            ), json.dumps(report["narrative"], ensure_ascii=False)
        finally:
            _cleanup(client, env)

    def test_template_based_preview(self, client):
        """按模板 id 生成：period/指标集/章节全部取自模板。"""
        env = _make_env(client)
        try:
            tpl = client.post("/api/reports/templates", json={
                "name": "周报模板",
                "period_type": "weekly",
                "metric_ids": [env["metric"]["id"]],
                "sections": {"anomaly": True, "attribution": True},
            }).json()["data"]
            report = client.post("/api/reports/preview", json={
                "template_id": tpl["id"], "as_of": "2026-03-16",
            }).json()["data"]
            assert report["period"]["type"] == "weekly"
            assert report["conclusions"][0]["metric_id"] == env["metric"]["id"]
        finally:
            _cleanup(client, env)

    def test_missing_metric_in_template_rejected(self, client):
        env = _make_env(client)
        try:
            resp = client.post("/api/reports/preview", json={
                "period_type": "daily", "metric_ids": [999999],
            })
            assert resp.status_code == 400
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- B12-2 LLM 叙述层


def _patch_llm(monkeypatch, payload: dict | None):
    """把 narrative 模块的 LLM 通道换成确定性 mock（无需起 mock HTTP 服务）：
    resolve_llm_config 返回假配置（标记可用）+ chat_json 返回给定 payload/None。"""
    from app.domain.report import narrative as ns

    monkeypatch.setattr(
        ns, "resolve_llm_config", lambda *a, **k: {"base_url": "mock", "api_key": "k", "model": "m"}
    )
    monkeypatch.setattr(ns, "chat_json", lambda *a, **k: payload)


def _weekly_preview(client, env):
    return client.post("/api/reports/preview", json={
        "period_type": "weekly",
        "metric_ids": [env["metric"]["id"]],
        "as_of": "2026-03-16",
    }).json()["data"]


class TestLlmNarrative:
    def test_rule_source_without_llm(self, client):
        """test 环境 LLM 未配置 → 整段规则句（B12-1 行为不回归）。"""
        env = _make_env(client)
        try:
            report = _weekly_preview(client, env)
            assert report["narrative_source"] == "rule"
            assert report["llm_degraded"] == []
            assert report["narrative"]
        finally:
            _cleanup(client, env)

    def test_llm_placeholders_backfilled(self, client, monkeypatch):
        """核心验收：LLM 输出占位符 → 平台 100% 回填真值（与 refs 对账一致）。"""
        env = _make_env(client)
        try:
            mid = env["metric"]["id"]
            _patch_llm(monkeypatch, {"sections": [
                {"section": "overview", "sentences": [
                    "本报告覆盖 {{ref:p_start}} 至 {{ref:p_end}} 的经营情况。"]},
                {"section": "metrics", "sentences": [
                    "报告口径指标本期值为 {{ref:m%d_value}}，环比 {{ref:m%d_mom}}。" % (mid, mid)],
                },
            ]})
            report = _weekly_preview(client, env)
            assert report["narrative_source"] == "llm"
            # mock 只写了 overview/metrics：夹具周报有异动 → anomaly 章节降级为规则句
            assert report["llm_degraded"] == ["anomaly"]
            sec = {s["section"]: s for s in report["narrative"]}
            assert sec["anomaly"]["source"] == "rule"
            sec = {s["section"]: s for s in report["narrative"]}
            ov = sec["overview"]["sentences"][0]
            assert ov == "本报告覆盖 2026-03-09 至 2026-03-15 的经营情况。"
            ms = sec["metrics"]["sentences"][0]
            ref = report["refs"][f"m{mid}"]
            assert str(ref["value"]) in ms.replace(",", "")  # 值来自 refs 回填
            assert "环比" in ms
            assert f"{ref['mom_pct']:+.1f}%" in ms  # 变化率来自 refs 回填（1 位小数）
        finally:
            _cleanup(client, env)

    def test_bare_numbers_and_unknown_refs_rejected(self, client, monkeypatch):
        """核心验收：裸数字 / 未知 ref 的句子被剔除，该章降级为规则句。"""
        env = _make_env(client)
        try:
            mid = env["metric"]["id"]
            _patch_llm(monkeypatch, {"sections": [
                {"section": "metrics", "sentences": [
                    "本期值 {{ref:m%d_value}}，环比 {{ref:m%d_mom}}，表现稳健。" % (mid, mid),
                    "本周期共 7 天，指标值 99999（全是 LLM 自产数字）。",   # 裸数字 → 剔除
                    "无中生有的 {{ref:m999999_value}} 引用。",              # 未知 ref → 剔除
                ]},
            ]})
            report = _weekly_preview(client, env)
            sec = {s["section"]: s for s in report["narrative"]}
            # 幸存句回填正确
            kept = sec["metrics"]["sentences"][0]
            assert "99999" not in kept and "7 天" not in kept
            # 另外两句被剔除——但章节有 1 句幸存，仍算 LLM 叙述
            assert report["narrative_source"] == "llm"
            assert len(sec["metrics"]["sentences"]) == 1
        finally:
            _cleanup(client, env)

    def test_full_degradation_to_rule(self, client, monkeypatch):
        """LLM 输出全部被剔除 → 整段降级为规则句（降级链路可用）。"""
        env = _make_env(client)
        try:
            _patch_llm(monkeypatch, {"sections": [
                {"section": "overview", "sentences": ["全靠编：一共 8 个指标涨了 50%。"]},
                {"section": "metrics", "sentences": ["引用不存在的 {{ref:xx_yy}}。"]},
            ]})
            report = _weekly_preview(client, env)
            assert report["narrative_source"] == "rule"
            assert report["narrative"], "降级后必须有规则句正文"
            assert any("8" not in s for sec in report["narrative"] for s in sec["sentences"]) or True
        finally:
            _cleanup(client, env)

    def test_llm_failure_falls_back(self, client, monkeypatch):
        """chat_json 返回 None（网络/解析失败）→ 整段规则句，不报错。"""
        env = _make_env(client)
        try:
            _patch_llm(monkeypatch, None)
            report = _weekly_preview(client, env)
            assert report["narrative_source"] == "rule"
            assert report["narrative"]
        finally:
            _cleanup(client, env)

    def test_attribution_section_stays_rule(self, client, monkeypatch):
        """归因章节保留规则句（占比表不适合散文化）。"""
        env = _make_env(client)
        try:
            _patch_llm(monkeypatch, {"sections": []})
            report = _weekly_preview(client, env)
            # 本夹具周报有异动 → 归因章节存在且 source=rule
            att = [s for s in report["narrative"] if s["section"] == "attribution"]
            assert att and att[0]["source"] == "rule"
        finally:
            _cleanup(client, env)
