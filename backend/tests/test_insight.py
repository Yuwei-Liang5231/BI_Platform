"""每日定时洞察测试（P5/A1 主动洞察）。

验收红线：
- 洞察幂等：同 (项目, 日期, 指标, 方向) 只生成一条，重复执行不重复推送；
- 权限同源：受限用户不收受限指标的洞察通知；
- 降级：LLM 未配置 → source=rule 规则文案，绝不 500；
- 通知区分 kind：insight 与 anomaly 不互相去重、互不污染。
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

import pytest

from app.infra.database import get_db
from app.infra.models import AiInsight, Metric, Notification


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _build_rows(spike_date: date) -> list[str]:
    """平时稳定（56 天，保证同星期几 ≥6 个样本）+ 当天暴刺（要紧异动）。"""
    lines = ["d,amount"]
    d = spike_date - timedelta(days=55)
    while d <= spike_date:
        if d == spike_date:
            lines.append(f"{d.isoformat()},8000")
        else:
            lines.append(f"{d.isoformat()},500")
        d += timedelta(days=1)
    return lines


@pytest.fixture()
def insight_env(client):
    """上传带暴刺的数据集 + 建指标 + 开启异动检测 + 建 viewer。"""
    from tests.conftest import create_test_user

    sfx = _suffix()
    ds_name = f"insight_ds_{sfx}"
    spike = date.today()  # 扫描默认检测「今天」
    csv = "\n".join(_build_rows(spike)) + "\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": (f"{ds_name}.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
        data={"name": ds_name},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/metrics", json={
        "code": f"insight_daily_{sfx}",
        "name": "洞察日销",
        "calc_rule": {"base_aggregation": "sum", "source": {"table": ds_name, "column": "amount"}},
    })
    assert resp.status_code == 200, resp.text
    metric = resp.json()["data"]
    client.put(f"/api/metrics/{metric['id']}/anomaly-config", json={
        "z_threshold": 3.0, "min_samples": 6, "enabled": True,
    })
    viewer = create_test_user(client, f"vw_insig_{sfx}", role="viewer")
    yield {"metric": metric, "viewer": viewer, "sfx": sfx}
    client.delete(f"/api/metrics/{metric['id']}")


class TestDailyInsight:
    def test_run_creates_insight_and_pushes(self, client, insight_env):
        """执行一次：洞察落库（无 LLM → rule）+ 可见用户收到 kind=insight 通知。"""
        from app.domain.insight.service import run_daily_insight

        db = next(get_db())
        try:
            r = run_daily_insight(db)
            assert r["created"] >= 1, r
            assert r["notified"] >= 1
            rows = (
                db.query(AiInsight)
                .filter(AiInsight.metric_id == insight_env["metric"]["id"])
                .all()
            )
            assert rows, "应有洞察落库"
            for row in rows:
                assert row.source == "rule"  # 测试环境无 LLM → 规则降级
                assert row.title.startswith("每日洞察")
            # viewer（指标可见）应收到 insight 通知
            viewer_notes = (
                db.query(Notification)
                .filter(
                    Notification.user_id > 0,
                    Notification.kind == "insight",
                    Notification.metric_id == insight_env["metric"]["id"],
                )
                .all()
            )
            assert viewer_notes, "洞察应推送站内通知"

            # 幂等：重复执行不新增
            before = db.query(AiInsight).count()
            r2 = run_daily_insight(db)
            after = db.query(AiInsight).count()
            assert after == before
            assert r2["notified"] == 0
        finally:
            db.rollback()
            db.close()

    def test_manual_run_endpoint(self, client, insight_env):
        """POST /ai/insight/run（admin）：200 且创建洞察；viewer 403。"""
        r = client.post("/api/ai/insight/run")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["created"] >= 1

        from tests.conftest import create_test_user

        vw = create_test_user(client, f"vw_run_{insight_env['sfx']}", role="viewer")
        r2 = client.post("/api/ai/insight/run", headers=vw)
        assert r2.status_code == 403

    def test_notifications_have_kind(self, client, insight_env):
        """通知 API 返回 kind：异动通知默认 anomaly，洞察通知为 insight。"""
        client.get("/api/query/anomalies")  # 产生 anomaly 通知
        from app.domain.insight.service import run_daily_insight

        db = next(get_db())
        try:
            run_daily_insight(db)
        finally:
            db.close()
        notes = client.get("/api/notifications").json()["data"]
        kinds = {n["kind"] for n in notes}
        assert "anomaly" in kinds
        assert "insight" in kinds


class TestInsightBar:
    """A2 常驻洞察条：GET /ai/insights 数据源契约。"""

    def test_insights_endpoint(self, client, insight_env):
        """生成后可见 + 同指标去重取最新 + 受限用户隐藏 + 软删指标过滤。"""
        from app.domain.insight.service import run_daily_insight

        mid = insight_env["metric"]["id"]
        db = next(get_db())
        try:
            run_daily_insight(db)
            pid = db.get(Metric, mid).project_id
            # 同指标再插一条昨日旧洞察：去重后只保留今天最新一条
            db.add(
                AiInsight(
                    project_id=pid,
                    insight_date=date.today() - timedelta(days=1),
                    metric_id=mid,
                    metric_code=insight_env["metric"]["code"],
                    direction="down",
                    title="每日洞察：旧",
                    body="",
                    source="rule",
                )
            )
            db.commit()
        finally:
            db.rollback()
            db.close()

        r = client.get("/api/ai/insights")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["generated_today"] is True
        mine = [it for it in data["items"] if it["metric_id"] == mid]
        assert len(mine) == 1, "同指标多条洞察应去重只保留最新一条"
        assert mine[0]["insight_date"] == date.today().isoformat()
        assert mine[0]["title"].startswith("每日洞察")
        assert mine[0]["metric_name"] == "洞察日销"
        assert mine[0]["source"] == "rule"

        # 受限 viewer：受限指标的洞察隐藏（不泄露名称），generated_today 不变
        from tests.conftest import create_test_user

        vw = create_test_user(client, f"vw_bar_{insight_env['sfx']}", role="viewer")
        client.put(
            f"/api/auth/metrics/{mid}/restrictions",
            json={"items": [{"subject_type": "role", "subject_value": "viewer"}]},
        )
        rv = client.get("/api/ai/insights", headers=vw)
        assert rv.status_code == 200, rv.text
        vd = rv.json()["data"]
        assert vd["generated_today"] is True
        assert all(it["metric_id"] != mid for it in vd["items"]), "受限指标洞察不应出现在洞察条"
        # 解除限制后恢复可见
        client.put(f"/api/auth/metrics/{mid}/restrictions", json={"items": []})
        rv2 = client.get("/api/ai/insights", headers=vw)
        assert any(it["metric_id"] == mid for it in rv2.json()["data"]["items"])

        # 软删指标：洞察行仍在库，但洞察条不再展示（断言后恢复，避免影响 fixture 清理）
        db2 = next(get_db())
        try:
            m = db2.get(Metric, mid)
            m.status = "deleted"
            db2.commit()
        finally:
            db2.rollback()
            db2.close()
        r2 = client.get("/api/ai/insights")
        assert all(it["metric_id"] != mid for it in r2.json()["data"]["items"])
        db3 = next(get_db())
        try:
            db3.get(Metric, mid).status = "active"
            db3.commit()
        finally:
            db3.rollback()
            db3.close()

    def test_feedback_daily_insight_kind(self, client):
        """daily_insight 加入反馈白名单：洞察条每条的赞踩可落库。"""
        r = client.post(
            "/api/ai/feedback",
            json={"kind": "daily_insight", "target": "insight:1", "rating": "up"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["recorded"] is True
