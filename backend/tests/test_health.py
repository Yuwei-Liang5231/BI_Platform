"""health 接口测试（B0 验收标准）。"""

from __future__ import annotations


def test_health_returns_200_and_code_0(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    data = body["data"]
    assert data["status"] == "ok"
    assert data["env"] == "test"
    assert data["database"] == "up"
    assert "time" in data


def test_health_contract_fields(client):
    """契约字段固定，前端与监控依赖这些键名。"""
    data = client.get("/api/health").json()["data"]
    assert set(data.keys()) == {"status", "app", "version", "env", "database", "time"}


def test_root_redirects_to_docs(client):
    """根路径不再 404，直接跳 Swagger。"""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"].endswith("/docs")


def test_unknown_route_returns_404_envelope(client):
    """404 也走统一响应体（码值对齐）。"""
    resp = client.get("/api/not-exists")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 40400
