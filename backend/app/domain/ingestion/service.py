"""接入服务：上传文件 → 编码识别 → 类型推断 → Parquet 落盘 → 元数据注册。

设计要点：
- 两遍流式扫描，内存占用与文件大小解耦（第一遍推断+质检，第二遍写 Parquet）
- 列数不一致的行：明确拒绝并给出前 10 个行号（质检哲学：拒绝静默修正）
- 混杂类型列存储为 string，并在 schema 中标记 mixed=true
- 行业无关（不变式 7）：本模块只认识行、列、类型，不认识任何业务语义
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.domain.ingestion.encoding import detect_encoding
from app.domain.ingestion.types import (
    NULL_WORDS,
    T_BOOL,
    T_DATE,
    T_DATETIME,
    T_FLOAT,
    T_INT,
    T_MIXED,
    ColumnStat,
    classify_value,
    normalize_header,
    parse_date_value,
    parse_datetime_value,
    scan_rows,
)
from app.infra.models import Dataset, DatasetCoverage
from app.infra.storage.paths import StoragePaths

SUPPORTED_EXTS = {".csv", ".xlsx"}
_PARQUET_BATCH = 50_000
_NAME_SANITIZE_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")

logger = logging.getLogger("app.domain.ingestion")


# ---------------------------------------------------------------- helpers


def sanitize_name(raw: str) -> str:
    name = _NAME_SANITIZE_RE.sub("_", (raw or "").strip()).strip("_")
    if not name:
        raise BusinessError("数据集名称非法（仅允许中英文、数字、下划线、连字符）")
    if len(name) > 190:
        name = name[:190]
    return name


def unique_name(session: Session, base: str) -> str:
    """名称唯一化：冲突时追加 _2、_3…（调用前须先 sanitize）。"""
    if not session.query(Dataset.id).filter(Dataset.name == base).first():
        return base
    i = 2
    while session.query(Dataset.id).filter(Dataset.name == f"{base}_{i}").first():
        i += 1
    return f"{base}_{i}"


def _open_rows(path: Path, encoding: str) -> tuple[list[str], Iterator[list[str]]]:
    """CSV 迭代器：流式读取，delimiter 自动嗅探；迭代结束后自动关闭句柄。"""
    f = open(path, "r", encoding="utf-8-sig" if encoding == "utf-8" else encoding, newline="")
    try:
        sample_line = f.readline()
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample_line, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(f, dialect)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise BusinessError("文件没有表头行") from exc
    except BaseException:
        f.close()
        raise

    def gen() -> Iterator[list[str]]:
        try:
            yield from reader
        finally:
            f.close()

    return normalize_header(header), gen()


def _arrow_type(col_type: str) -> pa.DataType:
    return {
        T_INT: pa.int64(),
        T_FLOAT: pa.float64(),
        T_DATE: pa.date32(),
        T_DATETIME: pa.timestamp("s"),
        T_BOOL: pa.bool_(),
    }.get(col_type, pa.string())


def _convert(raw: str, col_type: str):
    """按列类型转换；空串/NULL 字面量 → None。混杂/字符串列原样保留。"""
    v = raw.strip()
    if not v or v.lower() in NULL_WORDS:
        return None
    if col_type == T_INT:
        return int(v)
    if col_type == T_FLOAT:
        return float(v)
    if col_type == T_DATE:
        return parse_date_value(v)
    if col_type == T_DATETIME:
        return parse_datetime_value(v)
    if col_type == T_BOOL:
        return v.lower() == "true"
    return v


def _write_parquet(
    path: Path,
    header: list[str],
    col_types: list[str],
    rows: Iterator[list[str]],
    row_count: int,
) -> None:
    schema = pa.schema([pa.field(name, _arrow_type(t)) for name, t in zip(header, col_types)])
    writer = pq.ParquetWriter(path, schema)
    buffers: list[list] = [[] for _ in header]
    row_no = 0
    try:
        for row in rows:
            row_no += 1
            if len(row) != len(header):  # 第一遍已拦截，防御性跳过
                continue
            for i, raw in enumerate(row):
                try:
                    buffers[i].append(_convert(raw, col_types[i]))
                except (ValueError, OverflowError) as exc:
                    raise BusinessError(
                        f"第 {row_no} 行第 {i + 1} 列（{header[i]}）值 {raw!r} 无法转换为 {col_types[i]}：{exc}"
                    ) from exc
            if (len(buffers[0])) >= _PARQUET_BATCH:
                _flush(writer, schema, header, buffers)
        _flush(writer, schema, header, buffers)
    finally:
        writer.close()
    _ = row_count


def _flush(writer: pq.ParquetWriter, schema: pa.Schema, header: list[str], buffers: list[list]) -> None:
    if not buffers[0]:
        return
    arrays = []
    for i in range(len(header)):
        try:
            arrays.append(pa.array(buffers[i], type=schema.field(header[i]).type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
            # 典型场景：数值超出 int64、文本落在强类型列
            raise BusinessError(
                f"列 {header[i]!r} 存在无法转换为 {schema.field(header[i]).type} 的值（如超出 int64 范围的大整数）：{exc}"
            ) from exc
    writer.write_table(pa.table(dict(zip(header, arrays)), schema=schema))
    for buf in buffers:
        buf.clear()


def _iter_xlsx_rows(path: Path) -> tuple[list[str], Iterator[list[str]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        raw_header = next(rows)
    except StopIteration as exc:
        wb.close()
        raise BusinessError("Excel 没有表头行") from exc
    header = normalize_header([(str(c) if c is not None else "") for c in raw_header])

    def gen() -> Iterator[list[str]]:
        try:
            for row in rows:
                yield [("" if c is None else str(c)) for c in row]
        finally:
            wb.close()

    return header, gen()


# ---------------------------------------------------------------- 主流程


def ingest_file(
    *,
    session: Session,
    storage: StoragePaths,
    src_path: Path,
    original_filename: str,
    name: str | None = None,
) -> Dataset:
    """完整接入流程。任何质检失败都会抛 BusinessError，不产生半成品注册。"""
    ext = src_path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BusinessError(f"不支持的文件类型 {ext!r}，当前支持：{', '.join(sorted(SUPPORTED_EXTS))}")

    storage.ensure_dirs()
    final_name = unique_name(session, sanitize_name(name or src_path.stem))

    # 编码识别（xlsx 不需要）
    encoding = "xlsx" if ext == ".xlsx" else detect_encoding(src_path)

    # 保存原始上传件（保留原编码，供审计与重放）
    dest = storage.uploads_dir / f"{final_name}{ext}"
    if src_path.resolve() != dest.resolve():
        dest.write_bytes(src_path.read_bytes())

    if ext == ".xlsx":
        header, rows_iter = _iter_xlsx_rows(src_path)
        stats, row_count, ragged = scan_rows(header, rows_iter)
    else:
        # 采样识别可能被"前段纯 ASCII + 后段 GBK"的文件骗过（头部判成 utf-8，
        # 流式解码到中后段才炸 UnicodeDecodeError）→ 按候选编码依次整文件重试
        candidates = [encoding] + [e for e in ("gbk", "gb18030") if e != encoding]
        last_error: UnicodeDecodeError | None = None
        for enc in candidates:
            try:
                header, rows_iter = _open_rows(src_path, enc)
                stats, row_count, ragged = scan_rows(header, rows_iter)
                if enc != encoding:
                    logger.warning(
                        "编码重试生效: 采样判定 %s，实际按 %s 解码成功 (%s)",
                        encoding, enc, src_path.name,
                    )
                    encoding = enc
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        else:
            raise BusinessError(
                "文件无法用已支持的编码（UTF-8 / GBK / GB18030）完整解码，疑似混合编码；"
                "请将文件另存为 UTF-8 后重试",
                data={"byte_position": last_error.start if last_error else None},
            )
    if ragged:
        raise BusinessError(
            f"列数不一致：{len(ragged)}+ 行结构异常，首见数据行号 {ragged}（1-based，最多列前 10 个）",
            data={"ragged_rows": ragged},
        )
    if row_count == 0:
        raise BusinessError("文件没有数据行")

    col_types = [s.final_type() for s in stats]

    # 第二遍：写 Parquet（B8 起统一分片目录布局：data/parquet/{name}/part-0001.parquet，
    # 增量导入直接追加新分片，无需迁移）
    dataset_dir = storage.parquet_dir / final_name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = dataset_dir / "part-0001.parquet"
    header2, rows_iter2 = _iter_xlsx_rows(src_path) if ext == ".xlsx" else _open_rows(src_path, encoding)
    assert header2 == header  # 同一文件两遍表头一致
    _write_parquet(parquet_path, header, col_types, rows_iter2, row_count)

    # 注册元数据
    dataset = Dataset(
        name=final_name,
        source_filename=original_filename,
        file_path=str(dest),
        parquet_path=str(dataset_dir),
        file_encoding=encoding,
        row_count=row_count,
        column_count=len(header),
        schema_json=json.dumps([s.to_dict() for s in stats], ensure_ascii=False),
    )
    session.add(dataset)
    session.flush()

    for stat in stats:
        if stat.final_type() in (T_DATE, T_DATETIME) and stat.min_date is not None:
            session.add(
                DatasetCoverage(
                    dataset_id=dataset.id,
                    column_name=stat.name,
                    period_start=stat.min_date,
                    period_end=stat.max_date,
                    non_null_count=stat.non_null_count,
                )
            )
    session.flush()
    return dataset


# ---------------------------------------------------------------- 查询辅助


def dataset_to_dict(dataset: Dataset, coverages: list[DatasetCoverage] | None = None) -> dict:
    detail = {
        "id": dataset.id,
        "name": dataset.name,
        "source_filename": dataset.source_filename,
        "encoding": dataset.file_encoding,
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
        "columns": json.loads(dataset.schema_json),
        "dataset_ver": dataset.dataset_ver,
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
    }
    if coverages is not None:
        detail["coverage"] = [
            {
                "column": c.column_name,
                "period_start": c.period_start.isoformat(),
                "period_end": c.period_end.isoformat(),
                "non_null_count": c.non_null_count,
            }
            for c in coverages
        ]
    return detail


def load_column_values(dataset: Dataset, column: str) -> list[str]:
    """读取 Parquet 单列（字符串视图），用于预览与异常定位。"""
    table = pq.read_table(dataset.parquet_path, columns=[column])
    return ["" if v is None else str(v) for v in table.column(column).to_pylist()]


# ---------------------------------------------------------------- 增量导入（B8）


def view_target(parquet_path: str) -> str:
    """数据集存储路径 → DuckDB read_parquet 目标。

    B8 起数据集落盘为分片目录（data/parquet/{name}/part-*.parquet），
    目录路径展开为 glob；存量单文件数据集原样返回。"""
    p = Path(parquet_path)
    if p.is_dir():
        return (p / "*.parquet").as_posix()
    return parquet_path


def read_dataset_table(dataset: Dataset) -> pa.Table:
    """读取数据集全部数据（文件或分片目录，pyarrow dataset API 自动发现）。"""
    return pq.read_table(dataset.parquet_path)


def _next_part_path(dataset_dir: Path) -> Path:
    existing = sorted(dataset_dir.glob("part-*.parquet"))
    n = len(existing) + 1
    return dataset_dir / f"part-{n:04d}.parquet"


def refresh_coverage_from_table(session: Session, dataset: Dataset, table: pa.Table) -> None:
    """按当前数据重算覆盖区间并整体替换 dataset_coverage 行（导入事务内调用）。"""
    import pyarrow.compute as pc

    session.query(DatasetCoverage).filter(DatasetCoverage.dataset_id == dataset.id).delete(
        synchronize_session=False
    )
    for field in table.schema:
        if not pa.types.is_date(field.type) and not pa.types.is_timestamp(field.type):
            continue
        col = table.column(field.name)
        non_null = col.drop_null()
        if non_null.length() == 0:
            continue
        session.add(
            DatasetCoverage(
                dataset_id=dataset.id,
                column_name=field.name,
                period_start=pc.min(non_null).as_py(),
                period_end=pc.max(non_null).as_py(),
                non_null_count=non_null.length(),
            )
        )
    session.flush()


def import_data_file(
    *,
    session: Session,
    storage: StoragePaths,
    dataset: Dataset,
    src_path: Path,
    original_filename: str,
    mode: str = "append",
) -> dict:
    """向既有数据集增量导入（append）或全量覆盖（replace）。B8。

    流程：编码识别 → 表头列名校验（集合一致，顺序可不同）→ 按现有列类型
    两遍流式转换写分片 → row_count/覆盖区间刷新 → dataset_ver+1（同事务，
    由 get_db 统一提交）。任何校验失败都不落盘不递增。
    """
    if mode not in ("append", "replace"):
        raise BusinessError("mode 须为 append 或 replace")
    ext = src_path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BusinessError(f"不支持的文件类型 {ext!r}，当前支持：{', '.join(sorted(SUPPORTED_EXTS))}")
    storage.ensure_dirs()

    existing_cols = json.loads(dataset.schema_json)
    existing_names = [c["name"] for c in existing_cols]
    existing_types = {c["name"]: c["type"] for c in existing_cols}

    # ---- 读取与校验（与首次上传同款编码重试逻辑）----
    if ext == ".xlsx":
        header, rows_iter = _iter_xlsx_rows(src_path)
        stats, row_count, ragged = scan_rows(header, rows_iter)
        encoding = "xlsx"
    else:
        encoding = detect_encoding(src_path)
        candidates = [encoding] + [e for e in ("gbk", "gb18030") if e != encoding]
        last_error: UnicodeDecodeError | None = None
        for enc in candidates:
            try:
                header, rows_iter = _open_rows(src_path, enc)
                stats, row_count, ragged = scan_rows(header, rows_iter)
                if enc != encoding:
                    logger.warning("编码重试生效: 采样判定 %s，实际按 %s 解码成功 (%s)", encoding, enc, src_path.name)
                    encoding = enc
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        else:
            raise BusinessError(
                "文件无法用已支持的编码（UTF-8 / GBK / GB18030）完整解码；请将文件另存为 UTF-8 后重试",
                data={"byte_position": last_error.start if last_error else None},
            )
    if ragged:
        raise BusinessError(
            f"列数不一致：{len(ragged)}+ 行结构异常，首见数据行号 {ragged}（1-based，最多列前 10 个）",
            data={"ragged_rows": ragged},
        )
    if row_count == 0:
        raise BusinessError("文件没有数据行")

    missing = [n for n in existing_names if n not in set(header)]
    extra = [n for n in header if n not in set(existing_names)]
    if missing or extra:
        raise BusinessError(
            "表头与现有数据集不一致，增量导入要求列名集合完全一致（顺序可不同）",
            data={"missing_columns": missing, "extra_columns": extra},
        )

    # 按现有 schema 顺序重排列；类型沿用现有 schema（不重新推断）
    col_types = [existing_types[n] for n in existing_names]
    reorder = [header.index(n) for n in existing_names]

    def reordered(rows: Iterator[list[str]]) -> Iterator[list[str]]:
        for row in rows:
            yield [row[i] for i in reorder]

    # ---- 落盘（安全顺序：先写临时分片，全部成功后才动既有数据；
    #      任何失败只留下待清理的临时文件，既有分片/单文件原样不动）----
    old_path = Path(dataset.parquet_path)  # 可能是单文件（存量）或分片目录（B8 起）
    is_dir_layout = old_path.is_dir()
    dataset_dir = old_path if is_dir_layout else old_path.parent / dataset.name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = dataset_dir / f".tmp-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.parquet"

    header2, rows_iter2 = (
        _iter_xlsx_rows(src_path) if ext == ".xlsx" else _open_rows(src_path, encoding)
    )
    try:
        _write_parquet(tmp_path, existing_names, col_types, reordered(rows_iter2), row_count)
    except BaseException:
        # 显式关闭底层 CSV 迭代器（Windows 句柄释放后才可能清理临时文件）
        close = getattr(rows_iter2, "close", None)
        if close is not None:
            close()
        tmp_path.unlink(missing_ok=True)
        raise

    if mode == "append" and not is_dir_layout:
        # 存量单文件迁移：旧文件移入目录为 part-0001，新数据紧随其后
        old_path.rename(dataset_dir / "part-0001.parquet")
    if mode == "replace":
        # 全量覆盖：清掉既有分片/单文件后，新数据成为 part-0001
        if is_dir_layout:
            for old in dataset_dir.glob("part-*.parquet"):
                old.unlink()
        else:
            old_path.unlink()
    target = dataset_dir / "part-0001.parquet" if mode == "replace" else _next_part_path(dataset_dir)
    tmp_path.rename(target)
    dataset.parquet_path = str(dataset_dir)

    # 原始件留档（审计与重放）
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    audit_path = storage.uploads_dir / f"{dataset.name}__{mode}__{ts}{ext}"
    try:
        audit_path.write_bytes(src_path.read_bytes())
    except OSError as exc:
        logger.warning("增量导入原始件留档失败 %s: %s", audit_path, exc)

    # ---- 元数据刷新（同事务）----
    if mode == "replace":
        dataset.row_count = row_count
        rows_added = row_count
    else:
        dataset.row_count += row_count
        rows_added = row_count
    dataset.file_encoding = encoding
    dataset.dataset_ver += 1

    table = read_dataset_table(dataset)
    refresh_coverage_from_table(session, dataset, table)

    return {
        "mode": mode,
        "rows_added": rows_added,
        "dataset_ver": dataset.dataset_ver,
        "row_count": dataset.row_count,
        "part_file": target.name,
        "audit_file": audit_path.name,
    }


def locate_mixed_values(dataset: Dataset, column: str, limit: int = 50) -> dict:
    """定位混杂列中偏离主导类型的值（B1 最小"异常行定位"能力）。

    返回主导类型、各类型计数、以及前 limit 个偏离值（含 1-based 数据行号）。
    """
    stats = {c["name"]: c for c in json.loads(dataset.schema_json)}
    col_stat = stats.get(column)
    if col_stat is None:
        raise BusinessError(f"列 {column!r} 不存在")
    if not col_stat.get("mixed"):
        return {"column": column, "mixed": False, "deviations": []}

    values = load_column_values(dataset, column)
    counter: Counter[str] = Counter()
    parsed: list[tuple[int, str, str]] = []  # (row_no, value, type)
    for i, v in enumerate(values):
        kind = classify_value(v)
        if kind == "":
            continue
        counter[kind] += 1
        parsed.append((i + 1, v, kind))

    if not counter:
        return {"column": column, "mixed": False, "deviations": []}
    dominant = counter.most_common(1)[0][0]
    deviations = [
        {"row": row_no, "value": v, "value_type": kind}
        for row_no, v, kind in parsed
        if kind != dominant
    ][: max(1, min(limit, 500))]
    return {
        "column": column,
        "mixed": True,
        "dominant_type": dominant,
        "type_counts": dict(counter),
        "deviations": deviations,
    }
