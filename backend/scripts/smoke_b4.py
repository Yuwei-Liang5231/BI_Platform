"""B4 auth 与权限双出口 — 真实服务冒烟脚本（对运行中的 8100 服务逐条验证）。

用法（PowerShell，项目根 bi-platform 下）:
    ..\\..\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe backend\\scripts\\smoke_b4.py
可选端口: python smoke_b4.py 8100

覆盖验收要点:
1  admin 登录拿 token
2  无 token 请求 -> 401 (code 40100)
3  admin 建 viewer 账号
4  viewer 登录
5  登记 role=viewer 不可见某指标
6  双出口验证: 目录消失 / 详情403 / SQL403 / 取数403 / 导出403
7  admin 全量不受影响
8  items=[] 清空恢复, viewer 重新可见
"""

import sys

import httpx

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8100'}/api"
PASS, FAIL = [], []


def step(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))


def api(client: httpx.Client, method: str, path: str, token: str | None = None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, f"{BASE}{path}", headers=headers, **kw)


def main() -> None:
    with httpx.Client(timeout=30) as c:
        # 1. admin 登录
        r = c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
        admin_token = r.json()["data"]["token"]
        step("1 admin 登录返回 token", r.status_code == 200 and bool(admin_token))

        # 2. 无 token -> 401
        r = c.get(f"{BASE}/metrics")
        step("2 无 token GET /metrics -> 401/40100", r.status_code == 401 and r.json()["code"] == 40100)

        # 3. 建 viewer（存在则忽略 409）
        r = api(c, "POST", "/auth/users", admin_token,
                json={"username": "viewer01", "password": "viewer123", "role": "viewer"})
        step("3 创建 viewer01", r.status_code == 200 or r.json().get("code") == 40900)

        # 4. viewer 登录
        r = c.post(f"{BASE}/auth/login", json={"username": "viewer01", "password": "viewer123"})
        viewer_token = r.json()["data"]["token"]
        step("4 viewer01 登录", r.status_code == 200 and bool(viewer_token))

        # 5. 挑一个指标（取目录第一个 id）
        r = api(c, "GET", "/metrics", admin_token)
        metric_id = r.json()["data"][0]["id"]
        print(f"       选用指标 id={metric_id}")

        # 6. 登记 role=viewer 不可见
        r = api(c, "PUT", f"/auth/metrics/{metric_id}/restrictions", admin_token,
                json={"items": [{"subject_type": "role", "subject_value": "viewer"}]})
        step("6 登记 viewer 不可见", r.status_code == 200)

        # 7. 双出口验证（viewer 视角）
        r = api(c, "GET", "/metrics", viewer_token)
        ids = [m["id"] for m in r.json()["data"]]
        step("7a viewer 目录中该指标消失", metric_id not in ids)

        r = api(c, "GET", f"/metrics/{metric_id}", viewer_token)
        step("7b viewer 详情 -> 403/40300", r.status_code == 403 and r.json()["code"] == 40300)

        r = api(c, "GET", f"/metrics/{metric_id}/sql", viewer_token)
        step("7c viewer SQL -> 403", r.status_code == 403)

        body = {"metric": metric_id, "start": "2025-01-01", "end": "2025-01-31"}
        r = api(c, "POST", "/query/metric-value", viewer_token, json=body)
        step("7d viewer 取数 -> 403", r.status_code == 403)

        r = api(c, "POST", "/query/export", viewer_token, json=body)
        step("7e viewer 导出 -> 403", r.status_code == 403)

        # 8. admin 不受影响
        r = api(c, "GET", f"/metrics/{metric_id}", admin_token)
        ok_detail = r.status_code == 200
        r = api(c, "POST", "/query/metric-value", admin_token, json=body)
        step("8 admin 详情+取数正常", ok_detail and r.status_code == 200)

        # 9. 清空恢复
        r = api(c, "PUT", f"/auth/metrics/{metric_id}/restrictions", admin_token, json={"items": []})
        r = api(c, "GET", f"/metrics/{metric_id}", viewer_token)
        step("9 清空登记后 viewer 恢复可见", r.status_code == 200)

    print(f"\n合计 {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
