"""B13-1 预研实跑脚本：对 A/B 两套合成数据运行建模建议引擎并落盘结果。

一次性验证工具（可重跑）：临时 DATA_DIR + TestClient，不触碰真实数据目录。
用法：
    cd backend
    python scripts/b13_pilot.py
输出：bi-platform/docs/b13_pilot_result.json（建议全量结果）
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(BASE, "backend")
SYNTH = os.path.join(BASE, "data", "synthetic")
OUT = os.path.join(BASE, "docs", "b13_pilot_result.json")

FILES = [
    ("dataset_a", "channels.csv", "retail_channels"),
    ("dataset_a", "products.csv", "retail_products"),
    ("dataset_a", "users.csv", "retail_users"),
    ("dataset_a", "orders.csv", "retail_orders"),
    ("dataset_b", "plans.csv", "saas_plans"),
    ("dataset_b", "accounts.csv", "saas_accounts"),
    ("dataset_b", "subscriptions.csv", "saas_subscriptions"),
    ("dataset_b", "usage_events.csv", "saas_usage_events"),
]


def main() -> None:
    data_dir = os.path.join(tempfile.gettempdir(), "b13_pilot_data")
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    os.environ["APP_ENV"] = "test"
    os.environ["DATA_DIR"] = data_dir
    os.chdir(BACKEND)
    sys.path.insert(0, BACKEND)

    from app.infra.database import create_all, get_db, init_engine
    from app.domain.auth.service import ensure_bootstrap_admin

    init_engine(force=False)
    create_all()
    ensure_bootstrap_admin()

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        tok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"}
        ).json()["data"]["token"]
        client.headers.update({"Authorization": f"Bearer {tok}"})

        uploaded = []
        for sub, fname, ds_name in FILES:
            path = os.path.join(SYNTH, sub, fname)
            with open(path, "rb") as f:
                raw = f.read()
            resp = client.post(
                "/api/datasets/upload",
                files={"file": (fname, io.BytesIO(raw), "text/csv")},
                data={"name": ds_name},
            )
            assert resp.status_code == 200, f"{ds_name}: {resp.text[:300]}"
            d = resp.json()["data"]
            uploaded.append({"dataset": ds_name, "rows": d["row_count"], "cols": len(d["columns"])})
            print(f"uploaded {ds_name}: {d['row_count']} rows x {len(d['columns'])} cols")

        sug = client.post("/api/modeling/suggestions", json={}).json()["data"]

    result = {"uploaded": uploaded, "suggestions": sug}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("\n=== 关系建议（按分排序） ===")
    for r in sug["relations"]:
        print(
            f"{r['from_dataset']}.{r['from_column']} -> {r['to_dataset']}.{r['to_column']}"
            f"  score={r['score']} overlap={r['evidence']['overlap_ratio']}"
            f" name={r['evidence']['name_similarity']}"
        )
    print(f"\n=== 指标候选 {len(sug['metrics'])} 条 ===")
    for m in sug["metrics"]:
        print(f"{m['dataset']}.{m['column']} {m['aggregation']}  「{m['name']}」 ({m['source']})")
    print(f"\n结果已写入 {OUT}")
    shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
