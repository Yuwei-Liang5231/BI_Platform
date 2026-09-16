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
        """enabled=false 的指标不参与批量扫描（计数中体现）。"""
        mid = anomaly_env["metric"]["id"]
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": False})
        scan = client.get("/api/query/anomalies").json()["data"]
        assert all(a["metric_id"] != mid for a in scan["anomalies"])
        client.put(f"/api/metrics/{mid}/anomaly-config", json={"enabled": True})


class TestAnomalyScan:
    def test_scan_finds_spike_and_limited(self, client, anomaly_env):
        """批量扫描：默认项目内的突刺指标被检出且限量 ≤5。"""
        scan = client.get("/api/query/anomalies").json()["data"]
        assert scan["counts"]["abnormal"] >= 1
        assert len(scan["anomalies"]) <= 5
        top = scan["anomalies"][0]
        assert top["verdict"] == "abnormal"
        assert {"current", "baseline", "delta", "direction", "abnormality"} <= set(top.keys())
