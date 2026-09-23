"""AI 能力 P1 批测试：算写分离引擎单测 + 三接口无 LLM 降级 + 口径白名单校验。

验收红线：
- 算写分离：LLM 只见 {{ref:KEY}}，回填由服务端完成，裸数字/未知 ref 句子被剔除；
- 降级：LLM 未配置/失败时接口仍 200，绝不 500；
- 口径助手：aggregation 白名单 + column 必须真实存在，非法 → 40000。
"""

from __future__ import annotations

import io
import pytest
import uuid
from datetime import date, timedelta

from app.domain import ai_narrative as an_mod
from app.domain.ai import attribute_interpretation as ati_mod
from app.domain.ai import calc_notes as cn_mod
from app.domain.ai import dashboard_summary as ds_mod

# ---------------------------------------------------------------- 测试物料


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows() -> list[str]:
    """三列日数据：channel×region 双维度；平时每日 online+offline≈1000，突刺日 5000。"""
    lines = ["d,channel,region,amount"]
    d = date(2026, 1, 5)
    end = date(2026, 3, 15)
    while d <= end:
        noise = (d.toordinal() % 5) * 10
        region = "east" if d.toordinal() % 2 == 0 else "west"
        if d == date(2026, 3, 15):
            lines.append(f"{d.isoformat()},online,east,3000")
            lines.append(f"{d.isoformat()},offline,west,2000")
        else:
            lines.append(f"{d.isoformat()},online,{region},{600 + noise}")
            lines.append(f"{d.isoformat()},offline,{region},{400 + noise}")
        d += timedelta(days=1)
    return lines


def _make_env(client):
    sfx = _suffix()
    ds_name = f"ai_ds_{sfx}"
    csv = "\n".join(_build_rows()) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"ai_daily_{sfx}",
        "name": "AI日销",
        "calc_rule": {
            "base_aggregation": "sum",
            "source": {"table": ds_name, "column": "amount"},
        },
    })
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]
    return {"metric": metric, "ds_name": ds_name, "sfx": sfx}


def _make_env_tree(client):
    """带两层常用维度（channel→region）的指标：归因解读测试专用。"""
    sfx = _suffix()
    ds_name = f"ai_dst_{sfx}"
    csv = "\n".join(_build_rows()) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"ai_tree_{sfx}",
        "name": "AI树销",
        "calc_rule": {
            "base_aggregation": "sum",
            "source": {"table": ds_name, "column": "amount"},
        },
        "dimensions": ["channel", "region"],
    })
    assert resp.status_code == 200, resp.text
    return {"metric": resp.json()["data"], "ds_name": ds_name, "sfx": sfx}


def _enable_anomaly(client, metric_id: int) -> None:
    """为异动解释测试开启该指标的异动检测配置（保守档）。"""
    client.put(f"/api/metrics/{metric_id}/anomaly-config", json={
        "z_threshold": 3.0, "min_samples": 6, "enabled": True,
    })


def _dataset_id(client, ds_name: str) -> int:
    ds_list = client.get("/api/datasets").json()["data"]
    return next(d["id"] for d in ds_list if d["name"] == ds_name)


def _cleanup(client, env):
    # 关闭该指标的异动配置（best-effort），避免遗留 enabled 配置行污染共享会话库
    try:
        client.put(f"/api/metrics/{env['metric']['id']}/anomaly-config", json={"enabled": False})
    except Exception:
        pass
    client.delete(f"/api/metrics/{env['metric']['id']}")
    client.delete(f"/api/datasets/{_dataset_id(client, env['ds_name'])}")


_FAKE_LLM = {"base_url": "mock", "api_key": "k", "model": "m"}


# ---------------------------------------------------------------- 引擎单测


class TestEnginePrimitives:
    def test_audit_accepts_valid_ref(self):
        assert an_mod.audit_sentence("本期 {{ref:m1_value}} 增长", {"m1_value"}) is True

    def test_audit_rejects_unknown_ref(self):
        assert an_mod.audit_sentence("本期 {{ref:ghost}} 增长", {"m1_value"}) is False

    def test_audit_rejects_bare_digit(self):
        assert an_mod.audit_sentence("本期 123 增长", {"m1_value"}) is False

    def test_audit_accepts_plain_text(self):
        assert an_mod.audit_sentence("本期整体增长", {"m1_value"}) is True

    def test_backfill_replaces_refs(self):
        table = {"m1_value": ("销售额本期值", "1,234.5")}
        assert an_mod.backfill("值为 {{ref:m1_value}}", table) == "值为 1,234.5"

    def test_backfill_keeps_plain_text(self):
        table = {"m1_value": ("x", "1.0")}
        assert an_mod.backfill("无引用文本", table) == "无引用文本"

    def test_fmt_metric_value(self):
        assert an_mod.fmt_metric_value(1234.5) == "1,234.5"
        assert an_mod.fmt_metric_value(None) == "—"
        assert an_mod.fmt_metric_value("abc") == "—"

    def test_fmt_pct(self):
        assert an_mod.fmt_pct(12.34) == "12.3%"
        assert an_mod.fmt_pct(None) == "—"


class TestAuditGaps:
    """红线收紧（P3）：中文数量词与畸形占位符缺口已封堵，原 xfail 转正。

    设计红线：去掉占位符后残留任何数字（含中文数量表述）整句剔除；
    畸形占位符（单花括号）不得以明文残留。同时固化防误杀边界：
    「进一步/一致/三线城市」等不含数量后缀的正常文案必须放行。
    """

    def test_audit_rejects_fullwidth_digit(self):
        # 固化：全角数字应被拦截（证明 \d 覆盖 Unicode 十进制数字）
        assert an_mod.audit_sentence("前３名应被剔除", {"m1"}) is False

    def test_audit_rejects_chinese_numeral(self):
        # P3 收紧：中文数量词不再绕过审计
        assert an_mod.audit_sentence("环比上升约三成，需关注", {"m1_value"}) is False

    @pytest.mark.parametrize(
        "sentence",
        [
            "占比接近一半，需要关注",
            "销售额环比翻倍，主要由线上贡献",
            "整体约为去年的三分之一",
            "利润率相当于打了八折",
        ],
    )
    def test_audit_rejects_chinese_quantity_variants(self, sentence):
        assert an_mod.audit_sentence(sentence, set()) is False

    @pytest.mark.parametrize(
        "sentence",
        [
            "整体持续向好，需保持关注",  # 「持续」无数量后缀
            "改善仍在进一步深化",        # 「进一步」不放数量
            "三线城市贡献最大，{{ref:m1_name}}整体上升",  # 「三线」非数量
            "各区域表现较为一致",        # 「一致」
        ],
    )
    def test_audit_keeps_normal_chinese_text(self, sentence):
        assert an_mod.audit_sentence(sentence, {"m1_name"}) is True

    def test_audit_rejects_malformed_placeholder(self):
        # 畸形占位符（单花括号/缺右括号）会明文残留 → 审计剔除
        assert an_mod.audit_sentence("值 {ref:m1_value} 结束", {"m1_value"}) is False
        assert an_mod.audit_sentence("值 {{ref:m1_value 结束", {"m1_value"}) is False

    def test_backfill_does_not_leak_malformed_placeholder(self):
        table = {"m1_value": ("x", "9.0")}
        out = an_mod.backfill("值 {ref:m1_value} 结束", table)
        assert "{" not in out and "}" not in out
        assert "9.0" in out  # key 在表内 → 正常回填

    def test_backfill_strips_unknown_malformed_placeholder(self):
        out = an_mod.backfill("值 {ref:ghost_key} 结束", {})
        assert "{" not in out and "}" not in out
        assert "ghost_key" not in out


class TestEngineNarrative:
    def test_strips_bad_sentences_and_backfills(self, monkeypatch):
        monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

        def fake_chat(config, system, user, timeout=60.0, retries=0):
            return {"sections": [{
                "section": "overview",
                "sentences": [
                    "本期 {{ref:m1_name}} 为 {{ref:m1_value}}，环比 {{ref:m1_mom}}。",
                    "未知引用 {{ref:ghost}} 应被剔除。",
                    "裸数字 123 应被剔除。",
                ],
            }]}

        monkeypatch.setattr(an_mod, "chat_json", fake_chat)
        ref_table = {
            "m1_name": ("指标名", "销售额"),
            "m1_value": ("销售额本期值", "1,234.5"),
            "m1_mom": ("销售额环比", "+12.3%"),
        }
        out = an_mod.llm_narrative(
            None, None,
            ref_table=ref_table,
            sections_to_write=["overview"],
            system_prompt="x",
            user_payload={"rule_narrative": [{"section": "overview", "sentences": ["规则句兜底"]}]},
        )
        assert out is not None
        sec = out["sections"][0]
        assert sec["source"] == "llm"
        assert len(sec["sentences"]) == 1
        sent = sec["sentences"][0]
        assert "销售额" in sent and "1,234.5" in sent and "12.3%" in sent
        assert "ghost" not in sent and "123" not in sent

    def test_degrades_to_rule_when_all_stripped(self, monkeypatch):
        monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

        def fake_chat(config, system, user, timeout=60.0, retries=0):
            return {"sections": [{"section": "overview", "sentences": ["裸数字 999 被剔除"]}]}

        monkeypatch.setattr(an_mod, "chat_json", fake_chat)
        out = an_mod.llm_narrative(
            None, None,
            ref_table={},
            sections_to_write=["overview"],
            system_prompt="x",
            user_payload={"rule_narrative": [{"section": "overview", "sentences": ["规则句兜底"]}]},
        )
        # 全部被剔除 → 无 LLM 可用句 → 返回 None（调用方整段走规则句）
        assert out is None


# ---------------------------------------------------------------- 接口无 LLM 降级


class TestDashboardSummaryNoLLM:
    def test_degrades_without_llm(self, client):
        env = _make_env(client)
        try:
            r = client.get("/api/ai/dashboard-summary", params={
                "start": "2026-03-09", "end": "2026-03-15",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is False
            assert d["source"] in ("rule", "none")
            assert isinstance(d["rule_text"], str) and d["rule_text"]
        finally:
            _cleanup(client, env)


class TestAnomalyHypothesisNoLLM:
    def test_degrades_without_llm(self, client):
        env = _make_env(client)
        try:
            _enable_anomaly(client, env["metric"]["id"])
            r = client.get("/api/ai/anomaly-hypothesis", params={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is False
            assert d["has_anomaly"] is True
            assert d["source"] == "rule"
            assert d["fallback_action_hint"]
        finally:
            _cleanup(client, env)


class TestCalcNotesNoLLM:
    def test_valid_request_returns_empty_without_llm(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "amount", "aggregation": "sum",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is False
            assert d["name"] == ""
            assert d["calc_notes"]["rationale"] == ""
        finally:
            _cleanup(client, env)

    def test_invalid_aggregation_400(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "amount", "aggregation": "avg_of",
            })
            assert r.status_code == 400
            assert r.json()["code"] == 40000
        finally:
            _cleanup(client, env)

    def test_invalid_column_400(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "nonexistent_col", "aggregation": "sum",
            })
            assert r.status_code == 400
            assert r.json()["code"] == 40000
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- 接口 LLM 路径（mock）


class TestDashboardSummaryLLM:
    def test_llm_path_backfills(self, client, monkeypatch):
        env = _make_env(client)
        try:
            monkeypatch.setattr(ds_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)
            monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {"sections": [{
                    "section": "overview",
                    "sentences": ["{{ref:m1_name}} 本期 {{ref:m1_value}}，环比 {{ref:m1_mom}}。"],
                }]}

            monkeypatch.setattr(an_mod, "chat_json", fake_chat)
            r = client.get("/api/ai/dashboard-summary", params={
                "start": "2026-03-09", "end": "2026-03-15",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is True
            assert d["source"] == "llm"
            txt = d["sections"][0]["sentences"][0]
            # 占位符已被真实显示文本回填（指标名为 AI日销）
            assert "{{ref:" not in txt
            assert "AI日销" in txt
        finally:
            _cleanup(client, env)


class TestCalcNotesLLM:
    def test_returns_suggestions(self, client, monkeypatch):
        env = _make_env(client)
        try:
            monkeypatch.setattr(cn_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {
                    "name": "日销售额",
                    "aliases": ["销售额"],
                    "calc_notes": {
                        "rationale": "每日销售合计",
                        "alternatives": ["按渠道拆分"],
                        "pitfalls": "注意空值",
                    },
                }

            monkeypatch.setattr(cn_mod, "chat_json", fake_chat)
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "amount", "aggregation": "sum",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is True
            assert d["name"] == "日销售额"
            assert d["calc_notes"]["rationale"] == "每日销售合计"
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- 权限（B9 契约：受限指标不泄露）


class TestAiPermission:
    """主理人验证关卡发现（P0）：/ai 接口必须与 /query/anomaly 同源做可见性校验——
    受限指标对无权用户 403，且不进看板速览结果（不泄露名称/方向）。"""

    def _restrict(self, client, metric_id: int) -> None:
        r = client.put(
            f"/api/auth/metrics/{metric_id}/restrictions",
            json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
        )
        assert r.status_code == 200, r.text

    def test_anomaly_hypothesis_restricted_403(self, client):
        from tests.conftest import create_test_user

        env = _make_env(client)
        try:
            self._restrict(client, env["metric"]["id"])
            viewer = create_test_user(client, f"vw_ai_{env['sfx']}", role="viewer")
            r = client.get(
                "/api/ai/anomaly-hypothesis",
                params={
                    "metric_id": env["metric"]["id"],
                    "start": "2026-03-09", "end": "2026-03-15",
                },
                headers=viewer,
            )
            assert r.status_code == 403, r.text
            assert r.json()["code"] == 40300
        finally:
            _cleanup(client, env)

    def test_dashboard_summary_excludes_restricted(self, client):
        from tests.conftest import create_test_user

        env = _make_env(client)
        try:
            self._restrict(client, env["metric"]["id"])
            viewer = create_test_user(client, f"vw_ds_{env['sfx']}", role="viewer")
            r = client.get(
                "/api/ai/dashboard-summary",
                params={"start": "2026-03-09", "end": "2026-03-15"},
                headers=viewer,
            )
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            blob = d.get("rule_text", "") + "".join(
                "".join(s.get("sentences") or [])
                for s in d.get("sections") or []
            )
            assert "AI日销" not in blob
        finally:
            _cleanup(client, env)

    def test_anomaly_hypothesis_admin_unaffected(self, client):
        """admin 未被登记限制时不受影响（既有行为回归）。"""
        env = _make_env(client)
        try:
            _enable_anomaly(client, env["metric"]["id"])
            r = client.get(
                "/api/ai/anomaly-hypothesis",
                params={
                    "metric_id": env["metric"]["id"],
                    "start": "2026-03-09", "end": "2026-03-15",
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["data"]["has_anomaly"] is True
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- P2 #4 字段语义标注


class TestSemanticAnnotations:
    from app.domain.ai import semantic_annotations as sa_mod

    def test_no_llm_returns_empty(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/dataset-semantic-annotations",
                            json={"dataset_id": ds_id})
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is False
            assert d["annotations"] == {}
        finally:
            _cleanup(client, env)

    def test_llm_filters_invalid_keys(self, client, monkeypatch):
        from app.domain.ai import semantic_annotations as sa

        env = _make_env(client)
        try:
            monkeypatch.setattr(sa, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {"annotations": {
                    "channel": "销售渠道",
                    "ghost_col": "不应出现的列",
                }}

            monkeypatch.setattr(sa, "chat_json", fake_chat)
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/dataset-semantic-annotations",
                            json={"dataset_id": ds_id})
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["annotations"] == {"channel": "销售渠道"}  # 非法键被丢弃
        finally:
            _cleanup(client, env)

    def test_save_roundtrip(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.put(f"/api/datasets/{ds_id}/semantic-annotations",
                           json={"annotations": {"channel": "销售渠道", "amount": "订单金额（元）"}})
            assert r.status_code == 200, r.text
            detail = client.get(f"/api/datasets/{ds_id}").json()["data"]
            assert detail["column_semantics"] == {
                "channel": "销售渠道", "amount": "订单金额（元）",
            }
            # 整组替换语义：空 map 清空
            r = client.put(f"/api/datasets/{ds_id}/semantic-annotations",
                           json={"annotations": {}})
            assert r.status_code == 200, r.text
            detail = client.get(f"/api/datasets/{ds_id}").json()["data"]
            assert detail["column_semantics"] == {}
        finally:
            _cleanup(client, env)

    def test_save_invalid_column_400(self, client):
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.put(f"/api/datasets/{ds_id}/semantic-annotations",
                           json={"annotations": {"ghost": "x"}})
            assert r.status_code == 400
            assert r.json()["code"] == 40000
        finally:
            _cleanup(client, env)

    def test_save_requires_admin(self, client):
        from tests.conftest import create_test_user

        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            viewer = create_test_user(client, f"vw_sem_{env['sfx']}", role="viewer")
            r = client.put(f"/api/datasets/{ds_id}/semantic-annotations",
                           json={"annotations": {"channel": "x"}}, headers=viewer)
            assert r.status_code == 403, r.text
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- P2 #6 归因解读


class TestAttributeInterpretation:
    def test_no_llm_returns_null(self, client):
        env = _make_env_tree(client)
        try:
            r = client.post("/api/ai/attribute-interpretation", json={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
                "dimensions": ["channel", "region"], "compare": "mom",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["llm_configured"] is False
            assert d["interpretation"] is None
            assert d["reason"] == "llm_not_configured"  # 前端据此显降级提示
        finally:
            _cleanup(client, env)

    def test_llm_backfills_single_sentence(self, client, monkeypatch):
        env = _make_env_tree(client)
        try:
            # 归因解读模块从 app.infra.llm 直接导入 resolve_llm_config，
            # 须 patch 其自身命名空间（patch an_mod 不影响它）
            monkeypatch.setattr(ati_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)
            monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {"sections": [{
                    "section": "interpretation",
                    "sentences": [
                        "{{ref:metric_name}}整体{{ref:direction}}，"
                        "主要由{{ref:g1_name}}贡献（{{ref:g1_pct}}）。",
                        "裸数字 42 应被剔除。",
                    ],
                }]}

            monkeypatch.setattr(an_mod, "chat_json", fake_chat)
            r = client.post("/api/ai/attribute-interpretation", json={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
                "dimensions": ["channel", "region"], "compare": "mom",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["source"] == "llm"
            interp = d["interpretation"]
            assert interp and "{{ref:" not in interp and "42" not in interp
            assert "AI树销" in interp
        finally:
            _cleanup(client, env)

    def test_single_dimension_rejected(self, client):
        env = _make_env_tree(client)
        try:
            r = client.post("/api/ai/attribute-interpretation", json={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
                "dimensions": ["channel"], "compare": "mom",
            })
            assert r.status_code == 400  # 归因树契约：至少 2 层
        finally:
            _cleanup(client, env)

    def test_section_name_mismatch_tolerated(self, client, monkeypatch):
        """LLM 章节名不符（如返回中文 section 名）时取唯一章节，不整段降级。"""
        env = _make_env_tree(client)
        try:
            monkeypatch.setattr(ati_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)
            monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {"sections": [{
                    "section": "解读",  # 非 interpretation
                    "sentences": ["{{ref:metric_name}}整体{{ref:direction}}。"],
                }]}

            monkeypatch.setattr(an_mod, "chat_json", fake_chat)
            r = client.post("/api/ai/attribute-interpretation", json={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
                "dimensions": ["channel", "region"], "compare": "mom",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["source"] == "llm"
            assert d["interpretation"] and "AI树销" in d["interpretation"]
        finally:
            _cleanup(client, env)

    def test_candidate_sentences_first_valid_wins(self, client, monkeypatch):
        """多候选句子：首个含裸数字被剔除，取第一个合格候选（单句场景健壮性）。"""
        env = _make_env_tree(client)
        try:
            monkeypatch.setattr(ati_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)
            monkeypatch.setattr(an_mod, "resolve_llm_config", lambda *a, **k: _FAKE_LLM)

            def fake_chat(config, system, user, timeout=60.0, retries=0):
                return {"sections": [{
                    "section": "interpretation",
                    "sentences": [
                        "主要由前3大分组贡献。",  # 裸数字 → 剔除
                        "{{ref:g1_name}}贡献最高，占{{ref:g1_pct}}。{{ref:g9_x}}",  # 未知 ref → 剔除
                        "{{ref:metric_name}}整体{{ref:direction}}，主要由{{ref:g1_name}}贡献（{{ref:g1_pct}}）。",
                    ],
                }]}

            monkeypatch.setattr(an_mod, "chat_json", fake_chat)
            r = client.post("/api/ai/attribute-interpretation", json={
                "metric_id": env["metric"]["id"],
                "start": "2026-03-09", "end": "2026-03-15",
                "dimensions": ["channel", "region"], "compare": "mom",
            })
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["source"] == "llm"
            interp = d["interpretation"]
            assert interp.startswith("AI树销") and "3" not in interp and "{{ref:" not in interp
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- P4 反馈闭环与语义下游


class TestAiFeedback:
    def test_feedback_roundtrip_and_summary(self, client):
        """有用/无用反馈落库 + admin 质量概览（按功能聚合、差评率、最近 bad case）。"""
        r = client.post("/api/ai/feedback", json={
            "kind": "dashboard_summary", "target": "period:2026-01-01~2026-01-31",
            "rating": "down", "correction": "数字对不上",
        })
        assert r.status_code == 200, r.text
        assert r.json()["data"]["recorded"] is True
        r2 = client.post("/api/ai/feedback", json={
            "kind": "dashboard_summary", "rating": "up",
        })
        assert r2.json()["data"]["recorded"] is True

        s = client.get("/api/ai/feedback/summary")
        assert s.status_code == 200, s.text
        d = s.json()["data"]
        row = next(x for x in d["by_kind"] if x["kind"] == "dashboard_summary")
        assert row["up"] >= 1 and row["down"] >= 1
        assert row["down_rate"] > 0
        assert any(b["correction"] == "数字对不上" for b in d["recent_bad"])

    def test_feedback_rejects_unknown_kind(self, client):
        r = client.post("/api/ai/feedback", json={"kind": "whatever", "rating": "up"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["recorded"] is False

    def test_feedback_summary_requires_admin(self, client):
        from tests.conftest import create_test_user

        vw = create_test_user(client, f"vw_fb_{_suffix()}", role="viewer")
        r = client.get("/api/ai/feedback/summary", headers=vw)
        assert r.status_code == 403

    def test_feedback_summary_scoped_by_project(self, client):
        """概览按项目过滤：A 项目的反馈不混入 B 项目视图；NULL 存量归默认项目。"""
        import uuid

        pa = client.post("/api/projects", json={"name": f"fbpa_{uuid.uuid4().hex[:6]}"}).json()["data"]
        pb = client.post("/api/projects", json={"name": f"fbpb_{uuid.uuid4().hex[:6]}"}).json()["data"]
        try:
            # NULL（不带 project_id）→ 默认项目；A/B 各一条 down
            client.post("/api/ai/feedback", json={"kind": "ask", "rating": "down", "correction": "默认项目"})
            client.post("/api/ai/feedback", json={
                "kind": "ask", "rating": "down", "correction": "项目A", "project_id": pa["id"],
            })
            client.post("/api/ai/feedback", json={
                "kind": "ask", "rating": "down", "correction": "项目B", "project_id": pb["id"],
            })

            sa = client.get("/api/ai/feedback/summary", params={"project_id": pa["id"]}).json()["data"]
            assert any(b["correction"] == "项目A" for b in sa["recent_bad"])
            assert not any(b["correction"] in ("项目B", "默认项目") for b in sa["recent_bad"])

            # 缺省 → 默认项目：NULL 行可见，A/B 项目行不可见
            sd = client.get("/api/ai/feedback/summary").json()["data"]
            assert any(b["correction"] == "默认项目" for b in sd["recent_bad"])
            assert not any(b["correction"] in ("项目A", "项目B") for b in sd["recent_bad"])
        finally:
            client.delete(f"/api/projects/{pa['id']}")
            client.delete(f"/api/projects/{pb['id']}")


class TestSemanticsDownstream:
    def test_calc_notes_carries_confirmed_semantics(self, client):
        """P4-1：已人工确认的字段语义随口径助手返回（无标注时为空串，零退化）。"""
        env = _make_env(client)
        try:
            ds_id = _dataset_id(client, env["ds_name"])
            r = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "amount", "aggregation": "sum",
            })
            assert r.status_code == 200, r.text
            assert r.json()["data"]["confirmed_semantics"] == ""

            client.put(f"/api/datasets/{ds_id}/semantic-annotations",
                       json={"annotations": {"amount": "销售金额，单位元"}})
            r2 = client.post("/api/ai/metric-calc-notes", json={
                "dataset_id": ds_id, "column": "amount", "aggregation": "sum",
            })
            assert r2.status_code == 200, r2.text
            assert r2.json()["data"]["confirmed_semantics"] == "销售金额，单位元"
        finally:
            _cleanup(client, env)


# ---------------------------------------------------------------- P2 #5 问数动态推荐


class TestAskDynamicSuggestions:
    def test_dynamic_anomaly_suggestion_first(self, client):
        """有异动通知时推荐首位为动态异动问句（服务端预格式化，无裸数字审计问题）。"""
        env = _make_env(client)
        try:
            _enable_anomaly(client, env["metric"]["id"])
            # 项目级扫描 → 异常且要紧 → 通知落库（admin 为收件人）
            r = client.get("/api/query/anomalies")
            assert r.status_code == 200, r.text
            scan = r.json()["data"]
            assert scan["counts"].get("abnormal", 0) >= 1
            # 建议：首位应为动态推荐（含指标名与方向词），长度契约 ≤60
            sug = client.get("/api/query/ask/suggestions").json()["data"]
            assert isinstance(sug, list) and sug
            assert any("AI日销" in s and ("突增" in s or "骤降" in s) for s in sug)
            assert all(len(s) <= 60 for s in sug)
        finally:
            _cleanup(client, env)
