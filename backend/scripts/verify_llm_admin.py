"""LLM 模型管理端到端验证（隔离环境：8105 后端 + 8199 本地 Mock LLM）。

前置：backend 已在 8105 启动（APP_ENV=dev, DATA_DIR=../data-test-r3），
scripts/mock_llm_server.py 已在 8199 启动。

验证点：
1. 登录后创建 Mock 模型并启用 → 列表 effective.source=db、model=mock-v1
2. 连通性测试（model_id 指向 Mock）→ ok=true
3. /api/query/ask 理解卡：source=llm、时间取自 mock-v1 返回的意图（LLM 真实参与解析）
4. 添加第二个模型并切换启用 → 再次 ask：时间变为 mock-v2 的区间（切换实时生效）
5. api_key 脱敏：列表中不出现明文
6. 删除启用中的模型 → effective 回退 env 兜底（dev .env 已配置 DeepSeek → source=db 回退为 env）

运行：python scripts/verify_llm_admin.py（输出 PASS/FAIL 汇总，退出码 0/1）
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8106/api"
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, f"{'PASS' if ok else 'FAIL'} | {name} | {detail}"))


def main() -> int:
    c = httpx.Client(timeout=30)

    # 0) 登录
    resp = c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
    check("登录 admin", resp.status_code == 200, resp.text[:120])
    c.headers["Authorization"] = f"Bearer {resp.json()['data']['token']}"

    # 1) 创建 Mock 模型并启用
    resp = c.post(
        f"{BASE}/llm/models",
        json={
            "name": "本地Mock",
            "base_url": "http://127.0.0.1:8199",
            "api_key": "sk-mock-secret-123456",
            "model": "mock-v1",
            "activate": True,
        },
    )
    check("创建并启用 mock-v1", resp.status_code == 200, resp.text[:200])
    m1 = resp.json()["data"]

    # 2) 列表：effective 指向 db，key 脱敏
    data = c.get(f"{BASE}/llm/models").json()["data"]
    eff = data["effective"]
    check(
        "effective.source=db 且 model=mock-v1",
        eff and eff["source"] == "db" and eff["model"] == "mock-v1",
        str(eff),
    )
    check(
        "api_key 脱敏（无明文）",
        all("sk-mock-secret-123456" not in (m.get("api_key_masked") or "") for m in data["models"]),
        ";".join(m.get("api_key_masked") or "" for m in data["models"]),
    )

    # 3) 连通性测试（走 Mock）
    resp = c.post(f"{BASE}/llm/models/test", json={"model_id": m1["id"]})
    t = resp.json()["data"]
    check("连通性测试 ok=true", t["ok"] is True, t["message"][:120])

    # 4) ask：走 LLM（mock-v1 意图 → 2026-08-01~2026-08-31）
    card = c.post(f"{BASE}/query/ask", json={"question": "随便聊聊近期情况"}).json()["data"]
    check(
        "ask source=llm 且时间来自 mock-v1",
        card["source"] == "llm" and card["start"] == "2026-08-01" and card["end"] == "2026-08-31",
        f"source={card['source']} {card['start']}~{card['end']} llm_configured={card['llm_configured']}",
    )

    # 5) 添加 mock-v2 并切换 → ask 实时变化
    resp = c.post(
        f"{BASE}/llm/models",
        json={
            "name": "本地Mock-2",
            "base_url": "http://127.0.0.1:8199",
            "api_key": "sk-mock-secret-654321",
            "model": "mock-v2",
        },
    )
    m2 = resp.json()["data"]
    c.post(f"{BASE}/llm/models/{m2['id']}/activate")
    card = c.post(f"{BASE}/query/ask", json={"question": "再随便聊聊"}).json()["data"]
    check(
        "切换启用后 ask 时间变为 mock-v2 区间",
        card["source"] == "llm" and card["start"] == "2026-07-01" and card["end"] == "2026-07-31",
        f"source={card['source']} {card['start']}~{card['end']}",
    )
    check(
        "切换后 compare=mom（来自 mock-v2 意图）",
        card["compare"] == "mom",
        f"compare={card['compare']}",
    )

    # 6) 删除启用中的 mock-v2 → 回退 env 兜底（dev .env 已配置 DeepSeek）
    c.delete(f"{BASE}/llm/models/{m2['id']}")
    data = c.get(f"{BASE}/llm/models").json()["data"]
    eff = data["effective"]
    check(
        "删除启用模型后回退 env 兜底（source=env, DeepSeek）",
        eff and eff["source"] == "env" and "deepseek" in eff["base_url"],
        str(eff),
    )

    c.close()
    print("\n".join(msg for _, msg in results))
    failed = [msg for ok, msg in results if not ok]
    print(f"\n==== {len(results) - len(failed)}/{len(results)} passed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
