"""B10-1 异动检测测试：同星期几基准排除周期性误报、真实突刺检出、
样本不足不判断、指标级配置生效、项目批量扫描限量。

夹具设计（周期性靶点）：
- 平日（周一~周六）= 1000，周日 = 100（天然低谷——朴素环比必误报）；
- 2026-03-08（周日）设突刺 5000：同星期几基准下 z 极大 → 检出；
- 2026-03-01（周日）正常 100：朴素环比是 10 倍暴涨，同星期几基准 → 正常。
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

import pytest

START = date(2026, 1, 5)   # 周一
DAILY_OK = date(2026, 3, 1)    # 周日，正常低谷
SPIKE_SUNDAY = date(2026, 3, 8)  # 周日，突刺 5000
SPIKE_MONDAY = date(2026, 3, 9)  # 周一，突刺 5000


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows() -> list[str]:
    """基线带周期内噪声（0/10/20/30/40 循环）：保证同星期几基准样本方差非零，
    z-score 语义正常（恒定基线的阈值语义退化单独在 _judge 纯函数测试覆盖）。"""
    lines = ["d,amount"]
    d = START
    while d <= SPIKE_MONDAY:
        noise = (d.toordinal() % 5) * 10
        if d == SPIKE_SUNDAY or d == SPIKE_MONDAY:
            v = 5000
        elif d.weekday() == 6:  # 周日
            v = 100 + noise
        else:
            v = 1000 + noise
        lines.append(f"{d.isoformat()},{v}")
        d += timedelta(days=1)
    return lines


@pytest.fixture(scope="module")
def anomaly_env(client):
    sfx = _suffix()
    ds_name = f"anom_ds_{sfx}"
    csv = "\n".join(_build_rows()) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"anom_daily_{sfx}",
        "name": "异动检测日销",
        "calc_rule": {
            "base_aggregation": "sum",
            "source": {"table": ds_name, "column": "amount"},
        },
    })
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]

    # 周日天然低谷 → min_samples 需要 8 个同星期几样本；数据自 01-05 起
    client.put(f"/api/metrics/{metric['id']}/anomaly-config", json={
        "z_threshold": 3.0,
        "min_samples": 6,
        "enabled": True,
    })
    yield {"metric": metric, "sfx": sfx}
    client.delete(f"/api/metrics/{metric['id']}")
    ds_id = next(
        d["id"] for d in client.get("/api/datasets").json()["data"] if d["name"] == ds_name
    )
    client.delete(f"/api/datasets/{ds_id}")


class TestAnomalyDetect:
    def test_weekday_low_not_flagged(self, client, anomaly_env):
        """核心验收：周日低谷（朴素环比 10 倍暴涨）不被误报——周期修正生效。"""
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": DAILY_OK.isoformat(),
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["verdict"] == "normal", body
        assert body["abnormal"] is False
        assert body["baseline"]["kind"] == "same_weekday"
        assert body["baseline"]["samples"] >= 6

    def test_spike_on_sunday_detected(self, client, anomaly_env):
        """真实突刺（周日 5000）在同星期几基准下被检出。"""
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": SPIKE_SUNDAY.isoformat(),
        })
        body = resp.json()["data"]
        assert body["verdict"] == "abnormal", body
        assert body["direction"] == "up"
        assert body["current"] == 5000
        assert 100 <= body["baseline"]["mean"] <= 140
        assert body["baseline"]["stdev"] > 0
        assert body["abnormality"] is not None and body["abnormality"] >= 3.0

    def test_spike_on_weekday_detected(self, client, anomaly_env):
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": SPIKE_MONDAY.isoformat(),
        })
        body = resp.json()["data"]
        assert body["verdict"] == "abnormal"
        assert 1000 <= body["baseline"]["mean"] <= 1040

    def test_insufficient_baseline(self, client, anomaly_env):
        """检测日太早（基准样本不足）→ insufficient_baseline 不判断。"""
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": "2026-01-12",
        })
        body = resp.json()["data"]
        assert body["verdict"] == "insufficient_baseline"
        assert body["abnormal"] is False

    def test_no_data_day(self, client, anomaly_env):
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": "2030-01-01",
        })
        body = resp.json()["data"]
        assert body["verdict"] == "no_data"

    def test_bad_date_format_400(self, client, anomaly_env):
        resp = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": "2026/03/08",
        })
        assert resp.status_code == 400


class TestAnomalyConfig:
    def test_get_default_config(self, client, anomaly_env):
        resp = client.get(f"/api/metrics/{anomaly_env['metric']['id']}/anomaly-config")
        body = resp.json()["data"]
        assert body["z_threshold"] == 3.0
        assert body["min_samples"] == 6  # 夹具已 PUT 过
        assert body["enabled"] is True

    def test_put_config_validation(self, client, anomaly_env):
        resp = client.put(f"/api/metrics/{anomaly_env['metric']['id']}/anomaly-config", json={
            "z_threshold": 99,
        })
        assert resp.status_code == 400

    def test_threshold_semantics(self):
        """阈值语义（判定纯函数对账）：阈值越高越保守；恒定基准偏离即反常。"""
        from app.domain.anomaly.service import _judge

        noisy = [1000 + (i % 5) * 10 for i in range(8)]  # 有方差（mean≈1016, pstdev≈14.1）
        abnormal, ab, mean, stdev = _judge(1060, noisy, 3.0)  # z≈3.1
        assert abnormal is True and ab is not None and ab >= 3.0
        assert stdev > 0
        # 同一偏离、更高阈值 → 不判反常（越保守）
        abnormal, ab, _, _ = _judge(1060, noisy, 10.0)
        assert abnormal is False and ab is not None and ab < 10.0
        # 常规值 → 正常
        abnormal, _, _, _ = _judge(1010, noisy, 3.0)
        assert abnormal is False
        # 恒定基准：偏离即反常（阈值不参与），无偏离 → 正常；abnormality 无定义
        abnormal, ab, _, stdev = _judge(5000, [100.0] * 8, 10.0)
        assert abnormal is True and ab is None and stdev == 0
        abnormal, ab, _, _ = _judge(100, [100.0] * 8, 3.0)
        assert abnormal is False and ab is None

    def test_disabled_metric_skipped_in_scan(self, client, anomaly_env):
        """enabled=false 的配置不参与批量扫描（旧语义保留用例，由 TestAnomalyScan 覆盖）。"""
        mid = anomaly_env["metric"]["id"]
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": False})
        scan = client.get("/api/query/anomalies").json()["data"]
        assert all(a["metric_id"] != mid for a in scan["anomalies"])
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": True})


class TestAnomalyScan:
    def test_scan_finds_spike_and_limited(self, client, anomaly_env):
        """批量扫描：opt-in 语义——只扫配置过且启用的指标，突刺被检出且限量 ≤5。"""
        scan = client.get("/api/query/anomalies").json()["data"]
        assert scan["counts"]["configured"] >= 1
        assert scan["counts"]["abnormal"] >= 1
        assert len(scan["anomalies"]) <= 5
        top = scan["anomalies"][0]
        assert top["verdict"] == "abnormal"
        assert {"current", "baseline", "delta", "direction", "abnormality"} <= set(top.keys())

    def test_unconfigured_metrics_not_scanned(self, client, anomaly_env):
        """未配置检测的指标不参与扫描（opt-in 语义，保证扫描性能可控）。"""
        # 建一个未配置的新指标（同一数据集，同样有突刺）→ 不出现在扫描结果
        sfx = anomaly_env["sfx"]
        resp = client.post("/api/metrics", json={
            "code": f"anom_unconf_{sfx}",
            "name": "未配置检测",
            "calc_rule": {
                "base_aggregation": "count",
                "source": {"table": f"anom_ds_{sfx}", "column": "amount"},
            },
        })
        assert resp.status_code == 200, resp.text
        scan = client.get("/api/query/anomalies").json()["data"]
        assert all(a["metric_code"] != f"anom_unconf_{sfx}" for a in scan["anomalies"])
        client.delete(f"/api/metrics/{resp.json()['data']['id']}")

    def test_disabled_config_not_scanned(self, client, anomaly_env):
        """enabled=false 的配置不参与扫描。"""
        mid = anomaly_env["metric"]["id"]
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": False})
        scan = client.get("/api/query/anomalies").json()["data"]
        assert all(a["metric_id"] != mid for a in scan["anomalies"])
        assert scan["counts"]["configured"] == 0
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": True})

    def test_enable_all_idempotent(self, client, anomaly_env):
        """一键开启：未配置的指标批量建配置（保守档）；重复调用幂等。"""
        resp = client.post("/api/query/anomalies/enable-all")
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["total"] >= 1
        first_created = body["enabled_now"]
        resp2 = client.post("/api/query/anomalies/enable-all")
        assert resp2.json()["data"]["enabled_now"] == 0  # 幂等：已有配置不覆盖
        # 新配置生效：扫描覆盖到此前未配置的指标
        scan = client.get("/api/query/anomalies").json()["data"]
        assert scan["counts"]["configured"] >= first_created + 1


# ---------------------------------------------------------------- B10-2 要紧度 + 单层归因


class TestMateriality:
    def test_material_field_present(self, client, anomaly_env):
        """要紧度字段进检测结果：突刺（5000 vs 基准 ~120）远超 5% → material=True。"""
        body = client.post("/api/query/anomaly", json={
            "metric": anomaly_env["metric"]["id"], "date": SPIKE_SUNDAY.isoformat(),
        }).json()["data"]
        assert body["abnormal"] is True
        assert body["material"] is True

    def test_materiality_config_gates(self, client, anomaly_env):
        """要紧度阈值调高（>变化率）→ material=False，异动不构成结论。"""
        mid = anomaly_env["metric"]["id"]
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"materiality_pct": 10000})
        body = client.post("/api/query/anomaly", json={
            "metric": mid, "date": SPIKE_SUNDAY.isoformat(),
        }).json()["data"]
        assert body["abnormal"] is True
        assert body["material"] is False
        assert "要紧度" in body["reason"]
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"materiality_pct": 5.0})


@pytest.fixture(scope="module")
def attr_env(client):
    """归因夹具：两维度组（paid/churn），本期 paid 涨、churn 跌——手算可对账。

    基期（2026-01-10~01-11，紧邻上一等长周期）：paid=300, churn=100 → 总 400
    本期（2026-01-12~01-13）：paid=450, churn=40  → 总 490，总变化 +90
    贡献：paid=+150（166.7%），churn=-60（-66.7%），合计 +90 守恒。
    """
    sfx = _suffix()
    ds_name = f"attr_ds_{sfx}"
    rows = ["d,channel,amount"]
    for d, ch, v in [
        ("2026-01-10", "paid", 150), ("2026-01-10", "churn", 100),
        ("2026-01-11", "paid", 150), ("2026-01-11", "churn", 0),
        ("2026-01-12", "paid", 250), ("2026-01-12", "churn", 20),
        ("2026-01-13", "paid", 200), ("2026-01-13", "churn", 20),
    ]:
        rows.append(f"{d},{ch},{v}")
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(("\n".join(rows) + "\n").encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"attr_sum_{sfx}",
        "name": "归因求和",
        "calc_rule": {
            "base_aggregation": "sum",
            "source": {"table": ds_name, "column": "amount"},
        },
    })
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]
    yield {"metric": metric, "sfx": sfx}
    client.delete(f"/api/metrics/{metric['id']}")
    ds_id = next(
        d["id"] for d in client.get("/api/datasets").json()["data"] if d["name"] == ds_name
    )
    client.delete(f"/api/datasets/{ds_id}")


class TestAttribution:
    def test_contribution_conservation(self, client, attr_env):
        """核心验收：各维贡献合计 = 总变化（手算 +90：paid+150/churn-60）。"""
        resp = client.post("/api/query/attribute", json={
            "metric": attr_env["metric"]["id"],
            "start": "2026-01-12", "end": "2026-01-13",
            "dimension": "channel", "compare": "mom",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["delta"] == 90
        assert body["current_total"] == 490 and body["prev_total"] == 400
        tops = {c["value"]: c for c in body["top_dimensions"]}
        assert tops["paid"]["contribution"] == 150
        assert tops["churn"]["contribution"] == -60
        assert tops["paid"]["contribution_pct"] == pytest.approx(166.6667)
        assert body["explained_delta"] == pytest.approx(body["delta"])
        assert body["conservation_deviation_pct"] == pytest.approx(0, abs=0.5)

    def test_top_n_and_others_bucket(self, client, attr_env):
        """top_n=1 时其余组合并为「其他」桶，Top1+其他 = 总变化。"""
        body = client.post("/api/query/attribute", json={
            "metric": attr_env["metric"]["id"],
            "start": "2026-01-12", "end": "2026-01-13",
            "dimension": "channel", "compare": "mom", "top_n": 1,
        }).json()["data"]
        assert len(body["top_dimensions"]) == 2  # Top1 + 其他
        assert body["top_dimensions"][0]["value"] == "paid"
        total = sum(c["contribution"] for c in body["top_dimensions"])
        assert total == pytest.approx(body["delta"])

    def test_ratio_metric_rejected(self, client, attr_env):
        """比率类指标归因明确拒绝（率差不守恒）。"""
        sfx = _suffix()
        resp = client.post("/api/metrics", json={
            "code": f"attr_ratio_{sfx}",
            "name": "归因比率",
            "calc_rule": {
                "expression": "A / B",
                "operands": {
                    "A": {"table": f"attr_ds_{attr_env['sfx']}", "column": "amount",
                           "aggregation": "sum"},
                    "B": {"table": f"attr_ds_{attr_env['sfx']}", "column": "amount",
                           "aggregation": "count_distinct"},
                },
            },
        })
        assert resp.status_code == 200, resp.text
        ratio = resp.json()["data"]
        resp = client.post("/api/query/attribute", json={
            "metric": ratio["id"],
            "start": "2026-01-12", "end": "2026-01-13",
            "dimension": "channel",
        })
        assert resp.status_code == 400
        assert "比率类" in resp.json()["message"]
        client.delete(f"/api/metrics/{ratio['id']}")

    def test_compare_none_rejected(self, client, attr_env):
        resp = client.post("/api/query/attribute", json={
            "metric": attr_env["metric"]["id"],
            "start": "2026-01-12", "end": "2026-01-13",
            "dimension": "channel", "compare": "none",
        })
        assert resp.status_code == 400

    def test_no_data_400(self, client, attr_env):
        resp = client.post("/api/query/attribute", json={
            "metric": attr_env["metric"]["id"],
            "start": "2030-01-01", "end": "2030-01-02",
            "dimension": "channel",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------- B10-3 站内通知


class TestNotifications:
    def test_scan_generates_deduped_notifications(self, client, anomaly_env):
        """批量扫描落库通知：admin 收到、同 metric+日+方向去重（二次扫描不重复）。"""
        scan = client.get("/api/query/anomalies").json()["data"]
        assert scan["counts"]["abnormal"] >= 1
        first = client.get("/api/notifications").json()["data"]
        assert first, "扫描后应有通知"
        titles = [n["title"] for n in first]
        assert any("异动" in t for t in titles)
        # 二次扫描：去重不刷屏
        client.get("/api/query/anomalies")
        second = client.get("/api/notifications").json()["data"]
        assert len(second) == len(first)

    def test_unread_count_and_read_flow(self, client, anomaly_env):
        unread = client.get("/api/notifications/unread-count").json()["data"]["count"]
        assert unread >= 1
        first = client.get("/api/notifications", params={"unread_only": True}).json()["data"][0]
        resp = client.post("/api/notifications/read", json={"id": first["id"]})
        assert resp.status_code == 200
        after = client.get("/api/notifications/unread-count").json()["data"]["count"]
        assert after == unread - 1
        # 他人通知不可读（按用户隔离）
        from tests.conftest import create_test_user

        other = create_test_user(client, f"notif_other_{anomaly_env['sfx']}", role="analyst")
        resp = client.post("/api/notifications/read", json={"id": first["id"]}, headers=other)
        assert resp.status_code == 404

    def test_read_all(self, client, anomaly_env):
        client.post("/api/notifications/read-all")
        assert client.get("/api/notifications/unread-count").json()["data"]["count"] == 0

    def test_project_scope_filter(self, client, anomaly_env):
        """project_id 过滤：异动通知挂项目，空项目范围查不到。"""
        import uuid

        p = client.post("/api/projects", json={"name": f"notif_empty_{uuid.uuid4().hex[:6]}"}).json()["data"]
        listing = client.get("/api/notifications", params={"project_id": p["id"]}).json()["data"]
        assert listing == []
        client.delete(f"/api/projects/{p['id']}")
