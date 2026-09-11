"""元数据库建表回归测试。

背景缺陷：create_all() 若未先导入模型模块，Base.metadata 为空，
建表静默失败（metadata.db 为 0 字节，但 SELECT 1 仍然通过，极难察觉）。
本测试锁定该行为。
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.core.config import get_settings
from app.infra.database import create_all, get_engine, init_engine, reset_engine


def test_create_all_creates_registered_tables():
    reset_engine()
    init_engine(get_settings(), force=True)
    create_all()
    tables = set(inspect(get_engine()).get_table_names())
    assert "key_value" in tables
    reset_engine()
