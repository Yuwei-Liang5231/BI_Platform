"""元数据模型（B0 基础 + B1 数据接入三表 + B2 指标中心三表 + B4 账号权限两表）。

均继承 database.Base，create_all 自动纳管。
注意：新增模型模块须在 database.create_all 中登记导入（当前同模块，无需额外操作）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.database import Base


def local_now() -> datetime:
    """本地时间（含时区偏移）。

    历史教训：B0-B3 用 UTC 入库，SQLite 实际存 naive 值导致人工核对时
    慢 8 小时。单机部署场景统一改用系统本地时间（2026-09-10 修复，
    存量数据已批量 +8h 校正）。
    """
    return datetime.now().astimezone()


class Project(Base):
    """项目工作区（B9.3）：多行业/多项目逻辑隔离的顶层命名空间。

    单库内命名空间（方案 A）：datasets/metrics/ask_conversations 等主表挂
    project_id；数据集 name 保持全局唯一（DuckDB 视图按名创建）；指标 code
    唯一性收敛到**项目内**（不同项目可同名 sales）。行级/项目级权限留 B15。
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class KeyValue(Base):
    """系统级键值（schema 版本、种子标记等），也作为仓储实现的验证模型。"""

    __tablename__ = "key_value"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


# ---------------------------------------------------------------- B1 数据接入


class Dataset(Base):
    """数据集注册表：一份上传文件 = 一个数据集（架构 D13）。"""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    source_filename: Mapped[str] = mapped_column(String(500))
    file_path: Mapped[str] = mapped_column(String(1000))       # 原始上传文件（保留原编码）
    parquet_path: Mapped[str] = mapped_column(String(1000))    # 列式存储
    file_encoding: Mapped[str] = mapped_column(String(20))     # utf-8 / gbk / gb18030 / xlsx
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_json: Mapped[str] = mapped_column(Text, default="[]")  # [{name,type,mixed,null_count,sample}]
    dataset_ver: Mapped[int] = mapped_column(Integer, default=1)  # 缓存键因子：指标ID:ver:dataset_ver
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # B9.3 项目工作区
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class DatasetRelation(Base):
    """表关系：显式注册，禁止编译器隐式推断关联键（架构不变式 6）。"""

    __tablename__ = "dataset_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    from_column: Mapped[str] = mapped_column(String(200))
    target_dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    target_column: Mapped[str] = mapped_column(String(200))
    relation_type: Mapped[str] = mapped_column(String(20), default="many_to_one")
    created_by: Mapped[str] = mapped_column(String(20), default="manual")  # manual / auto_suggest
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)


class DatasetCoverage(Base):
    """覆盖区间：按日期列记录最小/最大日期（架构 D13）。"""

    __tablename__ = "dataset_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    column_name: Mapped[str] = mapped_column(String(200))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    non_null_count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- B2 指标中心


class Metric(Base):
    """指标定义（架构 7.1）：口径只定义一次，计算规则用受限结构 calc_rule 表达。"""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # code 唯一性收敛到项目内（B9.3）：DB 层不再全局 unique（存量库由
    # migrate_project_columns 重建表去除约束），应用层按 project_id 校验
    code: Mapped[str] = mapped_column(String(100), index=True)           # 稳定引用码（项目内唯一，供外部系统引用）
    name: Mapped[str] = mapped_column(String(200))
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")        # 别名 JSON 数组（搜索用）
    definition: Mapped[str] = mapped_column(Text, default="")            # 业务口径说明（自然语言，仅供人读）
    calc_rule_json: Mapped[str] = mapped_column(Text)                    # 受限结构计算规则（架构 7.2）
    dimensions_json: Mapped[str] = mapped_column(Text, default="[]")     # 常用维度（供看板/问数提示）
    filters_json: Mapped[str] = mapped_column(Text, default="{}")        # 默认过滤（预留 B3）
    topic: Mapped[str] = mapped_column(String(100), default="general")   # 业务主题（行业无关自由枚举）
    level: Mapped[int] = mapped_column(Integer, default=1)               # 目录层级（D15）
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("metrics.id"), nullable=True)
    disambiguation_json: Mapped[str] = mapped_column(Text, default="{}")  # 歧义默认算法（D17）
    primary_dataset_id: Mapped[int | None] = mapped_column(              # 编译产物回写：主事实表
        ForeignKey("datasets.id"), nullable=True
    )
    owner_user_id: Mapped[str] = mapped_column(String(64), default="system")  # B4 接入前占位
    owner_department: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")    # active / disabled / deleted
    ver: Mapped[int] = mapped_column(Integer, default=1)                 # 口径版本号（缓存键因子）
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # B9.3 项目工作区
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class MetricChange(Base):
    """口径变更留痕（架构 7.1）：before/after 存指标快照 JSON。"""

    __tablename__ = "metric_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), index=True)
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(Text, default="")
    operator_id: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)


class MetricSqlArchive(Base):
    """编译存档（架构 7.2）：每个 (metric_id, ver) 一份 SQL，与定义原子同步。"""

    __tablename__ = "metric_sql_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), index=True)
    ver: Mapped[int] = mapped_column(Integer)
    sql_text: Mapped[str] = mapped_column(Text)
    compiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)


# ---------------------------------------------------------------- B4 账号与权限


VALID_ROLES = ("admin", "analyst", "viewer")


class User(Base):
    """本地最小账号（架构 D10）：阶段 1 自建，区别于阶段 4b 的 SSO/LDAP。

    角色：admin（账号/数据集管理+全部权限）/ analyst（指标口径维护）/
    viewer（只读）。行级权限留阶段 4b（B15）。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))          # pbkdf2_sha256$iter$salt$hash
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin / analyst / viewer
    department: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / disabled
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class MetricVisibilityRestriction(Base):
    """指标可见性负向登记（不变式 2）：默认可见，登记后对目标主体隐藏。

    subject_type = role（按角色）或 department（按部门）；query 数值出口与
    metric 元数据接口共用同一套判定（权限双出口）。
    """

    __tablename__ = "metric_visibility_restriction"
    __table_args__ = (
        UniqueConstraint("metric_id", "subject_type", "subject_value", name="uq_metric_restriction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(20))   # role / department
    subject_value: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)


# ---------------------------------------------------------------- 阶段 2 LLM 模型管理


class LlmModel(Base):
    """LLM 模型登记（阶段 2）：OpenAI 兼容接口，支持多模型登记与运行时切换。

    is_active 全局唯一启用一条（切换在 service 层用事务保证互斥）；
    无任何记录或未启用时回退 env 配置（settings.llm_*）。
    """

    __tablename__ = "llm_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)     # 展示名（唯一）
    base_url: Mapped[str] = mapped_column(String(500))              # OpenAI 兼容根地址，如 https://api.deepseek.com
    api_key: Mapped[str] = mapped_column(String(300))               # 凭据（列表接口脱敏返回）
    model: Mapped[str] = mapped_column(String(200))                 # 模型标识，如 deepseek-v4-flash
    is_active: Mapped[int] = mapped_column(Integer, default=0)      # 1=当前启用（唯一）
    remark: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


# ---------------------------------------------------------------- 问数会话持久化（B9.2-6）


class AskConversation(Base):
    """问数会话（B9.2-6）：一次多轮对话 = 一条会话；标题取首问前缀。

    意图继承（追问）仍走内存 session（TTL 30 分钟），本表只持久化
    消息记录（问句 + 理解卡快照 + 结果快照）供历史查看与恢复续聊。
    """

    __tablename__ = "ask_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(60), default="")   # 首问前 24 字
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # B9.3 项目工作区
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class AskMessage(Base):
    """问数消息（B9.2-6）：user 存问句原文；assistant 存理解卡/结果快照 JSON。

    结果快照是历史留档（当时算出的真实值），恢复查看不重算——续聊后的
    新一轮仍由 compute 单点出口现算，红线不变。
    """

    __tablename__ = "ask_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("ask_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))                     # user / assistant
    question: Mapped[str] = mapped_column(Text, default="")           # 该轮问句原文
    card_json: Mapped[str] = mapped_column(Text, default="{}")        # assistant：理解卡快照
    results_json: Mapped[str] = mapped_column(Text, default="[]")     # assistant：结果数组 [{label,data}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)


# ---------------------------------------------------------------- 异动检测（B10）


class AnomalyConfig(Base):
    """指标级异动检测配置（B10-1）：指标唯一，缺省用保守档默认值。

    z_threshold：|z| 超过该值判反常（默认 3.0 保守档）；min_samples：同星期几
    基准样本下限（默认 8，不足不判断——宁漏不误）。enabled=false 指标不参与
    批量检测。project_id 随 metric 冗余存储（B10 起新表一律挂 project_id）。
    """

    __tablename__ = "anomaly_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), unique=True, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    z_threshold: Mapped[float] = mapped_column(default=3.0)
    min_samples: Mapped[int] = mapped_column(Integer, default=8)
    materiality_pct: Mapped[float] = mapped_column(default=5.0)  # B10-2 要紧度：|变化率| 低于此%的异动不构成结论
    enabled: Mapped[int] = mapped_column(Integer, default=1)  # 1=参与批量检测
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now, onupdate=local_now)


class Notification(Base):
    """站内通知（B10-3 最小版）：异动结论落库即生成，纯平台内"发现→告知"闭环。

    去重语义：同 (user, metric, 异动日, 方向) 只生成一条——重复扫描不刷屏。
    read_at 非空即已读；小红点按未读计数。
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # B10 规矩：新表挂 project_id
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), index=True)
    metric_code: Mapped[str] = mapped_column(String(100))
    anomaly_date: Mapped[date] = mapped_column(Date)                 # 异动发生日
    direction: Mapped[str] = mapped_column(String(10))               # up / down
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    current: Mapped[float | None] = mapped_column(nullable=True)     # 快照：当时算出的真值
    baseline_mean: Mapped[float | None] = mapped_column(nullable=True)
    abnormality: Mapped[float | None] = mapped_column(nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=local_now)
