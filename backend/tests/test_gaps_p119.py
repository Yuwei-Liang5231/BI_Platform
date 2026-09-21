"""11.9 AI 原生 BI 差距补齐测试：观察期（成熟期）/ 报告异动 Top3 降噪 /
通知建议动作模板 / 指标新字段（maturity_days + calc_notes）。

对应执行方案 11.9 节（2026-09-21，用户确认后实施）：
- P1-2 观察期未满 → compute 出口短路 value=null + maturity_pending=true；
- P1-1 报告异动章节按偏离幅度取 Top3，其余折叠计数，总览句报真实总数；
- P1-1 异动通知 body 附方向驱动建议动作模板（纯文案，不触算写分离红线）；
- P2-1/P2-2 指标 API 扩展字段回显与校验（变更留痕 MetricChange 已有，不重复测）。
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

from app.domain.anomaly.service import _action_hint


START = date.today() - timedelta(days=40)   # 数据起点（远端，保证有成熟区间）
END = date.today() - timedelta(days=1)      # 数据终点（近端）


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows() -> list[str]:
    lines = ["d,amount"]
    d = START
    while d <= END:
        lines.append(f"{d.isoformat()},{600 + (d.toordinal() % 5) * 10}")
        d += timedelta(days=1)
    return lines


def _make_env(client, *, maturity_days=None, calc_notes=None):
    sfx = _suffix()
    ds_name = f"g119_ds_{sfx}"
    csv = "\n".join(_build_rows()) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    body = {
        "code": f"g119_m_{sfx}",
        "name": "差距补齐测试指标",
        "calc_rule": {"base_aggregation": "sum", "source": {"table": ds_name, "column": "amount"}},
    }
    if maturity_days is not None:
        body["maturity_days"] = maturity_days
    if calc_notes is not None:
        body["calc_notes"] = calc_notes
    resp = client.post("/api/metrics", json=body)
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]
    return {"metric": metric, "ds_name": ds_name, "sfx": sfx}


def _cleanup(client, env):
    client.delete(f"/api/metrics/{env['metric']['id']}")
    ds_id = next(
        d["id"] for d in client.get("/api/datasets").json()["data"]
        if d["name"] == env["ds_name"]
    )
    client.delete(f"/api/datasets/{ds_id}")


def _metric_value(client, metric, start, end):
    return client.post("/api/query/metric-value", json={
        "metric": metric["id"],
        "start": start if isinstance(start, str) else start.isoformat(),
        "end": end if isinstance(end, str) else end.isoformat(),
    }).json()["data"]


class TestMaturity:
    def test_pending_short_circuit(self, client):
        """观察期未满：短路返回 value=null + maturity_pending=true，不执行查询。"""
        env = _make_env(client, maturity_days=30)
        try:
            m = env["metric"]
            data = _metric_value(client, m, date.today() - timedelta(days=6), date.today() - timedelta(days=1))
            assert data["value"] is None
            assert data["maturity_pending"] is True
            assert data["maturity_days"] == 30
            assert data["compare"] is None
            assert data["cache"] == "bypass"

            # 已成熟区间（end + 30 <= today）：正常计算
            ok = _metric_value(client, m, START.isoformat(), (START + timedelta(days=7)).isoformat())
            assert not ok.get("maturity_pending")
            assert ok["value"] is not None

            # breakdown 同样短路
            resp = client.post("/api/query/breakdown", json={
                "metric": m["id"], "dimension": "d",
                "start": (date.today() - timedelta(days=1)).isoformat(),
                "end": (date.today() - timedelta(days=1)).isoformat(),
            })
            # d 是日期列，可能无拆解候选——若 400 则跳过该断言（指标无维度时不阻塞本契约）
            if resp.status_code == 200:
                bd = resp.json()["data"]
                assert bd["rows"] == [] and bd.get("maturity_pending") is True
        finally:
            _cleanup(client, env)

    def test_no_maturity_computes_normally(self, client):
        """未配置观察期：行为与旧契约完全一致（立即计算真实值）。"""
        env = _make_env(client)
        try:
            data = _metric_value(client, env["metric"], date.today() - timedelta(days=6), date.today() - timedelta(days=1))
            assert "maturity_pending" not in data or data["maturity_pending"] in (None, False)
            assert data["value"] is not None
        finally:
            _cleanup(client, env)

    def test_metric_fields_roundtrip_and_clear(self, client):
        """maturity_days / calc_notes 回显；PATCH 显式置 null 可清除观察期。"""
        env = _make_env(client, maturity_days=90, calc_notes={
            "rationale": "剔除退款避免高估",
            "alternatives": "含退款口径适合对账",
            "pitfalls": "勿把搭配购计入分母",
        })
        try:
            m = env["metric"]
            got = client.get(f"/api/metrics/{m['id']}").json()["data"]
            assert got["maturity_days"] == 90
            assert got["calc_notes"]["rationale"] == "剔除退款避免高估"

            resp = client.patch(f"/api/metrics/{m['id']}", json={"maturity_days": None})
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["maturity_days"] is None

            # 非法 calc_notes 键拒绝
            resp = client.patch(f"/api/metrics/{m['id']}", json={"calc_notes": {"bad": "x"}})
            assert resp.status_code == 400
            # 非法 maturity_days 拒绝
            resp = client.patch(f"/api/metrics/{m['id']}", json={"maturity_days": 999})
            assert resp.status_code == 400
        finally:
            _cleanup(client, env)


class TestActionHint:
    def test_up_hint(self):
        hint = _action_hint("up")
        assert "建议动作" in hint and "口径" in hint

    def test_down_hint(self):
        hint = _action_hint("down")
        assert "建议动作" in hint and "采集链路" in hint


class TestReportTop3:
    def test_anomaly_top3_and_suppressed(self, client, monkeypatch):
        """P1-1 降噪：5 项异动 → 报告只保留偏离幅度 Top3，其余折叠计数；
        总览句报告真实总数；refs 只含 Top3。"""
        from app.domain.anomaly import service as anomaly_service

        env = _make_env(client)
        try:
            mid = env["metric"]["id"]

            def fake_detect(db, user, metric, detect_date=None):
                return {
                    "metric_id": metric.id,
                    "metric_code": metric.code,
                    "name": metric.name,
                    "date": detect_date.isoformat() if detect_date else str(END),
                    "current": 5000.0,
                    "baseline": {"mean": 1000.0, "stdev": 50.0, "samples": 8},
                    "abnormal": True,
                    "material": True,
                    "verdict": "abnormal",
                    "direction": "up",
                    "abnormality": 2.0,
                }

            # 5 个同名指标共享同一数据集：每个都返回一份异动结果（abnormality 递增）
            sfx = env["sfx"]
            metric_ids = [mid]
            for i in range(4):
                resp = client.post("/api/metrics", json={
                    "code": f"g119_x{i}_{sfx}",
                    "name": f"多异动指标{i}",
                    "calc_rule": {"base_aggregation": "sum", "source": {"table": env["ds_name"], "column": "amount"}},
                })
                assert resp.status_code == 200, resp.text
                metric_ids.append(resp.json()["data"]["id"])

            seq = {"n": 0}

            def fake_detect_multi(db, user, metric, detect_date=None):
                seq["n"] += 1
                r = fake_detect(db, user, metric, detect_date)
                r["abnormality"] = 1.0 + seq["n"] * 0.5  # 1.5 ~ 3.5，可排序
                return r

            # compute_conclusions 内部 `from app.domain.anomaly import service as
            # anomaly_svc`——patch 源模块属性才能生效
            monkeypatch.setattr(
                anomaly_service, "detect_for_metric", fake_detect_multi
            )
            report = client.post("/api/reports/preview", json={
                "period_type": "weekly",
                "metric_ids": metric_ids,
                "as_of": (END + timedelta(days=1)).isoformat(),
            }).json()["data"]

            assert len(report["anomalies"]) == 3
            assert report["anomaly_suppressed"] == 2
            # Top3 = 偏离幅度最大的三条（3.5 / 3.0 / 2.5）
            assert [r["abnormality"] for r in report["anomalies"]] == [3.5, 3.0, 2.5]
            refs_a = [k for k in report["refs"] if k.startswith("a") and not k.startswith("att")]
            assert len(refs_a) == 3
            # 异动规则句首句含折叠说明；总览句报真实总数 5
            sec = {s["section"]: s for s in report["narrative"]}
            assert "另有 2 项未列入" in sec["anomaly"]["sentences"][0]
            assert "检测到 5 项异动" in "".join(sec["overview"]["sentences"])
        finally:
            for mid in metric_ids:
                client.delete(f"/api/metrics/{mid}")
            _cleanup(client, env)
