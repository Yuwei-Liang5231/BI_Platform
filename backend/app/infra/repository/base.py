"""通用仓储抽象 + SQLAlchemy 实现。

设计原则（开发方案第三节"契约先于实现"）：
- 服务层只依赖本接口，不直接操作 Session
- 后续若元数据迁库（PostgreSQL），仅需替换实现，服务层零改动
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.infra.database import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """SQLAlchemy 2.x 风格的通用仓储。主键约定为单一整型/字符串列 `id`。"""

    def __init__(self, model: type[ModelT], session: Session):
        self.model = model
        self.session = session

    # ---- 读 ----

    def get(self, pk_value: Any) -> ModelT | None:
        return self.session.get(self.model, pk_value)

    def list(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        order_by: Any = None,
        **filters: Any,
    ) -> list[ModelT]:
        stmt = select(self.model)
        for col_name, value in filters.items():
            if value is None:
                continue
            col = getattr(self.model, col_name)
            stmt = stmt.where(col == value)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.offset(offset).limit(limit)
        return list(self.session.scalars(stmt))

    def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        for col_name, value in filters.items():
            if value is None:
                continue
            stmt = stmt.where(getattr(self.model, col_name) == value)
        return int(self.session.scalar(stmt))

    # ---- 写 ----

    def add(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        self.session.flush()
        return obj

    def add_all(self, objs: Iterable[ModelT]) -> list[ModelT]:
        objs = list(objs)
        self.session.add_all(objs)
        self.session.flush()
        return objs

    def update(self, obj: ModelT, **fields: Any) -> ModelT:
        for key, value in fields.items():
            setattr(obj, key, value)
        self.session.flush()
        return obj

    def delete(self, pk_value: Any) -> bool:
        obj = self.get(pk_value)
        if obj is None:
            return False
        self.session.delete(obj)
        self.session.flush()
        return True
