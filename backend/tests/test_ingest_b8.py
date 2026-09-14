"""B8 数据质检报告 + 增量导入 API 集成测试。

验收主线（执行方案阶段 2 / 架构 §7.9 扩展）：
- 质检报告：逐列空值/空列/常量列/混杂偏离（行号+原始内容）+ 覆盖区间
- 增量导入 append：列名校验、按现有类型转换、分片追加、ver+1、覆盖刷新
- 增量导入 replace：全量覆盖、ver+1
- 失败路径：schema 不一致 / 类型冲突 → 400 且不落盘不递增
- 缓存失效端到端：append 后同区间取数 cache=miss 且值更新（契约 v2）
- viewer 无权导入（admin 专用）
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy.orm import sessionmaker

from app.infra.cache import metric_cache
from app.infra.database import get_engine
from app.infra.models import Dataset


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _upload(client, data: bytes, filename: str, name: str):
    return client.post(
        "/api/datasets/upload",
        files={"file": (filename, io.BytesIO(data), "text/csv")},
        data={"name": name},
    )


def _append(client, dataset_id: int, data: bytes, filename: str, mode: str = "append"):
    return client.post(
        f"/api/datasets/{dataset_id}/data",
        files={"file": (filename, io.BytesIO(data), "text/csv")},
        data={"mode": mode},
    )


# ---------------------------------------------------------------- 质检报告


class TestQualityReport:
    @pytest.fixture(scope="class")
    def quality_id(self, client):
        resp = _upload(
            client,
            _csv_bytes(
                ["d", "amount", "note", "flag", "empty_col"],
                [
                    ["2025-01-01", "10", "A", "same", ""],
                    ["2025-01-02", "20", "99", "same", ""],
                    ["2025-01-03", "", "B", "same", ""],
                    ["2025-01-04", "40", "", "same", ""],
                ],
            ),
            "quality.csv",
            "b8_quality_ds",
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["id"]

    def test_report_structure_and_issues(self, client, quality_id):
        resp = client.get(f"/api/datasets/{quality_id}/quality")
        assert resp.status_code == 200, resp.text
        rep = resp.json()["data"]
        assert rep["row_count"] == 4
        cols = {c["name"]: c for c in rep["columns"]}

        # 空列：high
        assert cols["empty_col"]["is_empty"] is True
        assert cols["empty_col"]["issues"][0]["code"] == "empty_column"
        assert cols["empty_col"]["issues"][0]["level"] == "high"

        # 混杂列（note：A/B 字符串 + 99 数字）：high + 行级定位
        note = cols["note"]
        assert note["mixed"] is True
        codes = [i["code"] for i in note["issues"]]
        assert "mixed_types" in codes
        assert note["dominant_type"] == "string"
        dev_rows = {d["row"] for d in note["deviations"]}
        assert 2 in dev_rows  # 数据行 2 的 '99' 是数字偏离
        assert all("value" in d and "value_type" in d for d in note["deviations"])

        # 高空值列（amount 1/4 空 → 25%，不超阈值不标 medium）
        assert cols["amount"]["null_count"] == 1
        assert all(i["code"] != "high_null_ratio" for i in cols["amount"]["issues"])

        # 常量列：info
        assert cols["flag"]["is_constant"] is True
        assert any(i["code"] == "constant_column" for i in cols["flag"]["issues"])

        # 日期列带覆盖区间
        cov = {c["column"]: c for c in rep["coverage"]}
        assert cov["d"]["period_start"] == "2025-01-01"
        assert cov["d"]["period_end"] == "2025-01-04"

    def test_high_null_ratio_flagged(self, client):
        resp = _upload(
            client,
            _csv_bytes(
                ["d", "v"],
                [["2025-01-01", "1"], ["2025-01-02", ""], ["2025-01-03", ""], ["2025-01-04", ""]],
            ),
            "nulls.csv",
            "b8_nulls_ds",
        )
        ds_id = resp.json()["data"]["id"]
        rep = client.get(f"/api/datasets/{ds_id}/quality").json()["data"]
        v = {c["name"]: c for c in rep["columns"]}["v"]
        assert any(i["code"] == "high_null_ratio" for i in v["issues"])
        assert v["null_sample_rows"] == [2, 3, 4]

    def test_missing_dataset_404(self, client):
        assert client.get("/api/datasets/999999/quality").status_code == 404


# ---------------------------------------------------------------- 增量导入


class TestIncrementalImport:
    @pytest.fixture(scope="class")
    def ds_id(self, client):
        resp = _upload(
            client,
            _csv_bytes(
                ["sale_date", "amount", "status"],
                [
                    ["2025-03-01", "100", "paid"],
                    ["2025-03-15", "50", "paid"],
                ],
            ),
            "inc.csv",
            "b8_inc_ds",
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["id"]

    def test_append_success_updates_rows_ver_coverage(self, client, ds_id):
        resp = _append(
            client,
            ds_id,
            _csv_bytes(
                ["status", "sale_date", "amount"],  # 列顺序不同：应按现有 schema 重排
                [["paid", "2025-04-02", "70"]],
            ),
            "inc2.csv",
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        imp = data["import"]
        assert imp["mode"] == "append"
        assert imp["rows_added"] == 1
        assert imp["dataset_ver"] == 2
        assert data["row_count"] == 3

        cov = {c["column"]: c for c in data["coverage"]}
        assert cov["sale_date"]["period_start"] == "2025-03-01"
        assert cov["sale_date"]["period_end"] == "2025-04-02"
        assert cov["sale_date"]["non_null_count"] == 3

    def test_append_schema_mismatch_rejected(self, client, ds_id):
        before = client.get(f"/api/datasets/{ds_id}").json()["data"]
        resp = _append(
            client,
            ds_id,
            _csv_bytes(["sale_date", "revenue"], [["2025-05-01", "9"]]),
            "bad.csv",
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["data"]["missing_columns"] == ["amount", "status"]
        assert body["data"]["extra_columns"] == ["revenue"]
        after = client.get(f"/api/datasets/{ds_id}").json()["data"]
        assert after["dataset_ver"] == before["dataset_ver"] == 2
        assert after["row_count"] == before["row_count"]

    def test_append_type_violation_rejected_no_side_effect(self, client, ds_id):
        """amount 是 float 列：文本值必须 400，且不留半成品分片、ver 不变。"""
        before = client.get(f"/api/datasets/{ds_id}").json()["data"]
        resp = _append(
            client,
            ds_id,
            _csv_bytes(["sale_date", "amount", "status"], [["2025-05-01", "abc", "paid"]]),
            "badtype.csv",
        )
        assert resp.status_code == 400
        after = client.get(f"/api/datasets/{ds_id}").json()["data"]
        assert after["dataset_ver"] == before["dataset_ver"]
        assert after["row_count"] == before["row_count"]

        # 存储侧：数据集目录下无 .tmp 残留、分片数不增
        Session = sessionmaker(bind=get_engine())
        with Session() as s:
            row = s.query(Dataset).filter(Dataset.id == ds_id).first()
            from pathlib import Path

            d = Path(row.parquet_path)
            assert list(d.glob(".tmp-*")) == []
            assert len(list(d.glob("part-*.parquet"))) == 2  # part-0001 + part-0002

    def test_replace_overwrites(self, client, ds_id):
        resp = _append(
            client,
            ds_id,
            _csv_bytes(["sale_date", "amount", "status"], [["2025-06-01", "999", "paid"]]),
            "full.csv",
            mode="replace",
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["import"]["mode"] == "replace"
        assert data["row_count"] == 1
        assert data["import"]["dataset_ver"] == 3
        cov = {c["column"]: c for c in data["coverage"]}
        assert cov["sale_date"]["period_start"] == "2025-06-01"
        assert cov["sale_date"]["period_end"] == "2025-06-01"

    def test_bad_mode_rejected(self, client, ds_id):
        resp = _append(client, ds_id, _csv_bytes(["sale_date", "amount", "status"], []), "x.csv", mode="upsert")
        # 空数据行先于 mode 校验被拦截也可，但 mode 非法必须在任何落盘前报 400
        assert resp.status_code == 400

    def test_viewer_forbidden(self, client, ds_id):
        from tests.conftest import create_test_user

        viewer_headers = create_test_user(client, "b8_viewer", role="viewer")
        resp = client.post(
            f"/api/datasets/{ds_id}/data",
            files={"file": ("x.csv", io.BytesIO(_csv_bytes(["sale_date", "amount", "status"],
                                                          [["2025-07-01", "1", "paid"]])), "text/csv")},
            data={"mode": "append"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------- 缓存失效端到端


B8_CACHE_DS = "b8_cache_ds"
B8_CACHE_METRIC = "b8_cache_gmv"


class TestLegacySingleFileMigration:
    """存量数据集（B1 单文件布局 parquet_path=file）首次增量导入自动迁移为分片目录。"""

    def test_legacy_single_file_append_migrates(self, client, tmp_path):
        """手工构造单文件布局数据集 + 登记元数据，append 后目录化。"""
        import json as jsonlib
        from datetime import datetime

        import pyarrow as pa
        import pyarrow.parquet as pq
        from app.core.config import get_settings
        from app.infra.storage.paths import StoragePaths

        storage = StoragePaths(get_settings())
        storage.ensure_dirs()
        name = f"b8_legacy_{datetime.now().strftime('%H%M%S%f')}"
        parquet_file = storage.parquet_dir / f"{name}.parquet"
        schema = pa.schema([
            pa.field("sale_date", pa.date32()),
            pa.field("amount", pa.float64()),
            pa.field("status", pa.string()),
        ])
        import datetime as dt

        pq.write_table(
            pa.table({"sale_date": [dt.date(2025, 10, 1)], "amount": [5.0], "status": ["paid"]}, schema=schema),
            parquet_file,
        )

        # 直接登记元数据（绕过 upload，模拟 B1 存量布局）
        from app.infra.database import get_engine

        Session = sessionmaker(bind=get_engine())
        with Session() as s:
            ds = Dataset(
                name=name,
                source_filename=f"{name}.csv",
                file_path=str(storage.uploads_dir / f"{name}.csv"),
                parquet_path=str(parquet_file),  # 指向单文件
                file_encoding="utf-8",
                row_count=1,
                column_count=3,
                schema_json=jsonlib.dumps([
                    {"name": "sale_date", "type": "date", "mixed": False, "null_count": 0,
                     "non_null_count": 1, "sample": ["2025-10-01"]},
                    {"name": "amount", "type": "float", "mixed": False, "null_count": 0,
                     "non_null_count": 1, "sample": ["5.0"]},
                    {"name": "status", "type": "string", "mixed": False, "null_count": 0,
                     "non_null_count": 1, "sample": ["paid"]},
                ]),
            )
            s.add(ds)
            s.commit()
            ds_id = ds.id

        resp = _append(
            client,
            ds_id,
            _csv_bytes(["sale_date", "amount", "status"], [["2025-10-02", "6.0", "paid"]]),
            "legacy_append.csv",
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["row_count"] == 2
        assert data["import"]["dataset_ver"] == 2

        from pathlib import Path

        with Session() as s:
            row = s.query(Dataset).filter(Dataset.id == ds_id).first()
            d = Path(row.parquet_path)
            assert d.is_dir(), f"应迁移为分片目录: {row.parquet_path}"
            parts = sorted(p.name for p in d.glob("part-*.parquet"))
            assert parts == ["part-0001.parquet", "part-0002.parquet"], parts
            # 旧单文件应已移走
            assert not parquet_file.exists()

        # 迁移后取数链路可用（视图 glob 生效）
        resp = client.post("/api/metrics", json={
            "code": f"b8_legacy_gmv_{ds_id}",
            "name": "存量迁移GMV",
            "calc_rule": {"base_aggregation": "sum", "source": {"table": name, "column": "amount"}},
        })
        assert resp.status_code == 200, resp.text
        v = client.post("/api/query/metric-value", json={
            "metric": f"b8_legacy_gmv_{ds_id}", "start": "2025-10-01", "end": "2025-10-31",
        }).json()["data"]
        assert v["value"] == pytest.approx(11.0)


class TestCacheInvalidationOnAppend:
    @pytest.fixture(scope="class")
    def cache_env(self, client):
        metric_cache.clear()
        resp = _upload(
            client,
            _csv_bytes(
                ["sale_date", "amount", "status"],
                [["2025-08-01", "100", "paid"]],
            ),
            "c.csv",
            B8_CACHE_DS,
        )
        assert resp.status_code == 200, resp.text
        ds_id = resp.json()["data"]["id"]
        resp = client.post("/api/metrics", json={
            "code": B8_CACHE_METRIC,
            "name": "B8缓存GMV",
            "calc_rule": {"base_aggregation": "sum",
                          "source": {"table": B8_CACHE_DS, "column": "amount"}},
        })
        assert resp.status_code == 200, resp.text
        return ds_id

    def _query(self, client):
        return client.post("/api/query/metric-value", json={
            "metric": B8_CACHE_METRIC, "start": "2025-08-01", "end": "2025-08-31",
            "compare": "none",
        })

    def test_append_invalidates_cache_and_updates_value(self, client, cache_env):
        ds_id = cache_env
        r1 = self._query(client).json()["data"]
        assert r1["value"] == pytest.approx(100.0)
        assert r1["cache"] == "miss"
        r2 = self._query(client).json()["data"]
        assert r2["cache"] == "hit"  # 命中旧缓存

        # 增量导入新数据（同月另一天）→ ver+1 → 旧键失效
        resp = _append(
            client,
            ds_id,
            _csv_bytes(["sale_date", "amount", "status"], [["2025-08-15", "27", "paid"]]),
            "c2.csv",
        )
        assert resp.status_code == 200, resp.text

        r3 = self._query(client).json()["data"]
        assert r3["cache"] == "miss", "append 后同区间必须缓存 miss"
        assert r3["value"] == pytest.approx(127.0), "append 后同区间必须返回新值"
        r4 = self._query(client).json()["data"]
        assert r4["cache"] == "hit"
        assert r4["value"] == pytest.approx(127.0)
