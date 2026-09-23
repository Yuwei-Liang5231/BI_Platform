"""C2 操作审计日志测试：写操作白名单自动落库 + admin 查询接口。

验收红线：
- 白名单写操作（2xx）自动落一条 audit_logs，username/resource_id/detail 正确；
- 登录成败都记录（安全审计）；非白名单（读接口）与 4xx 校验失败不记；
- /audit-logs 仅 admin 可访问；过滤（user_id / action 前缀 / 日期）生效。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import create_test_user, login_as


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _fetch(client: TestClient, **params) -> dict:
    resp = client.get("/api/audit-logs", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestAudit:
    def test_write_operation_recorded(self, client: TestClient):
        """白名单写操作（project.create）自动落库：username/resource_id/detail 正确。"""
        sfx = _suffix()
        resp = client.post("/api/projects", json={"name": f"审计项目_{sfx}"})
        assert resp.status_code == 200, resp.text
        pid = resp.json()["data"]["id"]

        data = _fetch(client, action="project.create", page_size=50)
        row = next(r for r in data["items"] if r["resource_id"] == str(pid))
        assert row["action"] == "project.create"
        assert row["resource_type"] == "project"
        assert row["method"] == "POST" and row["status_code"] == 200
        assert row["username"] == "admin"
        assert row["detail"]["body"]["name"] == f"审计项目_{sfx}"

    def test_rename_recorded_with_resource_id(self, client: TestClient):
        """PATCH 路径参数进 resource_id；动作为 dataset.rename / project.update 等。"""
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"改名前_{sfx}"}).json()["data"]["id"]
        resp = client.patch(f"/api/projects/{pid}", json={"name": f"改名后_{sfx}"})
        assert resp.status_code == 200, resp.text

        data = _fetch(client, action="project.update", page_size=10)
        row = next(r for r in data["items"] if r["resource_id"] == str(pid))
        assert row["detail"]["body"]["name"] == f"改名后_{sfx}"

    def test_login_success_and_failure_recorded(self, client: TestClient):
        """登录成功与失败都落 user.login（安全审计）；失败 status=401。"""
        sfx = _suffix()
        create_test_user(client, f"aud_u_{sfx}", role="viewer")

        # 失败登录
        bad = client.post("/api/auth/login", json={"username": f"aud_u_{sfx}", "password": "wrong"})
        assert bad.status_code == 401, bad.text
        # 成功登录
        ok_headers = login_as(client, f"aud_u_{sfx}", "test-pw-123")
        assert ok_headers

        data = _fetch(client, action="user.login", page_size=50)
        rows = [r for r in data["items"] if (r["detail"] or {}).get("username") == f"aud_u_{sfx}"]
        statuses = {r["status_code"] for r in rows}
        assert 401 in statuses and 200 in statuses

    def test_non_whitelisted_and_4xx_not_recorded(self, client: TestClient):
        """读接口与 4xx 校验失败不产生审计记录。"""
        sfx = _suffix()
        # 读接口
        r = client.get("/api/projects")
        assert r.status_code == 200
        # 4xx 写失败（重名项目）
        name = f"重复_{sfx}"
        assert client.post("/api/projects", json={"name": name}).status_code == 200
        assert client.post("/api/projects", json={"name": name}).status_code == 409

        data = _fetch(client, action="project.", page_size=100)
        dup_fails = [
            r for r in data["items"]
            if r["status_code"] != 200 or (r["detail"] or {}).get("body", {}).get("name") == name and r["action"] != "project.create"
        ]
        assert all(r["status_code"] == 200 for r in data["items"])  # 无失败记录
        assert not any(
            r["method"] == "GET" for r in data["items"]
        )

    def test_filters(self, client: TestClient):
        """user_id / action 前缀 / 日期过滤生效。"""
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"过滤_{sfx}"}).json()["data"]["id"]

        # action 前缀
        data = _fetch(client, action="project.", page_size=100)
        assert all(r["action"].startswith("project.") for r in data["items"])
        # user_id
        meta = client.get("/api/audit-logs/meta").json()["data"]
        admin_id = next(u["id"] for u in meta["users"] if u["username"] == "admin")
        data = _fetch(client, user_id=admin_id, page_size=5)
        assert all(r["user_id"] == admin_id for r in data["items"]) and data["items"]
        # 日期（今天起查必有；查 2020 年必无）
        import datetime as dt

        today = dt.date.today().isoformat()
        assert _fetch(client, start=today, page_size=5)["items"]
        assert not _fetch(client, start="2020-01-01", end="2020-01-02", page_size=5)["items"]
        assert pid

    def test_meta(self, client: TestClient):
        """/audit-logs/meta：distinct 用户与动作。"""
        resp = client.get("/api/audit-logs/meta")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert any(u["username"] == "admin" for u in data["users"])
        assert "project.create" in data["actions"]

    def test_viewer_403(self, client: TestClient):
        """/audit-logs 仅 admin：viewer 403。"""
        sfx = _suffix()
        vw = create_test_user(client, f"aud_vw_{sfx}", role="viewer")
        assert client.get("/api/audit-logs", headers=vw).status_code == 403
        assert client.get("/api/audit-logs/meta", headers=vw).status_code == 403
