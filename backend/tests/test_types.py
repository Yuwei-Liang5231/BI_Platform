"""类型推断单元测试（含前导零保护、混杂判定）。"""

from __future__ import annotations

from app.domain.ingestion.types import classify_value, normalize_header, scan_rows


def test_classify_basics():
    assert classify_value("  42 ") == "int"
    assert classify_value("-3") == "int"
    assert classify_value("10.5") == "float"
    assert classify_value("-.5e3") == "float"
    assert classify_value("2026-09-10") == "date"
    assert classify_value("TRUE") == "bool"
    assert classify_value("false") == "bool"
    assert classify_value("SO2026001") == "string"
    assert classify_value("") == ""
    assert classify_value("   ") == ""
    assert classify_value("nan") == "string"  # 不许 float('nan') 混进来
    assert classify_value("2026-13-40") == "string"  # 日期格式对但不可解析


def test_classify_datetime():
    """真实 ETL 导出常见 yyyy-MM-dd HH:MM:SS；空格与 T 分隔符都要识别。"""
    assert classify_value("2023-08-21 08:30:00") == "datetime"
    assert classify_value("2023-08-21T08:30:00") == "datetime"
    assert classify_value("2023-08-21 08:30") == "datetime"
    assert classify_value("2023-08-21 08:30:00.123456") == "datetime"
    assert classify_value("2023-08-21") == "date"  # 纯日期不误判
    assert classify_value("2023-08-21 25:00:00") == "string"  # 不可解析不误判


def test_classify_slash_date():
    """中文 Excel 常见斜杠格式（真实案例：SDC Hours 的 SUBMITTED_PERIOD）。"""
    assert classify_value("2025/9/30") == "date"
    assert classify_value("2025/12/31") == "date"
    assert classify_value("2025-9-30") == "date"
    assert classify_value("2025/9/30 17:30") == "datetime"
    assert classify_value("2025/13/40") == "string"  # 不可解析
    assert classify_value("20/9/30") == "string"  # 年份必须 4 位

    stats, _, _ = scan_rows(["p"], iter([["2025/9/30"], ["2025/2/28"], ["2025/4/15"]]))
    assert stats[0].final_type() == "date"
    assert stats[0].min_date.isoformat() == "2025-02-28"
    assert stats[0].max_date.isoformat() == "2025-09-30"


def test_null_literal_is_null():
    """ETL 导出的 'NULL' 字面量按空值处理；N/A 可能是业务码，不纳入。"""
    assert classify_value("NULL") == ""
    assert classify_value("null") == ""
    assert classify_value(" Null ") == ""
    assert classify_value("N/A") == "string"
    assert classify_value("NA") == "string"


def test_date_datetime_mix_unifies_to_datetime():
    stats, _, _ = scan_rows(
        ["ts"], iter([["2025-01-01"], ["2025-01-02 08:30:00"], ["2025-01-03"]])
    )
    assert stats[0].final_type() == "datetime"
    assert stats[0].min_date.isoformat() == "2025-01-01"
    assert stats[0].max_date.isoformat() == "2025-01-03"


def test_leading_zero_stays_string():
    """前导零数字是编码类 ID（工单号、编码），必须保持字符串。"""
    assert classify_value("007") == "string"
    assert classify_value("0.5") == "float"  # 小数不受影响
    assert classify_value("0") == "int"


def test_scan_rows_mixed_and_nulls():
    header = ["id", "coupon", "d"]
    rows = iter(
        [
            ["1", "1001", "2025-01-01"],
            ["2", "VIP88", "2025-01-02"],
            ["3", "", "2025-01-03"],
            ["4", "2002", "2025-01-04"],
        ]
    )
    stats, row_count, ragged = scan_rows(header, rows)
    assert row_count == 4
    assert ragged == []

    id_stat, coupon_stat, d_stat = stats
    assert id_stat.final_type() == "int"
    assert coupon_stat.final_type() == "mixed"  # int + string
    assert coupon_stat.null_count == 1
    assert d_stat.final_type() == "date"
    assert d_stat.min_date.isoformat() == "2025-01-01"
    assert d_stat.max_date.isoformat() == "2025-01-04"


def test_scan_rows_detects_ragged():
    stats, row_count, ragged = scan_rows(["a", "b"], iter([["1", "2"], ["3"]]))
    assert ragged == [2]
    assert row_count == 2


def test_int_float_mix_is_float_not_mixed():
    """0 与 0.05 同列是精度差异，统一 float；"混杂"留给真正的类型冲突。"""
    stats, _, _ = scan_rows(["d"], iter([["0"], ["0.05"], ["1.5"]]))
    assert stats[0].final_type() == "float"

    stats2, _, _ = scan_rows(["x"], iter([["0.5"], ["VIP88"]]))
    assert stats2[0].final_type() == "mixed"


def test_normalize_header():
    assert normalize_header([" 订单ID", "金额", ""]) == ["订单ID", "金额", "col_3"]
    assert normalize_header(["a", "a"]) == ["a", "a_2"]
