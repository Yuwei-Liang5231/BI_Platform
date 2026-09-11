"""编译器 golden SQL 测试。

golden 约定：定义 → 期望 SQL 逐字比对（含 $__start__/$__end__ 参数占位）。
数据集 A（零售交易型）与 B（订阅服务型）各 ≥8 条，验证同一编译器不修改任何
产品代码即可服务两个行业（不变式 7 交叉验证）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.metric.compiler import CompileError, DatasetInfo, RelationInfo, compile_metric
from app.domain.metric.schema import CalcRuleError

# ---------------------------------------------------------------- 夹具：对齐合成数据 A/B 结构


def dataset_a() -> dict[str, DatasetInfo]:
    cov = {
        "orders": DatasetInfo(
            id=1, name="orders",
            columns={
                "order_id": "int", "order_date": "date", "user_id": "int",
                "product_id": "int", "channel_id": "int", "quantity": "int",
                "unit_price": "float", "pay_amount": "float", "refund_amount": "float",
                "order_status": "string", "coupon_code": "string",
            },
            coverage={"order_date": (date(2026, 1, 1), date(2026, 6, 30))},
        ),
        "users": DatasetInfo(
            id=2, name="users",
            columns={
                "user_id": "int", "user_name": "string", "city": "string",
                "region": "string", "register_date": "date",
            },
            coverage={"register_date": (date(2025, 1, 1), date(2026, 6, 30))},
        ),
        "products": DatasetInfo(
            id=3, name="products",
            columns={"product_id": "int", "category": "string", "unit_price": "float", "cost": "float"},
            coverage={},
        ),
    }
    return cov


def dataset_b() -> dict[str, DatasetInfo]:
    return {
        "subscriptions": DatasetInfo(
            id=11, name="subscriptions",
            columns={
                "subscription_id": "int", "account_id": "int", "plan_id": "int",
                "start_date": "date", "end_date": "date", "status": "string",
                "mrr_amount": "float", "seats": "int", "auto_renew": "bool",
            },
            coverage={
                "start_date": (date(2025, 6, 1), date(2026, 6, 30)),
                "end_date": (date(2026, 1, 1), date(2027, 5, 31)),
            },
        ),
        "accounts": DatasetInfo(
            id=12, name="accounts",
            columns={
                "account_id": "int", "account_name": "string", "industry": "string",
                "region": "string", "signup_date": "date",
            },
            coverage={"signup_date": (date(2025, 1, 1), date(2026, 6, 30))},
        ),
        "usage_events": DatasetInfo(
            id=13, name="usage_events",
            columns={
                "event_id": "int", "account_id": "int", "event_date": "date",
                "feature_name": "string", "usage_count": "int",
            },
            coverage={"event_date": (date(2026, 1, 1), date(2026, 6, 30))},
        ),
    }


REL_A = [RelationInfo("orders", "user_id", "users", "user_id")]
REL_B = [
    RelationInfo("subscriptions", "account_id", "accounts", "account_id"),
    RelationInfo("usage_events", "account_id", "accounts", "account_id"),
]


def compile_a(rule: dict):
    return compile_metric(rule, dataset_a(), REL_A)


def compile_b(rule: dict):
    return compile_metric(rule, dataset_b(), REL_B)


# ================================================================ 数据集 A（零售交易型）


class TestGoldenDatasetA:
    def test_g01_gmv_sum_with_filter(self):
        q = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "pay_amount", "filter": "order_status = 'paid'"},
        })
        assert q.sql == (
            'SELECT SUM("orders"."pay_amount") AS value FROM "orders" '
            'WHERE "orders"."order_status" = \'paid\' '
            'AND "orders"."order_date" >= $__start__ AND "orders"."order_date" <= $__end__'
        )
        assert q.primary_dataset == "orders"
        assert q.coverage_start == date(2026, 1, 1)
        assert q.coverage_end == date(2026, 6, 30)

    def test_g02_order_count(self):
        q = compile_a({
            "base_aggregation": "count",
            "source": {"table": "orders", "column": "order_id"},
        })
        assert q.sql == (
            'SELECT COUNT("orders"."order_id") AS value FROM "orders" '
            'WHERE "orders"."order_date" >= $__start__ AND "orders"."order_date" <= $__end__'
        )

    def test_g03_paying_users_count_distinct(self):
        q = compile_a({
            "base_aggregation": "count_distinct",
            "source": {"table": "orders", "column": "user_id", "filter": "order_status = 'paid'"},
        })
        assert 'COUNT(DISTINCT "orders"."user_id")' in q.sql

    def test_g04_avg_order_amount(self):
        q = compile_a({
            "base_aggregation": "avg",
            "source": {"table": "orders", "column": "pay_amount"},
        })
        assert q.sql.startswith('SELECT AVG("orders"."pay_amount") AS value FROM "orders"')

    def test_g05_max_refund(self):
        q = compile_a({
            "base_aggregation": "max",
            "source": {"table": "orders", "column": "refund_amount"},
        })
        assert 'MAX("orders"."refund_amount")' in q.sql

    def test_g06_avg_ticket_ratio(self):
        """客单价 = 支付金额 / 支付用户数（同表比率，除法包 NULLIF）。"""
        q = compile_a({
            "expression": "A / B",
            "operands": {
                "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum",
                       "filter": "order_status = 'paid'"},
                "B": {"table": "orders", "column": "user_id", "aggregation": "count_distinct",
                       "filter": "order_status = 'paid'"},
            },
        })
        assert q.sql == (
            'WITH "A" AS (SELECT SUM("orders"."pay_amount") AS value FROM "orders" '
            'WHERE "orders"."order_status" = \'paid\' '
            'AND "orders"."order_date" >= $__start__ AND "orders"."order_date" <= $__end__), '
            '"B" AS (SELECT COUNT(DISTINCT "orders"."user_id") AS value FROM "orders" '
            'WHERE "orders"."order_status" = \'paid\' '
            'AND "orders"."order_date" >= $__start__ AND "orders"."order_date" <= $__end__) '
            'SELECT (("A".value) / NULLIF(("B".value), 0)) AS value FROM "A", "B"'
        )

    def test_g07_refund_rate(self):
        """退货率 = 退货单 / 总单。"""
        q = compile_a({
            "expression": "A / B",
            "operands": {
                "A": {"table": "orders", "column": "order_id", "aggregation": "count",
                       "filter": "order_status = 'refunded'"},
                "B": {"table": "orders", "column": "order_id", "aggregation": "count"},
            },
        })
        assert "'refunded'" in q.sql
        assert "NULLIF" in q.sql

    def test_g08_cross_table_filter_join(self):
        """华东地区销售额：过滤列在维度表 users，走显式一跳关系。"""
        q = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "pay_amount",
                        "filter": "order_status = 'paid' AND region = '华东'"},
        })
        assert (
            'FROM "orders" JOIN "users" ON "orders"."user_id" = "users"."user_id"' in q.sql
        )
        assert '"users"."region" = \'华东\'' in q.sql
        assert q.used_relations == [
            {"from_dataset": "orders", "from_column": "user_id",
             "to_dataset": "users", "to_column": "user_id"}
        ]

    def test_g09_string_with_quote_escaping(self):
        q = compile_a({
            "base_aggregation": "count",
            "source": {"table": "orders", "column": "order_id",
                        "filter": "coupon_code = '满100减20'"},
        })
        assert "'满100减20'" in q.sql

    def test_g10_multi_condition_and(self):
        q = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "pay_amount",
                        "filter": "pay_amount >= 100 AND order_status != 'refunded'"},
        })
        assert '"orders"."pay_amount" >= 100.0' in q.sql or '"orders"."pay_amount" >= 100' in q.sql
        assert '"orders"."order_status" != \'refunded\'' in q.sql

    def test_g11_coverage_metadata(self):
        q = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "quantity"},
        })
        assert q.grain == "day"
        assert q.time_fields == {"orders": "order_date"}
        assert q.dataset_names == ["orders"]

    def test_g12_gmv_plus_refund_expression(self):
        """净销售额 = 销售额 - 退款额（减法，不包 NULLIF）。"""
        q = compile_a({
            "expression": "A - B",
            "operands": {
                "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum",
                       "filter": "order_status = 'paid'"},
                "B": {"table": "orders", "column": "refund_amount", "aggregation": "sum"},
            },
        })
        assert " - " in q.sql
        assert "NULLIF" not in q.sql


# ================================================================ 数据集 B（订阅服务型）


class TestGoldenDatasetB:
    def test_h01_mrr_sum(self):
        q = compile_b({
            "base_aggregation": "sum",
            "source": {"table": "subscriptions", "column": "mrr_amount",
                        "filter": "status = 'active'"},
            "time_field": "start_date",
        })
        assert q.sql == (
            'SELECT SUM("subscriptions"."mrr_amount") AS value FROM "subscriptions" '
            'WHERE "subscriptions"."status" = \'active\' '
            'AND "subscriptions"."start_date" >= $__start__ '
            'AND "subscriptions"."start_date" <= $__end__'
        )

    def test_h02_active_accounts_count_distinct(self):
        q = compile_b({
            "base_aggregation": "count_distinct",
            "source": {"table": "usage_events", "column": "account_id",
                        "filter": "event_date IS NOT NULL"},
        })
        assert 'COUNT(DISTINCT "usage_events"."account_id")' in q.sql
        assert '"usage_events"."event_date" IS NOT NULL' in q.sql

    def test_h03_paid_penetration_cross_table_ratio(self):
        """付费渗透率 = 有订阅的账户 / 总账户（跨表标量比率，无需 Join）。"""
        q = compile_b({
            "expression": "A / B",
            "operands": {
                "A": {"table": "subscriptions", "column": "account_id",
                       "aggregation": "count_distinct"},
                "B": {"table": "accounts", "column": "account_id", "aggregation": "count"},
            },
            "time_field": "start_date",
        })
        assert '"A" AS (SELECT COUNT(DISTINCT "subscriptions"."account_id")' in q.sql
        assert '"B" AS (SELECT COUNT("accounts"."account_id")' in q.sql
        assert 'FROM "A", "B"' in q.sql
        assert q.dataset_names == ["subscriptions", "accounts"]
        assert q.coverage_start == date(2025, 1, 1)   # accounts.signup_date 起点
        assert q.coverage_end == date(2026, 6, 30)    # 两个 operand 时间字段覆盖的并集终点

    def test_h04_avg_seats(self):
        q = compile_b({
            "base_aggregation": "avg",
            "source": {"table": "subscriptions", "column": "seats"},
            "time_field": "start_date",
        })
        assert 'AVG("subscriptions"."seats")' in q.sql

    def test_h05_usage_sum(self):
        q = compile_b({
            "base_aggregation": "sum",
            "source": {"table": "usage_events", "column": "usage_count",
                        "filter": "feature_name = 'report_export'"},
        })
        assert 'SUM("usage_events"."usage_count")' in q.sql

    def test_h06_cross_table_filter_join(self):
        """制造行业订阅数：过滤列在 accounts，走显式关系。"""
        q = compile_b({
            "base_aggregation": "count",
            "source": {"table": "subscriptions", "column": "subscription_id",
                        "filter": "industry = '制造业'"},
            "time_field": "start_date",
        })
        assert (
            'FROM "subscriptions" JOIN "accounts" '
            'ON "subscriptions"."account_id" = "accounts"."account_id"' in q.sql
        )

    def test_h07_bool_literal(self):
        q = compile_b({
            "base_aggregation": "count",
            "source": {"table": "subscriptions", "column": "subscription_id",
                        "filter": "auto_renew = true"},
            "time_field": "start_date",
        })
        assert '"subscriptions"."auto_renew" = TRUE' in q.sql

    def test_h08_explicit_time_field_required_for_multi_date(self):
        """subscriptions 有 start_date/end_date 两个日期列：不显式指定必须拒绝。"""
        with pytest.raises(CompileError, match="time_field"):
            compile_b({
                "base_aggregation": "sum",
                "source": {"table": "subscriptions", "column": "mrr_amount"},
            })
        q = compile_b({
            "base_aggregation": "sum",
            "source": {"table": "subscriptions", "column": "mrr_amount"},
            "time_field": "start_date",
        })
        assert '"subscriptions"."start_date" >= $__start__' in q.sql

    def test_h09_is_not_null_and_multi_condition(self):
        q = compile_b({
            "base_aggregation": "count",
            "source": {"table": "usage_events", "column": "event_id",
                        "filter": "feature_name IS NOT NULL AND usage_count > 0"},
        })
        assert '"usage_events"."feature_name" IS NOT NULL' in q.sql
        assert '"usage_events"."usage_count" > 0' in q.sql

    def test_h10_arpu_ratio(self):
        """ARPU = MRR / 活跃账户数（订阅服务典型比率）。"""
        q = compile_b({
            "expression": "A / B",
            "operands": {
                "A": {"table": "subscriptions", "column": "mrr_amount", "aggregation": "sum",
                       "filter": "status = 'active'"},
                "B": {"table": "usage_events", "column": "account_id",
                       "aggregation": "count_distinct"},
            },
            "time_field": "start_date",
        })
        assert q.dataset_names == ["subscriptions", "usage_events"]
        assert "NULLIF" in q.sql
