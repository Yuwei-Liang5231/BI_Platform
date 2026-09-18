"""B1 数据接入 API 集成测试。

覆盖验收主线：GBK 识别、混杂类型列标记、覆盖区间、自动命名、
预览回读、异常值定位、表关系注册校验、删除级联。
"""

from __future__ import annotations

import io

import pytest


def _csv_bytes(header: list[str], rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode(encoding)


def _upload(client, data: bytes, filename: str, name: str | None = None):
    fp = io.BytesIO(data)
    form = {"file": (filename, fp, "text/csv")}
    fields = {"name": (None, name)} if name else {}
    return client.post("/api/datasets/upload", files=form, data=fields)


@pytest.fixture()
def orders_id(client):
    """零售订单样例（UTF-8，含混杂列 + 日期列）。

    B5 起显式命名 ing_*：模板库按数据集名绑定 calc_rule（orders/users 等
    名字留给 test_templates_api 的 saga），避免全量回归时撞名。
    """
    resp = _upload(
        client,
        _csv_bytes(
            ["order_id", "order_date", "pay_amount", "coupon_code"],
            [
                ["SO1", "2025-01-01", "99.5", "1001"],
                ["SO2", "2025-01-15", "-5.0", "VIP88"],
                ["SO3", "2025-02-03", "120.0", ""],
            ],
            "utf-8",
        ),
        "orders.csv",
        name="ing_orders",
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


@pytest.fixture()
def users_id(client):
    resp = _upload(
        client,
        _csv_bytes(
            ["user_id", "region"],
            [["U1", "华东"], ["U2", "华南"]],
            "utf-8",
        ),
        "users.csv",
        name="ing_users",
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------- 上传与识别


def test_upload_gbk_detected(client):
    resp = _upload(
        client,
        _csv_bytes(
            ["订单号", "金额"],
            [["SO1", "99.5"], ["SO2", "120"]],
            "gbk",
        ),
        "orders_gbk.csv",
        name="gbk_orders",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["encoding"] == "gbk"
    assert data["row_count"] == 2
    assert data["columns"][0]["name"] == "订单号"


def test_upload_mixed_column_flagged(client, orders_id):
    detail = client.get(f"/api/datasets/{orders_id}").json()["data"]
    cols = {c["name"]: c for c in detail["columns"]}
    assert cols["coupon_code"]["mixed"] is True
    assert cols["coupon_code"]["type"] == "mixed"
    assert cols["order_date"]["type"] == "date"
    assert cols["pay_amount"]["type"] == "float"


def test_upload_coverage_correct(client, orders_id):
    detail = client.get(f"/api/datasets/{orders_id}").json()["data"]
    cov = detail["coverage"]
    assert len(cov) == 1
    assert cov[0]["column"] == "order_date"
    assert cov[0]["period_start"] == "2025-01-01"
    assert cov[0]["period_end"] == "2025-02-03"
    assert cov[0]["non_null_count"] == 3


def test_upload_duplicate_name_auto_suffix(client, users_id):
    resp = _upload(
        client,
        _csv_bytes(["user_id"], [["U9"]], "utf-8"),
        "users.csv",
        name="ing_users",
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] != "ing_users"
    assert resp.json()["data"]["name"].startswith("ing_users_")


def test_upload_unsupported_extension(client):
    resp = _upload(client, b"whatever", "data.txt")
    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


def test_upload_ragged_rows_rejected_with_locations(client):
    """列数不一致：明确拒绝并给出行号（异常行定位）。"""
    data = b"a,b\n1,2\n3\n4,5\n"
    resp = _upload(client, data, "ragged.csv")
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40000
    assert body["data"]["ragged_rows"] == [2]


def test_upload_leading_zero_stays_string(client):
    resp = _upload(
        client,
        _csv_bytes(["code"], [["007"], ["008"]], "utf-8"),
        "codes.csv",
    )
    cols = resp.json()["data"]["columns"]
    assert cols[0]["type"] == "string"


def test_upload_datetime_coverage_and_null_literal(client):
    """真实 ETL 场景：datetime 列进覆盖区间；'NULL' 字面量列计为空值。"""
    resp = _upload(
        client,
        _csv_bytes(
            ["id", "bkg_start", "etl_date"],
            [
                ["1", "2023-08-21 08:30:00", "NULL"],
                ["2", "2024-03-11 08:30:00", "NULL"],
                ["3", "2024-02-19 17:30:00", "NULL"],
            ],
            "utf-8",
        ),
        "bookings.csv",
        name="bookings",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    cols = {c["name"]: c for c in data["columns"]}
    assert cols["bkg_start"]["type"] == "datetime"
    assert cols["etl_date"]["type"] == "string"
    assert cols["etl_date"]["null_count"] == 3

    cov = {c["column"]: c for c in data["coverage"]}
    assert "bkg_start" in cov
    assert cov["bkg_start"]["period_start"] == "2023-08-21"
    assert cov["bkg_start"]["period_end"] == "2024-03-11"


# ---------------------------------------------------------------- 重命名


def test_rename_dataset(client, orders_id):
    resp = client.patch(f"/api/datasets/{orders_id}/name", json={"name": "orders_v2"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "orders_v2"

    detail = client.get(f"/api/datasets/{orders_id}").json()["data"]
    assert detail["name"] == "orders_v2"


def test_rename_conflict_rejected(client, orders_id, users_id):
    resp = client.patch(f"/api/datasets/{orders_id}/name", json={"name": "ing_users"})
    assert resp.status_code == 409


def test_rename_invalid_name_rejected(client, orders_id):
    resp = client.patch(f"/api/datasets/{orders_id}/name", json={"name": "###"})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 预览与定位


def test_preview_reads_parquet_back(client, orders_id):
    resp = client.get(f"/api/datasets/{orders_id}/preview", params={"rows": 2})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["returned"] == 2
    assert data["rows"][0]["order_id"] == "SO1"


def test_anomalies_locate_minority_values(client, orders_id):
    resp = client.get(f"/api/datasets/{orders_id}/columns/coupon_code/anomalies")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["mixed"] is True
    assert data["dominant_type"] == "int"
    rows = {d["row"] for d in data["deviations"]}
    assert 2 in rows  # VIP88 在数据行 2
    assert all(d["value_type"] == "string" for d in data["deviations"])


def test_anomalies_pure_column_returns_empty(client, orders_id):
    resp = client.get(f"/api/datasets/{orders_id}/columns/order_date/anomalies")
    assert resp.json()["data"]["mixed"] is False


# ---------------------------------------------------------------- 列表 / 详情 / 删除


def test_list_datasets(client, orders_id, users_id):
    resp = client.get("/api/datasets")
    names = {d["name"] for d in resp.json()["data"]}
    assert {"ing_orders", "ing_users"} <= names


def test_get_missing_dataset_404_envelope(client):
    resp = client.get("/api/datasets/99999")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


def test_delete_dataset_cascades(client, orders_id):
    resp = client.delete(f"/api/datasets/{orders_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/datasets/{orders_id}").status_code == 404


def test_delete_removes_files(client):
    """删除数据集必须同时清理上传件与 Parquet（回归：孤儿文件缺陷）。"""
    resp = _upload(
        client,
        _csv_bytes(["a"], [["1"]], "utf-8"),
        "todelete.csv",
        name="todelete",
    )
    data = resp.json()["data"]
    ds_id = data["id"]

    resp = client.delete(f"/api/datasets/{ds_id}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["files_removed"] == 2, f"文件未清理干净: {payload}"


def test_batch_delete_datasets(client):
    """批量删除：成功项回执 id、不存在项进 failed 不中断批次（逐条独立提交）。"""
    ids = []
    for i in range(2):
        resp = _upload(
            client,
            _csv_bytes(["a"], [[str(i)]], "utf-8"),
            f"batchdel{i}.csv",
            name=f"batchdel{i}",
        )
        ids.append(resp.json()["data"]["id"])

    resp = client.post("/api/datasets/batch-delete", json={"ids": ids + [999999]})
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert sorted(payload["deleted"]) == sorted(ids)
    assert len(payload["failed"]) == 1
    assert payload["failed"][0]["id"] == 999999
    # 已删的确实查不到了
    for ds_id in ids:
        assert client.get(f"/api/datasets/{ds_id}").status_code == 404


def test_batch_delete_datasets_empty_ids_rejected(client):
    resp = client.post("/api/datasets/batch-delete", json={"ids": []})
    assert resp.status_code == 400


def test_int64_overflow_rejected_as_400(client):
    """超出 int64 的大整数必须明确报 400，而不是 500（B14.1 起由 DuckDB TRY_CAST 校验拦截）。"""
    huge = "9" * 25  # 10^25 量级，超 int64 上限约 9.2×10^18
    resp = _upload(
        client,
        _csv_bytes(["n"], [[huge], ["1"]], "utf-8"),
        "huge.csv",
    )
    assert resp.status_code == 400, resp.text
    assert "n" in resp.json()["message"] or "int64" in resp.json()["message"]


def test_upload_slash_date_vectorized_cast(client):
    """B14.1 单遍解析改造回归：斜杠日期（中文 Excel 常见）经 DuckDB TRY_CAST 正确入库。

    旧实现逐格 Python 转换；现改为字符串旁路分片 + 向量化 cast，格式兼容面
    （ISO 与斜杠、单位数月/日）必须保持。
    """
    resp = _upload(
        client,
        _csv_bytes(["d", "v"], [["2025/9/30", "10"], ["2025/10/1", "20"]], "utf-8"),
        "slash_date.csv",
        name="slash_date",
    )
    assert resp.status_code == 200, resp.text
    ds = resp.json()["data"]
    try:
        cols = {c["name"]: c for c in ds["columns"]}
        assert cols["d"]["type"] == "date"
        prev = client.get(f"/api/datasets/{ds['id']}/preview").json()["data"]
        assert prev["rows"][0]["d"] == "2025-09-30"
    finally:
        client.delete(f"/api/datasets/{ds['id']}")


def test_upload_leaves_no_stage_temp_file(client):
    """B14.1 旁路缓存分片必须在物化完成后清理（.stage-*.parquet 不残留）。"""
    from app.core.config import get_settings
    from app.infra.storage.paths import StoragePaths

    resp = _upload(client, _csv_bytes(["a"], [["1"]], "utf-8"), "stg.csv", name="stage_check")
    assert resp.status_code == 200, resp.text
    ds = resp.json()["data"]
    try:
        d = StoragePaths(get_settings()).parquet_dir / "stage_check"
        files = sorted(p.name for p in d.iterdir())
        assert files == ["part-0001.parquet"], f"残留临时文件: {files}"
    finally:
        client.delete(f"/api/datasets/{ds['id']}")


def test_gbk_after_ascii_prefix_retried(client):
    """回归：前 64KB 纯 ASCII + 后段 GBK 中文，采样误判 utf-8 后必须自动重试成功。

    真实案例：SDC Hours CSV 头部全 ASCII 判成 utf-8，
    流式解码到中后段 0xa8 字节报 UnicodeDecodeError → 500。
    """
    lines = ["id,note"]
    # ~2500 行 × ~30 字节 ≈ 75KB 纯 ASCII，超过 64KB 采样窗口
    for i in range(2500):
        lines.append(f"{i},ascii_padding_line_{i:06d}_xxxxxxxxxx")
    lines.append("99999,华东区")
    lines.append("99998,华南区")
    payload = ("\n".join(lines) + "\n").encode("gbk")
    assert len(payload) > 65536  # 确认超过采样窗口

    resp = _upload(client, payload, "mixed_head.csv", name="mixed_head")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["encoding"] == "gbk"
    assert data["row_count"] == 2502

    # 中文内容完好（GBK 正确解码）——直接读 Parquet 尾部验证（B8 起为分片目录）
    import pyarrow.parquet as pq

    from app.core.config import get_settings
    from app.infra.storage.paths import StoragePaths

    table = pq.read_table(str(StoragePaths(get_settings()).parquet_dir / "mixed_head"))
    notes = table.column("note").to_pylist()
    assert "华东区" in notes
    assert "华南区" in notes


# ---------------------------------------------------------------- 表关系


def test_relation_flow(client, orders_id, users_id):
    # 注册
    resp = client.post(
        f"/api/datasets/{orders_id}/relations",
        json={"from_column": "order_id", "target_dataset_id": users_id, "target_column": "user_id"},
    )
    assert resp.status_code == 200, resp.text
    rel_id = resp.json()["data"]["id"]

    # 列表
    rels = client.get(f"/api/datasets/{orders_id}/relations").json()["data"]
    assert len(rels) == 1

    # 重复注册 → 409
    resp = client.post(
        f"/api/datasets/{orders_id}/relations",
        json={"from_column": "order_id", "target_dataset_id": users_id, "target_column": "user_id"},
    )
    assert resp.status_code == 409

    # 非法列 → 400
    resp = client.post(
        f"/api/datasets/{orders_id}/relations",
        json={"from_column": "no_such_col", "target_dataset_id": users_id, "target_column": "user_id"},
    )
    assert resp.status_code == 400

    # 删除关系
    assert client.delete(f"/api/datasets/{orders_id}/relations/{rel_id}").status_code == 200
    assert client.get(f"/api/datasets/{orders_id}/relations").json()["data"] == []


def test_relation_invalid_type_rejected(client, orders_id, users_id):
    resp = client.post(
        f"/api/datasets/{orders_id}/relations",
        json={
            "from_column": "order_id",
            "target_dataset_id": users_id,
            "target_column": "user_id",
            "relation_type": "weird",
        },
    )
    assert resp.status_code == 400


# --------------------------------------------------------------- xlsx 日期序列号回归
# 用户实例（2026-09-17）：WIP 表 MONTH_END 在 Excel 中显示 2026年9月30日，
# 入库后变 46295。根因：openpyxl BUILTIN_FORMATS 只定义到 49，东亚版 Excel/WPS
# 的中文日期内置 numFmtId（27-36、50-58）被 fallback 成 "General"，
# 读取端 date_formats 集合不收录 → 日期列返回裸序列号。


def _xlsx_bytes(rows) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _rewrite_cellxfs_numfmt(xlsx: bytes, from_id: int, to_id: int) -> bytes:
    """仅替换 cellXfs 段内 numFmtId（不动 numFmts 自定义定义段），模拟
    单元格引用东亚内置日期 ID 且自定义格式表中无该 ID 的真实场景。"""
    import re
    import zipfile

    zin = zipfile.ZipFile(io.BytesIO(xlsx))
    items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    zin.close()
    styles = items["xl/styles.xml"].decode("utf-8")
    m = re.search(r"(<cellXfs[^>]*>)(.*?)(</cellXfs>)", styles, re.S)
    assert m, "styles.xml 缺 cellXfs"
    inner = m.group(2).replace(f'numFmtId="{from_id}"', f'numFmtId="{to_id}"')
    assert inner != m.group(2), "cellXfs 内未找到目标 numFmtId"
    items["xl/styles.xml"] = (
        styles[: m.start()] + m.group(1) + inner + m.group(3) + styles[m.end() :]
    ).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in items.items():
            zout.writestr(name, data)
    return out.getvalue()


def _upload_xlsx(client, raw: bytes, name: str):
    return client.post(
        "/api/datasets/upload",
        files={
            "file": (
                f"{name}.xlsx",
                io.BytesIO(raw),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"name": name},
    )


def test_xlsx_ea_builtin_date_id_serial_converted(client):
    """numFmtId=58（东亚内置 yyyy年m月d日）的日期列序列号 → 入库转回日期。"""
    from datetime import datetime

    raw = _xlsx_bytes([["MONTH_END", "AR"], [datetime(2026, 9, 30), 100]])
    raw = _rewrite_cellxfs_numfmt(raw, 164, 58)
    resp = _upload_xlsx(client, raw, "ing_ea_date")
    assert resp.status_code == 200, resp.text
    ds = resp.json()["data"]
    try:
        cols = {c["name"]: c for c in ds["columns"]}
        assert cols["MONTH_END"]["type"] == "date"
        prev = client.get(f"/api/datasets/{ds['id']}/preview").json()["data"]
        assert prev["rows"][0]["MONTH_END"] == "2026-09-30"
        assert prev["rows"][0]["AR"] == 100  # 普通数值列不受日期换算影响
    finally:
        client.delete(f"/api/datasets/{ds['id']}")


def test_xlsx_datetime_midnight_as_date_and_time_kept(client):
    """openpyxl 正常写出的 datetime：零点归 date，带时间保留完整 datetime。"""
    from datetime import datetime

    raw = _xlsx_bytes(
        [
            ["MONTH_END", "ETL_TIME"],
            [datetime(2026, 9, 30), datetime(2026, 9, 17, 8, 30, 0)],
        ]
    )
    resp = _upload_xlsx(client, raw, "ing_ea_dt")
    assert resp.status_code == 200, resp.text
    ds = resp.json()["data"]
    try:
        prev = client.get(f"/api/datasets/{ds['id']}/preview").json()["data"]
        assert prev["rows"][0]["MONTH_END"] == "2026-09-30"
        assert prev["rows"][0]["ETL_TIME"] == "2026-09-17T08:30:00"
        cols = {c["name"]: c for c in ds["columns"]}
        assert cols["MONTH_END"]["type"] == "date"
        assert cols["ETL_TIME"]["type"] == "datetime"
    finally:
        client.delete(f"/api/datasets/{ds['id']}")
