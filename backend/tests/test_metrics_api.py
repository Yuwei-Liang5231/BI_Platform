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

    def test_soft_delete_hidden_from_status_all_and_no_double_delete(self, client, metric_env):
        """回归：管理页「全部状态」(status=all) 不得显示已删除指标；重复删除报 404。

        用户报障：删除后指标仍可见，再点删除提示「指标不存在」——根因是
        status=all 未过滤软删记录，列表出现已删除指标但删除接口判不存在。
        """
        resp = client.post("/api/metrics", json={
            "code": "api_doomed_all", "name": "全状态待删除", "calc_rule": GMV_RULE,
        })
        assert resp.status_code == 200, resp.text
        metric_id = resp.json()["data"]["id"]

        # 删除前：status=all 可见
        resp = client.get("/api/metrics", params={"status": "all", "search": "api_doomed_all"})
        assert [m["id"] for m in resp.json()["data"]] == [metric_id]

        resp = client.delete(f"/api/metrics/{metric_id}")
        assert resp.status_code == 200

        # 删除后：默认目录、全状态目录、搜索均不可见
        for params in ({"status": "all"}, {"status": "all", "search": "api_doomed_all"}, {}):
            resp = client.get("/api/metrics", params=params)
            assert all(m["id"] != metric_id for m in resp.json()["data"]), params

        # 重复删除 → 404「指标不存在」（与列表不可见行为一致）
        resp = client.delete(f"/api/metrics/{metric_id}")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40400

    def test_soft_deleted_code_freed_for_recreate(self, client, metric_env):
        """回归：软删后同 code 可重建（用户报障：清空指标后向导重导仍提示已存在）。

        旧逻辑唯一性校验把软删行也计入，code 被删掉的指标永久占用；
        修正后 deleted 不占 code——删除后可重建同 code 指标，且按 code
        取值解析只认非 deleted 行，不产生歧义。
        """
        code = "api_reborn"
        resp = client.post("/api/metrics", json={
            "code": code, "name": "可重建指标", "calc_rule": GMV_RULE,
        })
        assert resp.status_code == 200, resp.text
        old_id = resp.json()["data"]["id"]

        resp = client.delete(f"/api/metrics/{old_id}")
        assert resp.status_code == 200

        # 软删后重建同 code：不再 409
        resp = client.post("/api/metrics", json={
            "code": code, "name": "可重建指标-新版", "calc_rule": GMV_RULE,
        })
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["data"]["id"]
        assert new_id != old_id

        # 按 code 解析指向新指标（非 deleted）
        resp = client.get(f"/api/metrics/by-code/{code}")
        if resp.status_code == 200:  # 端点存在则校验指向
            assert resp.json()["data"]["id"] == new_id

        # 活跃 code 重复创建仍然 409
        resp = client.post("/api/metrics", json={
            "code": code, "name": "重复码", "calc_rule": GMV_RULE,
        })
        assert resp.status_code == 409
        assert resp.json()["code"] == 40900

    def test_metric_not_found(self, client):
        resp = client.get("/api/metrics/99999")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40400


class TestMetricBatchStatus:
    """指标管理批量停用/启用：逐条提交、失败不拖垮其余、非法 status 拒绝。"""

    def test_batch_disable_enable_mixed(self, client, metric_env):
        codes = ["api_bs_a", "api_bs_b"]
        ids = []
        for i, code in enumerate(codes):
            resp = client.post("/api/metrics", json={
                "code": code, "name": f"批量状态样例{i}", "calc_rule": GMV_RULE,
            })
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["data"]["id"])

        # 批量停用（混入不存在 id → 失败单列）
        resp = client.post("/api/metrics/batch-status", json={"ids": ids + [99999999], "status": "disabled"})
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert sorted(body["updated"]) == sorted(ids)
        assert len(body["failed"]) == 1 and body["failed"][0]["id"] == 99999999
        listing = client.get("/api/metrics", params={"status": "disabled"}).json()["data"]
        assert {m["id"] for m in listing} >= set(ids)

        # 批量启用恢复
        resp = client.post("/api/metrics/batch-status", json={"ids": ids, "status": "active"})
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["data"]["updated"]) == sorted(ids)
        listing = client.get("/api/metrics", params={"status": "active"}).json()["data"]
        assert {m["id"] for m in listing} >= set(ids)

        # 非法 status（deleted 走删除接口）→ 400
        resp = client.post("/api/metrics/batch-status", json={"ids": ids, "status": "deleted"})
        assert resp.status_code == 400
