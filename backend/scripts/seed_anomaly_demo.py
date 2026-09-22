"""异动演示种子脚本 — 向运行中的实例灌入一套可复现的异动验证数据。

用法（PowerShell，任意目录）:
    C:\\Users\\William Y Liang\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe backend\\scripts\\seed_anomaly_demo.py
可选端口: python seed_anomaly_demo.py 8101

覆盖内容（全部走正规 HTTP API，不直接碰 metadata.db）:
1  admin 登录
2  幂等准备：复用/创建项目「异动演示-零售电商」；清理其下旧指标与数据集
3  上传合成数据集 demo_anomaly_sales（112 天 × 3 渠道明细，固定随机种子可复现）
4  创建 3 个指标（销售额/活跃用户/订单数，均带 channel 常用维度）
5  取数 sanity check（各指标 9-14 ~ 9-20 区间值）
6  为 3 个指标开启异动检测（保守档）
7  项目级异动扫描（应命中 2 条异动：销售额↑ / 活跃用户↓，订单数正常）→ 落通知
8  生成日报（as_of=2026-09-20）验证报告异动章节
9  打印前端查看指引

数据故事（为何这样造数）:
- 基线带星期几周期（周末系数）+ 小幅噪声 → 同星期几基准稳定，z-score 可判；
- 2026-09-20（周日，覆盖末日=默认检测日）：
  · 线上商城销售额 ×4.2（模拟大促开场）→ 日销售额暴涨（up，z 远超 3）
  · 小程序活跃用户归零（模拟埋点/服务故障）→ 日活跃用户暴跌（down，约 -30%）
  · 订单数正常波动 → 对照组：verdict=normal
- 三条指标 z 值均按「min_samples=8 需 8 个同星期几样本」设计，112 天足够。
"""

from __future__ import annotations

import io
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import httpx

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8101'}/api"
PASS, FAIL = [], []
PROJECT_NAME = "异动演示-零售电商"
DATASET_NAME = "demo_anomaly_sales"
DETECT_DATE = "2026-09-20"  # 数据覆盖末日（默认检测日，周日）

CHANNELS = {
    "线上商城": {"sales": 4200, "users": 2600, "orders": 360},
    "门店": {"sales": 3100, "users": 1400, "orders": 250},
    "小程序": {"sales": 2100, "users": 1800, "orders": 190},
}
WEEKEND_FACTOR = {"sales": 1.45, "users": 1.25, "orders": 1.40}
NOISE = {"sales": 0.04, "users": 0.02, "orders": 0.03}


def step(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))


def api(client: httpx.Client, method: str, path: str, token: str | None = None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, f"{BASE}{path}", headers=headers, **kw)


def build_csv() -> bytes:
    """合成 2026-06-01 ~ 2026-09-20 的日粒度三渠道明细（固定种子可复现）。"""
    rng = random.Random(20260920)
    start = date(2026, 6, 1)
    end = date(2026, 9, 20)
    buf = io.StringIO()
    buf.write("sale_date,channel,sales_amount,active_users,orders\n")
    d = start
    while d <= end:
        weekend = d.weekday() >= 5  # 周六/周日
        for ch, base in CHANNELS.items():
            sales = base["sales"] * (WEEKEND_FACTOR["sales"] if weekend else 1.0)
            users = base["users"] * (WEEKEND_FACTOR["users"] if weekend else 1.0)
            orders = base["orders"] * (WEEKEND_FACTOR["orders"] if weekend else 1.0)
            sales *= 1 + NOISE["sales"] * rng.uniform(-1, 1)
            users *= 1 + NOISE["users"] * rng.uniform(-1, 1)
            orders *= 1 + NOISE["orders"] * rng.uniform(-1, 1)
            if d == date(2026, 9, 20):  # 异动日
                if ch == "线上商城":
                    sales *= 4.2  # 大促开场：暴涨
                if ch == "小程序":
                    users = 0.0  # 埋点故障：暴跌
            buf.write(
                f"{d.isoformat()},{ch},"
                f"{sales:.1f},{users:.1f},{max(0, round(orders))}\n"
            )
        d += timedelta(days=1)
    return buf.getvalue().encode("utf-8")


def main() -> None:
    with httpx.Client(timeout=60) as c:
        # 1. 登录
        r = c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"})
        token = r.json()["data"]["token"]
        step("1 admin 登录", r.status_code == 200 and bool(token))

        # 2. 幂等准备：复用或创建项目；清理其下旧指标/数据集
        r = api(c, "GET", "/projects", token)
        projects = r.json()["data"]
        proj = next((p for p in projects if p["name"] == PROJECT_NAME), None)
        if proj is None:
            r = api(c, "POST", "/projects", token,
                    json={"name": PROJECT_NAME,
                          "description": "异动检测验证专用项目（可整项目删除重建）"})
            proj = r.json()["data"]
            step("2a 创建项目", r.status_code == 200, r.text[:200])
        else:
            step("2a 复用已有项目", True)
        pid = proj["id"]

        r = api(c, "GET", f"/metrics?project_id={pid}", token)
        old_metrics = [m for m in r.json()["data"]
                       if m.get("project_id") == pid and m.get("status") != "deleted"]
        for m in old_metrics:
            api(c, "DELETE", f"/metrics/{m['id']}", token)
        step("2b 清理旧指标", True, f"删除 {len(old_metrics)} 个")

        r = api(c, "GET", f"/datasets?project_id={pid}", token)
        old_ds = [d for d in r.json()["data"] if d.get("project_id") == pid]
        for d in old_ds:
            api(c, "DELETE", f"/datasets/{d['id']}", token)
        step("2c 清理旧数据集", True, f"删除 {len(old_ds)} 个")

        # 3. 上传数据集
        csv_bytes = build_csv()
        r = api(c, "POST", "/datasets/upload", token,
                files={"file": (f"{DATASET_NAME}.csv", csv_bytes, "text/csv")},
                data={"name": DATASET_NAME, "project_id": str(pid)})
        ds = r.json().get("data") or {}
        step("3 上传数据集（336 行）", r.status_code == 200 and ds.get("row_count") == 336,
             f"row_count={ds.get('row_count')} {r.text[:200]}")

        # 4. 创建指标
        defs = [
            {
                "code": "demo_sales", "name": "日销售额",
                "calc_rule": {"base_aggregation": "sum",
                              "source": {"table": DATASET_NAME, "column": "sales_amount"}},
                "definition": "当日各渠道销售额合计（合成演示数据）",
                "aliases": ["销售额", "GMV", "销量"],
            },
            {
                "code": "demo_users", "name": "日活跃用户数",
                "calc_rule": {"base_aggregation": "sum",
                              "source": {"table": DATASET_NAME, "column": "active_users"}},
                "definition": "当日各渠道活跃用户合计（合成演示数据）",
                "aliases": ["活跃用户", "DAU"],
            },
            {
                "code": "demo_orders", "name": "日订单数",
                "calc_rule": {"base_aggregation": "sum",
                              "source": {"table": DATASET_NAME, "column": "orders"}},
                "definition": "当日各渠道订单合计（合成演示数据，异动对照组）",
                "aliases": ["订单数", "订单量"],
            },
        ]
        metric_ids = []
        for d in defs:
            r = api(c, "POST", "/metrics", token, json={**d, "dimensions": ["channel"],
                                                        "topic": "sales", "project_id": pid})
            mid = (r.json().get("data") or {}).get("id")
            metric_ids.append(mid)
            step(f"4 创建指标 {d['name']}", r.status_code == 200 and mid, r.text[:200])

        # 5. 取数 sanity check（区间 9-14 ~ 9-20）
        for name, mid in zip(("日销售额", "日活跃用户数", "日订单数"), metric_ids):
            r = api(c, "POST", "/query/metric-value", token,
                    json={"metric": mid, "start": "2026-09-14", "end": DETECT_DATE})
            v = (r.json().get("data") or {}).get("value")
            step(f"5 取数 {name}（近 7 天）", r.status_code == 200 and v is not None,
                 f"value={v} {r.text[:160]}")

        # 6. 开启异动检测（保守档：z=3 / 8 样本 / 5%）
        r = api(c, "POST", "/query/anomalies/configs", token,
                params={"project_id": pid}, json={"metric_ids": metric_ids})
        step("6 开启异动检测", r.status_code == 200, r.text[:200])

        # 7. 项目级扫描（触发通知落库）
        r = api(c, "GET", "/query/anomalies", token, params={"project_id": pid})
        scan = r.json().get("data") or {}
        anomalies = scan.get("anomalies") or []
        codes = sorted(a["metric_code"] for a in anomalies)
        step("7a 扫描命中 2 条异动", len(anomalies) == 2 and codes == ["demo_sales", "demo_users"],
             f"counts={scan.get('counts')} anomalies={codes}")
        for a in anomalies:
            direction = {"up": "↑", "down": "↓"}.get(a.get("direction"), "-")
            print(f"       · {a['name']} {direction} 当日 {a.get('current'):,.1f}，"
                  f"基准 {a.get('baseline', {}).get('mean', 0):,.1f}，"
                  f"{a.get('delta_pct')}%，{a.get('reason')}")

        # 8. 生成日报验证报告异动章节（模板可重复创建，不清理）
        r = api(c, "POST", "/reports/templates", token,
                json={"name": "异动演示日报", "period_type": "daily",
                      "metric_ids": metric_ids, "project_id": pid})
        tpl = (r.json().get("data") or {})
        step("8a 创建报告模板", r.status_code == 200 and tpl.get("id"), r.text[:200])

        r = api(c, "POST", "/reports/generate", token,
                json={"template_id": tpl.get("id"), "as_of": DETECT_DATE})
        # 存档接口返回 {instance 元信息, content: 完整报告}——异动章节在 content 内
        rep = (r.json().get("data") or {}).get("content") or {}
        n_anom = len(rep.get("anomalies") or [])
        step("8b 报告异动章节", r.status_code == 200 and n_anom >= 2,
             f"报告 instance_id={(r.json().get('data') or {}).get('id')}"
             f" 异动 {n_anom} 项 {r.text[:160]}")

        # 9. 通知确认
        r = api(c, "GET", "/notifications", token)
        notes = (r.json().get("data") or [])
        titles = [n.get("title") for n in notes[:4]]
        step("9 通知已落库", len(titles) >= 2, f"最新通知：{titles}")

        print("\n========== 前端查看指引 ==========")
        print(f"1. 打开 http://localhost:5173 ，右上角项目切换到「{PROJECT_NAME}」")
        print("2. 总览页：黄条异动提醒（来自第 7 步扫描）")
        print("3. 通知中心：2 条异动通知（含基准值与建议动作文案）")
        print("4. 指标目录：日销售额（↑ 大促）/ 日活跃用户数（↓ 故障）/ 日订单数（对照）")
        print("   → 详情页看折线末端突变 + 按渠道拆解（线上商城暴涨 / 小程序归零）")
        print("5. 报告中心：模板「异动演示日报」→ 生成日期选 2026-09-20 → 异动章节 + 溯源 chip")
        print("6. 问数页可试：「昨天销售额为什么涨这么多」等")

    print(f"\n合计 {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
