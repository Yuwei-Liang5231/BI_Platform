"""指标 API 端到端测试：上传 → 建关系 → 定义指标 → 编译存档 → 改口径 → 留痕 → 软删除。

走真实 HTTP 流程（TestClient），验证统一响应契约与「口径变更原子生效」（不变式 3）。
"""

from __future__ import annotations

import io

import pytest

ORDERS_CSV = """order_id,order_date,user_id,pay_amount,order_status
1,2026-01-05,100,120.5,paid
2,2026-01-12,101,80.0,paid
3,2026-02-03,100,200.0,refunded
4,2026-02-20,102,99.9,paid
"""

USERS_CSV = """user_id,region
100,华东
101,华北
102,华东
"""


def _upload(client, filename: str, content: str, name: str) -> dict:
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/csv")},
        data={"name": name},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


@pytest.fixture(scope="module")
def metric_env(client):
    """模块级：上传两份数据集并登记关系，返回数据集 id。"""
    orders = _upload(client, "api_orders.csv", ORDERS_CSV, "api_orders")
    users = _upload(client, "api_users.csv", USERS_CSV, "api_users")
    resp = client.post(
        f"/api/datasets/{orders['id']}/relations",
        json={"from_column": "user_id", "target_dataset_id": users["id"],
               "target_column": "user_id"},
    )
    assert resp.status_code == 200, resp.text
    return {"orders_id": orders["id"], "users_id": users["id"]}


GMV_RULE = {
    "base_aggregation": "sum",
    "source": {"table": "api_orders", "column": "pay_amount",
                "filter": "order_status = 'paid'"},
}


class TestMetricsApi:
    def test_create_metric(self, client, metric_env):
        resp = client.post("/api/metrics", json={
            "code": "api_gmv",
            "name": "销售额",
            "aliases": ["GMV", "成交额"],
            "definition": "已支付订单金额合计",
            "calc_rule": GMV_RULE,
            "topic": "trade",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["code"] == "api_gmv"
        assert data["ver"] == 1
        assert data["status"] == "active"
        assert data["aliases"] == ["GMV", "成交额"]
        assert data["primary_dataset_id"] == metric_env["orders_id"]

    def test_create_duplicate_code_conflict(self, client, metric_env):
        resp = client.post("/api/metrics", json={
            "code": "api_gmv", "name": "重复码", "calc_rule": GMV_RULE,
        })
        assert resp.status_code == 409
        assert resp.json()["code"] == 40900

    def test_create_with_out_of_scope_operator_rejected(self, client, metric_env):
        """超纲算子保存即拒绝（400），并说明原因。"""
        resp = client.post("/api/metrics", json={
            "code": "api_bad", "name": "坏指标",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": "api_orders", "column": "pay_amount"},
                "group_by": "user_id",
            },
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40000
        assert "group_by" in body["message"]

    def test_create_missing_relation_rejected(self, client, metric_env):
        """缺表关系保存即拒绝，报错指明缺哪条关系。"""
        resp = client.post("/api/metrics", json={
            "code": "api_norel",
            "name": "按产品类目销售额",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": "api_orders", "column": "pay_amount",
                            "filter": "category = '数码'"},
            },
        })
        assert resp.status_code == 400
        assert "表关系" in resp.json()["message"]

    def test_alias_search(self, client, metric_env):
        resp = client.get("/api/metrics", params={"search": "GMV"})
        assert resp.status_code == 200
        names = [m["code"] for m in resp.json()["data"]]
        assert "api_gmv" in names
        resp = client.get("/api/metrics", params={"search": "成交"})
        assert "api_gmv" in [m["code"] for m in resp.json()["data"]]

    def test_current_sql_archive(self, client, metric_env):
        resp = client.get("/api/metrics", params={"search": "api_gmv"})
        metric_id = resp.json()["data"][0]["id"]
        resp = client.get(f"/api/metrics/{metric_id}/sql")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ver"] == 1
        assert "SUM" in data["sql_text"]
        assert "$__start__" in data["sql_text"]

    def test_calc_rule_change_requires_reason(self, client, metric_env):
        resp = client.get("/api/metrics", params={"search": "api_gmv"})
        metric_id = resp.json()["data"][0]["id"]
        resp = client.patch(f"/api/metrics/{metric_id}", json={
            "calc_rule": {"base_aggregation": "sum",
                           "source": {"table": "api_orders", "column": "pay_amount"}},
        })
        assert resp.status_code == 400
        assert resp.json()["code"] == 40000
        assert "reason" in resp.json()["message"]

    def test_calc_rule_change_bumps_version_atomically(self, client, metric_env):
        """改口径 → 重编译 → 存档更新 → ver+1 → 留痕，一次请求内原子完成。"""
        resp = client.get("/api/metrics", params={"search": "api_gmv"})
        metric_id = resp.json()["data"][0]["id"]

        new_rule = {"base_aggregation": "sum",
                     "source": {"table": "api_orders", "column": "pay_amount"}}
        resp = client.patch(f"/api/metrics/{metric_id}", json={
            "calc_rule": new_rule, "reason": "口径评审：销售额含退款单",
            "operator_id": "reviewer01",
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["ver"] == 2
        assert data["calc_rule"] == new_rule

        # 存档：v2 SQL 已无过滤条件
        resp = client.get(f"/api/metrics/{metric_id}/sql")
        assert resp.json()["data"]["ver"] == 2
        assert "paid" not in resp.json()["data"]["sql_text"]

        # 留痕：before/after 快照 + 原因 + 操作人
        resp = client.get(f"/api/metrics/{metric_id}/changes")
        changes = resp.json()["data"]
        assert len(changes) >= 1
        latest = changes[0]
        assert latest["reason"] == "口径评审：销售额含退款单"
        assert latest["operator_id"] == "reviewer01"
        assert latest["before"]["ver"] == 1
        assert latest["after"]["ver"] == 2

    def test_cross_table_metric_via_api(self, client, metric_env):
        resp = client.post("/api/metrics", json={
            "code": "api_region_gmv",
            "name": "华北销售额",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": "api_orders", "column": "pay_amount",
                            "filter": "order_status = 'paid' AND region = '华北'"},
            },
        })
        assert resp.status_code == 200, resp.text

    def test_compile_dry_run(self, client, metric_env):
        resp = client.post("/api/metrics/compile", json={"calc_rule": GMV_RULE})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "SUM" in data["sql"]
        assert data["time_fields"] == {"api_orders": "order_date"}
        assert data["coverage_start"] is not None

    def test_soft_delete_and_hidden_from_list(self, client, metric_env):
        resp = client.post("/api/metrics", json={
            "code": "api_doomed", "name": "待删除指标", "calc_rule": GMV_RULE,
        })
        metric_id = resp.json()["data"]["id"]
        resp = client.delete(f"/api/metrics/{metric_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deleted"

        resp = client.get("/api/metrics", params={"search": "api_doomed"})
        assert resp.json()["data"] == []
        # 已删除指标详情 404
        resp = client.get(f"/api/metrics/{metric_id}")
        assert resp.status_code == 404

    def test_metric_not_found(self, client):
        resp = client.get("/api/metrics/99999")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40400
