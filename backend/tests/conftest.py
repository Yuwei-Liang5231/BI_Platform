"""pytest 全局夹具。

关键：必须在导入 app 之前设置环境变量，保证 Settings 用测试库。
"""

from __future__ import annotations

import os
import tempfile

# 1) 环境先行：APP_ENV=test，DATA_DIR 指向会话级临时目录
os.environ["APP_ENV"] = "test"
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="bi-test-data-")
os.environ["DATA_DIR"] = _TEST_DATA_DIR

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.infra.database import create_all, init_engine, reset_engine  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """应用测试客户端：独立临时元数据库，已注入引导 admin 登录态。

    B4 起所有接口需登录；存量测试统一以 admin 身份运行（引导账号由
    lifespan 自动创建，密码为 settings.bootstrap_admin_password 默认值）。
    """
    reset_engine()
    init_engine(force=False)
    create_all()
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        resp = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin123"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["data"]["token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def login_as(client: TestClient, username: str, password: str) -> dict:
    """测试辅助：以指定账号换取 Authorization 头（测试内部切换身份用）。"""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['token']}"}


def create_test_user(client: TestClient, username: str, *, role: str, department: str = "") -> dict:
    """测试辅助：admin 创建用户并返回其 Authorization 头。"""
    resp = client.post(
        "/api/auth/users",
        json={"username": username, "password": "test-pw-123", "role": role, "department": department},
    )
    assert resp.status_code == 200, resp.text
    return login_as(client, username, "test-pw-123")
