"""仓储抽象测试：B1 起所有元数据读写都走这层，必须先验证可靠。"""

from __future__ import annotations

import pytest

from app.infra.database import create_all, init_engine, reset_engine
from app.infra.models import KeyValue
from app.infra.repository import Repository


@pytest.fixture(scope="module", autouse=True)
def _module_db():
    reset_engine()
    init_engine()
    create_all()
    yield
    reset_engine()


@pytest.fixture()
def repo():
    from app.infra.database import _session_factory  # noqa: SLF001

    session = _session_factory()
    try:
        yield Repository(KeyValue, session)
        session.rollback()
    finally:
        session.close()


def test_add_and_get(repo):
    obj = repo.add(KeyValue(key="schema_version", value="0.1.0"))
    assert obj.key == "schema_version"
    fetched = repo.get("schema_version")
    assert fetched is not None
    assert fetched.value == "0.1.0"


def test_update(repo):
    repo.add(KeyValue(key="k1", value="v1"))
    obj = repo.get("k1")
    repo.update(obj, value="v2")
    assert repo.get("k1").value == "v2"


def test_list_with_filters_and_pagination(repo):
    for i in range(5):
        repo.add(KeyValue(key=f"page-{i}", value="page"))
    repo.add(KeyValue(key="page-other", value="other"))

    page_items = repo.list(order_by=KeyValue.key, **{"value": "page"})
    assert len(page_items) == 5

    limited = repo.list(limit=2, order_by=KeyValue.key)
    assert len(limited) == 2

    count = repo.count(value="page")
    assert count == 5


def test_delete(repo):
    repo.add(KeyValue(key="del-me", value="x"))
    assert repo.delete("del-me") is True
    assert repo.get("del-me") is None
    assert repo.delete("del-me") is False
