"""B5 行业指标模板库 — 真实服务冒烟脚本（对运行中的 dev 服务逐条验证）。

用法（服务启动后，bi-platform 目录下）:
    ..\\..\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe backend\\scripts\\smoke_b5.py
可选端口: python smoke_b5.py 8100

流程（幂等，可重复运行）:
1  admin 登录
2  行业列表 = 4 包 / 101 条
3  电商包明细 27 条
4  导入 ecommerce+saas（created+skipped = 54）
5  重复导入 → created=0（幂等）
6  单表模板指标（ecom_gmv_paid）取数 200
7  导入 restaurant → 23 pending + 3 disabled；pending 取数 400
   （提示"尚未绑定数据集"）
8  revalidate 重导入（无新数据则 pending 仍 skipped，不报错）
"""

import sys

import httpx

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8100'}/api"
PASS, FAIL = [], []


def step(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))


def main() -> None:
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
        token = r.json()["data"]["token"]
        h = {"Authorization": f"Bearer {token}"}
        step("1 admin 登录", r.status_code == 200)

        r = c.get(f"{BASE}/templates/industries", headers=h)
        packs = r.json()["data"]
        total = sum(p["metric_count"] for p in packs)
        step("2 行业列表 4 包 / 101 条", r.status_code == 200 and len(packs) == 4 and total == 101,
             f"实际 {len(packs)} 包 / {total} 条")

        r = c.get(f"{BASE}/templates/ecommerce", headers=h)
        detail = r.json()["data"]
        step("3 电商包明细 27 条", r.status_code == 200 and detail["metric_count"] == 27)

        def imp(**body):
            return c.post(f"{BASE}/templates/import", headers=h, json=body).json()["data"]

        first = imp(industries=["ecommerce", "saas"])
        created = sum(x["created"] for x in first["results"])
        touched = sum(x["created"] + x["skipped"] + x["upgraded"] for x in first["results"])
        step("4 导入电商+SaaS（created+skipped=54）", touched == 54, f"实际 touched={touched}")
        print(f"       首次导入 created={created}（重复运行时 created=0 属正常）")

        again = imp(industries=["ecommerce", "saas"])
        created2 = sum(x["created"] for x in again["results"])
        step("5 重复导入幂等 created=0", created2 == 0, f"实际 created={created2}")

        r = c.post(f"{BASE}/query/metric-value", headers=h,
                   json={"metric": "ecom_gmv_paid", "start": "2025-01-01", "end": "2025-12-31"})
        step("6 模板指标取数 200（区间超覆盖时 value=null 属周期完整性语义）",
             r.status_code == 200, r.text[:200])
        print(f"       ecom_gmv_paid -> {r.json()['data'].get('value')} "
              f"(period_complete={r.json()['data'].get('period_complete')})")

        rst = imp(industries=["restaurant"])
        counts = next(x["status_counts"] for x in rst["results"] if x["industry"] == "restaurant")
        step("7a 餐饮导入 23 pending + 3 disabled",
             counts.get("pending") == 23 and counts.get("disabled") == 3, f"实际 {counts}")

        r = c.post(f"{BASE}/query/metric-value", headers=h,
                   json={"metric": "rst_revenue", "start": "2025-01-01", "end": "2025-01-31"})
        step("7b pending 取数 400 提示绑定数据集",
             r.status_code == 400 and "尚未绑定数据集" in r.json()["message"], r.text[:200])

        rv = imp(industries=["restaurant"], revalidate=True)
        step("8 revalidate 幂等（无新数据 upgraded=0）",
             rv["totals"]["upgraded"] == 0, f"实际 {rv['totals']}")

    print(f"\n合计 {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
