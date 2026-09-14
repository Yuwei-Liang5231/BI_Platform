"""LLM 模型管理（阶段 2）API 集成测试。

覆盖：
- CRUD：创建/重名校验/base_url 校验/编辑（api_key 留空保持原值）/删除
- 启用切换：全局唯一启用，切换后互斥
- 配置优先级：启用 DB 记录 > env 兜底；删除启用记录后回退
- 权限：全部接口 admin 专用
- 连通性测试：不可达地址明确返回失败原因
- 问数联动：启用 DB 模型后 llm_configured=true、失败仍降级 fallback
"""

from __future__ import annotations

import pytest

from tests.conftest import create_test_user

VALID_MODEL = {
    "name": "DeepSeek 官方",
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-abcdefghijklmnop1234",
    "model": "deepseek-v4-flash",
}


@pytest.fixture(autouse=True)
def clean_llm_models(client):
    """会话级共享库：每个用例前清空 llm_models，保证命名唯一等断言确定性。"""
    from sqlalchemy import delete

    from app.infra.database import get_engine
    from app.infra.models import LlmModel

    with get_engine().begin() as conn:
        conn.execute(delete(LlmModel))
    yield


def _list_models(client):
    return client.get("/api/llm/models").json()["data"]


# ---------------------------------------------------------------- 权限


def test_admin_only(client):
    viewer = create_test_user(client, "llm_viewer", role="viewer")
    assert client.get("/api/llm/models", headers=viewer).status_code == 403
    assert client.post("/api/llm/models", json=VALID_MODEL, headers=viewer).status_code == 403
    assert client.post("/api/llm/models/1/activate", headers=viewer).status_code == 403
    assert client.delete("/api/llm/models/1", headers=viewer).status_code == 403


# ---------------------------------------------------------------- 初始状态与创建


def test_initial_empty(client):
    """test 环境无 env 兜底、无 DB 记录 → effective 为空。"""
    data = _list_models(client)
    assert data["models"] == []
    assert data["effective"] is None


def test_create_and_masked_key(client):
    resp = client.post("/api/llm/models", json=VALID_MODEL)
    assert resp.status_code == 200, resp.text
    m = resp.json()["data"]
    assert m["is_active"] is False
    assert m["api_key_masked"].startswith("sk-a")
    assert m["api_key_masked"].endswith("1234")
    assert "*" in m["api_key_masked"]
    assert "sk-abcdefghijklmnop1234" not in m["api_key_masked"]


def test_create_duplicate_name(client):
    client.post("/api/llm/models", json=VALID_MODEL)
    resp = client.post("/api/llm/models", json={**VALID_MODEL, "model": "other"})
    assert resp.status_code == 400
    assert "已存在" in resp.json()["message"]


def test_create_bad_base_url(client):
    resp = client.post(
        "/api/llm/models", json={**VALID_MODEL, "name": "bad", "base_url": "api.deepseek.com"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------- 启用切换与优先级


def test_activate_exclusive_and_priority(client):
    """启用互斥 + effective 跟随切换；停用全部后回退 env（test 无 env → None）。"""
    m1 = client.post("/api/llm/models", json={**VALID_MODEL, "activate": True}).json()["data"]
    m2 = client.post(
        "/api/llm/models",
        json={**VALID_MODEL, "name": "备选模型", "model": "deepseek-chat"},
    ).json()["data"]

    data = _list_models(client)
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id[m1["id"]]["is_active"] is True
    assert by_id[m2["id"]]["is_active"] is False
    assert data["effective"]["source"] == "db"
    assert data["effective"]["model"] == "deepseek-v4-flash"

    # 切换启用 → 互斥
    client.post(f"/api/llm/models/{m2['id']}/activate")
    data = _list_models(client)
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id[m1["id"]]["is_active"] is False
    assert by_id[m2["id"]]["is_active"] is True
    assert data["effective"]["model"] == "deepseek-chat"

    # 删除启用中的模型 → 回退
    client.delete(f"/api/llm/models/{m2['id']}")
    data = _list_models(client)
    assert data["effective"] is None  # test 环境无 env 兜底


# ---------------------------------------------------------------- 编辑


def test_update_keeps_api_key_when_blank(client):
    m = client.post("/api/llm/models", json=VALID_MODEL).json()["data"]
    resp = client.put(
        f"/api/llm/models/{m['id']}",
        json={"name": "改名后", "model": "deepseek-chat", "api_key": ""},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["data"]
    assert updated["name"] == "改名后"
    assert updated["model"] == "deepseek-chat"
    # api_key 留空 = 保持原值：脱敏前后缀不变
    assert updated["api_key_masked"] == m["api_key_masked"]


# ---------------------------------------------------------------- 连通性测试


def test_test_endpoint_unreachable(client):
    """不可达地址 → ok=false 且带原因（离线确定性，不依赖外部服务）。"""
    data = client.post(
        "/api/llm/models/test",
        json={"base_url": "http://127.0.0.1:9", "api_key": "sk-x", "model": "m"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["message"]


def test_test_endpoint_incomplete(client):
    data = client.post("/api/llm/models/test", json={"base_url": "", "api_key": "", "model": ""}).json()["data"]
    assert data["ok"] is False
    assert "不完整" in data["message"]


# ---------------------------------------------------------------- 问数联动


def test_ask_llm_configured_flag_follows_db(client):
    """启用 DB 模型（指向不可达地址）→ llm_configured=true、真实调用失败降级 fallback；
    删除后回退 llm_configured=false。"""
    m = client.post(
        "/api/llm/models",
        json={
            "name": "不可达",
            "base_url": "http://127.0.0.1:9",
            "api_key": "sk-x",
            "model": "m",
            "activate": True,
        },
    ).json()["data"]
    card = client.post("/api/query/ask", json={"question": "随便问点啥"}).json()["data"]
    assert card["llm_configured"] is True
    assert card["source"] == "fallback"  # 不可达 → 降级，不致命

    client.delete(f"/api/llm/models/{m['id']}")
    card = client.post("/api/query/ask", json={"question": "随便问点啥"}).json()["data"]
    assert card["llm_configured"] is False
    assert card["source"] == "fallback"
