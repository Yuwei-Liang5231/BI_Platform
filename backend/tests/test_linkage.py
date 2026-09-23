"""多指标联动归因测试（A4/P7）。

验收红线：
- 确定性：完全线性相关的候选以 r=1.0 命中且同向；零方差/无关候选被排除；
- 权限同源：受限候选对受限用户不可见（不泄露名称）；
- 降级：无 LLM → interpretation=None + reason=llm_not_configured，候选清单照常返回。
"""

from __future__ import annotations

import io
import uuid

import pytest


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture()
def linkage_env(client):
    """同一数据集四个指标（共享日期列，14 天）：
    a1 线性递增（目标）、a2 = 2×a1（完全同向联动 r=1）、
    a3 恒 100（零方差 → r 无法定义，排除）、a4 交替序列（与线性趋势近似零相关，排除）。
    """
    sfx = _suffix()
    ds = f"link_ds_{sfx}"
    lines = ["d,a1,a2,a3,a4"]
    for i in range(14):
        d = f"2026-06-{i + 1:02d}"
        a1 = 100 + i * 10
        lines.append(f"{d},{a1},{a1 * 2},100,{150 - (i % 2) * 50}")
    csv = "\n".join(lines) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds},
    )
    assert resp.status_code == 200, resp.text

    ids = {}
    for col, name in (("a1", "联动基准"), ("a2", "联动同步"), ("a3", "联动常数"), ("a4", "联动交替")):
        resp = client.post("/api/metrics", json={
            "code": f"link_{col}_{sfx}",
            "name": name,
            "calc_rule": {"base_aggregation": "sum", "source": {"table": ds, "column": col}},
        })
        assert resp.status_code == 200, resp.text
        ids[col] = resp.json()["data"]["id"]
    yield {"ids": ids, "sfx": sfx}
    for mid in ids.values():
        client.delete(f"/api/metrics/{mid}")


class TestMetricLinkage:
    def test_linear_candidate_hits_with_r1(self, client, linkage_env):
        """完全线性相关候选 r=1.0 同向命中；无关/零方差排除；无 LLM 降级不阻塞清单。"""
        r = client.get("/api/ai/metric-linkage", params={
            "metric_id": linkage_env["ids"]["a1"],
            "start": "2026-06-08",
            "end": "2026-06-14",
        })
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        codes = [c["metric_code"] for c in data["candidates"]]

        c2 = next(
            c for c in data["candidates"]
            if c["metric_code"] == f"link_a2_{linkage_env['sfx']}"
        )
        assert c2["correlation"] == 1.0
        assert c2["linkage"] == "same"
        assert c2["direction"] == "up"
        assert c2["change_pct"] is not None and c2["change_pct"] > 0
        assert "同向联动" in c2["note"]

        # 零方差（常数）与交替无关序列不入榜
        assert f"link_a3_{linkage_env['sfx']}" not in codes
        assert f"link_a4_{linkage_env['sfx']}" not in codes

        # 目标自身方向（递增序列：本期合计 > 前等长窗口合计）
        assert data["target_direction"] == "up"
        assert data["target_change_pct"] is not None and data["target_change_pct"] > 0

        # 无 LLM：解释降级、候选清单与规则句照常
        assert data["interpretation"] is None
        assert data["source"] == "rule"
        assert data["reason"] == "llm_not_configured"
        assert "联动同步" in data["rule_text"]

    def test_restricted_candidate_hidden(self, client, linkage_env):
        """受限候选对受限用户不可见（不泄露名称），清单为空显式给原因。"""
        from tests.conftest import create_test_user

        vw = create_test_user(client, f"vw_link_{linkage_env['sfx']}", role="viewer")
        client.put(
            f"/api/auth/metrics/{linkage_env['ids']['a2']}/restrictions",
            json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
        )
        r = client.get(
            "/api/ai/metric-linkage",
            params={"metric_id": linkage_env["ids"]["a1"], "start": "2026-06-08", "end": "2026-06-14"},
            headers=vw,
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["candidates"] == []
        assert data["reason"] == "no_candidates"
        assert "联动同步" not in (data["rule_text"] or "")

    def test_single_day_window_expanded(self, client, linkage_env):
        """详情页常见单日区间：窗口自动向前扩到 7 天（以 end 为锚），可正常算出联动。"""
        r = client.get("/api/ai/metric-linkage", params={
            "metric_id": linkage_env["ids"]["a1"],
            "start": "2026-06-14",
            "end": "2026-06-14",
        })
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["window"]["expanded"] is True
        assert data["window"]["start"] == "2026-06-08"
        assert data["window"]["end"] == "2026-06-14"
        codes = [c["metric_code"] for c in data["candidates"]]
        assert f"link_a2_{linkage_env['sfx']}" in codes

    def test_deleted_metric_rejected(self, client, linkage_env):
        """软删指标作为目标：显式报错（不静默返回空清单）。"""
        mid = linkage_env["ids"]["a3"]
        client.delete(f"/api/metrics/{mid}")
        r = client.get("/api/ai/metric-linkage", params={
            "metric_id": mid, "start": "2026-06-08", "end": "2026-06-14",
        })
        assert r.status_code in (400, 404), r.text
