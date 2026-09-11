#!/usr/bin/env python3
"""S1 多行业合成数据生成器（架构 D21：行业无关性验证载体）。

生成两套结构差异显著的合成数据集，用于 B1 数据接入与 B2 编译器的跨行业验证：

  数据集 A：零售交易型（电商零售）
      channels / products / users / orders
      特征：订单粒度、退款、折扣、品类与区域维度
  数据集 B：订阅服务型（SaaS）
      plans / accounts / subscriptions / usage_events
      特征：订阅周期、经常性收入、账户粒度，无"订单"概念

刻意制造的质检靶点（对应 B1 验收）：
  - 每套数据集各含 1 个 GBK 编码文件（A: orders.csv，B: accounts.csv），其余 UTF-8
  - 每套各含 ≥2 个混杂类型列（数值 / 字符串 / 空值混排）
  - 少量异常值（负金额、退款大于实付、数量为 0），用于异常行定位
  - 日期覆盖 2025-01-01 至 2026-09-09：当期（9 月）天然不完整，
    供 B3 周期完整性规则（不完整返回 null 而非 0）使用

用法：
  python scripts/gen_synthetic_data.py                      # 默认两套各 10 万行事实表
  python scripts/gen_synthetic_data.py --dataset a --rows 1000000
  python scripts/gen_synthetic_data.py --dataset both --rows 50000 --outdir ../data/synthetic

约束：本脚本只存在于 scripts/，backend/app 内禁止出现任何行业语义（不变式 7）。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

# 生成日为 2026-09-10；数据截止前一天，使"当期"天然不完整
COVERAGE_START = date(2025, 1, 1)
COVERAGE_END = date(2026, 9, 9)

DEFAULT_FACT_ROWS = 100_000
MAX_FACT_ROWS = 1_000_000


# ---------------------------------------------------------------- utilities


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randrange((end - start).days + 1))


def write_csv(path: Path, header: list[str], rows: list[list], encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding=encoding) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def maybe_mixed_ref(rng: random.Random, prefix: str) -> str | int:
    """混杂类型列生成器：整数 / 带前缀字符串 / 空值。"""
    roll = rng.random()
    if roll < 0.55:
        return rng.randrange(1_000_000_000, 9_999_999_999)
    if roll < 0.85:
        return f"{prefix}-{rng.randrange(100000, 999999)}"
    return ""


# ---------------------------------------------------------------- dataset A


CITIES_A = [
    ("成都", "西南"), ("重庆", "西南"), ("上海", "华东"), ("杭州", "华东"),
    ("广州", "华南"), ("深圳", "华南"), ("北京", "华北"), ("天津", "华北"),
    ("武汉", "华中"), ("长沙", "华中"), ("沈阳", "东北"), ("大连", "东北"),
]
CHANNELS_A = [
    ("天猫旗舰店", "线上"), ("京东自营", "线上"), ("抖音直播", "线上"),
    ("微信小程序", "线上"), ("线下门店-成都", "线下"), ("线下门店-上海", "线下"),
    ("线下门店-北京", "线下"), ("分销渠道", "分销"),
]
CATEGORIES_A = {
    "家电": ["冰箱", "洗衣机", "空调", "电饭煲"],
    "数码": ["手机", "耳机", "平板", "智能手表"],
    "服饰": ["男装", "女装", "童装", "运动鞋"],
    "食品": ["零食", "茶叶", "粮油", "饮品"],
    "美妆": ["护肤", "彩妆", "香氛", "个护"],
    "家居": ["床品", "收纳", "灯具", "厨具"],
}
SURNAMES_A = ["王", "李", "张", "刘", "陈", "杨", "黄", "赵", "周", "吴", "徐", "孙", "林", "何", "郭"]
GIVEN_A = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "霞", "平", "刚", "桂英"]
NOTES_A = ["", "", "", "", "", "", "急件", "礼盒包装", "次日达", "发票抬头另附"]

A_ORDERS_HEADER = [
    "order_id", "order_date", "user_id", "product_id", "channel_id",
    "quantity", "unit_price", "discount", "pay_amount", "refund_amount",
    "order_status", "coupon_code", "note",
]


def gen_dataset_a(outdir: Path, fact_rows: int, seed: int) -> dict:
    """零售交易型。orders.csv 为 GBK 编码（B1 编码识别靶点）。"""
    rng = random.Random(seed)

    # 维表：渠道
    channel_rows = [[f"CH{i:03d}", name, ctype] for i, (name, ctype) in enumerate(CHANNELS_A, 1)]
    write_csv(outdir / "dataset_a" / "channels.csv",
              ["channel_id", "channel_name", "channel_type"], channel_rows, "utf-8")

    # 维表：商品
    product_rows = []
    pid = 0
    for cat, subs in CATEGORIES_A.items():
        for sub in subs:
            for _ in range(10):
                pid += 1
                price = round(rng.uniform(9.9, 4999.0), 2)
                product_rows.append([f"P{pid:05d}", f"{cat}{sub}款{pid:04d}",
                                     cat, sub, price, round(price * rng.uniform(0.5, 0.75), 2)])
    write_csv(outdir / "dataset_a" / "products.csv",
              ["product_id", "product_name", "category", "sub_category", "unit_price", "cost"],
              product_rows, "utf-8")

    # 维表：用户（external_ref 为混杂类型列）
    n_users = max(2_000, fact_rows // 8)
    user_rows = []
    for i in range(1, n_users + 1):
        city, region = rng.choice(CITIES_A)
        user_rows.append([
            f"U{i:06d}",
            rng.choice(SURNAMES_A) + rng.choice(GIVEN_A),
            city, region,
            rand_date(rng, date(2024, 1, 1), date(2025, 12, 31)).isoformat(),
            rng.randint(1, 5),
            maybe_mixed_ref(rng, "WX"),
        ])
    write_csv(outdir / "dataset_a" / "users.csv",
              ["user_id", "user_name", "city", "region", "register_date", "level", "external_ref"],
              user_rows, "utf-8")

    # 事实表：订单（GBK 编码 + 混杂类型列 coupon_code + 少量异常值）
    order_rows = []
    for i in range(1, fact_rows + 1):
        d = rand_date(rng, COVERAGE_START, COVERAGE_END)
        product = rng.choice(product_rows)
        quantity = 0 if rng.random() < 0.0002 else rng.randint(1, 10)
        discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15])
        pay = round(product[4] * quantity * (1 - discount), 2)
        if rng.random() < 0.0005:
            pay = -pay  # 异常：负实付
        refund = 0.0
        status = "已支付"
        roll = rng.random()
        if roll < 0.05:
            refund = round(pay * rng.uniform(0.1, 0.5), 2)
            status = "部分退款"
        elif roll < 0.08:
            refund = pay
            status = "已退款"
        if rng.random() < 0.0003:
            refund = round(abs(pay) * 1.2, 2)  # 异常：退款大于实付
            status = "部分退款"
        coupon_roll = rng.random()
        coupon = "" if coupon_roll < 0.5 else (
            rng.randint(1000, 9999) if coupon_roll < 0.8 else rng.choice(["VIP88", "NEW50", "OLD30"])
        )
        order_rows.append([
            f"SO{d.strftime('%Y%m%d')}{i:07d}", d.isoformat(),
            rng.choice(user_rows)[0], product[0], rng.choice(channel_rows)[0],
            quantity, product[4], discount, pay, refund, status, coupon, rng.choice(NOTES_A),
        ])
    write_csv(outdir / "dataset_a" / "orders.csv", A_ORDERS_HEADER, order_rows, "gbk")

    return {
        "name": "dataset_a_retail",
        "label": "A 零售交易型（电商零售）",
        "files": {
            "channels.csv": {"rows": len(channel_rows), "encoding": "utf-8", "mixed_columns": []},
            "products.csv": {"rows": len(product_rows), "encoding": "utf-8", "mixed_columns": []},
            "users.csv": {"rows": len(user_rows), "encoding": "utf-8", "mixed_columns": ["external_ref"]},
            "orders.csv": {"rows": len(order_rows), "encoding": "gbk",
                           "mixed_columns": ["coupon_code"],
                           "deliberate_anomalies": ["quantity=0", "pay_amount<0", "refund>pay"]},
        },
        "relations": [
            "orders.user_id -> users.user_id",
            "orders.product_id -> products.product_id",
            "orders.channel_id -> channels.channel_id",
        ],
        "granularity": "订单（每行一笔订单）",
        "industry_semantics": ["退款", "折扣", "品类", "区域", "渠道"],
    }


# ---------------------------------------------------------------- dataset B


PLANS_B = [
    ("PL001", "免费版", 0.0, "月付", 1),
    ("PL002", "标准版", 299.0, "月付", 5),
    ("PL003", "专业版", 899.0, "月付", 20),
    ("PL004", "企业版", 2999.0, "月付", 100),
]
INDUSTRIES_B = ["制造业", "金融", "零售", "医疗", "教育", "互联网", "物流"]
REGIONS_B = ["西南", "华东", "华南", "华北", "华中", "东北"]
FEATURES_B = ["报表导出", "数据看板", "权限管理", "API调用", "数据接入", "协作空间", "审计日志"]
CSM_B = ["沈一鸣", "陆文萱", "傅子昂", "姜若彤", "宋雨薇", "程亦凡"]

B_ACCOUNTS_HEADER = [
    "account_id", "account_name", "industry", "region",
    "signup_date", "csm_owner", "crm_ref",
]
B_SUBS_HEADER = [
    "subscription_id", "account_id", "plan_id", "start_date", "end_date",
    "status", "billing_cycle", "mrr_amount", "seats", "auto_renew",
]
B_EVENTS_HEADER = [
    "event_id", "account_id", "event_date", "feature_name",
    "usage_count", "duration_min", "response_ms", "client_version",
]


def gen_dataset_b(outdir: Path, fact_rows: int, seed: int) -> dict:
    """订阅服务型。accounts.csv 为 GBK 编码；无"订单"概念（行业差异靶点）。"""
    rng = random.Random(seed)

    # 维表：套餐
    plan_rows = [[pid, name, price, cycle, seats] for pid, name, price, cycle, seats in PLANS_B]
    write_csv(outdir / "dataset_b" / "plans.csv",
              ["plan_id", "plan_name", "monthly_price", "billing_unit", "seats_included"],
              plan_rows, "utf-8")

    # 维表：账户（GBK 编码 + 混杂类型列 crm_ref）
    n_accounts = max(500, fact_rows // 50)
    account_rows = []
    for i in range(1, n_accounts + 1):
        account_rows.append([
            f"ACC{i:05d}",
            f"{rng.choice(['星辰', '云途', '衡石', '澜舟', '青梧', '知行', '致远'])}"
            f"{rng.choice(['科技', '信息', '数智', '云服务'])}有限公司{i:03d}",
            rng.choice(INDUSTRIES_B), rng.choice(REGIONS_B),
            rand_date(rng, date(2024, 1, 1), date(2025, 6, 30)).isoformat(),
            rng.choice(CSM_B),
            maybe_mixed_ref(rng, "SF"),
        ])
    write_csv(outdir / "dataset_b" / "accounts.csv", B_ACCOUNTS_HEADER, account_rows, "gbk")

    # 事实表 1：订阅（end_date 为空 = 在续订阅；经常性收入语义）
    plan_price = {p[0]: p[2] for p in PLANS_B}
    weights = [0.30, 0.35, 0.25, 0.10]
    sub_rows = []
    for i in range(1, fact_rows + 1):
        account = rng.choice(account_rows)
        plan_id = rng.choices([p[0] for p in PLANS_B], weights=weights, k=1)[0]
        cycle = rng.choices(["月付", "季付", "年付"], weights=[0.6, 0.2, 0.2], k=1)[0]
        start = rand_date(rng, date(2025, 1, 1), date(2026, 8, 10))
        term_days = {"月付": 30, "季付": 90, "年付": 365}[cycle]
        # MRR：年付按 10 个月折算（年付折扣），季付按 2.85 个月折算
        base = plan_price[plan_id]
        mrr = round(base * {"月付": 1.0, "季付": 2.85 / 3, "年付": 10.0 / 12}[cycle], 2)
        if rng.random() < 0.0005:
            mrr = -mrr  # 异常：负 MRR
        end: date | str
        roll = rng.random()
        if roll < 0.72:
            end = ""
            status = "active"
        elif roll < 0.90:
            end = (start + timedelta(days=term_days)).isoformat()
            status = "churned" if end <= COVERAGE_END.isoformat() else "active"
        else:
            end = (start + timedelta(days=term_days)).isoformat()
            status = "active"
        seat_base = 1 if plan_id == "PL001" else 8
        sub_rows.append([
            f"SUB{i:07d}", account[0], plan_id, start.isoformat(), end,
            status, cycle, mrr, max(1, int(rng.gauss(seat_base, 4))),
            rng.choice([0, 1, 1, 1]),
        ])
    write_csv(outdir / "dataset_b" / "subscriptions.csv", B_SUBS_HEADER, sub_rows, "utf-8")

    # 事实表 2：使用事件（response_ms 为混杂类型列）
    event_rows = []
    for i in range(1, fact_rows + 1):
        ms_roll = rng.random()
        if ms_roll < 0.90:
            response_ms = rng.randrange(20, 3000)
        elif ms_roll < 0.95:
            response_ms = "超时"
        else:
            response_ms = ""
        event_rows.append([
            f"EV{i:08d}", rng.choice(account_rows)[0],
            rand_date(rng, COVERAGE_START, COVERAGE_END).isoformat(),
            rng.choice(FEATURES_B),
            rng.randint(1, 200),
            round(rng.uniform(0.5, 120.0), 1),
            response_ms,
            rng.choices(["3.2.1", "4.0.0", "4.1.3", ""], weights=[0.3, 0.35, 0.3, 0.05], k=1)[0],
        ])
    write_csv(outdir / "dataset_b" / "usage_events.csv", B_EVENTS_HEADER, event_rows, "utf-8")

    return {
        "name": "dataset_b_saas",
        "label": "B 订阅服务型（SaaS）",
        "files": {
            "plans.csv": {"rows": len(plan_rows), "encoding": "utf-8", "mixed_columns": []},
            "accounts.csv": {"rows": len(account_rows), "encoding": "gbk",
                             "mixed_columns": ["crm_ref"]},
            "subscriptions.csv": {"rows": len(sub_rows), "encoding": "utf-8",
                                  "mixed_columns": [],
                                  "notes": "end_date 为空表示在续订阅；mrr 为经常性收入"},
            "usage_events.csv": {"rows": len(event_rows), "encoding": "utf-8",
                                 "mixed_columns": ["response_ms"],
                                 "deliberate_anomalies": ["mrr<0", "response_ms='超时'"]},
        },
        "relations": [
            "subscriptions.account_id -> accounts.account_id",
            "subscriptions.plan_id -> plans.plan_id",
            "usage_events.account_id -> accounts.account_id",
        ],
        "granularity": "订阅周期 + 使用事件（无订单概念）",
        "industry_semantics": ["订阅周期", "经常性收入MRR", "账户", "功能使用"],
    }


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="S1 多行业合成数据生成器")
    parser.add_argument("--dataset", choices=["a", "b", "both"], default="both",
                        help="生成哪套数据集（默认 both）")
    parser.add_argument("--rows", type=int, default=DEFAULT_FACT_ROWS,
                        help=f"事实表行数（默认 {DEFAULT_FACT_ROWS}，上限 {MAX_FACT_ROWS}）")
    parser.add_argument("--outdir", default=str(Path(__file__).resolve().parent.parent / "data" / "synthetic"),
                        help="输出目录（默认 bi-platform/data/synthetic）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，可复现）")
    args = parser.parse_args()

    fact_rows = min(max(args.rows, 1_000), MAX_FACT_ROWS)
    outdir = Path(args.outdir)

    manifest: dict = {
        "generated_by": "scripts/gen_synthetic_data.py (S1)",
        "seed": args.seed,
        "fact_rows": fact_rows,
        "coverage": {"start": COVERAGE_START.isoformat(), "end": COVERAGE_END.isoformat(),
                     "note": "截止 2026-09-09，当期（2026-09）不完整，用于周期完整性验证"},
        "datasets": [],
    }

    if args.dataset in ("a", "both"):
        info = gen_dataset_a(outdir, fact_rows, args.seed)
        manifest["datasets"].append(info)
        print(f"[A] {info['label']} 完成：orders={fact_rows:,} 行（GBK）")
    if args.dataset in ("b", "both"):
        info = gen_dataset_b(outdir, fact_rows, args.seed)
        manifest["datasets"].append(info)
        print(f"[B] {info['label']} 完成：subscriptions={fact_rows:,} / usage_events={fact_rows:,} 行")

    manifest_path = outdir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
