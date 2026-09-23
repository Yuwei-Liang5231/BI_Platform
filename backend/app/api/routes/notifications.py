"""站内通知路由（B10-3 最小版）：异动结论的"发现→告知"闭环。

列表按当前用户隔离（project_id 过滤当前项目）；未读计数供导航栏小红点；
单条已读/全部已读。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, DbDep
from app.core.response import ok_response
from app.infra.models import Notification
from app.infra.repository import Repository

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_dict(n: Notification) -> dict:
    return {
        "id": n.id,
        "project_id": n.project_id,
        "metric_id": n.metric_id,
        "metric_code": n.metric_code,
        "title": n.title,
        "body": n.body,
        "anomaly_date": n.anomaly_date.isoformat() if n.anomaly_date else None,
        "direction": n.direction,
        "current": n.current,
        "baseline_mean": n.baseline_mean,
        "abnormality": n.abnormality,
        "read": n.read_at is not None,
        "kind": n.kind or "anomaly",
        "created_at": n.created_at,
    }


@router.get("")
def list_notifications(
    db: DbDep, user: CurrentUser, project_id: int | None = None,
    unread_only: bool = False,
):
    """当前用户的站内通知（最近 50 条，按时间倒序）。"""
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if project_id is not None:
        query = query.filter(Notification.project_id == project_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    rows = query.order_by(Notification.created_at.desc()).limit(50).all()
    return ok_response([_to_dict(n) for n in rows])


@router.get("/unread-count")
def unread_count(db: DbDep, user: CurrentUser, project_id: int | None = None):
    """未读数（导航栏小红点）。"""
    query = db.query(Notification).filter(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if project_id is not None:
        query = query.filter(Notification.project_id == project_id)
    return ok_response({"count": query.count()})


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int


@router.post("/read")
def mark_read(body: ReadRequest, db: DbDep, user: CurrentUser):
    """单条已读（仅本人的通知）。"""
    n = Repository(Notification, db).get(body.id)
    if n is None or n.user_id != user.id:
        from app.core.response import BusinessError

        raise BusinessError("通知不存在", 40400)
    if n.read_at is None:
        n.read_at = datetime.now().astimezone()
        Repository(Notification, db).update(n)
    return ok_response({"id": n.id, "read": True})


@router.post("/read-all")
def mark_all_read(db: DbDep, user: CurrentUser, project_id: int | None = None):
    """全部已读（当前用户；project_id 指定时仅该项目范围）。"""
    query = db.query(Notification).filter(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if project_id is not None:
        query = query.filter(Notification.project_id == project_id)
    now = datetime.now().astimezone()
    count = query.update({Notification.read_at: now}, synchronize_session=False)
    db.flush()
    return ok_response({"marked": count})
