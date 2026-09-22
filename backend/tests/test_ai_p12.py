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
from app.domain.ai import calc_notes as cn_mod
from app.domain.ai import dashboard_summary as ds_mod

# ---------------------------------------------------------------- 测试物料


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows() -> list[str]:
    """双渠道日数据：平时每日 online+offline≈1000，突刺日 3000+2000=5000。"""
    lines = ["d,channel,amount"]
    d = date(2026, 1, 5)
    end = date(2026, 3, 15)
    while d <= end:
        noise = (d.toordinal() % 5) * 10
        online, offline = (3000, 2000) if d == date(2026, 3, 15) else (600 + noise, 400 + noise)
        lines.append(f"{d.isoformat()},online,{online}")
        lines.append(f"{d.isoformat()},offline,{offline}")
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
    """红线复核：审计正则对"裸数字"的覆盖边界（独立 QA 发现，待工程师修复）。

    设计红线：去掉占位符后残留任何数字（裸数字，含日期/数量）整句剔除。
    当前 ``BARE_DIGIT_RE = re.compile(r"\\d")`` 仅匹配 Unicode 十进制数字字符，
    **中文数字（一/三/成 等表意文字，非 Nd 类）不被覆盖**——LLM 可写"环比上升约三成"
    这类含数量的中文表述，绕过审计被原样放行，构成"LLM 偷产数量"的红线下钻缺口。
    （全角数字 ３、ASCII 日期 2024 均已被正确拦截，已用下方用例固化。）
    """

    def test_audit_rejects_fullwidth_digit(self):
        # 固化：全角数字应被拦截（证明 \\d 覆盖 Unicode 十进制数字）
        assert an_mod.audit_sentence("前３名应被剔除", {"m1"}) is False

    @pytest.mark.xfail(
        reason="已知缺口：中文数字（如'三成'）绕过裸数字审计，待收紧正则/加中文数字黑名单",
        strict=False,
    )
    def test_audit_rejects_chinese_numeral(self):
        # 期望（红线要求）：含中文数量词的句子应被剔除，当前实现放行 → xfail
        assert an_mod.audit_sentence("环比上升约三成，需关注", {"m1_value"}) is False

    @pytest.mark.xfail(
        reason="已知缺口：畸形占位符（单花括号 {ref:x}）回填后作为明文残留在输出中",
        strict=False,
    )
    def test_backfill_does_not_leak_malformed_placeholder(self):
        table = {"m1_value": ("x", "9.0")}
        out = an_mod.backfill("值 {ref:m1_value} 结束", table)
        assert "{" not in out and "}" not in out


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
