"""B4 认证与权限 API 测试：登录、401/403 码值对齐、用户管理、角色边界。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import create_test_user, login_as


def _bare_client(fastapi_app) -> TestClient:
    """不带默认登录态的客户端（401 场景用）。"""
    return TestClient(fastapi_app)


@pytest.fixture(scope="module")
def app_instance(client):
    from app.main import app as fastapi_app

    return fastapi_app


class TestLogin:
    def test_login_ok_returns_token_and_admin(self, app_instance):
        with _bare_client(app_instance) as c:
            resp = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["token_type"] == "bearer"
        assert data["user"]["role"] == "admin"
        assert data["user"]["username"] == "admin"

    def test_login_wrong_password_401(self, app_instance):
        with _bare_client(app_instance) as c:
            resp = c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert resp.status_code == 401
        assert resp.json()["code"] == 40100

    def test_login_unknown_user_same_message(self, app_instance):
        """不存在的用户与错误密码返回同一文案（防用户名枚举）。"""
        with _bare_client(app_instance) as c:
            miss = c.post("/api/auth/login", json={"username": "ghost", "password": "x"}).json()
            bad = c.post("/api/auth/login", json={"username": "admin", "password": "x"}).json()
        assert miss["message"] == bad["message"]

    def test_missing_token_401(self, app_instance):
        with _bare_client(app_instance) as c:
            resp = c.get("/api/datasets")
        assert resp.status_code == 401
        assert resp.json()["code"] == 40100

    def test_garbage_token_401(self, app_instance):
        with _bare_client(app_instance) as c:
            resp = c.get("/api/datasets", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401
        assert resp.json()["code"] == 40100

    def test_me_with_default_admin(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == "admin"

    def test_health_remains_public(self, app_instance):
        with _bare_client(app_instance) as c:
            resp = c.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


class TestUserManagement:
    def test_create_and_login_viewer(self, client):
        headers = create_test_user(client, "auth_viewer", role="viewer", department="测试部")
        resp = client.get("/api/auth/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "viewer"

    def test_duplicate_username_409(self, client):
        create_test_user(client, "auth_dupe", role="viewer")
        resp = client.post(
            "/api/auth/users",
            json={"username": "auth_dupe", "password": "test-pw-123"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == 40900

    def test_create_invalid_role_400(self, client):
        resp = client.post(
            "/api/auth/users",
            json={"username": "auth_bad_role", "password": "test-pw-123", "role": "boss"},
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_list_users(self, client):
        viewer_headers = create_test_user(client, "auth_viewer2", role="viewer")
        resp = client.get("/api/auth/users", headers=viewer_headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == 40300

    def test_patch_role_and_status(self, client):
        create_test_user(client, "auth_patch", role="viewer")
        users = client.get("/api/auth/users").json()["data"]
        uid = next(u["id"] for u in users if u["username"] == "auth_patch")
        resp = client.patch(f"/api/auth/users/{uid}", json={"role": "analyst"})
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "analyst"
        old_headers = login_as(client, "auth_patch", "test-pw-123")  # 停用前先取 token
        resp = client.patch(f"/api/auth/users/{uid}", json={"status": "disabled"})
        assert resp.status_code == 200
        # 停用后：登录 403；停用前签发的旧 token 也立即失效 403
        with _bare_client(client.app) as c:
            login = c.post(
                "/api/auth/login", json={"username": "auth_patch", "password": "test-pw-123"}
            )
        assert login.status_code == 403
        assert login.json()["code"] == 40300
        resp = client.get("/api/datasets", headers=old_headers)
        assert resp.status_code == 403
        # 恢复
        client.patch(f"/api/auth/users/{uid}", json={"status": "active"})

    def test_admin_cannot_disable_self(self, client):
        admin = client.get("/api/auth/me").json()["data"]
        resp = client.patch(f"/api/auth/users/{admin['id']}", json={"status": "disabled"})
        assert resp.status_code == 400

    def test_role_boundaries_on_write_ops(self, client, app_instance):
        """viewer 只读：不能建指标、不能传数据集；analyst 可建指标、不能传数据集。"""
        viewer = create_test_user(client, "auth_ro", role="viewer")
        analyst = create_test_user(client, "auth_analyst", role="analyst", department="数据部")
        metric_body = {
            "code": "auth_boundary_metric",
            "name": "角色边界指标",
            "calc_rule": {
                "base_aggregation": "sum",
                "source": {"table": "auth_boundary_ds", "column": "amount"},
            },
        }
        csv = b"order_date,amount\n2026-01-01,100\n"
        upload = client.post(
            "/api/datasets/upload",
            files={"file": ("boundary.csv", io.BytesIO(csv), "text/csv")},
            data={"name": "auth_boundary_ds"},
        )
        assert upload.status_code == 200

        # viewer：读可以，写 403
        assert client.get("/api/datasets", headers=viewer).status_code == 200
        resp = client.post("/api/metrics", json=metric_body, headers=viewer)
        assert resp.status_code == 403
        assert resp.json()["code"] == 40300
        resp = client.post(
            "/api/datasets/upload",
            files={"file": ("x.csv", io.BytesIO(csv), "text/csv")},
            data={"name": "viewer_upload_blocked"},
            headers=viewer,
        )
        assert resp.status_code == 403

        # analyst：建指标可以，传数据集 403（D12 管理员专用）
        resp = client.post("/api/metrics", json=metric_body, headers=analyst)
        assert resp.status_code == 200
        resp = client.post(
            "/api/datasets/upload",
            files={"file": ("x.csv", io.BytesIO(csv), "text/csv")},
            data={"name": "analyst_upload_blocked"},
            headers=analyst,
        )
        assert resp.status_code == 403

        # 清理边界指标，避免污染可见性用例的目录断言
        metrics = client.get("/api/metrics", params={"search": "auth_boundary_metric"}).json()["data"]
        client.delete(f"/api/metrics/{metrics[0]['id']}")
