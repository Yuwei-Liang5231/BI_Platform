"""元数据库（SQLite）引擎与会话管理。

- 引擎按 Settings 惰性初始化；测试可反复调用 init_engine 指向临时库
- Base 为全部元数据模型的声明基类（datasets/metrics 等表在 B1/B2 加入）
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings, get_settings

_engine = None
_session_factory: sessionmaker | None = None


class Base(DeclarativeBase):
    """全部元数据 ORM 模型的基类。"""


def init_engine(settings: Settings | None = None, *, force: bool = False):
    """初始化引擎。已初始化且未 force 时为幂等空操作。"""
    global _engine, _session_factory
    if _engine is not None and not force:
        return _engine

    settings = settings or get_settings()
    db_path: Path = settings.metadata_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connect_args = {}
    if settings.metadata_db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    _engine = create_engine(settings.metadata_db_url, connect_args=connect_args, future=True)
    _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话。"""
    if _session_factory is None:
        init_engine()
    session = _session_factory()  # type: ignore[misc]
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database() -> bool:
    """健康检查：SELECT 1。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def create_all() -> None:
    """建表（B0 阶段仅元数据基础表；B1/B2 起模型自动纳入）。

    注意：必须先导入模型模块，否则 Base.metadata 为空、建表静默失败
    （表现为 metadata.db 为 0 字节）。新增模型模块时在此登记导入。
    """
    import app.infra.models  # noqa: F401  —— 触发模型注册

    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """测试辅助：丢弃当前引擎，下次 init_engine 重建。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
