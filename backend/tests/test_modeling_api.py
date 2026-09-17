"""B13-1 自动建模建议测试：关系建议（值域重叠+命名相似）与指标候选启发。

验收主线：
- 事实表↔维度表关系被建议（高值域重叠），方向 many_to_one 正确；
- 无关列不产生建议（双低过滤）；
- 已登记关系标注 existing（不重复推荐入库）；
- 指标候选：无日期列的数据集跳过；数值列 sum / 高基数文本 count_distinct；
- 只读铁律：建议接口不写任何业务表。
"""

from __future__ import annotations

import io
import uuid


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


def _mk_env(client):
    """数据集上传到独立项目——与其他测试文件的默认项目数据完全隔离。"""
    sfx = _suffix()
    pid = client.post("/api/projects", json={"name": f"b13_{sfx}"}).json()["data"]["id"]
    # 事实表：orders（product_id 值域与 products.id 完全重叠）
    orders = "order_id,product_id,amount,d\n" + "\n".join(
        f"o{i},p{i % 50},{100 + i},2026-01-{(i % 28) + 1:02d}" for i in range(200)
    ) + "\n"
    # 维度表：products（id 与 orders.product_id 重叠，名称不同但值同）
    products = "id,product_name\n" + "\n".join(
        f"p{i},商品{i}" for i in range(50)
    ) + "\n"
    # 无关表：logs（device 列与上两者值域零重叠、命名无关）
    logs = "log_id,device,ts\n" + "\n".join(
        f"l{i},dev{i},2026-01-0{(i % 9) + 1}" for i in range(30)
    ) + "\n"
    return {
        "sfx": sfx,
        "pid": pid,
        "orders": _upload(client, f"b13_orders_{sfx}", orders, pid),
        "products": _upload(client, f"b13_products_{sfx}", products, pid),
        "logs": _upload(client, f"b13_logs_{sfx}", logs, pid),
    }


def _cleanup(client, env):
    for key in ("orders", "products", "logs"):
        client.delete(f"/api/datasets/{env[key]['id']}")
    client.delete(f"/api/projects/{env['pid']}")


def _suggestions(client, env):
    resp = client.post("/api/modeling/suggestions", json={"project_id": env["pid"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestRelationSuggestions:
    def test_fact_dim_relation_found_with_direction(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            hits = [
                r for r in data["relations"]
                if {r["from_dataset"], r["to_dataset"]} == {
                    env["orders"]["name"], env["products"]["name"],
                }
                and r["from_column"] == "product_id"
            ]
            assert hits, f"应建议 orders.product_id → products 的关系：{data['relations']}"
            top = hits[0]
            assert top["relation_type"] == "many_to_one"
            assert top["to_column"] == "id"
            assert top["evidence"]["overlap_ratio"] >= 0.5
            assert top["score"] >= 0.5
            assert top["existing"] is False
        finally:
            _cleanup(client, env)

    def test_unrelated_pairs_not_suggested(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            bad = [
                r for r in data["relations"]
                if env["logs"]["name"] in {r["from_dataset"], r["to_dataset"]}
            ]
            assert not bad, f"零重叠且命名无关的列不应产生建议：{bad}"
        finally:
            _cleanup(client, env)

    def test_existing_relation_marked(self, client):
        env = _mk_env(client)
        try:
            # 人工先登记 orders.product_id → products.id
            orders_id = env["orders"]["id"]
            products_id = env["products"]["id"]
            resp = client.post(f"/api/datasets/{orders_id}/relations", json={
                "target_dataset_id": products_id,
                "from_column": "product_id",
                "target_column": "id",
                "relation_type": "many_to_one",
            })
            assert resp.status_code == 200, resp.text
            data = _suggestions(client, env)
            hit = next(
                r for r in data["relations"]
                if r["from_dataset"] == env["orders"]["name"]
                and r["from_column"] == "product_id"
            )
            assert hit["existing"] is True
        finally:
            _cleanup(client, env)


class TestMetricSuggestions:
    def test_numeric_and_high_card_candidates(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            m = {
                (c["dataset"], c["column"]): c
                for c in data["metrics"]
                if c["dataset"] == env["orders"]["name"]
            }
            # 数值列 → sum 候选
            assert m[(env["orders"]["name"], "amount")]["aggregation"] == "sum"
            # 高基数文本列（order_id 200 值）→ count_distinct
            assert m[(env["orders"]["name"], "order_id")]["aggregation"] == "count_distinct"
            # 日期列本身不产出候选
            assert (env["orders"]["name"], "d") not in m
            # 无日期列的 products 不产生候选
            assert not any(
                c["dataset"] == env["products"]["name"] for c in data["metrics"]
            )
            assert data["disclaimer"]
        finally:
            _cleanup(client, env)

    def test_suggestions_are_readonly(self, client):
        """只读铁律：连续调用两次结果一致（无副作用入库）。"""
        env = _mk_env(client)
        try:
            a = _suggestions(client, env)
            b = _suggestions(client, env)
            assert a["relations"] == b["relations"]
            # 建议的关系未出现在已登记关系列表
            for key in ("orders", "products", "logs"):
                rels = client.get(
                    f"/api/datasets/{env[key]['id']}/relations"
                ).json()["data"]
                assert rels == [] or rels is None or (
                    isinstance(rels, list) and len(rels) == 0
                )
        finally:
            _cleanup(client, env)
