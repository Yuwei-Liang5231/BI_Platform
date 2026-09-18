"""数据集路由：上传、列表、详情、删除、预览、异常定位、表关系。

B1 阶段 1 最小版；B4 权限：上传/改名/删除/关系登记为管理员专用（D12），
查询类接口登录即可。上传走 multipart；业务异常统一 BusinessError → ApiResponse。
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.api.deps import AdminUser, CurrentUser, DbDep, SettingsDep
from app.core.response import BusinessError, ok_response
from app.domain.ingestion.quality import quality_report
from app.domain.ingestion.service import (
    dataset_to_dict,
    import_data_file,
    ingest_file,
    load_column_values,
    locate_mixed_values,
    read_dataset_table,
    sanitize_name,
)
from app.infra.models import Dataset, DatasetCoverage, DatasetRelation
from app.infra.repository import Repository
from app.infra.storage.paths import StoragePaths

logger = logging.getLogger("app.api.datasets")

router = APIRouter(prefix="/datasets", tags=["datasets"])

MAX_UPLOAD_MB = 500
VALID_RELATION_TYPES = {"many_to_one", "one_to_one", "one_to_many", "many_to_many"}


def _unlink_quiet(path: Path) -> None:
    """尽力删除临时文件；Windows 下句柄未释放等场景不能掩盖原始业务异常
    （回归：unlink 抛 PermissionError 把「超限」提示顶成 500）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("临时文件清理失败 %s: %s", path, exc)


def _recv_upload_to_temp(file: UploadFile, suffix: str) -> Path:
    """上传流式暂存到临时文件（不整包进内存），超限报业务错。

    关键顺序：先退出 with（关闭写句柄）再 unlink——Windows 不允许删除
    被占用文件，否则 PermissionError 会掩盖超限提示。
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin") as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                break
            tmp.write(chunk)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        _unlink_quiet(tmp_path)
        raise BusinessError(f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    return tmp_path


# ---------------------------------------------------------------- helpers


def _get_dataset(db, dataset_id: int) -> Dataset:
    ds = Repository(Dataset, db).get(dataset_id)
    if ds is None:
        raise BusinessError(f"数据集 {dataset_id} 不存在", 40400)
    return ds


def _coverages_of(db, dataset_id: int) -> list:
    return list(db.query(DatasetCoverage).filter(DatasetCoverage.dataset_id == dataset_id))


def _detail(db, ds: Dataset) -> dict:
    return dataset_to_dict(ds, _coverages_of(db, ds.id))


def ds_columns(ds: Dataset) -> list[dict]:
    import json

    return json.loads(ds.schema_json)


# ---------------------------------------------------------------- 上传


@router.post("/upload")
def upload_dataset(
    settings: SettingsDep,
    db: DbDep,
    user: AdminUser,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    project_id: int | None = Form(None),
):
    """上传 CSV/XLSX 并完成接入：编码识别 → 类型推断 → Parquet → 注册（管理员专用，D12）。

    B9.3：project_id 缺省归入默认项目；数据集 name 仍全局唯一（DuckDB 视图按名创建）。
    """
    _ = user
    from app.domain.project.service import resolve_project_id

    resolved_project = resolve_project_id(db, project_id)
    original = file.filename or "upload.csv"
    suffix = Path(original).suffix.lower()

    storage = StoragePaths(settings)
    storage.ensure_dirs()

    # 流式暂存到临时文件（不整包进内存）；超限在句柄关闭后清理并报业务错
    tmp_path = _recv_upload_to_temp(file, suffix)

    try:
        dataset = ingest_file(
            session=db,
            storage=storage,
            src_path=tmp_path,
            original_filename=original,
            # 关键：名称必须源自用户上传的原始文件名（或显式 name），
            # 而非服务端临时文件名
            name=name or Path(original).stem,
        )
    except PermissionError as exc:
        # Windows 常见：源文件正被 Excel/WPS 独占打开
        raise BusinessError("文件被其他程序占用（可能正被 Excel 打开），请关闭后重试") from exc
    finally:
        _unlink_quiet(tmp_path)

    dataset.project_id = resolved_project  # B9.3 项目归属
    return ok_response(_detail(db, dataset), message="数据集接入成功")


# ---------------------------------------------------------------- 查询


@router.get("")
def list_datasets(db: DbDep, _: CurrentUser, project_id: int | None = None):
    """数据集列表。project_id 缺省=None=全部项目；指定 id 仅返回该项目。"""
    repo = Repository(Dataset, db)
    items = repo.list(order_by=Dataset.id)
    if project_id is not None:
        items = [ds for ds in items if ds.project_id == project_id]
    return ok_response(
        [
            {
                "id": ds.id,
                "name": ds.name,
                "encoding": ds.file_encoding,
                "row_count": ds.row_count,
                "column_count": ds.column_count,
                "dataset_ver": ds.dataset_ver,
                "project_id": ds.project_id,
            }
            for ds in items
        ]
    )


@router.get("/{dataset_id}")
def get_dataset(dataset_id: int, db: DbDep, _: CurrentUser):
    return ok_response(_detail(db, _get_dataset(db, dataset_id)))


class RenameIn(BaseModel):
    name: str


@router.patch("/{dataset_id}/name")
def rename_dataset(dataset_id: int, body: RenameIn, db: DbDep, _: AdminUser):
    """重命名数据集（仅元数据标签，不改文件路径；Parquet/上传件不受影响）。"""
    ds = _get_dataset(db, dataset_id)
    new_name = sanitize_name(body.name)
    conflict = (
        db.query(Dataset.id)
        .filter(Dataset.name == new_name, Dataset.id != dataset_id)
        .first()
    )
    if conflict:
        raise BusinessError(f"数据集名称 {new_name!r} 已被占用", 40900)
    old_name = ds.name
    ds.name = new_name
    db.commit()
    return ok_response(
        {"id": dataset_id, "old_name": old_name, "name": new_name},
        message="数据集已重命名",
    )


@router.get("/{dataset_id}/preview")
def preview_dataset(dataset_id: int, db: DbDep, _: CurrentUser, rows: int = 20):
    """回读 Parquet 前 N 行（上限 200），无需前端即可肉眼验证数据。"""
    import pyarrow.parquet as pq

    ds = _get_dataset(db, dataset_id)
    limit = max(1, min(rows, 200))
    table = pq.read_table(ds.parquet_path).slice(0, limit)
    return ok_response(
        {
            "columns": table.column_names,
            "rows": table.to_pylist(),
            "returned": table.num_rows,
        }
    )


@router.get("/{dataset_id}/columns/{column_name}/anomalies")
def column_anomalies(dataset_id: int, column_name: str, db: DbDep, _: CurrentUser, limit: int = 50):
    """定位混杂列中偏离主导类型的值（行号 + 值 + 实际类型）。"""
    ds = _get_dataset(db, dataset_id)
    return ok_response(locate_mixed_values(ds, column_name, limit=limit))


# ---------------------------------------------------------------- 质检报告（B8）


@router.get("/{dataset_id}/quality")
def dataset_quality(dataset_id: int, db: DbDep, _: CurrentUser):
    """全量质检报告：逐列类型/空值/空列/常量列/混杂偏离（行列级定位）+ 覆盖区间。"""
    ds = _get_dataset(db, dataset_id)
    table = read_dataset_table(ds)
    return ok_response(quality_report(ds, _coverages_of(db, dataset_id), table))


# ---------------------------------------------------------------- 增量导入（B8）


@router.post("/{dataset_id}/data")
def append_dataset_data(
    dataset_id: int,
    settings: SettingsDep,
    db: DbDep,
    _: AdminUser,
    file: UploadFile = File(...),
    mode: str = Form("append"),
):
    """向既有数据集增量导入（append）或全量覆盖（replace，管理员专用）。

    成功后 dataset_ver+1（同事务）→ 相关指标缓存自然失效，覆盖区间自动刷新。"""
    ds = _get_dataset(db, dataset_id)
    original = file.filename or "data.csv"
    suffix = Path(original).suffix.lower()

    storage = StoragePaths(settings)
    tmp_path = _recv_upload_to_temp(file, suffix)

    try:
        result = import_data_file(
            session=db,
            storage=storage,
            dataset=ds,
            src_path=tmp_path,
            original_filename=original,
            mode=mode,
        )
    except PermissionError as exc:
        raise BusinessError("文件被其他程序占用（可能正被 Excel 打开），请关闭后重试") from exc
    finally:
        _unlink_quiet(tmp_path)

    db.commit()  # 增量导入显式提交：保证 ver+1 与覆盖刷新原子生效
    return ok_response({**_detail(db, _get_dataset(db, dataset_id)), "import": result}, message=f"数据{'覆盖' if mode == 'replace' else '追加'}成功")


# ---------------------------------------------------------------- 删除


def _hard_delete_dataset(db, ds: Dataset) -> dict:
    """硬删除单数据集：级联清理关系/覆盖区间，提交后删文件（逐条提交防连带回滚）。"""
    db.query(DatasetRelation).filter(
        (DatasetRelation.dataset_id == ds.id) | (DatasetRelation.target_dataset_id == ds.id)
    ).delete(synchronize_session=False)
    db.query(DatasetCoverage).filter(DatasetCoverage.dataset_id == ds.id).delete(synchronize_session=False)
    # 提交前先取文件路径（防对象状态变化导致取不到）
    file_path, parquet_path = ds.file_path, ds.parquet_path
    dataset_id, dataset_name = ds.id, ds.name
    db.delete(ds)
    db.commit()

    removed, failed = 0, []
    for p in (file_path, parquet_path):
        try:
            path = Path(p)
            if path.is_dir():  # B8 分片目录：先逐文件删（句柄占用可定位），再删空目录
                for f in sorted(path.rglob("*"), reverse=True):
                    if f.is_file():
                        f.unlink(missing_ok=True)
                    elif f.is_dir():
                        f.rmdir()
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            # 不静默：文件被占用等情况下必须可追溯（元数据已删，文件成孤儿）
            logger.warning("删除数据集 %s 的文件失败 %s: %s", dataset_id, p, exc)
            failed.append(str(p))
    return {"id": dataset_id, "name": dataset_name, "files_removed": removed, "files_failed": failed}


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: int, db: DbDep, _: AdminUser):
    ds = _get_dataset(db, dataset_id)
    result = _hard_delete_dataset(db, ds)
    _ = result
    return ok_response(
        {"id": dataset_id, "deleted": True, "files_removed": result["files_removed"], "files_failed": result["files_failed"]},
        message="数据集已删除",
    )


class BatchDeleteIn(BaseModel):
    ids: list[int]


@router.post("/batch-delete")
def batch_delete_datasets(body: BatchDeleteIn, db: DbDep, _: AdminUser):
    """批量删除数据集（admin）。逐条独立提交：单条失败不影响已成功项，
    响应逐条回执（成功/失败原因），前端据此汇总提示。"""
    if not body.ids:
        raise BusinessError("未选择要删除的数据集")
    ok_items, failed_items = [], []
    for ds_id in body.ids:
        ds = db.query(Dataset).filter(Dataset.id == ds_id).first()
        if ds is None:
            failed_items.append({"id": ds_id, "reason": "数据集不存在（可能已删除）"})
            continue
        try:
            ok_items.append(_hard_delete_dataset(db, ds))
        except BusinessError as exc:
            db.rollback()
            failed_items.append({"id": ds_id, "name": ds.name, "reason": exc.message})
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断批次
            db.rollback()
            logger.exception("批量删除数据集 %s 失败", ds_id)
            failed_items.append({"id": ds_id, "name": ds.name, "reason": str(exc)})
    return ok_response(
        {"deleted": [i["id"] for i in ok_items], "failed": failed_items},
        message=f"已删除 {len(ok_items)} 个数据集" + (f"，{len(failed_items)} 个失败" if failed_items else ""),
    )


# ---------------------------------------------------------------- 表关系


class RelationIn(BaseModel):
    from_column: str
    target_dataset_id: int
    target_column: str
    relation_type: str = "many_to_one"
    # P2 复合键：[[from, target], ...]（含首对，须与 from_column/target_column 一致）；
    # 缺省/None = 单列键
    column_pairs: list[list[str]] | None = None


def _normalize_column_pairs(body: RelationIn) -> list[list[str]] | None:
    """复合键校验与归一：非空、每对恰好 2 个非空列名、无重复对、首对须与
    from_column/target_column 一致（存储上首对冗余存放于 from/target_column，
    编译器 pairs 属性据此退化兼容）。返回 None 表示单列键。"""
    if body.column_pairs is None:
        return None
    pairs = body.column_pairs
    if not pairs:
        raise BusinessError("column_pairs 提供时不能为空数组（复合键至少 1 对）")
    norm: list[list[str]] = []
    for i, p in enumerate(pairs):
        if not isinstance(p, list) or len(p) != 2 or not all(isinstance(x, str) and x.strip() for x in p):
            raise BusinessError(f"column_pairs[{i}] 须为 [from_column, target_column] 两个非空字符串")
        norm.append([p[0].strip(), p[1].strip()])
    if norm[0] != [body.from_column.strip(), body.target_column.strip()]:
        raise BusinessError("column_pairs 首对须与 from_column/target_column 一致")
    seen = {tuple(p) for p in norm}
    if len(seen) != len(norm):
        raise BusinessError("column_pairs 存在重复列对")
    return norm


@router.post("/{dataset_id}/relations")
def create_relation(dataset_id: int, body: RelationIn, db: DbDep, _: AdminUser):
    ds = _get_dataset(db, dataset_id)
    target = _get_dataset(db, body.target_dataset_id)

    # B9.3：表关系禁止跨项目（一跳星型关系只服务于同项目事实/维表）
    if (ds.project_id or 0) != (target.project_id or 0):
        raise BusinessError(
            f"数据集 {ds.name}（项目 {ds.project_id}）与 {target.name}（项目 {target.project_id}）"
            "不属于同一项目，禁止跨项目登记表关系"
        )

    if body.relation_type not in VALID_RELATION_TYPES:
        raise BusinessError(f"relation_type 须为 {sorted(VALID_RELATION_TYPES)} 之一")

    pairs = _normalize_column_pairs(body)

    ds_cols = {c["name"] for c in ds_columns(ds)}
    target_cols = {c["name"] for c in ds_columns(target)}
    check_pairs = pairs if pairs is not None else [[body.from_column, body.target_column]]
    for fk, tk in check_pairs:
        if fk not in ds_cols:
            raise BusinessError(f"from_column {fk!r} 在数据集 {ds.name} 中不存在")
        if tk not in target_cols:
            raise BusinessError(f"target_column {tk!r} 在数据集 {target.name} 中不存在")

    dup = (
        db.query(DatasetRelation)
        .filter(
            DatasetRelation.dataset_id == dataset_id,
            DatasetRelation.from_column == body.from_column,
            DatasetRelation.target_dataset_id == body.target_dataset_id,
            DatasetRelation.target_column == body.target_column,
        )
        .first()
    )
    if dup:
        raise BusinessError("相同关系已存在", 40900)

    rel = DatasetRelation(
        dataset_id=dataset_id,
        from_column=body.from_column,
        target_dataset_id=body.target_dataset_id,
        target_column=body.target_column,
        # 首对冗余存于 from/target_column（单列键零差异）；完整列对存 JSON
        column_pairs=json.dumps(pairs, ensure_ascii=False) if pairs is not None else None,
        relation_type=body.relation_type,
    )
    db.add(rel)
    db.commit()
    return ok_response(
        {
            "id": rel.id,
            "dataset_id": rel.dataset_id,
            "from_column": rel.from_column,
            "target_dataset_id": rel.target_dataset_id,
            "target_dataset": target.name,
            "target_column": rel.target_column,
            "column_pairs": pairs,
            "relation_type": rel.relation_type,
        },
        message="关系已注册",
    )


@router.get("/{dataset_id}/relations")
def list_relations(dataset_id: int, db: DbDep, _: CurrentUser):
    _get_dataset(db, dataset_id)
    rels = db.query(DatasetRelation).filter(DatasetRelation.dataset_id == dataset_id).all()
    return ok_response(
        [
            {
                "id": r.id,
                "from_column": r.from_column,
                "target_dataset_id": r.target_dataset_id,
                "target_column": r.target_column,
                "column_pairs": (
                    json.loads(r.column_pairs)
                    if getattr(r, "column_pairs", None)
                    else [[r.from_column, r.target_column]]
                ),
                "relation_type": r.relation_type,
                "created_by": r.created_by,
            }
            for r in rels
        ]
    )


@router.delete("/{dataset_id}/relations/{relation_id}")
def delete_relation(dataset_id: int, relation_id: int, db: DbDep, _: AdminUser):
    _get_dataset(db, dataset_id)
    rel = db.query(DatasetRelation).filter(DatasetRelation.id == relation_id).first()
    if rel is None or rel.dataset_id != dataset_id:
        raise BusinessError(f"关系 {relation_id} 不存在", 40400)
    db.delete(rel)
    db.commit()
    return ok_response({"id": relation_id, "deleted": True})
