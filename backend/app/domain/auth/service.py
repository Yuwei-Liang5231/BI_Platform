"""auth 服务：认证、用户管理、指标可见性判定（权限双出口的单一实现）。

可见性模型（阶段 1 最小，不变式 2）：
- 默认所有指标对登录用户可见；
- `metric_visibility_restriction` 负向登记后，命中登记（按角色或按部门）
  的非 admin 用户在两处出口同时不可见：query 数值出口 403 拒算，
  metric 目录/详情/SQL/历史接口同样 403/过滤——名称与口径不泄露；
- admin 永远全量可见（管理员需要管理受限指标本身）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.auth.security import create_access_token, hash_password, verify_password
from app.infra.models import Metric, MetricVisibilityRestriction, User, VALID_ROLES


# ---------------------------------------------------------------- 用户


def user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "department": u.department,
        "status": u.status,
        "last_login_at": u.last_login_at,
        "created_at": u.created_at,
        "updated_at": u.updated_at,
    }


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def authenticate(db: Session, username: str, password: str) -> tuple[User, str]:
    """登录：成功返回 (user, token)。用户名不存在与密码错误统一文案（防枚举）。"""
    user = get_user_by_username(db, username or "")
    if user is None or not verify_password(password or "", user.password_hash):
        raise BusinessError("用户名或密码错误", 40100)
    if user.status != "active":
        raise BusinessError("账号已停用，请联系管理员", 40300)
    token = create_access_token(user_id=user.id, username=user.username, role=user.role)
    return user, token


def ensure_bootstrap_admin() -> None:
    """users 表无 admin 时创建引导管理员（应用启动时调用，幂等）。"""
    from app.core.config import get_settings
    from app.infra.database import get_engine
    from sqlalchemy.orm import Session

    settings = get_settings()
    session = Session(bind=get_engine())
    try:
        if session.query(User).filter(User.role == "admin").first() is not None:
            return
        session.add(
            User(
                username=settings.bootstrap_admin_username,
                password_hash=hash_password(settings.bootstrap_admin_password),
                role="admin",
            )
        )
        session.commit()
    finally:
        session.close()


def create_user(
    db: Session, *, username: str, password: str, role: str = "viewer", department: str = ""
) -> User:
    username = (username or "").strip()
    if len(username) < 2 or len(username) > 64:
        raise BusinessError("username 长度须为 2-64", 40000)
    if get_user_by_username(db, username) is not None:
        raise BusinessError(f"用户名 {username!r} 已存在", 40900)
    if not password or len(password) < 6:
        raise BusinessError("password 长度至少 6 位", 40000)
    if role not in VALID_ROLES:
        raise BusinessError(f"role 须为 {VALID_ROLES} 之一", 40000)
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        department=(department or "").strip(),
    )
    db.add(user)
    db.commit()
    return user


def update_user(
    db: Session, user_id: int, changes: dict, *, operator: User
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise BusinessError(f"用户 {user_id} 不存在", 40400)

    allowed = {"role", "department", "status", "password"}
    unknown = set(changes) - allowed
    if unknown:
        raise BusinessError(f"不支持修改的字段：{sorted(unknown)}", 40000)

    if "role" in changes:
        if changes["role"] not in VALID_ROLES:
            raise BusinessError(f"role 须为 {VALID_ROLES} 之一", 40000)
        if user.id == operator.id and changes["role"] != "admin":
            raise BusinessError("不能降级自己的 admin 角色", 40000)
        user.role = changes["role"]
    if "department" in changes:
        user.department = (changes["department"] or "").strip()
    if "status" in changes:
        if changes["status"] not in ("active", "disabled"):
            raise BusinessError("status 只允许 active / disabled", 40000)
        if user.id == operator.id and changes["status"] != "active":
            raise BusinessError("不能停用自己的账号", 40000)
        user.status = changes["status"]
    if "password" in changes:
        if not changes["password"] or len(changes["password"]) < 6:
            raise BusinessError("password 长度至少 6 位", 40000)
        user.password_hash = hash_password(changes["password"])
    db.commit()
    return user


# ---------------------------------------------------------------- 可见性（双出口共用）


def _restriction_subjects(user: User) -> list[tuple[str, str]]:
    """该用户会被命中的登记主体：自己的角色 + 自己的部门。"""
    subjects = [("role", user.role)]
    if user.department:
        subjects.append(("department", user.department))
    return subjects


def restricted_metric_ids(db: Session, user: User) -> set[int]:
    """对 user 隐藏的指标 id 集合。admin 恒为空集。"""
    if user.role == "admin":
        return set()
    conds = [
        and_(
            MetricVisibilityRestriction.subject_type == stype,
            MetricVisibilityRestriction.subject_value == svalue,
        )
        for stype, svalue in _restriction_subjects(user)
    ]
    rows = db.query(MetricVisibilityRestriction.metric_id).filter(or_(*conds))
    return {row.metric_id for row in rows}


def build_user_hidden_map(db: Session) -> dict[int, set[int]]:
    """批量构建「用户 id -> 受限指标集合」映射（P5 提取共用）。

    供通知 fan-out（异动扫描/每日洞察）等需要按用户逐个判权的场景使用，
    一次查询全部受限登记，避免逐用户 N 次查询；语义与 restricted_metric_ids
    完全一致（admin 恒空集，其余按 role+department 命中）。
    """
    from app.infra.models import MetricVisibilityRestriction

    restrict_by_subject: dict[tuple[str, str], set[int]] = {}
    for stype, svalue, mid in (
        db.query(
            MetricVisibilityRestriction.subject_type,
            MetricVisibilityRestriction.subject_value,
            MetricVisibilityRestriction.metric_id,
        ).all()
    ):
        restrict_by_subject.setdefault((stype, svalue), set()).add(mid)

    users = db.query(User).filter(User.status == "active").all()
    out: dict[int, set[int]] = {}
    for u in users:
        if u.role == "admin":
            out[u.id] = set()
            continue
        hidden: set[int] = set()
        subjects = [("role", u.role)]
        if u.department:
            subjects.append(("department", u.department))
        for s in subjects:
            hidden |= restrict_by_subject.get(s, set())
        out[u.id] = hidden
    return out


def filter_visible_metrics(db: Session, user: User, metrics: list[Metric]) -> list[Metric]:
    hidden = restricted_metric_ids(db, user)
    if not hidden:
        return metrics
    return [m for m in metrics if m.id not in hidden]


def ensure_metric_visible(db: Session, user: User, metric: Metric) -> None:
    """单指标可见性断言：受限即 403（query 出口与 metric 详情出口共用）。"""
    if metric.id in restricted_metric_ids(db, user):
        raise BusinessError("无权访问该指标", 40300)


# ---------------------------------------------------------------- 可见性登记管理


def list_restrictions(db: Session, metric_id: int) -> list[dict]:
    rows = (
        db.query(MetricVisibilityRestriction)
        .filter(MetricVisibilityRestriction.metric_id == metric_id)
        .order_by(MetricVisibilityRestriction.id)
        .all()
    )
    return [
        {
            "id": r.id,
            "metric_id": r.metric_id,
            "subject_type": r.subject_type,
            "subject_value": r.subject_value,
            "created_by": r.created_by,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def replace_restrictions(
    db: Session, metric: Metric, items: list[dict], *, operator: User
) -> list[dict]:
    """整组替换该指标的可见性登记（PUT 语义，幂等）。"""
    cleaned: list[tuple[str, str]] = []
    for item in items or []:
        stype = (item.get("subject_type") or "").strip()
        svalue = (item.get("subject_value") or "").strip()
        if stype not in ("role", "department"):
            raise BusinessError("subject_type 须为 role / department", 40000)
        if not svalue:
            raise BusinessError("subject_value 不能为空", 40000)
        if stype == "role" and svalue not in VALID_ROLES:
            raise BusinessError(f"role 主体须为 {VALID_ROLES} 之一", 40000)
        if (stype, svalue) not in cleaned:
            cleaned.append((stype, svalue))

    db.query(MetricVisibilityRestriction).filter(
        MetricVisibilityRestriction.metric_id == metric.id
    ).delete(synchronize_session=False)
    for stype, svalue in cleaned:
        db.add(
            MetricVisibilityRestriction(
                metric_id=metric.id,
                subject_type=stype,
                subject_value=svalue,
                created_by=operator.username,
            )
        )
    db.commit()
    return list_restrictions(db, metric.id)
