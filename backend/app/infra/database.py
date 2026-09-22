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
    migrate_project_columns()


def migrate_project_columns() -> None:
    """B9.3 轻量迁移（幂等）：存量表补 project_id 列 + 去除 metrics.code 全局唯一。

    - create_all 不会为已存在的表加列：对 datasets/metrics/ask_conversations
      逐个 PRAGMA 检查，缺 project_id 则 ALTER TABLE ADD COLUMN（存量行先置
      NULL，由 ensure_default_project 回填默认项目）；
    - 指标 code 唯一性收敛到项目内（B9.3）：SQLite 无法 DROP 建表自带的
      UNIQUE 自动索引，检测到即重建 metrics 表（行数小，单事务完成；
      metric_changes 等子表按 metric_id 引用不受影响——SQLite 默认不启用
      外键强制，重命名/重建不影响子表数据）；
    - 新装库由 ORM 直接生成新结构（code 无 unique、含 project_id），检测
      不到旧约束时迁移为空操作。
    """
    with get_engine().begin() as conn:
        for table in ("datasets", "metrics", "ask_conversations", "anomaly_configs"):
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                continue  # 表尚不存在：create_all 已按新结构处理
            cols = {r[1] for r in rows}
            if "project_id" not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN project_id INTEGER")
            if table == "anomaly_configs" and "materiality_pct" not in cols:
                conn.exec_driver_sql("ALTER TABLE anomaly_configs ADD COLUMN materiality_pct REAL DEFAULT 5.0")
            # P2 复合键关系：存量表补 column_pairs JSON 列（NULL = 单列键，零迁移兼容）
            if table == "datasets":
                rel_rows = conn.exec_driver_sql("PRAGMA table_info(dataset_relations)").fetchall()
                if rel_rows and "column_pairs" not in {r[1] for r in rel_rows}:
                    conn.exec_driver_sql("ALTER TABLE dataset_relations ADD COLUMN column_pairs TEXT")
                # 11.9 P1-2：字段语义标注列（存量表补列，{} 零迁移兼容）
                if "column_semantics_json" not in cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE datasets ADD COLUMN column_semantics_json TEXT DEFAULT '{}'"
                    )
            # 11.9 P1-2/P2-1：指标成熟期与口径结构化说明（存量表补列，NULL/{} 零迁移兼容）
            if table == "metrics":
                if "maturity_days" not in cols:
                    conn.exec_driver_sql("ALTER TABLE metrics ADD COLUMN maturity_days INTEGER")
                if "calc_notes_json" not in cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE metrics ADD COLUMN calc_notes_json TEXT DEFAULT '{}'"
                    )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_metrics_project_id ON metrics (project_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_datasets_project_id ON datasets (project_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_ask_conversations_project_id ON ask_conversations (project_id)"
        )
        if _metrics_has_unique_code(conn):
            _rebuild_metrics_without_unique_code(conn)


def _metrics_has_unique_code(conn) -> bool:
    """metrics.code 是否带建表级 UNIQUE 约束（origin='u' 的唯一索引）。"""
    rows = conn.exec_driver_sql("PRAGMA index_list('metrics')").fetchall()
    for r in rows:
        # 列序：seq, name, unique, origin, partial
        if r[2] and r[3] == "u":
            return True
    return False


_METRICS_DDL = """
CREATE TABLE metrics (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    aliases_json TEXT DEFAULT '[]',
    definition TEXT DEFAULT '',
    calc_rule_json TEXT NOT NULL,
    dimensions_json TEXT DEFAULT '[]',
    filters_json TEXT DEFAULT '{}',
    topic VARCHAR(100) DEFAULT 'general',
    level INTEGER DEFAULT 1,
    parent_id INTEGER,
    disambiguation_json TEXT DEFAULT '{}',
    primary_dataset_id INTEGER,
    owner_user_id VARCHAR(64) DEFAULT 'system',
    owner_department VARCHAR(100) DEFAULT '',
    status VARCHAR(20) DEFAULT 'active',
    ver INTEGER DEFAULT 1,
    project_id INTEGER,
    created_at DATETIME,
    updated_at DATETIME,
    FOREIGN KEY(parent_id) REFERENCES metrics (id),
    FOREIGN KEY(primary_dataset_id) REFERENCES datasets (id)
)
"""

_METRICS_COLS = [
    "id", "code", "name", "aliases_json", "definition", "calc_rule_json",
    "dimensions_json", "filters_json", "topic", "level", "parent_id",
    "disambiguation_json", "primary_dataset_id", "owner_user_id",
    "owner_department", "status", "ver", "created_at", "updated_at",
]


def _rebuild_metrics_without_unique_code(conn) -> None:
    """重建 metrics 表去除 code 的 UNIQUE 约束（数据原样保留，project_id 置 NULL
    由 ensure_default_project 回填）。单事务内执行（调用方处于 begin 块）。"""
    conn.exec_driver_sql("ALTER TABLE metrics RENAME TO metrics_old_b93")
    conn.exec_driver_sql(_METRICS_DDL)
    cols = ", ".join(_METRICS_COLS)
    conn.exec_driver_sql(
        f"INSERT INTO metrics ({cols}, project_id) SELECT {cols}, NULL FROM metrics_old_b93"
    )
    conn.exec_driver_sql("DROP TABLE metrics_old_b93")
    conn.exec_driver_sql("CREATE INDEX ix_metrics_code ON metrics (code)")
    conn.exec_driver_sql("CREATE INDEX ix_metrics_project_id ON metrics (project_id)")


def reset_engine() -> None:
    """测试辅助：丢弃当前引擎，下次 init_engine 重建。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
