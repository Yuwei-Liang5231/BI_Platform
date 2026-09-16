"""B9.3 项目工作区 API 测试：默认项目、CRUD、项目内 code 唯一、
跨项目数据集关系拒绝、问数候选项目隔离、模板导入挂项目、列表过滤。"""

from __future__ import annotations

import io
import uuid

import pytest


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _upload(client, name: str, project_id: int | None = None) -> dict:
    csv = "order_id,order_date,amount,status\n1,2026-01-05,10,paid\n2,2026-01-06,20,paid\n"
    data = {"name": name}
    if project_id is not None:
        data["project_id"] = str(project_id)
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data=data,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.fixture(scope="module")
def proj_env(client):
    """模块级：两个项目 + 各自数据集/指标。"""
    sfx = _suffix()
    p1 = client.post("/api/projects", json={"name": f"审计_{sfx}"}).json()["data"]
    p2 = client.post("/api/projects", json={"name": f"零售_{sfx}"}).json()["data"]
    ds1 = _upload(client, f"audit_ds_{sfx}", p1["id"])
    ds2 = _upload(client, f"retail_ds_{sfx}", p2["id"])
    return {"sfx": sfx, "p1": p1, "p2": p2, "ds1": ds1, "ds2": ds2}


class TestProjectCrud:
    def test_default_project_exists(self, client):
        names = [p["name"] for p in client.get("/api/projects").json()["data"]]
        assert "默认项目" in names

    def test_create_and_rename(self, client, proj_env):
        name = f"临时项目_{proj_env['sfx']}_x"
        created = client.post("/api/projects", json={"name": name}).json()["data"]
        resp = client.patch(f"/api/projects/{created['id']}", json={"name": name + "_改"})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == name + "_改"

    def test_duplicate_name_conflict(self, client, proj_env):
        resp = client.post("/api/projects", json={"name": proj_env["p1"]["name"]})
        assert resp.status_code == 409

    def test_default_project_cannot_delete(self, client):
        default_id = next(
            p["id"] for p in client.get("/api/projects").json()["data"] if p["name"] == "默认项目"
        )
        resp = client.delete(f"/api/projects/{default_id}")
        assert resp.status_code == 400

    def test_nonempty_project_cannot_delete(self, client, proj_env):
        resp = client.delete(f"/api/projects/{proj_env['p1']['id']}")
        assert resp.status_code == 400
        assert "数据集" in resp.json()["message"]

    def test_empty_project_delete(self, client, proj_env):
        created = client.post("/api/projects", json={"name": f"空项目_{proj_env['sfx']}"}).json()["data"]
        assert client.delete(f"/api/projects/{created['id']}").status_code == 200

    def test_write_requires_admin(self, client, proj_env):
        from tests.conftest import create_test_user

        analyst = create_test_user(client, f"proj_analyst_{proj_env['sfx']}", role="analyst")
        resp = client.post("/api/projects", json={"name": "x"}, headers=analyst)
        assert resp.status_code == 403
        # viewer 可读（切换器需要）
        viewer = create_test_user(client, f"proj_viewer_{proj_env['sfx']}", role="viewer")
        assert client.get("/api/projects", headers=viewer).status_code == 200


class TestProjectScopedData:
    def test_dataset_belongs_to_project_and_list_filters(self, client, proj_env):
        listing = client.get("/api/datasets", params={"project_id": proj_env["p1"]["id"]}).json()["data"]
        names = [d["name"] for d in listing]
        assert proj_env["ds1"]["name"] in names
        assert proj_env["ds2"]["name"] not in names
        assert all(d["project_id"] == proj_env["p1"]["id"] for d in listing)

    def test_metric_code_unique_within_project_only(self, client, proj_env):
        """核心价值验证：不同项目可建同名 code；同项目重复 409。"""
        rule = {
            "base_aggregation": "sum",
            "source": {"table": proj_env["ds1"]["name"], "column": "amount"},
        }
        resp1 = client.post("/api/metrics", json={
            "code": f"shared_sales_{proj_env['sfx']}", "name": "销售额",
            "calc_rule": rule, "project_id": proj_env["p1"]["id"],
        })
        assert resp1.status_code == 200, resp1.text
        assert resp1.json()["data"]["project_id"] == proj_env["p1"]["id"]

        # 同项目重复 → 409
        resp_dup = client.post("/api/metrics", json={
            "code": f"shared_sales_{proj_env['sfx']}", "name": "销售额2",
            "calc_rule": rule, "project_id": proj_env["p1"]["id"],
        })
        assert resp_dup.status_code == 409

        # 另一项目同名 code → 成功（绑定该项目数据集）
        rule2 = {
            "base_aggregation": "sum",
            "source": {"table": proj_env["ds2"]["name"], "column": "amount"},
        }
        resp2 = client.post("/api/metrics", json={
            "code": f"shared_sales_{proj_env['sfx']}", "name": "销售额",
            "calc_rule": rule2, "project_id": proj_env["p2"]["id"],
        })
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["data"]["project_id"] == proj_env["p2"]["id"]

    def test_metric_list_filters_by_project(self, client, proj_env):
        code = f"shared_sales_{proj_env['sfx']}"
        p1_ids = {m["code"] for m in client.get(
            "/api/metrics", params={"project_id": proj_env["p1"]["id"]}
        ).json()["data"]}
        p2_ids = {m["code"] for m in client.get(
            "/api/metrics", params={"project_id": proj_env["p2"]["id"]}
        ).json()["data"]}
        assert code in p1_ids and code in p2_ids  # 两项目同名指标并存

    def test_relation_across_projects_rejected(self, client, proj_env):
        resp = client.post(f"/api/datasets/{proj_env['ds1']['id']}/relations", json={
            "from_column": "order_id",
            "target_dataset_id": proj_env["ds2"]["id"],
            "target_column": "order_id",
        })
        assert resp.status_code == 400
        assert "同一项目" in resp.json()["message"]

    def test_upload_to_unknown_project_404(self, client):
        resp = client.post(
            "/api/datasets/upload",
            files={"file": ("x.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
            data={"name": f"orphan_{_suffix()}", "project_id": "999999"},
        )
        assert resp.status_code == 404


class TestAskProjectIsolation:
    def test_ask_candidates_scoped_to_project(self, client, proj_env):
        """问数锁定当前项目：另一项目的同名指标不进候选（同 code 不同 id）。"""
        code = f"shared_sales_{proj_env['sfx']}"
        p1_card = client.post("/api/query/ask", json={
            "question": f"上个月{code}是多少", "project_id": proj_env["p1"]["id"],
        }).json()["data"]
        assert p1_card["can_compute"] is True
        assert p1_card["metric"]["project_id"] == proj_env["p1"]["id"]

        p2_card = client.post("/api/query/ask", json={
            "question": f"上个月{code}是多少", "project_id": proj_env["p2"]["id"],
        }).json()["data"]
        assert p2_card["can_compute"] is True
        assert p2_card["metric"]["project_id"] == proj_env["p2"]["id"]
        # 项目隔离：p1 卡片不会带出 p2 的指标
        assert p1_card["metric"]["id"] != p2_card["metric"]["id"]

    def test_conversations_scoped_to_project(self, client, proj_env):
        conv_p1 = client.post("/api/query/ask", json={
            "question": f"2026年1月 shared_sales_{proj_env['sfx']} 是多少",
            "project_id": proj_env["p1"]["id"],
        }).json()["data"]
        listing_p1 = client.get(
            "/api/query/ask/conversations", params={"project_id": proj_env["p1"]["id"]}
        ).json()["data"]
        assert conv_p1["conversation_id"] in [c["id"] for c in listing_p1]
        listing_p2 = client.get(
            "/api/query/ask/conversations", params={"project_id": proj_env["p2"]["id"]}
        ).json()["data"]
        assert conv_p1["conversation_id"] not in [c["id"] for c in listing_p2]


class TestTemplateImportProject:
    def test_template_import_scoped(self, client, proj_env):
        """同 code 模板可导入两个项目（项目内幂等判定）；默认项目导入不干扰。"""
        sfx = proj_env["sfx"]
        p3 = client.post("/api/projects", json={"name": f"模板项目_{sfx}"}).json()["data"]
        resp = client.post("/api/templates/import", json={
            "industries": ["restaurant"],
            "codes": ["rst_revenue"],
            "project_id": p3["id"],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["totals"]["created"] == 1

        # 再导入另一项目：同 code 不受影响（项目内判定 created）
        p4 = client.post("/api/projects", json={"name": f"模板项目B_{sfx}"}).json()["data"]
        resp2 = client.post("/api/templates/import", json={
            "industries": ["restaurant"],
            "codes": ["rst_revenue"],
            "project_id": p4["id"],
        })
        assert resp2.json()["data"]["totals"]["created"] == 1

        # 同项目重导 → skipped（幂等按项目判定）
        resp3 = client.post("/api/templates/import", json={
            "industries": ["restaurant"],
            "codes": ["rst_revenue"],
            "project_id": p3["id"],
        })
        assert resp3.json()["data"]["totals"]["skipped"] == 1

        # pack_detail 按项目判定导入状态
        detail = client.get(
            "/api/templates/restaurant", params={"project_id": p4["id"]}
        ).json()["data"]
        target = next(m for m in detail["metrics"] if m["code"] == "rst_revenue")
        assert target["imported"] is True
