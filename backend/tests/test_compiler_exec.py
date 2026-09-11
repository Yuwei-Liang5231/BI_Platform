"""编译 SQL 真实执行验证：DuckDB + 临时 Parquet 夹具，验证编译产物算得对。

golden 测试保证 SQL 形状，本文件保证语义：数字必须正确，且与手工 SQL 一致。
这是 B3 统一计算服务的执行方式预演（DuckDB 直查 Parquet + 命名参数绑定）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest
from pyarrow import Table, parquet

from app.domain.metric.compiler import DatasetInfo, RelationInfo, compile_metric
from tests.test_compiler_golden import REL_A, dataset_a


@pytest.fixture(scope="module")
def duck_env(tmp_path_factory):
    """生成零售夹具 parquet 并注册 DuckDB 视图（视图名 = 数据集名，B3 同款约定）。"""
    tmp: Path = tmp_path_factory.mktemp("exec")
    orders = Table.from_pydict({
        "order_id": [1, 2, 3, 4, 5],
        "order_date": [date(2026, 1, 10), date(2026, 1, 20), date(2026, 2, 5),
                        date(2026, 2, 25), date(2026, 3, 30)],
        "user_id": [100, 101, 100, 102, 101],
        "pay_amount": [120.5, 80.0, 200.0, 99.5, 300.0],
        "order_status": ["paid", "paid", "refunded", "paid", "paid"],
    })
    users = Table.from_pydict({
        "user_id": [100, 101, 102],
        "region": ["华东", "华北", "华东"],
    })
    parquet.write_table(orders, tmp / "orders.parquet")
    parquet.write_table(users, tmp / "users.parquet")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW orders AS SELECT * FROM read_parquet('{(tmp / 'orders.parquet').as_posix()}')")
    con.execute(f"CREATE VIEW users AS SELECT * FROM read_parquet('{(tmp / 'users.parquet').as_posix()}')")
    return con


def _run(con, rule: dict, start: date, end: date):
    compiled = compile_metric(rule, dataset_a(), REL_A)
    rows = con.execute(compiled.sql, {"__start__": start, "__end__": end}).fetchall()
    return rows[0][0] if rows else None


class TestExecDatasetA:
    def test_gmv_with_time_range(self, duck_env):
        """1-2 月支付 GMV = 120.5 + 80 + 99.5 = 300（3 月与退款剔除）。"""
        value = _run(
            duck_env,
            {"base_aggregation": "sum",
             "source": {"table": "orders", "column": "pay_amount",
                         "filter": "order_status = 'paid'"}},
            date(2026, 1, 1), date(2026, 2, 28),
        )
        assert value == pytest.approx(300.0)

    def test_avg_ticket_ratio(self, duck_env):
        """客单价（1-2 月，支付）= 300 / 3（用户 100/101/102）= 100。"""
        value = _run(
            duck_env,
            {"expression": "A / B",
             "operands": {
                 "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum",
                        "filter": "order_status = 'paid'"},
                 "B": {"table": "orders", "column": "user_id", "aggregation": "count_distinct",
                        "filter": "order_status = 'paid'"},
             }},
            date(2026, 1, 1), date(2026, 2, 28),
        )
        assert value == pytest.approx(100.0)

    def test_cross_table_region_filter(self, duck_env):
        """华东支付 GMV（1-2 月）= 120.5 + 99.5 = 220（走 JOIN）。"""
        value = _run(
            duck_env,
            {"base_aggregation": "sum",
             "source": {"table": "orders", "column": "pay_amount",
                         "filter": "order_status = 'paid' AND region = '华东'"}},
            date(2026, 1, 1), date(2026, 2, 28),
        )
        assert value == pytest.approx(220.0)

    def test_refund_rate(self, duck_env):
        """1-2 月退货率 = 1 / 4 = 0.25。"""
        value = _run(
            duck_env,
            {"expression": "A / B",
             "operands": {
                 "A": {"table": "orders", "column": "order_id", "aggregation": "count",
                        "filter": "order_status = 'refunded'"},
                 "B": {"table": "orders", "column": "order_id", "aggregation": "count"},
             }},
            date(2026, 1, 1), date(2026, 2, 28),
        )
        assert value == pytest.approx(0.25)

    def test_division_by_zero_returns_null(self, duck_env):
        """区间内无支付单 → 分母 0 → NULL（不是报错，也不是 0）。"""
        value = _run(
            duck_env,
            {"expression": "A / B",
             "operands": {
                 "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum",
                        "filter": "order_status = 'paid'"},
                 "B": {"table": "orders", "column": "user_id", "aggregation": "count_distinct",
                        "filter": "order_status = 'paid'"},
             }},
            date(2026, 5, 1), date(2026, 5, 31),
        )
        assert value is None

    def test_empty_period_returns_null_not_zero(self, duck_env):
        value = _run(
            duck_env,
            {"base_aggregation": "sum",
             "source": {"table": "orders", "column": "pay_amount"}},
            date(2026, 6, 1), date(2026, 6, 30),
        )
        assert value is None

    def test_full_range_matches_manual_sql(self, duck_env):
        """编译 SQL 全区间结果必须与手工书写 SQL 一致（口径一致性底线）。"""
        compiled_value = _run(
            duck_env,
            {"base_aggregation": "sum",
             "source": {"table": "orders", "column": "pay_amount",
                         "filter": "order_status = 'paid' AND region = '华东'"}},
            date(2026, 1, 1), date(2026, 12, 31),
        )
        manual = duck_env.execute(
            "SELECT SUM(o.pay_amount) FROM orders o JOIN users u ON o.user_id = u.user_id "
            "WHERE o.order_status = 'paid' AND u.region = '华东' "
            "AND o.order_date >= ? AND o.order_date <= ?",
            [date(2026, 1, 1), date(2026, 12, 31)],
        ).fetchone()[0]
        assert compiled_value == pytest.approx(manual)
