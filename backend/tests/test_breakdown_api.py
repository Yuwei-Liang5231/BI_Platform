"""B9.2-1 维度拆解计算出口 golden 测试。

验收主线（执行方案 11.3 B9.2-1）：
- 加性指标：各组值合计 ≈ 同口径单值（守恒）
- 比率指标：每组 = 该组分子/该组分母（禁止对"率"求平均——用构造数据区分）
- 越纲拒绝：维度列不存在 / 过滤列解析不了 → 400 可行动报错
- 组内环比：基期区间与单值口径一致；无基期数据的组 change=None
- 排序/TopN/空值末尾；缓存命中；全期常数指标 compare=null
- 候选维度列：文本列 + 现算基数（日期/数值列不进候选）
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy.orm import sessionmaker

from app.infra.cache import metric_cache
from app.infra.database import get_engine
from app.infra.models import Dataset

DATASET_NAME = "br_sales"
GMV_CODE = "br_gmv"
AOV_CODE = "br_aov"
CONST_CODE = "br_const"

# 2026-01: east paid 300 + 100 = 400（另有 unpaid 30），west paid 70
# 2025-12: east paid 200（环比基期）
SALES_ROWS = [
    ["2025-12-15", "200", "paid", "east"],
    ["2026-01-10", "300", "paid", "east"],
    ["2026-01-20", "70", "paid", "west"],
    ["2026-01-21", "30", "unpaid", "east"],
    ["2026-01-25", "100", "paid", "east"],
]

# 常数指标数据集：无日期列（全期常数）
CONST_ROWS = [["east", "10"], ["west", "5"], ["east", "15"]]


def _csv_bytes(header, rows):
    lines = [",".join(header)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def br_env(client):
    """上传含维度列的销售数据集 + 加性/比率/常数三个指标。"""
    metric_cache.clear()
    resp = client.post(
        "/api/datasets/upload",
        files={"file": ("br_sales.csv", io.BytesIO(_csv_bytes(
            ["sale_date", "amount", "status", "region"], SALES_ROWS
        )), "text/csv")},
        data={"name": DATASET_NAME},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/datasets/upload", files={
        "file": ("br_const.csv", io.BytesIO(_csv_bytes(["region", "amount"], CONST_ROWS)), "text/csv")
    }, data={"name": "br_const_ds"})
    assert resp.status_code == 200, resp.text

    resp = client.post("/api/metrics", json={
        "code": GMV_CODE, "name": "拆解GMV",
        "calc_rule": {"base_aggregation": "sum",
                      "source": {"table": DATASET_NAME, "column": "amount",
                                 "filter": "status = 'paid'"}},
    })
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": AOV_CODE, "name": "拆解客单价",
        "calc_rule": {"expression": "A / B", "operands": {
            "A": {"table": DATASET_NAME, "column": "amount", "aggregation": "sum",
                  "filter": "status = 'paid'"},
            "B": {"table": DATASET_NAME, "aggregation": "count"},
        }},
    })
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": CONST_CODE, "name": "拆解常数",
        "calc_rule": {"base_aggregation": "sum", "time_field": None,
                      "source": {"table": "br_const_ds", "column": "amount"}},
    })
    assert resp.status_code == 200, resp.text
    return {"gmv": GMV_CODE, "aov": AOV_CODE, "const": CONST_CODE}


def _breakdown(client, metric, start, end, dimension="region", **extra):
    body = {"metric": metric, "start": start, "end": end, "dimension": dimension}
    body.update(extra)
    return client.post("/api/query/breakdown", json=body)


def _rows_map(data):
    return {r["dimension"]: r for r in data["rows"]}


class TestAdditiveBreakdown:
    def test_group_sum_conserves_single_value(self, client, br_env):
        """加性指标：各组合计 ≈ 同口径单值（2026-01 paid = 470）。
        区间取 01-01~01-25（覆盖截止日）保证完整周期。"""
        single = client.post("/api/query/metric-value", json={
            "metric": br_env["gmv"], "start": "2026-01-01", "end": "2026-01-25",
        }).json()["data"]["value"]
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-25")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        total = sum(r["value"] for r in data["rows"])
        assert total == pytest.approx(single)
        assert total == pytest.approx(470.0)
        assert data["total_groups"] == 2
        assert data["period_complete"] is True
        assert data["cache"] == "miss"
        # B9.2-5 占比：正值组 share 合计 = 100%
        assert sum(r["share"] for r in data["rows"]) == pytest.approx(1.0)

    def test_share_null_when_total_not_positive(self, client, br_env):
        """B9.2-5：全体组值非正（如全 0）→ 占比无语义，share 恒为 None 不误报。"""
        resp = _breakdown(client, br_env["gmv"], "2026-02-01", "2026-02-25")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        if data["rows"]:
            assert all(r["share"] is None for r in data["rows"])


class TestRatioBreakdown:
    def test_per_group_numerator_denominator(self, client, br_env):
        """比率指标按组重算分子分母：east = paid合计400 / 全部行数3 = 133.33。
        若错误地"先平均再拆"会得到 (300+30+100)/3 = 143.33 —— 本断言区分两者。"""
        resp = _breakdown(client, br_env["aov"], "2026-01-01", "2026-01-31")
        assert resp.status_code == 200, resp.text
        rows = _rows_map(resp.json()["data"])
        assert rows["east"]["value"] == pytest.approx(400.0 / 3, rel=1e-6)
        assert rows["west"]["value"] == pytest.approx(70.0)
        assert rows["east"]["value"] != pytest.approx(143.333, rel=1e-3)

    def test_ratio_group_values_differ_from_simple_avg(self, client, br_env):
        """west 组仅 1 行（70/1=70），此时组率与均值重合不构成区分，
        east 组才是区分点；再验证比率各组值不可加（133.33+70 ≠ 单值口径）。"""
        single = client.post("/api/query/metric-value", json={
            "metric": br_env["aov"], "start": "2026-01-01", "end": "2026-01-31",
        }).json()["data"]["value"]
        resp = _breakdown(client, br_env["aov"], "2026-01-01", "2026-01-31")
        rows = _rows_map(resp.json()["data"])
        group_total = sum(r["value"] for r in rows.values())
        assert single == pytest.approx(470.0 / 4, rel=1e-6)  # 400+70 / 4 行
        assert group_total != pytest.approx(single)          # 率不可加


class TestScopeValidation:
    def test_unknown_dimension_400(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          dimension="not_a_column")
        assert resp.status_code == 400
        assert "拆解维度列" in resp.json()["message"]

    def test_out_of_scope_filter_400(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          filters=[{"column": "nope", "op": "=", "value": "x"}])
        assert resp.status_code == 400
        assert "过滤列" in resp.json()["message"]

    def test_bad_op_400(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          filters=[{"column": "region", "op": "LIKE", "value": "e%"}])
        assert resp.status_code == 400

    def test_bad_order_by_400(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          order_by="hacker")
        assert resp.status_code == 400


class TestFiltersAndCompare:
    def test_filter_narrows_groups(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          filters=[{"column": "region", "op": "=", "value": "east"}])
        data = resp.json()["data"]
        assert data["total_groups"] == 1
        assert data["rows"][0]["dimension"] == "east"
        assert data["rows"][0]["value"] == pytest.approx(400.0)

    def test_mom_per_group(self, client, br_env):
        """组内环比：完整窗口 01-01~01-25 前移 25 天 → 基期 12-07~12-31，
        east 基期 200 → +100%；west 无基期 → None。"""
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-25",
                          compare="mom")
        data = resp.json()["data"]
        assert data["compare"]["type"] == "mom"
        assert data["compare"]["start"] == "2025-12-07"
        assert data["compare"]["end"] == "2025-12-31"
        rows = _rows_map(data)
        assert rows["east"]["prev_value"] == pytest.approx(200.0)
        assert rows["east"]["change_pct"] == pytest.approx(100.0)
        assert rows["east"]["change_abs"] == pytest.approx(200.0)
        assert rows["west"]["prev_value"] is None
        assert rows["west"]["change_pct"] is None

    def test_no_intersection_returns_empty(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2027-01-01", "2027-01-31")
        data = resp.json()["data"]
        assert data["rows"] == []
        assert data["period_complete"] is False
        assert data["cache"] == "bypass"


class TestOrderAndCache:
    def test_order_by_change_abs_asc_nulls_last(self, client, br_env):
        """按绝对变化量升序（"掉得最厉害"在前），无基期组（None）恒排末尾。"""
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          compare="mom", order_by="change_abs", order="asc")
        rows = resp.json()["data"]["rows"]
        assert [r["dimension"] for r in rows] == ["east", "west"]
        assert rows[-1]["change_abs"] is None

    def test_top_n_limits_and_keeps_order(self, client, br_env):
        resp = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-31",
                          order_by="value", order="desc", top_n=1)
        data = resp.json()["data"]
        assert data["total_groups"] == 2
        assert len(data["rows"]) == 1
        assert data["rows"][0]["dimension"] == "east"  # 400 > 70

    def test_cache_hit_on_repeat(self, client, br_env):
        metric_cache.clear()
        first = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-25",
                           compare="mom").json()["data"]
        assert first["cache"] == "miss"
        second = _breakdown(client, br_env["gmv"], "2026-01-01", "2026-01-25",
                            compare="mom").json()["data"]
        assert second["cache"] == "hit"
        assert second["rows"] == first["rows"]

    def test_filter_changes_cache_key(self, client, br_env):
        metric_cache.clear()
        a = _breakdown(client, br_env["gmv"], "2025-12-01", "2025-12-31",
                       filters=[{"column": "region", "op": "=", "value": "east"}]).json()["data"]
        b = _breakdown(client, br_env["gmv"], "2025-12-01", "2025-12-31").json()["data"]
        assert a["cache"] == "miss" and b["cache"] == "miss"  # 不同键，互不污染


class TestConstantMetric:
    def test_constant_breakdown_compare_null(self, client, br_env):
        """全期常数指标拆解：无时间过滤任意区间同值；compare 恒 null（契约 v3）。"""
        resp = _breakdown(client, br_env["const"], "2026-01-01", "2026-01-31",
                          compare="mom")
        data = resp.json()["data"]
        assert data["constant"] is True
        assert data["compare"] is None
        rows = _rows_map(data)
        assert rows["east"]["value"] == pytest.approx(25.0)
        assert rows["west"]["value"] == pytest.approx(5.0)
        assert all(r["prev_value"] is None for r in rows.values())


class TestDimensionCandidates:
    def test_candidates_text_only_with_cardinality(self, client, br_env):
        """候选 = 文本列（string/mixed）+ 现算基数；日期/数值列不进候选。"""
        resp = client.post("/api/query/breakdown-dimensions", json={
            "metric": br_env["gmv"],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["primary_dataset"] == DATASET_NAME
        dims = {(d["dataset"], d["column"]): d for d in data["dimensions"]}
        region = dims[(DATASET_NAME, "region")]
        assert region["distinct_count"] == 2
        assert region["low_cardinality"] is True
        assert (DATASET_NAME, "sale_date") not in dims   # 日期列不是维度候选
        assert (DATASET_NAME, "amount") not in dims      # 数值列不是维度候选
        assert (DATASET_NAME, "status") in dims          # 状态文本列是候选
