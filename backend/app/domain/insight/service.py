"""每日定时洞察（P5/A1 主动洞察）：自动发现 → AI 解释 → 站内推送。

AI 作用层级从「事后解释」跃到「主动洞察」的关键一步：不再等用户进页面点
扫描，而是后台任务每日自动执行——

1. 对每个项目执行异动检测（复用 anomaly.detect_for_project，opt-in 语义不变）；
2. 取要紧度最高的 TOP N 条异动，逐条生成 AI 假设（复用
   ai.anomaly_hypothesis.build_anomaly_hypothesis，权限上下文 = 项目内任一
   admin；无 admin / 无 LLM → 规则文案降级）；
3. 洞察落 `ai_insights`（同 项目+日期+指标+方向 去重，重复执行幂等）；
4. 以 kind="insight" 站内通知推送对该指标可见的全部活跃用户（权限同源）。

邮件/企微等外部渠道属后续 C1，本期站内（铃铛）即可达。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.auth.service import build_user_hidden_map
from app.infra.models import AiInsight, Metric, Notification, User

logger = get_logger("app.domain.insight")

_DIRECTION_WORD = {"up": "上升", "down": "下降"}


def _pick_admin(db: Session, project_id: int | None) -> User | None:
    """项目内任一 active admin（AI 假设的权限上下文；无则 None 走规则文案）。"""
    q = db.query(User).filter(User.status == "active", User.role == "admin")
    if project_id is not None:
        # 项目成员归属未建独立关系表，任一 admin 均可（假设生成不涉跨项目数据：
        # 编译按 metric 自身 project 解析），取最先注册者保证稳定。
        pass
    return q.order_by(User.id).first()


def _week_window(d: date) -> tuple[str, str]:
    """异动日所在自然周（周一~周日）作为假设生成的统计窗口（与总览页一致）。"""
    day = (d.weekday())  # Monday=0
    monday = d - timedelta(days=day)
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def run_daily_insight(
    db: Session,
    *,
    insight_date: date | None = None,
    project_ids: list[int] | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    """执行一次每日洞察（幂等；可由调度器或 admin 手动触发）。

    返回 {"insight_date", "projects": [...], "created": N, "notified": N,
    "skipped_existing": N}。
    """
    from app.domain.ai.anomaly_hypothesis import build_anomaly_hypothesis
    from app.domain.anomaly.service import detect_for_project

    settings = get_settings()
    d = insight_date or date.today()
    top = top_n or settings.ai_insight_top_n

    # 项目清单：显式指定，或全部已有指标异动配置的项目
    if project_ids is None:
        from app.infra.models import AnomalyConfig

        rows = db.query(AnomalyConfig.project_id).distinct().all()
        project_ids = [r[0] for r in rows if r[0] is not None]

    admin_by_pid: dict[int | None, User] = {}
    users = db.query(User).filter(User.status == "active").all()
    user_hidden = build_user_hidden_map(db)

    created = 0
    notified = 0
    skipped_existing = 0
    project_report: list[dict] = []

    for pid in project_ids:
        scan = detect_for_project(db, users[0] if users else None, pid)
        anomalies = scan.get("anomalies") or []
        material = [a for a in anomalies if a.get("material") is not False]
        material = material[:top]
        pid_report = {"project_id": pid, "anomalies": len(material), "insights": 0}

        for r in material:
            metric_id = r["metric_id"]
            direction = r.get("direction") or "up"
            dup = (
                db.query(AiInsight)
                .filter(
                    AiInsight.project_id == pid,
                    AiInsight.insight_date == d,
                    AiInsight.metric_id == metric_id,
                    AiInsight.direction == direction,
                )
                .first()
            )
            if dup is not None:
                skipped_existing += 1
                continue

            metric = db.get(Metric, metric_id)
            metric_name = (metric.name if metric else r.get("name")) or r.get("metric_code", "")
            word = _DIRECTION_WORD.get(direction, "波动")

            # AI 假设（权限上下文 = 项目内 admin；失败/无 LLM → 规则文案）
            hypothesis: list[str] = []
            source = "rule"
            admin = admin_by_pid.get(pid) or _pick_admin(db, pid)
            admin_by_pid[pid] = admin
            if admin is not None:
                start, end = _week_window(r.get("date") and date.fromisoformat(r["date"]) or d)
                try:
                    hp = build_anomaly_hypothesis(
                        db, admin, metric_id, start, end
                    )
                    if hp.get("has_anomaly") and hp.get("hypothesis"):
                        hypothesis = [str(s) for s in hp["hypothesis"]][:4]
                        source = hp.get("source") or "rule"
                except Exception:  # noqa: BLE001 - 单条假设失败不拖垮整批（洞察零阻塞）
                    logger.warning("insight: 指标 %s AI 假设生成失败，降级规则文案", metric_id, exc_info=True)

            data_bits = []
            if r.get("current") is not None:
                data_bits.append(f"{r['date']} 值 {round(r['current'], 1):,}")
            baseline = r.get("baseline") or {}
            if baseline.get("mean") is not None:
                data_bits.append(f"正常水平约 {round(baseline['mean'], 1):,}")
            body_parts = ["；".join(data_bits)]
            body_parts.extend(hypothesis)
            if not hypothesis:
                body_parts.append(str(r.get("reason") or ""))
            body = "\n".join(p for p in body_parts if p)

            title = f"每日洞察：「{metric_name}」{word}（{r['date']}）"
            insight = AiInsight(
                project_id=pid,
                insight_date=d,
                metric_id=metric_id,
                metric_code=r.get("metric_code") or (metric.code if metric else ""),
                direction=direction,
                title=title[:200],
                body=body,
                source=source,
                current=r.get("current"),
                baseline_mean=baseline.get("mean"),
                abnormality=r.get("abnormality"),
            )
            db.add(insight)
            db.flush()
            created += 1
            pid_report["insights"] += 1

            # 推送：kind=insight 站内通知，收件人 = 对该指标可见的活跃用户
            for u in users:
                if metric_id in user_hidden.get(u.id, set()):
                    continue
                dup_n = (
                    db.query(Notification)
                    .filter(
                        Notification.user_id == u.id,
                        Notification.metric_id == metric_id,
                        Notification.anomaly_date == d,
                        Notification.direction == direction,
                        Notification.kind == "insight",
                    )
                    .first()
                )
                if dup_n is not None:
                    continue
                db.add(
                    Notification(
                        user_id=u.id,
                        project_id=pid,
                        metric_id=metric_id,
                        metric_code=insight.metric_code,
                        anomaly_date=d,
                        direction=direction,
                        title=title[:200],
                        body=body,
                        current=r.get("current"),
                        baseline_mean=baseline.get("mean"),
                        abnormality=r.get("abnormality"),
                        kind="insight",
                    )
                )
                notified += 1
        project_report.append(pid_report)

    db.commit()
    logger.info(
        "insight: date=%s created=%s notified=%s skipped=%s projects=%s",
        d, created, notified, skipped_existing,
        json.dumps(project_report, ensure_ascii=False),
    )
    return {
        "insight_date": d.isoformat(),
        "projects": project_report,
        "created": created,
        "notified": notified,
        "skipped_existing": skipped_existing,
    }
