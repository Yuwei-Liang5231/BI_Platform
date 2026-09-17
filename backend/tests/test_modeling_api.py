"""B13-1 自动建模建议测试：关系建议（值域重叠+命名相似）与指标候选启发。

B13-2 追加：高基数截断采样（sampled 标注）、孤立巧合降权警示、
布尔/枚举数值列 sum 噪音过滤、LLM 语义复审（mock）。

验收主线：
- 事实表↔维度表关系被建议（高值域重叠），方向 many_to_one 正确；
- 无关列不产生建议（双低过滤）；
- 已登记关系标注 existing（不重复推荐入库）；
- 指标候选：无日期列的数据集跳过；数值列 sum / 高基数文本 count_distinct；
- 只读铁律：建议接口不写任何业务表。
"""

from __future__ import annotations

import io
import uuid


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _upload(client, name: str, csv: str, project_id: int):
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": name, "project_id": str(project_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _mk_env(client):
    """数据集上传到独立项目——与其他测试文件的默认项目数据完全隔离。"""
    sfx = _suffix()
    pid = client.post("/api/projects", json={"name": f"b13_{sfx}"}).json()["data"]["id"]
    # 事实表：orders（product_id 值域与 products.id 完全重叠）
    orders = "order_id,product_id,amount,d\n" + "\n".join(
        f"o{i},p{i % 50},{100 + i},2026-01-{(i % 28) + 1:02d}" for i in range(200)
    ) + "\n"
    # 维度表：products（id 与 orders.product_id 重叠，名称不同但值同）
    products = "id,product_name\n" + "\n".join(
        f"p{i},商品{i}" for i in range(50)
    ) + "\n"
    # 无关表：logs（device 列与上两者值域零重叠、命名无关）
    logs = "log_id,device,ts\n" + "\n".join(
        f"l{i},dev{i},2026-01-0{(i % 9) + 1}" for i in range(30)
    ) + "\n"
    return {
        "sfx": sfx,
        "pid": pid,
        "orders": _upload(client, f"b13_orders_{sfx}", orders, pid),
        "products": _upload(client, f"b13_products_{sfx}", products, pid),
        "logs": _upload(client, f"b13_logs_{sfx}", logs, pid),
    }


def _cleanup(client, env):
    for key, ds in env.items():
        if key in ("pid", "sfx") or not isinstance(ds, dict):
            continue
        client.delete(f"/api/datasets/{ds['id']}")
    client.delete(f"/api/projects/{env['pid']}")


def _suggestions(client, env):
    resp = client.post("/api/modeling/suggestions", json={"project_id": env["pid"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestRelationSuggestions:
    def test_fact_dim_relation_found_with_direction(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            hits = [
                r for r in data["relations"]
                if {r["from_dataset"], r["to_dataset"]} == {
                    env["orders"]["name"], env["products"]["name"],
                }
                and r["from_column"] == "product_id"
            ]
            assert hits, f"应建议 orders.product_id → products 的关系：{data['relations']}"
            top = hits[0]
            assert top["relation_type"] == "many_to_one"
            assert top["to_column"] == "id"
            assert top["evidence"]["overlap_ratio"] >= 0.5
            assert top["score"] >= 0.5
            assert top["existing"] is False
        finally:
            _cleanup(client, env)

    def test_unrelated_pairs_not_suggested(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            bad = [
                r for r in data["relations"]
                if env["logs"]["name"] in {r["from_dataset"], r["to_dataset"]}
            ]
            assert not bad, f"零重叠且命名无关的列不应产生建议：{bad}"
        finally:
            _cleanup(client, env)

    def test_existing_relation_marked(self, client):
        env = _mk_env(client)
        try:
            # 人工先登记 orders.product_id → products.id
            orders_id = env["orders"]["id"]
            products_id = env["products"]["id"]
            resp = client.post(f"/api/datasets/{orders_id}/relations", json={
                "target_dataset_id": products_id,
                "from_column": "product_id",
                "target_column": "id",
                "relation_type": "many_to_one",
            })
            assert resp.status_code == 200, resp.text
            data = _suggestions(client, env)
            hit = next(
                r for r in data["relations"]
                if r["from_dataset"] == env["orders"]["name"]
                and r["from_column"] == "product_id"
            )
            assert hit["existing"] is True
        finally:
            _cleanup(client, env)


class TestMetricSuggestions:
    def test_numeric_and_high_card_candidates(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            m = {
                (c["dataset"], c["column"]): c
                for c in data["metrics"]
                if c["dataset"] == env["orders"]["name"]
            }
            # 数值列 → sum 候选
            assert m[(env["orders"]["name"], "amount")]["aggregation"] == "sum"
            # 高基数文本列（order_id 200 值）→ count_distinct
            assert m[(env["orders"]["name"], "order_id")]["aggregation"] == "count_distinct"
            # 日期列本身不产出候选
            assert (env["orders"]["name"], "d") not in m
            # 无日期列的 products 不产生候选
            assert not any(
                c["dataset"] == env["products"]["name"] for c in data["metrics"]
            )
            assert data["disclaimer"]
        finally:
            _cleanup(client, env)

    def test_suggestions_are_readonly(self, client):
        """只读铁律：连续调用两次结果一致（无副作用入库）。"""
        env = _mk_env(client)
        try:
            a = _suggestions(client, env)
            b = _suggestions(client, env)
            assert a["relations"] == b["relations"]
            # 建议的关系未出现在已登记关系列表
            for key in ("orders", "products", "logs"):
                rels = client.get(
                    f"/api/datasets/{env[key]['id']}/relations"
                ).json()["data"]
                assert rels == [] or rels is None or (
                    isinstance(rels, list) and len(rels) == 0
                )
        finally:
            _cleanup(client, env)


class TestSampledHighCardinality:
    """B13-2：护栏外（>5000 基数）列截断采样参与关系建议，标注 sampled。"""

    def test_high_cardinality_key_detected_with_sampled_flag(self, client):
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"b13hc_{sfx}"}).json()["data"]["id"]
        # users.user_id 与 orders.user_id 各 5001 个不同值——旧护栏（5000）下两侧均被整列排除 → 漏检
        n = 5001
        users_csv = "user_id,region\n" + "\n".join(f"u{i},east" for i in range(n)) + "\n"
        orders_csv = (
            "order_id,user_id,amount,d\n"
            + "\n".join(f"o{i},u{i},{100 + i},2026-01-01" for i in range(n))
            + "\n"
        )
        env = {
            "pid": pid,
            "users": _upload(client, f"b13hc_users_{sfx}", users_csv, pid),
            "orders": _upload(client, f"b13hc_orders_{sfx}", orders_csv, pid),
        }
        try:
            data = _suggestions(client, env)
            hits = [
                r for r in data["relations"]
                if {r["from_dataset"], r["to_dataset"]}
                == {env["users"]["name"], env["orders"]["name"]}
                and r["from_column"] == "user_id" and r["to_column"] == "user_id"
            ]
            assert hits, f"高基数键（>5000）经截断采样后应被建议：{data['relations']}"
            ev = hits[0]["evidence"]
            assert ev["sampled"] is True
            assert ev["overlap_ratio"] >= 0.99
        finally:
            _cleanup(client, env)


class TestMetricNoiseFilter:
    """B13-2：布尔/枚举数值列排除 sum 候选。"""

    def test_boolean_and_enum_numeric_excluded_from_sum(self, client):
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"b13nf_{sfx}"}).json()["data"]["id"]
        csv = (
            "order_id,amount,auto_renew,level,d\n"
            + "\n".join(
                f"o{i},{100 + i},{i % 2},{i % 5},2026-01-{(i % 28) + 1:02d}"
                for i in range(100)
            )
            + "\n"
        )
        env = {"pid": pid, "orders": _upload(client, f"b13nf_orders_{sfx}", csv, pid)}
        try:
            data = _suggestions(client, env)
            got = {
                (c["column"], c["aggregation"])
                for c in data["metrics"]
                if c["dataset"] == env["orders"]["name"]
            }
            assert ("auto_renew", "sum") not in got, "布尔列（0/1）不应建议 sum"
            assert ("level", "sum") not in got, "等级列（枚举数值）不应建议 sum"
            assert ("amount", "sum") in got
        finally:
            _cleanup(client, env)


class TestIsolatedPairDownweight:
    """B13-2：同数据集对间仅一条建议（孤立）时标注 isolated_pair。"""

    def test_isolated_enum_pair_flagged(self, client):
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"b13iso_{sfx}"}).json()["data"]["id"]
        # 两个不同业务的表共享同名枚举列 region（值域巧合相同），无其他关联佐证
        # （键列名故意错开：a_key/b_key 命名不相似、值域不重叠，避免产生第二条建议）
        a_csv = "a_key,region\nx1,east\nx2,west\nx3,north\n"
        b_csv = "b_key,region\ny1,east\ny2,west\ny3,north\n"
        env = {
            "pid": pid,
            "a": _upload(client, f"b13iso_a_{sfx}", a_csv, pid),
            "b": _upload(client, f"b13iso_b_{sfx}", b_csv, pid),
        }
        try:
            data = _suggestions(client, env)
            hits = [
                r for r in data["relations"]
                if {r["from_dataset"], r["to_dataset"]}
                == {env["a"]["name"], env["b"]["name"]}
                and r["from_column"] == "region"
            ]
            assert hits, "枚举巧合列应仍输出（交人工判断），只是降权警示"
            assert hits[0]["evidence"].get("isolated_pair") is True
        finally:
            _cleanup(client, env)


class TestMetricTimeField:
    """B13-2.1 修复回归：多日期列数据集的候选必须携带 time_field，
    否则向导入库触发「保存即编译拒绝」400（用户实测报障）。"""

    def test_candidates_carry_time_field_and_import_succeeds(self, client):
        sfx = _suffix()
        pid = client.post("/api/projects", json={"name": f"b13tf_{sfx}"}).json()["data"]["id"]
        csv = (
            "ar_amount,snapshot_date,due_date\n"
            "100,2026-01-01,2026-02-01\n"
            "200,2026-01-02,2026-02-02\n"
            "350,2026-01-03,2026-02-03\n"
        )
        env = {"pid": pid, "wip": _upload(client, f"b13tf_wip_{sfx}", csv, pid)}
        try:
            data = _suggestions(client, env)
            cand = next(
                c for c in data["metrics"]
                if c["dataset"] == env["wip"]["name"] and c["column"] == "ar_amount"
            )
            assert cand["date_columns"] == ["snapshot_date", "due_date"]
            assert cand["time_field"] == "snapshot_date"
            # 向导同构 payload（带显式 time_field）应创建成功
            resp = client.post("/api/metrics", json={
                "code": f"b13tf_{sfx}_ar_sum",
                "name": "AR 合计",
                "calc_rule": {
                    "base_aggregation": "sum",
                    "source": {"table": env["wip"]["name"], "column": "ar_amount"},
                    "time_field": cand["time_field"],
                },
                "definition": "向导入库",
                "project_id": pid,
            })
            assert resp.status_code == 200, resp.text
        finally:
            _cleanup(client, env)


class TestLlmRelationReview:
    """B13-2：配置 LLM 时对关系建议附语义复审结论（mock）。"""

    def test_llm_review_attached(self, client, monkeypatch):
        import app.infra.llm as llm_mod

        monkeypatch.setattr(
            llm_mod,
            "resolve_llm_config",
            lambda db, settings: {"base_url": "http://mock", "api_key": "k", "model": "mock"},
        )

        def fake_chat(config, system, user, timeout=30.0):
            if "评审员" in system:  # 关系语义复审调用
                return {"reviews": [{"index": 0, "verdict": "likely", "reason": "同实体键"}]}
            return None  # 指标候选 LLM 提议调用：降级跳过

        monkeypatch.setattr(llm_mod, "chat_json", fake_chat)

        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            assert data["relations"], "应至少有一条关系建议"
            assert data["relations"][0]["llm_review"]["verdict"] == "likely"
            assert data["relations"][0]["llm_review"]["reason"] == "同实体键"
        finally:
            _cleanup(client, env)

    def test_no_llm_config_no_review(self, client):
        """未配置 LLM：建议照常输出，不带 llm_review 字段。"""
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            for r in data["relations"]:
                assert "llm_review" not in r
        finally:
            _cleanup(client, env)


class TestDatasetParticipationNotes:
    """B13-2 补充：每张表附「是否参与关系建议 + 原因」，向导明示缺席原因。"""

    def test_notes_carry_reasons(self, client):
        env = _mk_env(client)
        try:
            data = _suggestions(client, env)
            notes = {n["name"]: n for n in data["datasets"]}
            assert set(notes) == {env["orders"]["name"], env["products"]["name"], env["logs"]["name"]}
            # logs：device 列与上两者零重叠、命名无关 → 明示未参与原因
            logs_note = notes[env["logs"]["name"]]
            assert logs_note["relation_note"], "logs 未参与但无说明"
            # orders/products 参与建议 → 无缺席说明
            assert notes[env["orders"]["name"]]["relation_note"] == ""
            assert notes[env["products"]["name"]]["relation_note"] == ""
            # 列数统计可用（前端展示）
            assert notes[env["orders"]["name"]]["date_columns"] == 1
        finally:
            _cleanup(client, env)


class TestMetricBatchDelete:
    """指标管理多选/全选删除：逐条成功提交、失败不拖垮其余。"""

    def _mk_metrics(self, client, pid, table, n=3):
        ids = []
        for i in range(n):
            resp = client.post(
                "/api/metrics",
                json={
                    "code": f"b13bd_{uuid.uuid4().hex[:8]}_{i}",
                    "name": f"批量删除样例{i}",
                    "calc_rule": {
                        "base_aggregation": "sum",
                        "source": {"table": table, "column": "amount"},
                        "time_field": "d",
                    },
                    "project_id": pid,
                },
            )
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["data"]["id"])
        return ids

    def test_batch_delete_mixed_success_and_failure(self, client):
        env = _mk_env(client)
        try:
            pid = env["pid"]
            ids = self._mk_metrics(client, pid, env["orders"]["name"])
            # 1 个不存在 id 混入 → 失败单列，不拖垮其余
            resp = client.post("/api/metrics/batch-delete", json={"ids": ids + [99999999]})
            assert resp.status_code == 200, resp.text
            body = resp.json()["data"]
            assert sorted(body["deleted"]) == sorted(ids)
            assert len(body["failed"]) == 1 and body["failed"][0]["id"] == 99999999
            # 删除生效：列表 active 不再可见
            listing = client.get(f"/api/metrics?project_id={pid}").json()["data"]
            items = listing if isinstance(listing, list) else listing.get("items", [])
            assert all(m["id"] not in ids for m in items)
        finally:
            _cleanup(client, env)
