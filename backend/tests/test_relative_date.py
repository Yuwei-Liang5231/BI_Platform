"""相对日期字面量（today / today±Nd，2026-09-16）全链路测试。

覆盖：filter 文法解析（合法/非法）、编译 SQL 文本、DuckDB 真实执行
（锚定运行时当天）、缓存键含当天锚点、API 端到端（创建 → 取数 → 缓存）。
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import duckdb
import pytest
from pyarrow import Table, parquet

from app.domain.metric.compiler import (
    CompiledQuery,
    DatasetInfo,
    compile_metric,
)
from app.domain.metric.schema import CalcRuleError, RelativeDate, parse_filter
from app.domain.query.service import _cache_key
from app.infra.models import Metric
from tests.test_compiler_golden import REL_A, dataset_a

TODAY = date.today()


# ---------------------------------------------------------------- 文法解析


class TestParseRelativeDate:
    def test_today(self):
        conds = parse_filter("due_date <= today")
        assert conds[0].literal == RelativeDate(0)

    def test_plus_days(self):
        assert parse_filter("due_date <= today+15d")[0].literal == RelativeDate(15)

    def test_minus_days(self):
        assert parse_filter("due_date <= today-1d")[0].literal == RelativeDate(-1)

    def test_spaced_form(self):
        """空格形态 today - 15 d（词法把 -15 拆成 op + number）。"""
        assert parse_filter("due_date >= today - 15 d")[0].literal == RelativeDate(-15)

    def test_case_insensitive(self):
        assert parse_filter("due_date <= TODAY")[0].literal == RelativeDate(0)

    def test_mixed_with_static_condition(self):
        conds = parse_filter("due_date >= today AND order_status = 'paid'")
        assert conds[0].literal == RelativeDate(0)
        assert conds[1].literal == "paid"

    def test_render_round_trip(self):
        for offset in (0, 15, -1):
            text = RelativeDate(offset).render()
            assert parse_filter(f"d <= {text}")[0].literal == RelativeDate(offset)

    @pytest.mark.parametrize("bad", [
        "due_date <= today+abc",
        "due_date <= today 15 d",     # 缺符号
        "due_date <= today+15.5d",    # 非整数天数
        "due_date <= today+",         # 缺数字
        "due_date <= today+15x",      # 单位不是 d
        "due_date <= todayd",         # 粘连不是 today 记号
    ])
    def test_invalid_forms_rejected(self, bad):
        with pytest.raises(CalcRuleError):
            parse_filter(bad)


# ---------------------------------------------------------------- 编译 SQL 文本


class TestCompileRelativeDate:
    def test_today_placeholder(self):
        compiled = compile_metric(
            {"base_aggregation": "count",
             "source": {"table": "orders", "column": "order_id",
                         "filter": "order_status = 'paid' AND order_date <= today"}},
            dataset_a(), REL_A,
        )
        assert "$__today__" in compiled.sql

    def test_offset_interval_text(self):
        compiled = compile_metric(
            {"base_aggregation": "count",
             "source": {"table": "orders", "column": "order_id",
                         "filter": "order_date <= today+15d"}},
            dataset_a(), REL_A,
        )
        assert "($__today__ + INTERVAL 15 DAY)" in compiled.sql

    def test_negative_offset_interval_text(self):
        compiled = compile_metric(
            {"base_aggregation": "count",
             "source": {"table": "orders", "column": "order_id",
                         "filter": "order_date >= today-1d"}},
            dataset_a(), REL_A,
        )
        assert "($__today__ - INTERVAL 1 DAY)" in compiled.sql


# ---------------------------------------------------------------- DuckDB 执行


class TestRelativeDateExec:
    @pytest.fixture(scope="class")
    def due_env(self, tmp_path_factory):
        """动态夹具：行日期锚定运行时当天（昨天/今天/明天），相对条件语义可断言。"""
        tmp = tmp_path_factory.mktemp("rela")
        t = Table.from_pydict({
            "due_id": [1, 2, 3],
            "due_date": [TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=1)],
            "amount": [10.0, 20.0, 40.0],
        })
        parquet.write_table(t, tmp / "dues.parquet")
        con = duckdb.connect()
        con.execute(
            f"CREATE VIEW dues AS SELECT * FROM read_parquet('{(tmp / 'dues.parquet').as_posix()}')"
        )
        return con

    def _run(self, con, filt: str, datasets=None):
        ds = datasets or {
            "dues": DatasetInfo(
                id=11, name="dues",
                columns={"due_id": "int", "due_date": "date", "amount": "float"},
                coverage={"due_date": (TODAY - timedelta(days=1), TODAY + timedelta(days=1))},
            )
        }
        compiled = compile_metric(
            {"base_aggregation": "sum", "source": {"table": "dues", "column": "amount",
                                                    "filter": filt}},
            ds, [],
        )
        row = con.execute(
            compiled.sql,
            {"__start__": TODAY - timedelta(days=10), "__end__": TODAY + timedelta(days=10),
             "__today__": TODAY},
        ).fetchone()
        return row[0]

    def test_up_to_today(self, due_env):
        """<= today：昨天 + 今天 = 30。"""
        assert self._run(due_env, "due_date <= today") == pytest.approx(30.0)

    def test_before_today(self, due_env):
        """< today：严格今天之前 = 10。"""
        assert self._run(due_env, "due_date < today") == pytest.approx(10.0)

    def test_from_today(self, due_env):
        """>= today：今天 + 明天 = 60。"""
        assert self._run(due_env, "due_date >= today") == pytest.approx(60.0)

    def test_window_around_today(self, due_env):
        """未来 1 天窗口（>= today-1d AND <= today+1d）= 全部 70。"""
        assert self._run(due_env, "due_date >= today-1d AND due_date <= today+1d") == pytest.approx(70.0)

    def test_constant_metric_with_today_filter(self, due_env, tmp_path_factory):
        """全期常数指标 + today 过滤：无时间占位符但含 $__today__，空参数 + today 绑定可执行。"""
        tmp = tmp_path_factory.mktemp("rela-const")
        t = Table.from_pydict({
            "due_id": [1, 2, 3],
            "amount": [10.0, 20.0, 40.0],
            "due_date": [TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=1)],
        })
        parquet.write_table(t, tmp / "dues2.parquet")
        due_env.execute(
            f"CREATE VIEW dues2 AS SELECT * FROM read_parquet('{(tmp / 'dues2.parquet').as_posix()}')"
        )
        ds = {
            "dues2": DatasetInfo(
                id=12, name="dues2",
                columns={"due_id": "int", "amount": "float", "due_date": "date"},
                coverage={},  # 无覆盖登记 → time_field 解析为 None → 常数
            )
        }
        compiled = compile_metric(
            {"base_aggregation": "sum",
             "source": {"table": "dues2", "column": "amount", "filter": "due_date >= today"}},
            ds, [],
        )
        assert compiled.is_constant  # 无时间绑定（常数），但 SQL 含 $__today__
        row = due_env.execute(compiled.sql, {"__today__": TODAY}).fetchone()
        assert row[0] == pytest.approx(60.0)


# ---------------------------------------------------------------- 缓存键锚点


class TestCacheKeyTodayAnchor:
    def _compiled(self) -> CompiledQuery:
        return CompiledQuery(
            sql="SELECT COUNT(*) AS value FROM t WHERE d <= $__today__",
            primary_dataset="t", primary_dataset_id=1, dataset_names=["t"],
            time_fields={}, coverage_start=None, coverage_end=None,
        )

    def test_static_metric_key_stable(self, monkeypatch):
        compiled = CompiledQuery(
            sql="SELECT COUNT(*) AS value FROM t WHERE d <= $__end__",
            primary_dataset="t", primary_dataset_id=1, dataset_names=["t"],
            time_fields={"t": "d"}, coverage_start=date(2026, 1, 1), coverage_end=date(2026, 1, 31),
        )
        m = Metric(id=1, ver=1)
        k1 = _cache_key(m, compiled, {"t": {"ver": 1}}, date(2026, 1, 1), date(2026, 1, 31))
        monkeypatch.setattr("app.domain.query.service._today_anchor", lambda: date(2030, 1, 1))
        k2 = _cache_key(m, compiled, {"t": {"ver": 1}}, date(2026, 1, 1), date(2026, 1, 31))
        assert k1 == k2  # 非 today 指标：键不含锚点，跨天不变

    def test_today_metric_key_rolls_daily(self, monkeypatch):
        compiled = self._compiled()
        m = Metric(id=1, ver=1)
        monkeypatch.setattr("app.domain.query.service._today_anchor", lambda: date(2026, 9, 16))
        k1 = _cache_key(m, compiled, {"t": {"ver": 1}}, date(2026, 9, 1), date(2026, 9, 30))
        monkeypatch.setattr("app.domain.query.service._today_anchor", lambda: date(2026, 9, 17))
        k2 = _cache_key(m, compiled, {"t": {"ver": 1}}, date(2026, 9, 1), date(2026, 9, 30))
        assert k1 != k2  # today 指标：锚点变化 → 键变化 → 昨日缓存自然失效


# ---------------------------------------------------------------- API 端到端


def _upload(client, name: str) -> dict:
    lines = ["due_id,due_date,amount"]
    for i, d in enumerate((TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=1)), 1):
        lines.append(f"{i},{d.isoformat()},{i * 10}")
    resp = client.post(
        "/api/datasets/upload",
        files={"file": ("rela_dues.csv", io.BytesIO("\n".join(lines).encode("utf-8")), "text/csv")},
        data={"name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestRelativeDateApi:
    def test_metric_with_today_filter_end_to_end(self, client):
        ds = _upload(client, f"rela_dues_{TODAY.strftime('%Y%m%d%H%M%S')}")
        resp = client.post("/api/metrics", json={
            "code": f"rela_due_sum_{TODAY.strftime('%Y%m%d%H%M%S')}",
            "name": "到期金额（含今天）",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": ds["name"], "column": "amount", "filter": "due_date <= today"},
            },
        })
        assert resp.status_code == 200, resp.text

        start = (TODAY - timedelta(days=10)).isoformat()
        end = (TODAY + timedelta(days=10)).isoformat()
        r1 = client.post("/api/query/metric-value", json={
            "metric": resp.json()["data"]["id"], "start": start, "end": end,
        })
        assert r1.status_code == 200, r1.text
        body = r1.json()["data"]
        # <= today：昨天(10) + 今天(20)
        assert body["value"] == 30
        assert body["cache"] == "miss"

        r2 = client.post("/api/query/metric-value", json={
            "metric": resp.json()["data"]["id"], "start": start, "end": end,
        })
        assert r2.json()["data"]["cache"] == "hit"  # 同一天内缓存命中（锚点相同）

        # 收尾清理：删除指标与数据集，避免污染共享测试库
        client.delete(f"/api/metrics/{resp.json()['data']['id']}")
        client.delete(f"/api/datasets/{ds['id']}")
