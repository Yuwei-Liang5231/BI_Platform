"""操作审计日志查询（C2，admin）。"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, DbDep
from app.core.response import ok_response
from app.infra.models import AuditLog

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
def list_audit_logs(
    db: DbDep,
    _admin: AdminUser,
    user_id: int | None = None,
    action: str | None = Query(None, description="精确或前缀匹配（如 dataset. 匹配全部数据集动作）"),
    resource_type: str | None = None,
    start: str | None = Query(None, description="YYYY-MM-DD（含）"),
    end: str | None = Query(None, description="YYYY-MM-DD（含）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """审计日志分页查询（admin）：按用户 / 动作（前缀）/ 资源类型 / 日期过滤。"""
    from sqlalchemy import distinct, func

    q = db.query(AuditLog)
    if user_id is not None:
        q = q.filter(AuditLog.user_id == user_id)
    if action:
        q = q.filter(AuditLog.action.like(f"{action}%"))
    if resource_type:
        q = q.filter(AuditLog.resource_type == resource_type)
    if start:
        try:
            start_d = date.fromisoformat(start)
            q = q.filter(AuditLog.created_at >= datetime(start_d.year, start_d.month, start_d.day))
        except ValueError:
            pass
    if end:
        try:
            from datetime import timedelta

            end_d = date.fromisoformat(end)
            q = q.filter(AuditLog.created_at < datetime.combine(end_d, datetime.min.time()) + timedelta(days=1))
        except ValueError:
            pass

    total = q.count()
    rows = (
        q.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "username": r.username,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "method": r.method,
            "path": r.path,
            "status_code": r.status_code,
            "detail": json_loads_safe(r.detail_json),
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return ok_response({"items": items, "total": total, "page": page, "page_size": page_size})


def json_loads_safe(raw: str | None) -> dict | None:
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


@router.get("/meta")
def audit_meta(db: DbDep, _admin: AdminUser):
    """过滤器选项：出现过的用户与动作类型（distinct）。"""
    from sqlalchemy import distinct, func

    users = (
        db.query(AuditLog.user_id, AuditLog.username)
        .distinct()
        .filter(AuditLog.user_id.isnot(None))
        .order_by(AuditLog.user_id)
        .all()
    )
    actions = [a for (a,) in db.query(distinct(AuditLog.action)).order_by(AuditLog.action).all()]
    return ok_response(
        {
            "users": [{"id": uid, "username": uname or str(uid)} for uid, uname in users],
            "actions": actions,
            "total": db.query(func.count(AuditLog.id)).scalar() or 0,
        }
    )
