"""B3 统一计算服务 API 集成测试。

验收主线（执行方案 B3，契约 v2 于 2026-09-11 修订）：
- 口径一致性：同一指标同一范围，metric-value 结果与手工 SQL 一致
- 部分周期（契约 v2）：覆盖与区间相交即返回真实值 + data_through 标注；
  值可入缓存（键含覆盖端点，补录数据后自然失效）
- 缓存失效：改口径（ver+1）/ 数据集版本变化（dataset_ver+1）后旧缓存失效
- 环比/同比：查询层基于同一编译产物计算；本期不完整时基期对齐数据截止日
- 导出：CSV（UTF-8 BOM）、按日行数、Content-Disposition
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from app.infra.cache import metric_cache
from app.infra.database import get_engine
from app.infra.models import Dataset

DATASET_NAME = "q_sales_2026"
METRIC_CODE = "q_gmv_sum"

SALES_ROWS = [
    # 覆盖区间 2025-01-01 ~ 2026-02-05；paid GMV：2025-01=150，2026-01=370
    ["2025-01-01", "100", "paid"],
    ["2025-01-15", "50", "paid"],
    ["2025-02-10", "200", "paid"],
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


@pytest.fixture(scope="session")
def query_env(client):
    """上传销售数据集并创建 GMV 指标（session 级：client 共享一个测试库，
    函数级 fixture 会在第二个测试因 code 冲突失败）。"""
    metric_cache.clear()  # 缓存是进程级单例，避免跨测试状态污染 miss/hit 断言
    resp = client.post(
        "/api/datasets/upload",
        files={"file": ("q_sales.csv", io.BytesIO(_csv_bytes(
            ["sale_date", "amount", "status"], SALES_ROWS
        )), "text/csv")},
        data={"name": DATASET_NAME},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": METRIC_CODE,
        "name": "查询测试GMV",
        "calc_rule": GMV_RULE,
    })
    assert resp.status_code == 200, resp.text
    return METRIC_CODE


def _query(client, metric, start, end, compare="none"):
    return client.post("/api/query/metric-value", json={
        "metric": metric, "start": start, "end": end, "compare": compare,
    })


class TestCountAggregation:
    """count 聚合不受列类型限制（类型检查仅覆盖 sum/avg/max/min）：
    数值列 count 必须编译成功且执行正确；不带列时为 COUNT(*)。"""

    def test_compile_count_on_numeric_column(self, client, query_env):
        resp = client.post("/api/metrics/compile", json={
            "calc_rule": {"base_aggregation": "count",
                          "source": {"table": DATASET_NAME, "column": "amount"}},
        })
        assert resp.status_code == 200, resp.text
        assert "COUNT(" in resp.json()["data"]["sql"]

    def test_count_numeric_value_and_count_star(self, client, query_env):
        """端到端：数值列 count 指标创建 + 查询（2026-01 两行 → 2）。
        扁平形态不带列的 count 被设计性拒绝（提示用 operand 形态）；
        operand 形态省略 column 即 COUNT(*)（全表 6 行）。"""
        code = "q_count_num_0"
        resp = client.post("/api/metrics", json={
            "code": code, "name": "计数测试数值列",
            "calc_rule": {"base_aggregation": "count",
                          "source": {"table": DATASET_NAME, "column": "amount"}},
        })
        assert resp.status_code == 200, resp.text
        data = _query(client, code, "2026-01-01", "2026-01-31").json()["data"]
        assert data["value"] == pytest.approx(2.0)

        flat_no_col = client.post("/api/metrics/compile", json={
            "calc_rule": {"base_aggregation": "count", "source": {"table": DATASET_NAME}},
        })
        assert flat_no_col.status_code == 400
        assert "operand 形态" in flat_no_col.json()["message"]

        code2 = "q_count_star_0"
        resp = client.post("/api/metrics", json={
            "code": code2, "name": "计数测试全表",
            "calc_rule": {"expression": "A", "operands": {
                "A": {"table": DATASET_NAME, "aggregation": "count"},
            }},
        })
        assert resp.status_code == 200, resp.text
        data = _query(client, code2, "2026-01-01", "2026-01-31").json()["data"]
        assert data["value"] == pytest.approx(2.0)  # 区间内共 2 行


class TestMetricValue:
    def test_value_matches_expectation(self, client, query_env):
        """口径一致性：2026-01 paid GMV = 300 + 70 = 370。"""
        resp = _query(client, query_env, "2026-01-01", "2026-01-31")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["value"] == pytest.approx(370.0)
        assert data["period_complete"] is True
        assert data["data_through"] == "2026-01-31"  # min(end, cov_end)
        assert data["cache"] == "miss"
        assert data["coverage"] == {"start": "2025-01-01", "end": "2026-02-05"}
        assert data["metric_code"] == METRIC_CODE

    def test_cache_hit_on_repeat(self, client, query_env):
        metric_cache.clear()  # 前序测试可能已用相同键填充缓存
        assert _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]["cache"] == "miss"
        data = _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]
        assert data["cache"] == "hit"
        assert data["value"] == pytest.approx(370.0)

    def test_partial_period_returns_real_value_and_caches(self, client, query_env):
        """契约 v2：end 超出覆盖 → 返回覆盖内实际值（370 + 02-05 的 80 = 450），
        data_through 标注数据截止日；部分值可入缓存（键含覆盖端点，
        数据补录后键变化自然失效），第二次请求命中缓存。"""
        data = _query(client, query_env, "2026-01-01", "2026-02-28").json()["data"]
        assert data["value"] == pytest.approx(450.0)
        assert data["period_complete"] is False
        assert data["data_through"] == "2026-02-05"
        assert data["cache"] == "miss"
        again = _query(client, query_env, "2026-01-01", "2026-02-28").json()["data"]
        assert again["cache"] == "hit"
        assert again["value"] == pytest.approx(450.0)

    def test_no_intersection_returns_null(self, client, query_env):
        """区间与数据覆盖无交集 → value=None（前端显示区间无数据）。"""
        resp = _query(client, query_env, "2026-03-01", "2026-03-31")
        data = resp.json()["data"]
        assert data["value"] is None
        assert data["period_complete"] is False
        assert data["data_through"] is None

    def test_complete_but_empty_period_returns_null(self, client, query_env):
        """范围内无数据：完整周期但值为 null（与不完整区分）。"""
        resp = _query(client, query_env, "2025-12-01", "2025-12-31")
        data = resp.json()["data"]
        assert data["value"] is None
        assert data["period_complete"] is True

    def test_query_by_metric_id(self, client, query_env):
        listing = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"]
        metric_id = listing[0]["id"]
        resp = _query(client, metric_id, "2026-01-01", "2026-01-31")
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == pytest.approx(370.0)

    def test_disabled_metric_rejected(self, client, query_env):
        listing = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"]
        metric_id = listing[0]["id"]
        resp = client.patch(f"/api/metrics/{metric_id}", json={"status": "disabled", "reason": "停用"})
        assert resp.status_code == 200
        resp = _query(client, metric_id, "2026-01-01", "2026-01-31")
        assert resp.status_code == 400
        assert resp.json()["code"] == 40000
        # 恢复（session 级共享状态，后续测试仍要计算）
        resp = client.patch(f"/api/metrics/{metric_id}", json={"status": "active", "reason": "恢复"})
        assert resp.status_code == 200, resp.text


class TestCacheInvalidation:
    def test_calc_rule_change_invalidates(self, client, query_env):
        """改口径 → ver+1 → 缓存键变化 → 重新计算，值随新口径。"""
        assert _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]["value"] == pytest.approx(370.0)
        listing = client.get("/api/metrics", params={"search": METRIC_CODE}).json()["data"]
        metric_id = listing[0]["id"]
        resp = client.patch(f"/api/metrics/{metric_id}", json={
            "calc_rule": {"base_aggregation": "max",
                           "source": {"table": DATASET_NAME, "column": "amount",
                                       "filter": "status = 'paid'"}},
            "reason": "口径变更验证缓存失效",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["ver"] == 2

        data = _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]
        assert data["cache"] == "miss"          # 旧缓存未命中
        assert data["ver"] == 2
        assert data["value"] == pytest.approx(300.0)  # max(300, 70)

        # 恢复 sum 口径（fixture 是 session 级，后续测试仍依赖 370/150 断言）
        resp = client.patch(f"/api/metrics/{metric_id}", json={
            "calc_rule": GMV_RULE, "reason": "恢复原口径",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["ver"] == 3
        assert _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]["value"] == pytest.approx(370.0)

    def test_dataset_ver_change_invalidates(self, client, query_env):
        """dataset_ver 递增（模拟 B8 数据重导入）→ 缓存失效。"""
        metric_cache.clear()
        assert _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]["cache"] == "miss"
        assert _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]["cache"] == "hit"

        Session = sessionmaker(bind=get_engine(), expire_on_commit=False)
        s = Session()
        row = s.query(Dataset).filter(Dataset.name == DATASET_NAME).one()
        row.dataset_ver += 1
        s.commit()
        s.close()

        data = _query(client, query_env, "2026-01-01", "2026-01-31").json()["data"]
        assert data["cache"] == "miss"
        assert data["value"] == pytest.approx(370.0)


class TestCompare:
    def test_yoy_values_and_change_pct(self, client, query_env):
        """同比：2026-01=370 vs 2025-01=150 → +146.6667%。"""
        resp = _query(client, query_env, "2026-01-01", "2026-01-31", compare="yoy")
        data = resp.json()["data"]
        assert data["value"] == pytest.approx(370.0)
        assert data["compare"]["value"] == pytest.approx(150.0)
        assert data["compare"]["period_complete"] is True
        assert data["compare"]["change_pct"] == pytest.approx(146.6667)
        assert data["compare"]["start"] == "2025-01-01"
        assert data["compare"]["end"] == "2025-01-31"

    def test_mom_previous_empty_period(self, client, query_env):
        """环比上期为完整周期但无数据 → change_pct 为 None 而非报错。"""
        resp = _query(client, query_env, "2026-01-01", "2026-01-31", compare="mom")
        data = resp.json()["data"]
        assert data["value"] == pytest.approx(370.0)
        assert data["compare"]["start"] == "2025-12-01"
        assert data["compare"]["end"] == "2025-12-31"
        assert data["compare"]["value"] is None
        assert data["compare"]["change_pct"] is None

    def test_compare_partial_current_aligned_baseline(self, client, query_env):
        """契约 v2：本期部分周期（2026-02 仅 02-05 有数据）→ 同比基期对齐到
        数据实际截止日（2025-02-01~05，同月同日），避免"5 天 vs 全月"假跌幅；
        基期落在覆盖内但区间无付费数据 → value=None、change_pct=None。"""
        resp = _query(client, query_env, "2026-02-01", "2026-02-28", compare="yoy")
        data = resp.json()["data"]
        assert data["value"] == pytest.approx(80.0)
        assert data["period_complete"] is False
        assert data["data_through"] == "2026-02-05"
        assert data["compare"]["start"] == "2025-02-01"
        assert data["compare"]["end"] == "2025-02-05"
        assert data["compare"]["value"] is None
        assert data["compare"]["change_pct"] is None


class TestErrors:
    def test_unknown_metric_404(self, client, query_env):
        resp = _query(client, "no_such_metric", "2026-01-01", "2026-01-31")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40400

    def test_bad_date_format_400(self, client, query_env):
        resp = _query(client, query_env, "2026/01/01", "2026-01-31")
        assert resp.status_code == 400
        assert resp.json()["code"] == 40000

    def test_start_after_end_400(self, client, query_env):
        resp = _query(client, query_env, "2026-02-01", "2026-01-01")
        assert resp.status_code == 400

    def test_invalid_compare_400(self, client, query_env):
        resp = _query(client, query_env, "2026-01-01", "2026-01-31", compare="wow")
        assert resp.status_code == 400


class TestExport:
    def test_export_csv_content(self, client, query_env):
        """31 天序列 + BOM + 表头；1 月两个有值日 300/70，其余空值。"""
        resp = client.post("/api/query/export", json={
            "metric": query_env, "start": "2026-01-01", "end": "2026-01-31",
        })
        assert resp.status_code == 200, resp.text
        assert "attachment" in resp.headers["content-disposition"]
        assert METRIC_CODE in resp.headers["content-disposition"]

        raw = resp.content
        assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM（Excel 兼容）
        lines = raw.decode("utf-8-sig").strip().splitlines()
        assert lines[0] == "date,value,period_complete"
        assert len(lines) == 32  # header + 31 天

        by_date = {line.split(",")[0]: line.split(",")[1] for line in lines[1:]}
        assert by_date["2026-01-10"] == "300"   # amount 列推断为 int，sum → int
        assert by_date["2026-01-20"] == "70"
        assert by_date["2026-01-01"] == ""      # 有覆盖、无数据 → 空
        flags = {line.split(",")[0]: line.split(",")[2] for line in lines[1:]}
        assert flags["2026-01-31"] == "1"   # 覆盖期内每天 complete

    def test_export_far_future_all_empty_incomplete(self, client, query_env):
        """覆盖期外导出：全部 incomplete（value 空、标志 0）。"""
        resp = client.post("/api/query/export", json={
            "metric": query_env, "start": "2027-01-01", "end": "2027-01-03",
        })
        lines = resp.content.decode("utf-8-sig").strip().splitlines()
        assert len(lines) == 4
        for line in lines[1:]:
            assert line.endswith(",0")

    def test_export_range_too_long_400(self, client, query_env):
        resp = client.post("/api/query/export", json={
            "metric": query_env, "start": "2020-01-01", "end": "2026-01-01",
        })
        assert resp.status_code == 400
        assert resp.json()["code"] == 40000


class TestDatetimeBoundary:
    """datetime 列边界回归（2026-09-11）：

    时间过滤曾是 `col <= __end__`，datetime 列下被解释为 `<= 端点日 00:00:00`，
    端点日的全部日间记录被截掉——卡片整段聚合尚有值、逐日序列却几乎全 null
    （线上实例：61 天只剩 5 个 00:00:00 孤立点，折线图"消失"）。修复为半开
    区间 `>= __start__ AND < __end__ + 1d`，date/datetime 两种列型统一正确。
    """

    TS_DATASET = "q_events_ts"
    TS_METRIC = "q_ts_amount_sum"

    TS_ROWS = [
        # 覆盖区间 2026-01-10 ~ 2026-02-01；端点日带日间记录
        ["ts", "amount"],
        ["2026-01-10 09:15:00", "100"],
        ["2026-01-10 23:59:59", "50"],
        ["2026-01-31 12:00:00", "30"],
        ["2026-02-01 08:00:00", "20"],
    ]

    @pytest.fixture(scope="class")
    def ts_env(self, client):
        rows = self.TS_ROWS
        resp = client.post(
            "/api/datasets/upload",
            files={"file": ("q_events_ts.csv", io.BytesIO(_csv_bytes(rows[0], rows[1:])), "text/csv")},
            data={"name": self.TS_DATASET},
        )
        assert resp.status_code == 200, resp.text
        resp = client.post("/api/metrics", json={
            "code": self.TS_METRIC,
            "name": "时间戳边界测试",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": self.TS_DATASET, "column": "amount"},
            },
        })
        assert resp.status_code == 200, resp.text
        return self.TS_METRIC

    def test_end_day_intraday_rows_included(self, client, ts_env):
        """区间端点日（01-31）的日间记录必须计入：sum = 100+50+30 = 180
        （修复前 `<= 2026-01-31 00:00:00` 截掉 12:00 的 30 → 150）。
        区间从覆盖起点（01-10）起 → 完整周期。"""
        data = _query(client, ts_env, "2026-01-10", "2026-01-31").json()["data"]
        assert data["value"] == pytest.approx(180.0)
        assert data["period_complete"] is True
        # 起点早于覆盖起点（01-10）：值不变，但周期不完整
        partial = _query(client, ts_env, "2026-01-01", "2026-01-31").json()["data"]
        assert partial["value"] == pytest.approx(180.0)
        assert partial["period_complete"] is False

    def test_per_day_series_includes_intraday(self, client, ts_env):
        """逐日序列：01-10 当天 = 150（修复前该日全天记录被截 → null）。"""
        resp = client.post("/api/query/export", json={
            "metric": ts_env, "start": "2026-01-10", "end": "2026-01-10",
        })
        lines = resp.content.decode("utf-8-sig").strip().splitlines()
        assert lines[1] == "2026-01-10,150,1"

    def test_range_end_on_last_coverage_day(self, client, ts_env):
        """区间终点 = 覆盖终点（02-01）：当天 08:00 的 20 计入，sum = 200。"""
        data = _query(client, ts_env, "2026-01-10", "2026-02-01").json()["data"]
        assert data["value"] == pytest.approx(200.0)
        assert data["period_complete"] is True

    def test_beyond_coverage_partial(self, client, ts_env):
        """契约 v2 不回归：区间越过覆盖端 → 真实值 + data_through 标注。"""
        data = _query(client, ts_env, "2026-01-01", "2026-02-28").json()["data"]
        assert data["value"] == pytest.approx(200.0)
        assert data["period_complete"] is False
        assert data["data_through"] == "2026-02-01"
