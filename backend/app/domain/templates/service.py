"""模板导入服务：把 YAML 模板包批量导入为正式指标（幂等）。

状态判定（架构 7.5）：
- requires = extended-operator → status=disabled（口径已登记、暂不可计算）；
- calc_rule 编译通过（数据集齐备）→ status=active，同步写编译存档；
- 缺数据集 / 编译未通过 → status=pending（待绑定数据集）；
  对应数据上传后再次导入（revalidate=true）自动升级为 active。

幂等：同 code 指标已存在即跳过（不重复创建、不覆盖人工修改）；
仅 pending 指标在 revalidate 且编译通过时升级。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.response import BusinessError
from app.domain.metric.compiler import compile_metric
from app.domain.metric.schema import CalcRuleError
from app.domain.metric.service import load_datasets, load_relations
from app.domain.templates import loader
from app.infra.models import Dataset, Metric, MetricSqlArchive, User

STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_ACTIVE = "active"


def _exists(db: Session, code: str) -> Metric | None:
    return db.query(Metric).filter(Metric.code == code).first()


def _referenced_tables(calc_rule: dict) -> set[str]:
    """收集 calc_rule 引用的数据集名（用于缺失提示）。"""
    tables: set[str] = set()
    source = calc_rule.get("source")
    if isinstance(source, dict) and source.get("table"):
        tables.add(source["table"])
    for operand in (calc_rule.get("operands") or {}).values():
        if isinstance(operand, dict) and operand.get("table"):
            tables.add(operand["table"])
    return tables


def _missing_datasets(db: Session, calc_rule: dict) -> list[str]:
    existing = {name for (name,) in db.query(Dataset.name).all()}
    return sorted(_referenced_tables(calc_rule) - existing)


def _try_compile(db: Session, calc_rule: dict):
    """编译成功返回 CompiledQuery；失败返回错误消息字符串。"""
    try:
        return compile_metric(calc_rule, load_datasets(db), load_relations(db))
    except (CalcRuleError, BusinessError, ValueError) as exc:
        message = exc.message if isinstance(exc, BusinessError) else str(exc)
        return message


def _create_metric(
    db: Session,
    item: dict,
    *,
    status: str,
    operator: User,
    compiled=None,
) -> Metric:
    parent = _exists(db, item["parent"]) if item["parent"] else None
    metric = Metric(
        code=item["code"],
        name=item["name"],
        aliases_json=json.dumps(item["aliases"], ensure_ascii=False),
        definition=item["definition"],
        calc_rule_json=json.dumps(item["calc_rule"], ensure_ascii=False),
        dimensions_json=json.dumps(item["dimensions"], ensure_ascii=False),
        filters_json=json.dumps({}, ensure_ascii=False),
        topic=item["topic"],
        level=1 if parent is None else parent.level + 1,
        parent_id=parent.id if parent is not None else None,
        disambiguation_json=json.dumps(item["disambiguation"], ensure_ascii=False),
        primary_dataset_id=compiled.primary_dataset_id if compiled is not None else None,
        owner_user_id=operator.username,
        status=status,
    )
    db.add(metric)
    db.flush()  # 取 id 供存档使用
    if compiled is not None:
        db.add(MetricSqlArchive(metric_id=metric.id, ver=metric.ver, sql_text=compiled.sql))
    return metric


def _import_one(db: Session, item: dict, *, revalidate: bool, operator: User) -> tuple[str, str | None]:
    """导入单条，返回 (动作, 说明)。动作 ∈ created/upgraded/skipped。"""
    existing = _exists(db, item["code"])
    if existing is not None:
        if (
            revalidate
            and existing.status == STATUS_PENDING
            and item["requires"] is None
        ):
            compiled = _try_compile(db, item["calc_rule"])
            if not isinstance(compiled, str):
                existing.status = STATUS_ACTIVE
                existing.primary_dataset_id = compiled.primary_dataset_id
                db.add(MetricSqlArchive(
                    metric_id=existing.id, ver=existing.ver, sql_text=compiled.sql
                ))
                return "upgraded", None
            return "skipped", f"{item['code']}: 仍不可编译——{compiled}"
        return "skipped", None

    if item["requires"] is not None:
        _create_metric(db, item, status=STATUS_DISABLED, operator=operator)
        return "created", f"{item['code']}: 依赖阶段 4a 扩展算子，已登记为暂不可计算"

    missing = _missing_datasets(db, item["calc_rule"])
    if missing:
        _create_metric(db, item, status=STATUS_PENDING, operator=operator)
        return "created", f"{item['code']}: 待绑定数据集 {missing}"

    compiled = _try_compile(db, item["calc_rule"])
    if isinstance(compiled, str):
        _create_metric(db, item, status=STATUS_PENDING, operator=operator)
        return "created", f"{item['code']}: 编译未通过，待修正——{compiled}"

    _create_metric(db, item, status=STATUS_ACTIVE, operator=operator, compiled=compiled)
    return "created", None


def import_packs(
    db: Session,
    *,
    industries: list[str] | None = None,
    codes: list[str] | None = None,
    revalidate: bool = False,
    operator: User,
) -> dict:
    """批量导入模板指标。industries/codes 缺省 = 全量。"""
    available = [p["industry"] for p in loader.list_packs() if not p.get("error")]
    if not available:
        directory = get_settings().resolved_templates_dir
        raise BusinessError(f"模板目录为空或全部损坏：{directory}", 50000)
    targets = industries or available
    unknown = [i for i in targets if i not in available]
    if unknown:
        raise BusinessError(f"未知行业模板：{unknown}（可用：{available}）", 40400)
    code_filter = {c.strip() for c in codes} if codes else None

    results = []
    totals = {"created": 0, "skipped": 0, "upgraded": 0, "pending": 0, "disabled": 0, "active": 0}
    for industry in targets:
        pack = loader.load_pack(industry)
        created = skipped = upgraded = 0
        notes: list[str] = []
        for item in pack["metrics"]:
            if code_filter is not None and item["code"] not in code_filter:
                continue
            action, note = _import_one(db, item, revalidate=revalidate, operator=operator)
            if note:
                notes.append(note)
            if action == "created":
                created += 1
            elif action == "upgraded":
                upgraded += 1
            else:
                skipped += 1
        db.commit()

        status_count = (
            db.query(Metric.status, Metric.id)
            .filter(
                Metric.code.in_([i["code"] for i in pack["metrics"]]),
                Metric.status != "deleted",
            )
            .all()
        )
        by_status: dict[str, int] = {}
        for status, _ in status_count:
            by_status[status] = by_status.get(status, 0) + 1

        results.append({
            "industry": industry,
            "name": pack["name"],
            "created": created,
            "skipped": skipped,
            "upgraded": upgraded,
            "status_counts": by_status,
            "notes": notes,
        })
        totals["created"] += created
        totals["skipped"] += skipped
        totals["upgraded"] += upgraded
        totals["pending"] += by_status.get(STATUS_PENDING, 0)
        totals["disabled"] += by_status.get(STATUS_DISABLED, 0)
        totals["active"] += by_status.get(STATUS_ACTIVE, 0)

    return {"results": results, "totals": totals}


def pack_detail(db: Session, industry: str) -> dict:
    """模板包明细，附带每条的库内导入状态（导入向导勾选用）。"""
    pack = loader.load_pack(industry)
    codes = [m["code"] for m in pack["metrics"]]
    rows = (
        db.query(Metric.code, Metric.status, Metric.id)
        .filter(Metric.code.in_(codes), Metric.status != "deleted")
        .all()
    )
    imported = {code: {"status": status, "metric_id": mid} for code, status, mid in rows}
    metrics = [
        {
            "code": m["code"],
            "name": m["name"],
            "aliases": m["aliases"],
            "definition": m["definition"],
            "topic": m["topic"],
            "parent": m["parent"],
            "dimensions": m["dimensions"],
            "disambiguation": m["disambiguation"],
            "requires": m["requires"],
            "calc_rule": m["calc_rule"],
            "imported": m["code"] in imported,
            "imported_status": imported.get(m["code"], {}).get("status"),
            "imported_metric_id": imported.get(m["code"], {}).get("metric_id"),
        }
        for m in pack["metrics"]
    ]
    return {
        "industry": pack["industry"],
        "name": pack["name"],
        "description": pack["description"],
        "metric_count": len(metrics),
        "metrics": metrics,
    }
