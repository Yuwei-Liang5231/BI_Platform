"""P1/P2 表关系增强测试：

P1 常用维度跨表：`表名.列名` 限定名在过滤列/拆解维度列中解析为一跳维表列
（本表精确匹配优先；限定名无匹配显式报错；纯列名多路径仍歧义拒绝）。
P2 复合键关系：column_pairs 多列对 JOIN（全部列对 AND），API 校验与存取。

编译层用本地夹具直验 SQL 形状与 DuckDB 执行数值；API 层走 /datasets/{id}/relations。
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from pathlib import Path

import duckdb
import pytest
from pyarrow import Table, parquet

from app.domain.metric.compiler import (
    CompileError,
    DatasetInfo,
    RelationInfo,
    compile_metric,
    compile_metric_breakdown,
)

# ---------------------------------------------------------------- 编译层夹具

ORDERS_COLS = {
    "order_id": "int", "k1": "string", "k2": "string", "amt": "float",
    "order_date": "date", "status": "string",
}
DIM_COLS = {"k1": "string", "k2": "string", "label": "string", "tier": "string", "tag": "string"}
OTHER_COLS = {"k1": "string", "region": "string", "tag": "string"}  # 与 dim 同名 tag 列（歧义用）


def _datasets() -> dict[str, DatasetInfo]:
    return {
        "orders": DatasetInfo(
            id=1, name="orders", columns=ORDERS_COLS,
            coverage={"order_date": (date(2026, 1, 1), date(2026, 6, 30))},
        ),
        "dim": DatasetInfo(id=2, name="dim", columns=DIM_COLS, coverage={}),
        "other": DatasetInfo(id=3, name="other", columns=OTHER_COLS, coverage={}),
    }


PAIRS = (("k1", "k1"), ("k2", "k2"))
REL_COMPOSITE = [RelationInfo("orders", "k1", "dim", "k1", column_pairs=PAIRS)]
REL_SINGLE = [RelationInfo("orders", "k1", "dim", "k1")]


@pytest.fixture(scope="module")
def duck(tmp_path_factory):
    tmp: Path = tmp_path_factory.mktemp("p12")
    parquet.write_table(
        Table.from_pydict({
            "order_id": [1, 2, 3, 4],
            "k1": ["1", "1", "2", "1"],
            "k2": ["10", "20", "10", "10"],
            "amt": [100.0, 200.0, 300.0, 50.0],
            "order_date": [date(2026, 2, 1)] * 4,
            "status": ["paid"] * 4,
        }),
        tmp / "orders.parquet",
    )
    parquet.write_table(
        Table.from_pydict({
            "k1": ["1", "1", "2", "9"],
            "k2": ["10", "20", "10", "10"],
            "label": ["A", "B", "C", "D"],
            "tier": ["T1", "T1", "T2", "T2"],
        }),
        tmp / "dim.parquet",
    )
    con = duckdb.connect()
    for name in ("orders", "dim"):
        con.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{(tmp / f'{name}.parquet').as_posix()}')"
        )
    return con


def _run_breakdown(con, sql: str) -> dict:
    params = {"__start__": date(2026, 1, 1), "__end__": date(2026, 6, 30)}
    return {r[0]: r[1] for r in con.execute(sql, params).fetchall()}


# ---------------------------------------------------------------- P1 限定名


class TestQualifiedColumn:
    def test_breakdown_qualified_dimension_sql(self):
        """`表名.列名` 拆解维度：JOIN 补齐 + 全限定列引用。"""
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_SINGLE, "dim.label",
        )
        assert 'JOIN "dim" ON "orders"."k1" = "dim"."k1"' in q.sql
        assert '"dim"."label" AS dimension' in q.sql

    def test_breakdown_qualified_filter(self):
        """结构化过滤（归因下钻 path 同款路径）支持限定名列。"""
        from app.domain.metric.schema import FilterCondition

        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_SINGLE, "dim.label",
            extra_filters=[FilterCondition(column="dim.tier", op="=", literal="T1")],
        )
        assert '"dim"."tier" = ' in q.sql

    def test_main_table_exact_match_wins(self):
        """本表存在同名 status 列：纯列名优先本表（限定名不抢精确匹配）。"""
        q = compile_metric(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt",
                                                     "filter": "status = 'paid'"}},
            _datasets(), REL_SINGLE,
        )
        assert '"orders"."status" = ' in q.sql

    def test_unknown_qualified_rejected(self):
        """限定名无匹配：显式报错（不静默退回纯列名解析）。"""
        with pytest.raises(CompileError, match="限定名"):
            compile_metric_breakdown(
                {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
                _datasets(), REL_SINGLE, "dim.nonexistent",
            )

    def test_ambiguous_plain_name_rejected_with_hint(self):
        """纯列名多路径命中（dim.tag / other.tag，不在本表）→ 歧义拒绝，提示用限定名。"""
        rels = [RelationInfo("orders", "k1", "dim", "k1"), RelationInfo("orders", "k1", "other", "k1")]
        with pytest.raises(CompileError, match="限定"):
            compile_metric_breakdown(
                {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
                _datasets(), rels, "tag",
            )

    def test_qualified_disambiguates(self):
        """同名列歧义场景下，限定名可精确选择目标维表。"""
        rels = [RelationInfo("orders", "k1", "dim", "k1"), RelationInfo("orders", "k1", "other", "k1")]
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), rels, "dim.tag",
        )
        assert '"dim"."tag" AS dimension' in q.sql


# ---------------------------------------------------------------- P2 复合键


class TestCompositeKey:
    def test_join_on_contains_all_pairs(self):
        """JOIN ON 渲染全部列对（AND 连接）。"""
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_COMPOSITE, "dim.label",
        )
        assert '"orders"."k1" = "dim"."k1" AND "orders"."k2" = "dim"."k2"' in q.sql

    def test_single_pair_relation_unchanged(self):
        """单列键（无 column_pairs）SQL 形状与旧版一致。"""
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_SINGLE, "dim.label",
        )
        assert '"orders"."k1" = "dim"."k1"' in q.sql
        assert " AND \"orders\".\"k2\"" not in q.sql

    def test_composite_join_exec_numbers(self, duck):
        """执行数值：仅 k1 匹配、k2 不匹配的行不得关联（复合键生效）。

        orders(1,10)→A=100+50=150；(1,20)→B=200；(2,10)→C=300；
        dim(9,10)=D 无订单。若错误地只按 k1 关联，(1,20)/(1,10) 会混到 A。
        """
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_COMPOSITE, "dim.label",
        )
        got = _run_breakdown(duck, q.sql)
        assert got == {"A": 150.0, "B": 200.0, "C": 300.0}

    def test_composite_exec_matches_single_when_keys_aligned(self, duck):
        """本夹具中单列键（k1）会把 (1,*) 全并到 A——与复合键结果不同，反向佐证。"""
        q = compile_metric_breakdown(
            {"base_aggregation": "sum", "source": {"table": "orders", "column": "amt"}},
            _datasets(), REL_SINGLE, "dim.label",
        )
        got = _run_breakdown(duck, q.sql)
        # 单列键扇出：k1=1 的订单同时匹配 dim 的 A 与 B 两行，各得 350（数值被放大，
        # 且 (2,*)→C=300 正常）——正是复合键要消除的错误关联
        assert got == {"A": 350.0, "B": 350.0, "C": 300.0}


# ---------------------------------------------------------------- API 层


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _upload(client, name: str, csv: str, project_id: int):
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": name, "project_id": str(project_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.fixture(scope="module")
def rel_env(client):
    sfx = _suffix()
    pid = client.post("/api/projects", json={"name": f"p12_{sfx}"}).json()["data"]["id"]
    fact = _upload(client, f"p12_fact_{sfx}", "k1,k2,amt\n1,10,100\n1,20,200\n2,10,300\n", pid)
    dim = _upload(client, f"p12_dim_{sfx}", "k1,k2,label\n1,10,A\n1,20,B\n2,10,C\n", pid)
    env = {"pid": pid, "fact": fact, "dim": dim}
    yield env
    client.delete(f"/api/datasets/{fact['id']}")
    client.delete(f"/api/datasets/{dim['id']}")
    client.delete(f"/api/projects/{pid}")


def _create_relation(client, env, body: dict):
    return client.post(f"/api/datasets/{env['fact']['id']}/relations", json=body)


class TestRelationApi:
    def test_create_composite_and_list(self, client, rel_env):
        resp = _create_relation(client, rel_env, {
            "from_column": "k1",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k1",
            "column_pairs": [["k1", "k1"], ["k2", "k2"]],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["column_pairs"] == [["k1", "k1"], ["k2", "k2"]]

        rels = client.get(f"/api/datasets/{rel_env['fact']['id']}/relations").json()["data"]
        assert rels[0]["column_pairs"] == [["k1", "k1"], ["k2", "k2"]]

    def test_first_pair_must_match(self, client, rel_env):
        resp = _create_relation(client, rel_env, {
            "from_column": "k1",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k1",
            "column_pairs": [["k2", "k2"], ["k1", "k1"]],
        })
        assert resp.status_code == 400
        assert "首对" in resp.json()["message"]

    def test_duplicate_pair_rejected(self, client, rel_env):
        resp = _create_relation(client, rel_env, {
            "from_column": "k1",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k1",
            "column_pairs": [["k1", "k1"], ["k1", "k1"]],
        })
        assert resp.status_code == 400

    def test_missing_column_rejected(self, client, rel_env):
        resp = _create_relation(client, rel_env, {
            "from_column": "k1",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k1",
            "column_pairs": [["k1", "k1"], ["nope", "k2"]],
        })
        assert resp.status_code == 400
        assert "nope" in resp.json()["message"]

    def test_single_key_returns_default_pairs(self, client, rel_env):
        """无 column_pairs 的单列关系：列表兜底 [[from, target]]；同首对重复登记仍 409。"""
        resp = _create_relation(client, rel_env, {
            "from_column": "k2",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k2",
        })
        assert resp.status_code == 200
        assert resp.json()["data"]["column_pairs"] is None  # 单列键：不写 pairs
        rels = client.get(f"/api/datasets/{rel_env['fact']['id']}/relations").json()["data"]
        k2_rel = next(r for r in rels if r["from_column"] == "k2")
        assert k2_rel["column_pairs"] == [["k2", "k2"]]

        # 同首对（k1→k1）再次登记（即使 pairs 不同）→ 视为重复拒绝，防编译歧义
        dup = _create_relation(client, rel_env, {
            "from_column": "k1",
            "target_dataset_id": rel_env["dim"]["id"],
            "target_column": "k1",
            "column_pairs": [["k1", "k1"], ["k2", "k1"]],
        })
        assert dup.status_code == 409
