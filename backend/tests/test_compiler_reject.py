"""编译器拒绝用例：超纲算子/缺表关系/列不存在等，保存即拒绝并给出明确报错。

同时验证行业无关的边界：订阅 cohort 类语义在 v1 结构外，必须被拒绝而非静默算错。
"""

from __future__ import annotations

import pytest

from app.domain.metric.compiler import CompileError, DatasetInfo, RelationInfo, compile_metric
from app.domain.metric.schema import CalcRuleError, parse_calc_rule
from tests.test_compiler_golden import REL_A, REL_B, dataset_a, dataset_b


def compile_a(rule: dict):
    return compile_metric(rule, dataset_a(), REL_A)


def compile_b(rule: dict):
    return compile_metric(rule, dataset_b(), REL_B)


# ---------------------------------------------------------------- 结构层拒绝（无需数据集）


class TestCalcRuleReject:
    def test_unknown_aggregation(self):
        with pytest.raises(CalcRuleError, match="aggregation"):
            parse_calc_rule({
                "base_aggregation": "median",
                "source": {"table": "orders", "column": "pay_amount"},
            })

    def test_group_by_key_rejected(self):
        """cohort/分组语义：结构外字段，明确拒绝而非静默算错。"""
        with pytest.raises(CalcRuleError, match="group_by"):
            parse_calc_rule({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount"},
                "group_by": "register_month",
            })

    def test_window_operand_key_rejected(self):
        with pytest.raises(CalcRuleError, match="partition_by"):
            parse_calc_rule({
                "expression": "A",
                "operands": {"A": {"table": "orders", "column": "pay_amount",
                                    "aggregation": "sum", "partition_by": "user_id"}},
            })

    def test_or_not_supported(self):
        with pytest.raises(CalcRuleError, match="AND"):
            parse_calc_rule({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount",
                            "filter": "order_status = 'paid' OR order_status = 'refunded'"},
            })

    def test_function_call_rejected(self):
        with pytest.raises(CalcRuleError):
            parse_calc_rule({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount",
                            "filter": "YEAR(order_date) = 2026"},
            })

    def test_expression_numeric_constant_rejected(self):
        with pytest.raises(CalcRuleError, match="非法记号"):
            parse_calc_rule({
                "expression": "A + 1",
                "operands": {"A": {"table": "orders", "column": "pay_amount",
                                    "aggregation": "sum"}},
            })

    def test_expression_undefined_operand(self):
        with pytest.raises(CalcRuleError, match="未定义的操作数"):
            parse_calc_rule({
                "expression": "A / C",
                "operands": {"A": {"table": "orders", "column": "pay_amount",
                                    "aggregation": "sum"}},
            })

    def test_expression_unused_operand(self):
        with pytest.raises(CalcRuleError, match="未引用"):
            parse_calc_rule({
                "expression": "A",
                "operands": {
                    "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum"},
                    "B": {"table": "orders", "column": "quantity", "aggregation": "sum"},
                },
            })

    def test_both_forms_rejected(self):
        with pytest.raises(CalcRuleError, match="不能同时"):
            parse_calc_rule({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount"},
                "expression": "A",
                "operands": {"A": {"table": "orders", "column": "pay_amount",
                                    "aggregation": "sum"}},
            })

    def test_nested_aggregation_key_rejected(self):
        with pytest.raises(CalcRuleError, match="不支持的字段"):
            parse_calc_rule({
                "base_aggregation": "avg",
                "source": {"table": "orders", "column": "pay_amount",
                            "having": "COUNT(*) > 1"},
            })

    def test_operand_name_reserved(self):
        with pytest.raises(CalcRuleError, match="保留字"):
            compile_b({
                "expression": "__start__ / B",
                "operands": {
                    "__start__": {"table": "accounts", "column": "account_id",
                                   "aggregation": "count"},
                    "B": {"table": "accounts", "column": "account_id", "aggregation": "count"},
                },
            })


# ---------------------------------------------------------------- 编译层拒绝（需要数据集/关系）


class TestCompileReject:
    def test_dataset_not_exist(self):
        with pytest.raises(CompileError, match="不存在"):
            compile_a({
                "base_aggregation": "sum",
                "source": {"table": "merchandise", "column": "pay_amount"},
            })

    def test_column_not_exist(self):
        with pytest.raises(CompileError, match="列"):
            compile_a({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "gross_amount"},
            })

    def test_sum_on_string_column(self):
        with pytest.raises(CompileError, match="数值列"):
            compile_a({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "order_status"},
            })

    def test_missing_relation_for_cross_table_filter(self):
        """缺表关系：保存即拒绝并提示登记，禁止隐式推断（不变式 6）。"""
        with pytest.raises(CompileError, match="表关系"):
            compile_metric(
                {"base_aggregation": "sum",
                 "source": {"table": "orders", "column": "pay_amount",
                             "filter": "category = '数码'"}},
                dataset_a(),
                [],  # 未登记 orders → products
            )

    def test_time_field_not_date_column(self):
        with pytest.raises(CompileError, match="不是日期列"):
            compile_a({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount"},
                "time_field": "order_status",
            })

    def test_time_field_not_exist(self):
        with pytest.raises(CompileError, match="time_field"):
            compile_a({
                "base_aggregation": "sum",
                "source": {"table": "orders", "column": "pay_amount"},
                "time_field": "pay_day",
            })

    def test_operand_name_conflicts_with_dataset(self):
        with pytest.raises(CalcRuleError, match="与数据集名冲突"):
            compile_a({
                "expression": "users / B",
                "operands": {
                    "users": {"table": "orders", "column": "pay_amount", "aggregation": "sum"},
                    "B": {"table": "orders", "column": "user_id", "aggregation": "count_distinct"},
                },
            })

    def test_no_date_column_dataset_is_constant(self):
        """日期可选（2026-09-14）：无日期列数据集不再拒绝，编译为全期常数——
        SQL 无时间过滤占位符，coverage 为 None（周期完整性无从谈起）。"""
        compiled = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "products", "column": "cost"},
        })
        assert compiled.is_constant
        assert compiled.time_fields == {}
        assert compiled.coverage_start is None and compiled.coverage_end is None
        assert "$__start__" not in compiled.sql and "$__end__" not in compiled.sql

    def test_explicit_time_field_null_declares_constant(self):
        """规则级 time_field: null 显式声明全期常数（即使数据集有日期列）；
        键缺失时维持自动解析语义（orders 唯一日期列被自动采用）。"""
        explicit = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "pay_amount"},
            "time_field": None,
        })
        assert explicit.is_constant
        assert "$__start__" not in explicit.sql

        implicit = compile_a({
            "base_aggregation": "sum",
            "source": {"table": "orders", "column": "pay_amount"},
        })
        assert not implicit.is_constant
        assert implicit.time_fields == {"orders": "order_date"}

    def test_operand_time_field_null_mixed_constant_operand(self):
        """operand 级 time_field: null：该操作数为常数（全表），另一操作数仍按
        时间过滤；coverage 只来自有时间绑定的操作数。"""
        compiled = compile_a({
            "expression": "A / B",
            "operands": {
                "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum"},
                "B": {"table": "products", "column": "product_id",
                       "aggregation": "count_distinct", "time_field": None},
            },
        })
        assert not compiled.is_constant  # A 有时间绑定
        assert compiled.time_fields == {"orders": "order_date"}
        assert "products" in compiled.dataset_names
        assert "$__end__" in compiled.sql

    def test_rule_null_conflicts_with_operand_time_field(self):
        """规则级 time_field: null 与 operand 级显式 time_field 同时出现 = 口径
        自相矛盾，保存即拒绝。"""
        with pytest.raises(CalcRuleError, match="冲突"):
            compile_a({
                "expression": "A / B",
                "operands": {
                    "A": {"table": "orders", "column": "pay_amount", "aggregation": "sum"},
                    "B": {"table": "orders", "column": "user_id",
                           "aggregation": "count_distinct", "time_field": "order_date"},
                },
                "time_field": None,
            })

    def test_relation_to_unknown_dataset_ignored(self):
        """指向不存在数据集的关系在编译时被忽略，等效于缺关系 → 拒绝。"""
        broken_rels = [RelationInfo("orders", "user_id", "ghost_dim", "id")]
        with pytest.raises(CompileError, match="表关系"):
            compile_metric(
                {"base_aggregation": "sum",
                 "source": {"table": "orders", "column": "pay_amount", "filter": "region = '华东'"}},
                dataset_a(),
                broken_rels,
            )
