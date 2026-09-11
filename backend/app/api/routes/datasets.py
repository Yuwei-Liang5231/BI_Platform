"""数据集路由：上传、列表、详情、删除、预览、异常定位、表关系。

B1 阶段 1 最小版；B4 权限：上传/改名/删除/关系登记为管理员专用（D12），
查询类接口登录即可。上传走 multipart；业务异常统一 BusinessError → ApiResponse。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.api.deps import AdminUser, CurrentUser, DbDep, SettingsDep
from app.core.response import BusinessError, ok_response
from app.domain.ingestion.service import (
    dataset_to_dict,
    ingest_file,
    load_column_values,
    locate_mixed_values,
    sanitize_name,
)
from app.infra.models import Dataset, DatasetCoverage, DatasetRelation
from app.infra.repository import Repository
from app.infra.storage.paths import StoragePaths

logger = logging.getLogger("app.api.datasets")

router = APIRouter(prefix="/datasets", tags=["datasets"])

MAX_UPLOAD_MB = 200
VALID_RELATION_TYPES = {"many_to_one", "one_to_one", "one_to_many", "many_to_many"}


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
):
    """上传 CSV/XLSX 并完成接入：编码识别 → 类型推断 → Parquet → 注册（管理员专用，D12）。"""
    _ = user
    original = file.filename or "upload.csv"
    suffix = Path(original).suffix.lower()

    storage = StoragePaths(settings)
    storage.ensure_dirs()

    # 流式暂存到临时文件（不整包进内存）
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin") as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                tmp_path.unlink(missing_ok=True)
                raise BusinessError(f"文件超过 {MAX_UPLOAD_MB}MB 上限")
            tmp.write(chunk)

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
        tmp_path.unlink(missing_ok=True)

    return ok_response(_detail(db, dataset), message="数据集接入成功")


# ---------------------------------------------------------------- 查询


@router.get("")
def list_datasets(db: DbDep, _: CurrentUser):
    repo = Repository(Dataset, db)
    items = repo.list(order_by=Dataset.id)
    return ok_response(
        [
            {
                "id": ds.id,
                "name": ds.name,
                "encoding": ds.file_encoding,
                "row_count": ds.row_count,
                "column_count": ds.column_count,
                "dataset_ver": ds.dataset_ver,
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


# ---------------------------------------------------------------- 删除


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: int, db: DbDep, settings: SettingsDep, _: AdminUser):
    ds = _get_dataset(db, dataset_id)

    # 级联清理：关系（双向）、覆盖区间、文件
    db.query(DatasetRelation).filter(
        (DatasetRelation.dataset_id == dataset_id) | (DatasetRelation.target_dataset_id == dataset_id)
    ).delete(synchronize_session=False)
    db.query(DatasetCoverage).filter(DatasetCoverage.dataset_id == dataset_id).delete(synchronize_session=False)
    # 提交前先取文件路径（防对象状态变化导致取不到）
    file_path, parquet_path = ds.file_path, ds.parquet_path
    db.delete(ds)
    db.commit()

    removed, failed = 0, []
    for p in (file_path, parquet_path):
        try:
            Path(p).unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            # 不静默：文件被占用等情况下必须可追溯（元数据已删，文件成孤儿）
            logger.warning("删除数据集 %s 的文件失败 %s: %s", dataset_id, p, exc)
            failed.append(str(p))
    _ = settings
    return ok_response(
        {"id": dataset_id, "deleted": True, "files_removed": removed, "files_failed": failed},
        message="数据集已删除",
    )


# ---------------------------------------------------------------- 表关系


class RelationIn(BaseModel):
    from_column: str
    target_dataset_id: int
    target_column: str
    relation_type: str = "many_to_one"


@router.post("/{dataset_id}/relations")
def create_relation(dataset_id: int, body: RelationIn, db: DbDep, _: AdminUser):
    ds = _get_dataset(db, dataset_id)
    target = _get_dataset(db, body.target_dataset_id)

    if body.relation_type not in VALID_RELATION_TYPES:
        raise BusinessError(f"relation_type 须为 {sorted(VALID_RELATION_TYPES)} 之一")

    ds_cols = {c["name"] for c in ds_columns(ds)}
    target_cols = {c["name"] for c in ds_columns(target)}
    if body.from_column not in ds_cols:
        raise BusinessError(f"from_column {body.from_column!r} 在数据集 {ds.name} 中不存在")
    if body.target_column not in target_cols:
        raise BusinessError(f"target_column {body.target_column!r} 在数据集 {target.name} 中不存在")

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
