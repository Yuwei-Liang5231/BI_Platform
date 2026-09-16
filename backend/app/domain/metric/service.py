"""指标服务：CRUD、版本递增、变更留痕、编译存档原子同步、别名搜索。

口径变更原子生效（不变式 3）：定义变更 → 重新编译 → 存档更新 → 版本递增
→ 缓存自然失效，全部在同一个请求事务内完成（get_db 统一 commit）。
权限（写校验）留 B4 接入；owner_user_id 暂以 operator_id 占位。
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.metric.compiler import (
    CompileError,
    CompiledQuery,
    DatasetInfo,
    RelationInfo,
    calc_rule_signature,
    compile_metric,
)
from app.domain.metric.schema import CalcRuleError
from app.infra.models import Dataset, DatasetCoverage, DatasetRelation, Metric, MetricChange, MetricSqlArchive
from app.infra.repository import Repository

_CODE_RE = re.compile(r"^[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff-]{0,98}$")
VALID_STATUS = ("active", "disabled", "deleted")

SQL_AFFECTING_KEYS = ("calc_rule",)


# ---------------------------------------------------------------- 装载编译输入


def load_datasets(db: Session) -> dict[str, DatasetInfo]:
    datasets: dict[str, DatasetInfo] = {}
    for ds in Repository(Dataset, db).list():
        columns = {col["name"]: col["type"] for col in json.loads(ds.schema_json)}
        coverage: dict[str, tuple] = {}
        for cov in db.query(DatasetCoverage).filter(DatasetCoverage.dataset_id == ds.id):
            coverage[cov.column_name] = (cov.period_start, cov.period_end)
        datasets[ds.name] = DatasetInfo(id=ds.id, name=ds.name, columns=columns, coverage=coverage)
    return datasets


def load_relations(db: Session) -> list[RelationInfo]:
    result = []
    for r in Repository(DatasetRelation, db).list():
        from_ds = Repository(Dataset, db).get(r.dataset_id)
        to_ds = Repository(Dataset, db).get(r.target_dataset_id)
        if from_ds is None or to_ds is None:
            continue  # 关系指向已删除数据集，编译时忽略
        result.append(
            RelationInfo(
                from_dataset=from_ds.name,
                from_column=r.from_column,
                to_dataset=to_ds.name,
                to_column=r.target_column,
            )
        )
    return result


def compile_for_db(db: Session, calc_rule: dict) -> CompiledQuery:
    """对当前元数据状态编译；结构/语义错误统一转 BusinessError(400)。"""
    try:
        return compile_metric(calc_rule, load_datasets(db), load_relations(db))
    except (CalcRuleError, CompileError) as exc:
        raise BusinessError(str(exc), 40000) from exc


# ---------------------------------------------------------------- 序列化


def _load_json(text: str, default):
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


def metric_to_dict(m: Metric) -> dict:
    return {
        "id": m.id,
        "code": m.code,
        "name": m.name,
        "aliases": _load_json(m.aliases_json, []),
        "definition": m.definition,
        "calc_rule": _load_json(m.calc_rule_json, {}),
        "dimensions": _load_json(m.dimensions_json, []),
        "filters": _load_json(m.filters_json, {}),
        "topic": m.topic,
        "level": m.level,
        "parent_id": m.parent_id,
        "disambiguation": _load_json(m.disambiguation_json, {}),
        "primary_dataset_id": m.primary_dataset_id,
        "owner_user_id": m.owner_user_id,
        "owner_department": m.owner_department,
        "status": m.status,
        "ver": m.ver,
        "project_id": m.project_id,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


# ---------------------------------------------------------------- 校验辅助


def _validate_aliases(aliases) -> list[str]:
    if aliases is None:
        return []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise BusinessError("aliases 必须是字符串数组", 40000)
    cleaned: list[str] = []
    for a in aliases:
        a = a.strip()
        if a and a not in cleaned:
            cleaned.append(a)
    return cleaned


def _validate_dimensions(dimensions) -> list:
    if dimensions is None:
        return []
    if not isinstance(dimensions, list):
        raise BusinessError("dimensions 必须是数组（列名或 {dataset, column} 对象）", 40000)
    return dimensions


def _validate_filters(filters) -> dict:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise BusinessError("filters 必须是对象", 40000)
    return filters


def _validate_parent(db: Session, parent_id, self_id=None) -> int | None:
    if parent_id is None:
        return None
    if not isinstance(parent_id, int):
        raise BusinessError("parent_id 必须是整数或 null", 40000)
    if self_id is not None and parent_id == self_id:
        raise BusinessError("parent_id 不能指向自身", 40000)
    if Repository(Metric, db).get(parent_id) is None:
        raise BusinessError(f"父指标 {parent_id} 不存在", 40400)
    return parent_id


# ---------------------------------------------------------------- CRUD


def create_metric(
    db: Session,
    *,
    code: str,
    name: str,
    calc_rule: dict,
    aliases=None,
    definition: str = "",
    dimensions=None,
    filters=None,
    topic: str = "general",
    parent_id=None,
    disambiguation=None,
    owner_department: str = "",
    operator_id: str = "system",
    project_id: int | None = None,
) -> Metric:
    from app.domain.project.service import resolve_project_id

    if not isinstance(code, str) or not _CODE_RE.match(code.strip() or ""):
        raise BusinessError(
            "code 必须以字母/中文开头，仅含字母、数字、下划线、连字符，长度 2-99", 40000
        )
    code = code.strip()
    if not isinstance(name, str) or not name.strip():
        raise BusinessError("name 必须是非空字符串", 40000)
    proj_id = resolve_project_id(db, project_id)
    repo = Repository(Metric, db)
    # B9.3：code 唯一性收敛到项目内（不同项目可同名 code；软删 code 同项目仍占用）
    if repo.count(code=code, project_id=proj_id):
        raise BusinessError(f"指标 code {code!r} 在当前项目中已存在", 40900)

    compiled = compile_for_db(db, calc_rule)  # 保存即拒绝：编译不过不允许创建
    parent_id = _validate_parent(db, parent_id)
    level = 1 if parent_id is None else Repository(Metric, db).get(parent_id).level + 1

    metric = repo.add(
        Metric(
            code=code,
            name=name.strip(),
            aliases_json=json.dumps(_validate_aliases(aliases), ensure_ascii=False),
            definition=definition or "",
            calc_rule_json=json.dumps(calc_rule, ensure_ascii=False),
            dimensions_json=json.dumps(_validate_dimensions(dimensions), ensure_ascii=False),
            filters_json=json.dumps(_validate_filters(filters), ensure_ascii=False),
            topic=topic or "general",
            level=level,
            parent_id=parent_id,
            disambiguation_json=json.dumps(disambiguation or {}, ensure_ascii=False),
            primary_dataset_id=compiled.primary_dataset_id,
            owner_user_id=operator_id,
            owner_department=owner_department or "",
            project_id=proj_id,
        )
    )
    Repository(MetricSqlArchive, db).add(
        MetricSqlArchive(metric_id=metric.id, ver=metric.ver, sql_text=compiled.sql)
    )
    return metric


def update_metric(
    db: Session,
    metric_id: int,
    changes: dict,
    *,
    reason: str = "",
    operator_id: str = "system",
) -> Metric:
    repo = Repository(Metric, db)
    metric = repo.get(metric_id)
    if metric is None or metric.status == "deleted":
        raise BusinessError(f"指标 {metric_id} 不存在", 40400)
    before = metric_to_dict(metric)

    allowed = {
        "name", "aliases", "definition", "calc_rule", "dimensions", "filters",
        "topic", "parent_id", "disambiguation", "owner_department", "status",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise BusinessError(f"不支持修改的字段：{sorted(unknown)}", 40000)

    if "status" in changes and changes["status"] not in VALID_STATUS:
        raise BusinessError("status 只允许 active / disabled（删除请用 DELETE）", 40000)
    if "name" in changes and (not isinstance(changes["name"], str) or not changes["name"].strip()):
        raise BusinessError("name 必须是非空字符串", 40000)

    recompiled: CompiledQuery | None = None
    if "calc_rule" in changes:
        if not changes.get("reason", reason):
            raise BusinessError("修改 calc_rule（口径变更）必须提供 reason", 40000)
        recompiled = compile_for_db(db, changes["calc_rule"])

    if "parent_id" in changes:
        changes["parent_id"] = _validate_parent(db, changes["parent_id"], self_id=metric_id)

    field_map = {
        "name": lambda v: v.strip(),
        "aliases": lambda v: json.dumps(_validate_aliases(v), ensure_ascii=False),
        "definition": lambda v: v or "",
        "calc_rule": lambda v: json.dumps(v, ensure_ascii=False),
        "dimensions": lambda v: json.dumps(_validate_dimensions(v), ensure_ascii=False),
        "filters": lambda v: json.dumps(_validate_filters(v), ensure_ascii=False),
        "topic": lambda v: v or "general",
        "parent_id": lambda v: v,
        "disambiguation": lambda v: json.dumps(v or {}, ensure_ascii=False),
        "owner_department": lambda v: v or "",
        "status": lambda v: v,
    }
    column_map = {
        "name": "name", "aliases": "aliases_json", "definition": "definition",
        "calc_rule": "calc_rule_json", "dimensions": "dimensions_json",
        "filters": "filters_json", "topic": "topic", "parent_id": "parent_id",
        "disambiguation": "disambiguation_json", "owner_department": "owner_department",
        "status": "status",
    }
    updates = {}
    for key, value in changes.items():
        if key in SQL_AFFECTING_KEYS:
            continue  # calc_rule 统一走下方重编译分支
        updates[column_map[key]] = field_map[key](value)

    if recompiled is not None:
        metric.ver += 1
        updates["calc_rule_json"] = json.dumps(changes["calc_rule"], ensure_ascii=False)
        updates["primary_dataset_id"] = recompiled.primary_dataset_id
        if "parent_id" in changes:  # 口径变更可能伴随层级调整，父级变化时同步 level
            parent = (
                Repository(Metric, db).get(changes["parent_id"])
                if changes["parent_id"] is not None
                else None
            )
            updates["level"] = 1 if parent is None else parent.level + 1
    repo.update(metric, **updates)

    if recompiled is not None:
        Repository(MetricSqlArchive, db).add(
            MetricSqlArchive(metric_id=metric.id, ver=metric.ver, sql_text=recompiled.sql)
        )

    after = metric_to_dict(metric)
    if after != before:
        Repository(MetricChange, db).add(
            MetricChange(
                metric_id=metric.id,
                before_json=json.dumps(before, ensure_ascii=False, default=str),
                after_json=json.dumps(after, ensure_ascii=False, default=str),
                reason=reason,
                operator_id=operator_id,
            )
        )
    return metric


def delete_metric(db: Session, metric_id: int, *, reason: str = "", operator_id: str = "system") -> dict:
    """软删除：status=deleted，目录与搜索不再可见；留痕保留。"""
    metric = update_metric(
        db, metric_id, {"status": "deleted"}, reason=reason or "删除指标", operator_id=operator_id
    )
    return {"id": metric.id, "status": metric.status}


def list_metrics(
    db: Session,
    *,
    search: str | None = None,
    topic: str | None = None,
    status: str = "active",
    project_id: int | None = None,
) -> list[Metric]:
    """指标目录。project_id=None 表示全部项目（跨项目浏览视图）；
    指定 id 时仅返回该项目下的指标（含软删过滤语义不变）。"""
    repo = Repository(Metric, db)
    if status == "all":
        # 软删指标目录与搜索不可见（DELETE 契约），「全部状态」仅含 active/disabled/pending
        metrics = [
            m for m in repo.list(order_by=Metric.id, limit=10000) if m.status != "deleted"
        ]
    else:
        metrics = repo.list(order_by=Metric.id, limit=10000, status=status)
    if project_id is not None:
        metrics = [m for m in metrics if m.project_id == project_id]
    if topic:
        metrics = [m for m in metrics if m.topic == topic]
    if search:
        needle = search.strip().lower()
        if needle:
            metrics = [
                m for m in metrics
                if needle in m.name.lower()
                or needle in m.code.lower()
                or any(needle in alias.lower() for alias in _load_json(m.aliases_json, []))
            ]
    return metrics


def get_metric(db: Session, metric_id: int) -> Metric:
    metric = Repository(Metric, db).get(metric_id)
    if metric is None or metric.status == "deleted":
        raise BusinessError(f"指标 {metric_id} 不存在", 40400)
    return metric


def get_current_sql(db: Session, metric_id: int) -> dict:
    metric = get_metric(db, metric_id)
    archive = (
        db.query(MetricSqlArchive)
        .filter(MetricSqlArchive.metric_id == metric_id, MetricSqlArchive.ver == metric.ver)
        .first()
    )
    if archive is None:
        raise BusinessError(f"指标 {metric_id} 缺少 v{metric.ver} 编译存档（数据异常）", 50000)
    return {
        "metric_id": metric.id,
        "ver": metric.ver,
        "sql_text": archive.sql_text,
        "compiled_at": archive.compiled_at,
    }


def get_changes(db: Session, metric_id: int) -> list[dict]:
    get_metric(db, metric_id)
    rows = (
        db.query(MetricChange)
        .filter(MetricChange.metric_id == metric_id)
        .order_by(MetricChange.id.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "before": _load_json(row.before_json, {}),
            "after": _load_json(row.after_json, {}),
            "reason": row.reason,
            "operator_id": row.operator_id,
            "created_at": row.created_at,
        }
        for row in rows
    ]
