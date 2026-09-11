"""B5 行业指标模板库测试：包完整性、幂等导入、pending→激活升级、扩展算子登记、权限。

数据流（saga 夹具按显式顺序执行一次）：
1. 上传电商/SaaS 数据集 + 注册 orders->channels 关系 → 导入 ecommerce+saas
   （可编译 → active；requires → disabled）
2. 重复导入 → 全部 skipped（幂等，不重复建指标/存档）
3. 上传餐饮/通用数据前先导入 restaurant+general → 待绑定数据集 → pending
4. pending 指标取数 → 400 明确提示；默认目录不含 pending 指标
5. 上传餐饮/通用数据集 → revalidate 重导入 → 升级 active
6. 升级后取数返回真实数值
"""

from __future__ import annotations

import io

import pytest

from tests.conftest import create_test_user

# ---------------------------------------------------------------- 测试数据


ECO_ORDERS = """order_id,order_date,user_id,channel_id,quantity,pay_amount,refund_amount,discount,order_status,coupon_code
SO1,2025-01-01,U1,CH1,2,100.5,0,10,已支付,C1
SO2,2025-01-02,U2,CH2,1,200,0,0,已支付,
SO3,2025-01-03,U1,CH2,3,300,50,0,部分退款,
SO4,2025-01-04,U3,CH2,1,400,400,0,已退款,
SO5,2025-01-05,U2,CH1,5,12000,0,100,已支付,C2
"""

ECO_USERS = """user_id,register_date
U1,2025-01-01
U2,2025-01-02
U3,2024-12-01
"""

ECO_CHANNELS = """channel_id,channel_type,channel_name
CH1,线上,自营
CH2,线下,门店
"""

SAAS_SUBS = """subscription_id,account_id,plan_id,start_date,end_date,status,billing_cycle,mrr_amount,seats,auto_renew
SB1,ACC1,PL1,2025-01-01,,active,月付,299,5,1
SB2,ACC2,PL1,2025-01-02,,active,年付,1999,20,0
SB3,ACC1,PL2,2025-01-03,2025-06-30,churned,月付,99,2,0
SB4,ACC3,PL2,2025-01-05,,active,季付,599,8,1
"""

SAAS_USAGE = """event_id,account_id,event_date,feature_name,usage_count,duration_min
EV1,ACC1,2025-01-01,报表,10,5.5
EV2,ACC2,2025-01-02,看板,3,2.0
EV3,ACC1,2025-01-03,报表,7,8.0
"""

RST_ORDERS = """order_id,order_date,store_id,member_id,channel_type,table_no,diner_count,pay_amount,discount_amount,status
DO1,2025-01-01,S1,M1,堂食,T01,4,500,50,已结账
DO2,2025-01-01,S1,,外卖,,1,80,0,已结账
DO3,2025-01-04,S2,M2,堂食,T02,2,300,0,已结账
DO4,2025-01-05,S1,,堂食,T03,3,200,0,已退单
"""

RST_ITEMS = """order_id,order_date,item_id,quantity,amount,status
DO1,2025-01-01,I1,2,200,正常
DO1,2025-01-01,I2,1,150,正常
DO2,2025-01-01,I3,1,80,正常
DO3,2025-01-02,I1,2,180,已退
DO3,2025-01-02,I4,3,210,正常
"""

RST_MEMBERS = """member_id,member_name,join_date,level
M1,张三,2025-01-01,1
M2,李四,2025-01-02,2
"""

RST_STORES = """store_id,store_name,city,open_date
S1,一店,北京,2024-01-01
S2,二店,上海,2024-02-01
"""

GEN_TX = """tx_id,tx_date,amount,type,category,customer_id
T1,2025-01-01,1000,收入,主营业务,C1
T2,2025-01-02,4000,收入,主营业务,C2
T3,2025-01-03,1500,支出,日常,C1
T4,2025-01-04,200,退款,,C2
T5,2025-01-05,120000,收入,其他,C3
"""

GEN_CUST = """customer_id,customer_name,register_date,region
C1,甲,2024-12-01,华北
C2,乙,2025-01-01,华东
C3,丙,2025-01-02,华南
C4,丁,2025-01-03,华北
"""

# 查询区间与上传数据覆盖区间一致（超出会触发周期完整性 value=null，见 B3 语义）
RANGE = {"start": "2025-01-01", "end": "2025-01-05"}


def _upload(client, name: str, content: str) -> dict:
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{name}.csv", io.BytesIO(content.encode("utf-8")), "text/csv")},
        data={"name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _relation(client, from_ds: dict, from_col: str, to_ds: dict, to_col: str):
    resp = client.post(
        f"/api/datasets/{from_ds['id']}/relations",
        json={"from_column": from_col, "target_dataset_id": to_ds["id"], "target_column": to_col},
    )
    assert resp.status_code == 200, resp.text


def _import(client, **body):
    resp = client.post("/api/templates/import", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _by_industry(result: dict, industry: str) -> dict:
    return next(r for r in result["results"] if r["industry"] == industry)


def _query(client, metric, **kw):
    body = {"metric": metric, **RANGE, **kw}
    return client.post("/api/query/metric-value", json=body)


def _template_metric_total(client) -> int:
    """库内模板指标总数（按行业前缀过滤；全量回归时库里还有其他测试的指标）。"""
    codes = [
        m["code"] for m in client.get("/api/metrics", params={"status": "all"}).json()["data"]
        if m["code"].startswith(("ecom_", "saas_", "rst_", "gen_"))
    ]
    return len(codes)


@pytest.fixture(scope="module")
def saga(client):
    out: dict = {}

    # 1. 电商 + SaaS 数据集（先上传后导入 → active）
    orders = _upload(client, "orders", ECO_ORDERS)
    users = _upload(client, "users", ECO_USERS)
    channels = _upload(client, "channels", ECO_CHANNELS)
    _relation(client, orders, "channel_id", channels, "channel_id")
    _upload(client, "subscriptions", SAAS_SUBS)
    _upload(client, "usage_events", SAAS_USAGE)
    out["import_main"] = _import(client, industries=["ecommerce", "saas"])

    # 2. 重复导入（幂等）
    out["import_again"] = _import(client, industries=["ecommerce", "saas"])
    out["total_main"] = _template_metric_total(client)

    # 3. 餐饮/通用：数据未上传 → pending；扩展算子 → disabled
    out["import_pending"] = _import(client, industries=["restaurant", "general"])

    # 4. pending 指标：取数被守卫拦截、默认目录不含、详情可见
    resp = _query(client, "gen_customer_penetration")
    out["pending_query"] = {"status": resp.status_code, "body": resp.json()}
    resp = client.get("/api/metrics", params={"status": "active"})
    out["active_codes_after_pending"] = resp.json()["data"]
    resp = client.get("/api/templates/general")
    out["general_detail"] = resp.json()["data"]

    # 5. 上传餐饮/通用数据 → revalidate 升级
    dine = _upload(client, "dine_orders", RST_ORDERS)
    _upload(client, "order_items", RST_ITEMS)
    _upload(client, "members", RST_MEMBERS)
    _upload(client, "stores", RST_STORES)
    tx = _upload(client, "transactions", GEN_TX)
    cust = _upload(client, "customers", GEN_CUST)
    _relation(client, tx, "customer_id", cust, "customer_id")
    out["revalidate"] = _import(client, industries=["restaurant", "general"], revalidate=True)
    out["total_final"] = _template_metric_total(client)

    # 6. 升级后取数（真实数值，供断言）
    for key, code in (
        ("q_gmv_paid", "ecom_gmv_paid"),
        ("q_mrr_active", "saas_mrr_active"),
        ("q_revenue", "rst_revenue"),
        ("q_net_margin", "gen_net_margin"),
        ("q_online_ratio", "ecom_online_gmv_ratio"),
    ):
        resp = _query(client, code)
        out[key] = {"status": resp.status_code, "body": resp.json()}
    return out


# ---------------------------------------------------------------- 包完整性


class TestPackIntegrity:
    def test_four_industries_listed(self, client):
        resp = client.get("/api/templates/industries")
        assert resp.status_code == 200
        packs = resp.json()["data"]
        assert [p["industry"] for p in packs] == ["ecommerce", "general", "restaurant", "saas"]
        assert all(p["metric_count"] > 0 for p in packs)

    def test_total_metrics_about_100(self, client):
        packs = client.get("/api/templates/industries").json()["data"]
        total = sum(p["metric_count"] for p in packs)
        assert 95 <= total <= 110, total

    def test_detail_shape(self, client):
        resp = client.get("/api/templates/ecommerce")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["industry"] == "ecommerce"
        assert data["metric_count"] == len(data["metrics"])
        first = data["metrics"][0]
        for field in ("code", "name", "aliases", "calc_rule", "imported", "requires"):
            assert field in first

    def test_unknown_industry_404(self, client):
        resp = client.get("/api/templates/nonexistent")
        assert resp.status_code == 404

    def test_broken_pack_flagged_not_fatal(self, client, monkeypatch, tmp_path):
        """损坏包在列表中标记 error=True，不阻断其他包；导入时报 400。"""
        from app.core.config import get_settings

        seed_dir = get_settings().resolved_templates_dir
        for f in seed_dir.glob("*.yaml"):
            (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "bad.yaml").write_text(
            "industry: bad\nname: 坏包\nmetrics:\n  - code: bad_x\n    name: x\n"
            "    requires: banana\n    calc_rule: {}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(get_settings(), "templates_dir", tmp_path)
        try:
            packs = {p["industry"]: p for p in client.get("/api/templates/industries").json()["data"]}
            assert packs["bad"]["error"] is True
            # 坏包不可导入（不进入可用列表）
            resp = client.post("/api/templates/import", json={"industries": ["bad"]})
            assert resp.status_code == 404
            # 好包不受影响
            assert client.get("/api/templates/ecommerce").status_code == 200
        finally:
            monkeypatch.undo()


# ---------------------------------------------------------------- 导入主流程


class TestImportFlow:
    def test_import_creates_active_and_disabled(self, saga):
        eco = _by_industry(saga["import_main"], "ecommerce")
        saas = _by_industry(saga["import_main"], "saas")
        # 电商 27 条：3 条扩展算子 disabled，其余 active
        assert eco["created"] == 27
        assert eco["status_counts"]["active"] == 24
        assert eco["status_counts"]["disabled"] == 3
        assert saas["created"] == 27
        assert saas["status_counts"]["active"] == 25
        assert saas["status_counts"]["disabled"] == 2

    def test_import_idempotent(self, client, saga):
        again = saga["import_again"]
        for industry in ("ecommerce", "saas"):
            row = _by_industry(again, industry)
            assert row["created"] == 0
            assert row["upgraded"] == 0
            assert row["skipped"] == 27
        # 库内总数 = 电商 27 + SaaS 27，重复导入未新增
        assert saga["total_main"] == 54

    def test_pending_import_without_data(self, saga):
        rst = _by_industry(saga["import_pending"], "restaurant")
        gen = _by_industry(saga["import_pending"], "general")
        assert rst["status_counts"]["pending"] == 23  # 26 - 3 扩展算子
        assert rst["status_counts"]["disabled"] == 3
        assert gen["status_counts"]["pending"] == 19  # 21 - 2 扩展算子
        assert gen["status_counts"]["disabled"] == 2
        # pending 查询被守卫拦截（400 + 明确文案）
        assert saga["pending_query"]["status"] == 400
        assert "尚未绑定数据集" in saga["pending_query"]["body"]["message"]
        # 默认目录（status=active）不含 pending 指标
        codes = {m["code"] for m in saga["active_codes_after_pending"]}
        assert "rst_revenue" not in codes
        assert "gen_customer_penetration" not in codes
        # 模板明细中标记 pending 导入状态
        flagged = {m["code"]: m for m in saga["general_detail"]["metrics"]}
        assert flagged["gen_revenue"]["imported"] is True
        assert flagged["gen_revenue"]["imported_status"] == "pending"

    def test_revalidate_upgrades_pending(self, saga):
        rst = _by_industry(saga["revalidate"], "restaurant")
        gen = _by_industry(saga["revalidate"], "general")
        assert rst["created"] == 0
        assert rst["upgraded"] == 23
        assert rst["status_counts"]["active"] == 23
        assert gen["upgraded"] == 19
        assert gen["status_counts"]["active"] == 19
        assert saga["total_final"] == 101  # 54 + 26 + 21，四包全量且无重复

    def test_values_after_upgrade(self, saga):
        assert saga["q_gmv_paid"]["status"] == 200
        assert saga["q_gmv_paid"]["body"]["data"]["value"] == pytest.approx(12300.5)
        assert saga["q_mrr_active"]["body"]["data"]["value"] == pytest.approx(2897)
        assert saga["q_revenue"]["body"]["data"]["value"] == pytest.approx(880)
        assert saga["q_net_margin"]["body"]["data"]["value"] == pytest.approx((125000 - 1500) / 125000)
        # 线上GMV占比（原始比率；×100 为前端展示职责）
        assert saga["q_online_ratio"]["body"]["data"]["value"] == pytest.approx(
            (100.5 + 12000) / 12300.5, rel=1e-6
        )


# ---------------------------------------------------------------- 行为细节


class TestBehaviors:
    def test_template_metric_in_catalog_and_search(self, client):
        resp = client.get("/api/metrics", params={"search": "客单价"})
        codes = [m["code"] for m in resp.json()["data"]]
        assert "rst_avg_check" in codes and "ecom_avg_ticket" in codes
        resp = client.get("/api/metrics", params={"search": "GMV"})
        assert any(m["code"] == "ecom_gmv_paid" for m in resp.json()["data"])

    def test_import_codes_filter(self, client):
        # 已全量导入过 → 指定单条重导入仍幂等 skipped
        result = _import(client, industries=["general"], codes=["gen_revenue"])
        row = _by_industry(result, "general")
        assert row["created"] == 0 and row["skipped"] == 1

    def test_import_unknown_industry(self, client):
        resp = client.post("/api/templates/import", json={"industries": ["nope"]})
        assert resp.status_code == 404

    def test_import_rejects_unknown_fields(self, client):
        resp = client.post("/api/templates/import", json={"where": "?"})
        assert resp.status_code == 400

    def test_archive_written_for_active_only(self, client):
        """active 指标有当前版编译存档；disabled/requires 无存档。"""
        data = client.get("/api/templates/ecommerce").json()["data"]
        by_code = {m["code"]: m for m in data["metrics"]}
        ok, missing = 0, 0
        for code, m in by_code.items():
            mid = m["imported_metric_id"]
            resp = client.get(f"/api/metrics/{mid}/sql")
            if m["imported_status"] == "active":
                assert resp.status_code == 200, code
                ok += 1
            else:
                assert resp.status_code != 200, code
                missing += 1
        assert ok == 24 and missing == 3

    def test_authz_viewer(self, client):
        viewer_headers = create_test_user(client, "tpl_viewer_01", role="viewer")
        resp = client.get("/api/templates/industries", headers=viewer_headers)
        assert resp.status_code == 200  # 读接口登录即可
        resp = client.post("/api/templates/import", headers=viewer_headers, json={})
        assert resp.status_code == 403
        # 未登录（显式覆盖会话级 admin 头为空）
        resp = client.get("/api/templates/industries", headers={"Authorization": ""})
        assert resp.status_code == 401
