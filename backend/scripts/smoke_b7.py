"""B7 MVP 联调与验收 — 端到端场景串测（对运行中的 dev 服务逐条验证）。

用法（服务启动后，bi-platform 目录下）:
    ..\\..\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe backend\\scripts\\smoke_b7.py
可选端口: python smoke_b7.py 8100

端到端主线（幂等可重复运行，测试对象带 b7_ 前缀，结尾自动清理）:
  A 接入   上传 GBK orders + utf-8 channels（自定义名 b7_*）→ 登记关系 → 试编译
  B 定义   创建指标 b7_gmv（ver=1）→ 取数 → 缓存 hit → 环比 → CSV 导出 → 周期完整性
  C 改口径 PATCH（reason 必填）→ ver=2 → 缓存 miss 新值生效 → 超纲拒绝不破坏口径
           → 口径变更影响清单（changes 留痕 + SQL 存档）
  D 权限   建 viewer → 登记 role 限制 → 目录消失 + 取数 403 + admin 不受影响
  清理     限制清空 / viewer 停用 / 指标软删 / 数据集级联删除 / 列表终检
"""

import sys
import time
from pathlib import Path

import httpx

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8100'}/api"
DATA = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "dataset_a"
PASS, FAIL = [], []


def step(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))


def upload(c, h, fname: str, name: str) -> dict:
    with open(DATA / fname, "rb") as f:
        r = c.post(f"{BASE}/datasets/upload", headers=h,
                   files={"file": (fname, f)}, data={"name": name})
    assert r.status_code == 200, r.text[:300]
    return r.json()["data"]


def get_value(c, h, metric, start, end, compare="none"):
    r = c.post(f"{BASE}/query/metric-value", headers=h,
               json={"metric": metric, "start": start, "end": end, "compare": compare})
    return r, (r.json()["data"] if r.status_code == 200 else None)


def cleanup_leftovers(c, h) -> None:
    """自愈清理：删除上一轮崩溃残留的 b7_ 数据集 / 活跃 b7_ 指标 / 活跃 b7_viewer 账号。

    说明：软删指标的 code 仍占用唯一性（repo.count 不过滤 status），
    因此 code 占用由 pick_suffix 换名解决，这里只清可见残留。
    """
    n_ds = n_m = n_u = 0
    for d in c.get(f"{BASE}/datasets", headers=h).json()["data"]:
        if d["name"].startswith("b7_"):
            c.delete(f"{BASE}/datasets/{d['id']}", headers=h)
            n_ds += 1
    for m in c.get(f"{BASE}/metrics", headers=h, params={"search": "b7_", "status": "all"}).json()["data"]:
        if m["status"] == "active":
            c.delete(f"{BASE}/metrics/{m['id']}", headers=h, params={"reason": "B7 smoke 残留清理"})
            n_m += 1
    for u in c.get(f"{BASE}/auth/users", headers=h).json()["data"]:
        if u["username"].startswith("b7_") and u.get("status") == "active":
            c.patch(f"{BASE}/auth/users/{u['id']}", headers=h, json={"status": "disabled"})
            n_u += 1
    if n_ds or n_m or n_u:
        print(f"       自愈清理：删除残留数据集 {n_ds} 个、活跃残留指标 {n_m} 个、停用残留账号 {n_u} 个")


def pick_suffix(c, h, base_suffix: str) -> str:
    """选一个未被占用（含软删）的指标 code 后缀，保证本次运行不撞 409。"""
    suffix = base_suffix
    for _ in range(5):
        r = c.get(f"{BASE}/metrics", headers=h, params={"search": f"b7_gmv{suffix}", "status": "all"})
        if not any(m["code"] == f"b7_gmv{suffix}" for m in r.json()["data"]):
            return suffix
        suffix = time.strftime("_%H%M%S")
    raise RuntimeError("无法找到未占用的 b7_gmv code 后缀")


def main() -> None:
    base_suffix = sys.argv[2] if len(sys.argv) > 2 else ""

    with httpx.Client(timeout=120) as c:
        # ---------------- A 接入 ----------------
        r = c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
        token = r.json()["data"]["token"]
        h = {"Authorization": f"Bearer {token}"}
        step("A1 admin 登录", r.status_code == 200)

        cleanup_leftovers(c, h)
        suffix = pick_suffix(c, h, base_suffix)
        if suffix != base_suffix:
            print(f"       code b7_gmv{base_suffix or ''} 已被占用（软删亦占用），"
                  f"本次自动改用后缀 {suffix!r}")
        code = f"b7_gmv{suffix}"
        viewer_name = f"b7_viewer{suffix}"

        orders = upload(c, h, "orders.csv", f"b7_orders{suffix}")
        step("A2 上传 orders.csv（GBK 靶点）10 万行 gbk",
             orders["row_count"] == 100000 and orders["encoding"] == "gbk",
             f"实际 rows={orders.get('row_count')} encoding={orders.get('encoding')}")
        oid = orders["id"]

        chan = upload(c, h, "channels.csv", f"b7_channels{suffix}")
        step("A3 上传 channels.csv 8 行", chan["row_count"] == 8, f"实际 {chan.get('row_count')}")
        cid = chan["id"]

        r = c.post(f"{BASE}/datasets/{oid}/relations", headers=h,
                   json={"from_column": "channel_id", "target_dataset_id": cid,
                         "target_column": "channel_id", "relation_type": "many_to_one"})
        step("A4 登记关系 channel_id -> channels", r.status_code == 200, r.text[:200])

        r = c.post(f"{BASE}/metrics/compile", headers=h, json={
            "calc_rule": {"base_aggregation": "sum",
                          "source": {"table": f"b7_orders{suffix}", "column": "pay_amount",
                                     "filter": "order_status = '已支付'"},
                          "time_field": "order_date"}})
        compiled = r.json()["data"]
        step("A5 试编译出参数化 SQL", r.status_code == 200 and "$__start__" in compiled.get("sql", ""),
             r.text[:200])

        # ---------------- B 定义与看板取数 ----------------
        r = c.post(f"{BASE}/metrics", headers=h, json={
            "code": code, "name": "B7验收-已支付GMV",
            "calc_rule": {"base_aggregation": "sum",
                          "source": {"table": f"b7_orders{suffix}", "column": "pay_amount",
                                     "filter": "order_status = '已支付'"},
                          "time_field": "order_date"},
            "definition": "验收用：已支付订单金额合计", "topic": "sales"})
        step("B1 创建指标 ver=1", r.status_code == 200 and r.json()["data"]["ver"] == 1, r.text[:200])
        mid = r.json()["data"]["id"]

        r, v1 = get_value(c, h, code, "2025-01-01", "2025-12-31")
        ok = r.status_code == 200 and v1["value"] is not None and v1["period_complete"] is True
        step("B2 2025 全年取数 value 非空 / 周期完整", ok, r.text[:200])
        print(f"       {code} 2025 全年 = {v1['value'] if v1 else None}（对照 smoke_b5 ecom_gmv_paid 7.156 亿）")

        _, v1b = get_value(c, h, code, "2025-01-01", "2025-12-31")
        step("B3 同区间再取缓存 hit", v1b["cache"] == "hit", f"实际 cache={v1b['cache']}")

        r, vm = get_value(c, h, code, "2025-03-01", "2025-03-31", compare="mom")
        cmp_data = (vm or {}).get("compare") or {}
        step("B4 环比结构（type=mom + 上期值 + change_pct）",
             r.status_code == 200 and cmp_data.get("type") == "mom"
             and "change_pct" in cmp_data and "value" in cmp_data, r.text[:200])
        print(f"       3 月环比 = {cmp_data.get('change_pct')}%"
              f"（上期 {cmp_data.get('start')}~{cmp_data.get('end')} = {cmp_data.get('value')}）")

        r = c.post(f"{BASE}/query/export", headers=h,
                   json={"metric": code, "start": "2025-01-01", "end": "2025-01-31"})
        head = r.content[:3]
        step("B5 导出 CSV（BOM + 文件名头）",
             r.status_code == 200 and head == b"\xef\xbb\xbf"
             and f"{code}_2025-01-01_2025-01-31.csv" in r.headers.get("content-disposition", ""),
             f"head={head!r} cd={r.headers.get('content-disposition')}")

        _, vd = get_value(c, h, code, "2025-01-01", "2026-12-31")
        step("B6 超覆盖区间 value=null + period_complete=false（缓存 bypass）",
             vd["value"] is None and vd["period_complete"] is False and vd["cache"] == "bypass",
             f"实际 {vd}")

        # ---------------- C 改口径全局生效 ----------------
        r = c.patch(f"{BASE}/metrics/{mid}", headers=h, json={
            "calc_rule": {"base_aggregation": "sum",
                          "source": {"table": f"b7_orders{suffix}", "column": "pay_amount",
                                     "filter": "order_status = '已退款'"},
                          "time_field": "order_date"},
            "reason": "验收演示：口径切换为已退款金额"})
        step("C1 PATCH 口径变更 ver=2（reason 必填）",
             r.status_code == 200 and r.json()["data"]["ver"] == 2, r.text[:200])

        r, v2 = get_value(c, h, code, "2025-01-01", "2025-12-31")
        ok = r.status_code == 200 and v2["cache"] == "miss" and v2["value"] is not None
        step("C2 改口径后取数缓存 miss（旧键失效）且新值非空", ok, r.text[:200])
        step("C3 新口径数值与旧口径不同（全局生效）",
             v2["value"] != v1["value"], f"v1={v1['value']} v2={v2['value']}")
        print(f"       改口径后 2025 全年 = {v2['value']}")

        r = c.patch(f"{BASE}/metrics/{mid}", headers=h, json={
            "calc_rule": {"base_aggregation": "sum",
                          "source": {"table": f"b7_orders{suffix}", "column": "pay_amount",
                                     "filter": "order_status = '已支付' OR order_status = '已退款'"},
                          "time_field": "order_date"},
            "reason": "验收演示：尝试超纲 OR"})
        step("C4 超纲 OR 保存即拒绝 400（ver 不变）",
             r.status_code == 400 and r.json()["code"] == 40000, r.text[:200])

        r = c.get(f"{BASE}/metrics/{mid}/changes", headers=h)
        changes = r.json()["data"]  # 按 id 倒序，最新在前
        latest = changes[0] if changes else {}
        after_filter = (latest.get("after", {}).get("calc_rule", {})
                        .get("source", {}).get("filter", ""))
        step("C5 影响清单：changes 留痕含 reason / 前后快照",
             r.status_code == 200 and len(changes) >= 1
             and latest.get("reason") == "验收演示：口径切换为已退款金额"
             and "已退款" in after_filter,
             f"实际 {len(changes)} 条; 最新 reason={latest.get('reason')} after_filter={after_filter!r}")
        print(f"       影响清单 {len(changes)} 条：操作人 {latest.get('operator_id')}，"
              f"快照 filter {after_filter!r}")

        r = c.get(f"{BASE}/metrics/{mid}/sql", headers=h)
        sql_arch = r.json()["data"]
        step("C6 当前版编译存档 SQL 可回看（已退款口径）",
             r.status_code == 200 and "已退款" in (sql_arch.get("sql_text") or ""),
             r.text[:200])

        # ---------------- D 权限双出口抽查 ----------------
        r = c.post(f"{BASE}/auth/users", headers=h,
                   json={"username": viewer_name, "password": "b7viewer123", "role": "viewer"})
        vid = r.json()["data"]["id"] if r.status_code == 200 else None
        step("D1 创建 viewer", r.status_code == 200, r.text[:200])

        r = c.post(f"{BASE}/auth/login", json={"username": viewer_name, "password": "b7viewer123"})
        vh = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r = c.get(f"{BASE}/metrics", headers=vh, params={"search": code})
        step("D2 登记前 viewer 目录可见", r.status_code == 200
             and any(m["code"] == code for m in r.json()["data"]), r.text[:200])

        r = c.put(f"{BASE}/auth/metrics/{mid}/restrictions", headers=h,
                  json={"items": [{"subject_type": "role", "subject_value": "viewer"}]})
        step("D3 登记 role=viewer 限制", r.status_code == 200, r.text[:200])

        r = c.get(f"{BASE}/metrics", headers=vh, params={"search": code})
        step("D4 元数据出口：viewer 目录不可见（名称口径不泄露）",
             r.status_code == 200 and not any(m["code"] == code for m in r.json()["data"]),
             r.text[:200])
        r = c.post(f"{BASE}/query/metric-value", headers=vh,
                   json={"metric": code, "start": "2025-01-01", "end": "2025-01-31"})
        step("D5 数值出口：viewer 取数 403",
             r.status_code == 403 and r.json()["code"] == 40300, r.text[:200])
        r = c.post(f"{BASE}/query/metric-value", headers=h,
                   json={"metric": code, "start": "2025-01-01", "end": "2025-01-31"})
        step("D6 admin 不受限制", r.status_code == 200, r.text[:200])

        # ---------------- 清理 ----------------
        r = c.put(f"{BASE}/auth/metrics/{mid}/restrictions", headers=h, json={"items": []})
        step("X1 清空可见性登记（恢复全员可见）", r.status_code == 200, r.text[:200])

        r = c.patch(f"{BASE}/auth/users/{vid}", headers=h, json={"status": "disabled"})
        step("X2 停用 b7_viewer 账号", r.status_code == 200, r.text[:200])

        r = c.delete(f"{BASE}/metrics/{mid}", headers=h, params={"reason": "B7 验收清理"})
        step("X3 指标软删除", r.status_code == 200, r.text[:200])

        ok_ds = True
        for did, nm in ((oid, "b7_orders"), (cid, "b7_channels")):
            r = c.delete(f"{BASE}/datasets/{did}", headers=h)
            ok_ds = ok_ds and r.status_code == 200 and r.json()["data"]["files_failed"] == []
        step("X4 数据集级联删除（文件清理无失败）", ok_ds, "")

        r = c.get(f"{BASE}/datasets", headers=h)
        step("X5 终检：数据集列表无 b7_ 残留",
             not any(d["name"].startswith("b7_") for d in r.json()["data"]), r.text[:300])

    print(f"\n合计 {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, TypeError, RuntimeError) as exc:
        print(f"\n[ABORT] 脚本异常终止：{exc!r}")
        print("提示：重跑即可——脚本启动时会自愈清理上一次崩溃残留（数据集/活跃指标/账号），"
              "code 被软删占用时自动换后缀。")
        sys.exit(1)
