"""接入服务：上传文件 → 编码识别 → 类型推断 → Parquet 落盘 → 元数据注册。

设计要点：
- 单遍流式解析（B14.1 性能改造）：第一遍扫描推断类型的同时把字符串行旁路写入
  临时 parquet，类型确定后用 DuckDB 向量化 TRY_CAST 一次性物化为最终强类型
  parquet——xlsx 不再二次解析（openpyxl 解析 200MB/48 万行文件占整程一半以上），
  内存占用与文件大小解耦
- 列数不一致的行：明确拒绝并给出前 10 个行号（质检哲学：拒绝静默修正）
- 转换失败仍显式报错：DuckDB TRY_CAST 失败计数 + 定位样本行（拒绝静默置 NULL）
- 混杂类型列存储为 string，并在 schema 中标记 mixed=true
- 行业无关（不变式 7）：本模块只认识行、列、类型，不认识任何业务语义
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
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
    T_BOOL,
    T_DATE,
    T_DATETIME,
    T_FLOAT,
    T_INT,
    ColumnStat,
    classify_value,
    normalize_header,
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


def _stage_rows(
    rows: Iterator[list[str]], header: list[str], stage_path: Path
) -> Iterator[list[str]]:
    """行旁路缓存（B14.1）：扫描统计的同时把字符串行流式写入临时 parquet。

    包装行迭代器（yield-through），使 xlsx/CSV 只解析一遍：scan_rows 消费行的
    同时分批写入全字符串 schema 的临时分片，类型确定后由
    _materialize_typed_parquet 向量化转换，替代旧的"第二遍重新解析文件"。
    """
    schema = pa.schema([pa.field(n, pa.string()) for n in header])
    writer = pq.ParquetWriter(stage_path, schema)
    buf: list[list[str]] = []
    n_cols = len(header)

    def _flush() -> None:
        if not buf:
            return
        arrays = [pa.array([row[i] for row in buf], type=pa.string()) for i in range(n_cols)]
        writer.write_table(pa.table(dict(zip(header, arrays)), schema=schema))
        buf.clear()

    def gen() -> Iterator[list[str]]:
        try:
            for row in rows:
                if len(row) == n_cols:  # ragged 行只计数报错，不入缓存
                    buf.append(row)
                    if len(buf) >= _PARQUET_BATCH:
                        _flush()
                yield row
        finally:
            _flush()
            writer.close()

    return gen()


_SQL_CAST_TYPE = {
    T_INT: "BIGINT",
    T_FLOAT: "DOUBLE",
    T_DATE: "DATE",
    T_DATETIME: "TIMESTAMP",
    T_BOOL: "BOOLEAN",
}


def _quote_ident(name: str) -> str:
    """SQL 标识符双引号包裹（列名可能含中文/保留字）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _typed_expr(col: str, col_type: str) -> str:
    """列转换表达式：NULL 归一（空串/null 字面量）+ 强类型 TRY_CAST。"""
    q = _quote_ident(col)
    guard = f"WHEN trim({q}) = '' OR lower(trim({q})) = 'null' THEN NULL"
    sql_t = _SQL_CAST_TYPE.get(col_type)
    if sql_t is None:  # string/mixed：原样保留（去首尾空白）
        return f"CASE {guard} ELSE trim({q}) END"
    return f"CASE {guard} ELSE TRY_CAST(trim({q}) AS {sql_t}) END"


def _typed_fail_cond(col: str, col_type: str) -> str:
    q = _quote_ident(col)
    sql_t = _SQL_CAST_TYPE[col_type]
    return (
        f"trim({q}) <> '' AND lower(trim({q})) <> 'null' "
        f"AND TRY_CAST(trim({q}) AS {sql_t}) IS NULL"
    )


def _materialize_typed_parquet(
    stage_path: Path,
    out_path: Path,
    out_columns: list[str],
    col_types: dict[str, str],
) -> None:
    """把字符串临时分片向量化物化为最终强类型 parquet（DuckDB TRY_CAST）。

    质检哲学不放松：非字符串列存在无法转换的值时显式报错（计数 + 前 3 个
    坏值样本及所在数据行号），绝不静默置 NULL。
    """
    import duckdb

    con = duckdb.connect()
    src = stage_path.as_posix().replace("'", "''")
    try:
        # 逐强类型列校验转换失败数（全量，非采样）
        for name in out_columns:
            t = col_types[name]
            if t not in _SQL_CAST_TYPE:
                continue
            cond = _typed_fail_cond(name, t)
            cnt = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{src}') WHERE {cond}"
            ).fetchone()[0]
            if cnt:
                samples = con.execute(
                    f"SELECT rn, val FROM ("
                    f"  SELECT row_number() OVER () AS rn, {_quote_ident(name)} AS val"
                    f"  FROM read_parquet('{src}')"
                    f") t WHERE {_typed_fail_cond('val', t)} ORDER BY rn LIMIT 3"
                ).fetchall()
                detail = "、".join(f"第 {int(rn)} 行 {val!r}" for rn, val in samples)
                raise BusinessError(
                    f"列 {name!r} 存在 {cnt} 个无法转换为 {t} 的值（{detail}）"
                )

        exprs = ", ".join(
            f"{_typed_expr(n, col_types[n])} AS {_quote_ident(n)}" for n in out_columns
        )
        schema = pa.schema([pa.field(n, _arrow_type(col_types[n])) for n in out_columns])
        writer = pq.ParquetWriter(out_path, schema)
        try:
            reader = con.execute(
                f"SELECT {exprs} FROM read_parquet('{src}')"
            ).fetch_record_batch(_PARQUET_BATCH)
            for batch in reader:
                # safe=False 仅放宽 timestamp 亚秒截断；值合法性已由 TRY_CAST 校验
                writer.write_table(pa.Table.from_batches([batch]).cast(schema, safe=False))
        finally:
            writer.close()
    finally:
        con.close()


# openpyxl BUILTIN_FORMATS 只定义到 49；东亚版 Excel/WPS 的中文日期内置
# numFmtId（27-36、50-58，如 58 = yyyy"年"m"月"d"日"）会 fallback 成 "General"，
# is_date 判否 → 日期列返回裸序列号（46295）。这里显式补认这批 ID。
_EA_BUILTIN_DATE_FMT_IDS = frozenset(range(27, 37)) | frozenset(range(50, 59))


def _is_xlsx_date_cell(cell) -> bool:
    """日期格式判定：openpyxl is_date + 东亚内置日期 ID 兜底。"""
    if cell.is_date:
        return True
    try:
        return cell.has_style and cell.style_array.numFmtId in _EA_BUILTIN_DATE_FMT_IDS
    except (AttributeError, IndexError, KeyError):
        return False


def _iter_xlsx_rows(path: Path) -> tuple[list[str], Iterator[list[str]]]:
    """xlsx → (表头, 字符串行迭代器)。

    优先 python-calamine（Rust 引擎，实测比 openpyxl 快 5~10 倍，日期按样式
    原生解析——46295 序列号问题不存在）；未安装时回退 openpyxl（慢但零依赖）。
    两条路径输出完全同构：日期 → ISO 字符串（零点归 date），数值 → 整数无
    小数点、小数保原样，空 → ""。
    """
    try:
        from python_calamine import CalamineWorkbook  # noqa: F401
    except ImportError:
        return _iter_xlsx_rows_openpyxl(path)
    return _iter_xlsx_rows_calamine(path)


def _norm_xlsx_number(v: float | int) -> str:
    """数值单元格 → 字符串：整数值去掉 .0 后缀（calamine 一律返回 float，
    若直接 str 会把整数列变 '100.0'，破坏 int 推断与既有分片的 append 兼容）。"""
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return str(int(v))
    return str(v)


def _iter_xlsx_rows_calamine(path: Path) -> tuple[list[str], Iterator[list[str]]]:
    from datetime import date as _date, datetime as _datetime

    from openpyxl.utils.datetime import from_excel
    from python_calamine import CalamineWorkbook

    wb = CalamineWorkbook.from_path(str(path))
    rows = iter(wb.get_sheet_by_index(0).to_python(skip_empty_area=False))
    try:
        raw_header = next(rows)
    except StopIteration as exc:
        raise BusinessError("Excel 没有表头行") from exc
    header = normalize_header([(str(v) if v is not None else "") for v in raw_header])

    # calamine 与 openpyxl 一样不认识东亚内置日期 numFmtId（27-36/50-58），
    # 这些列会返回裸序列号。用 openpyxl 只读前 25 行（read_only 惰性解析，
    # 不触发全文件扫描，开销毫秒级）探测该类列，值路径再按纪元换算。
    ea_cols = _detect_ea_date_columns(path)

    def _cell_str(v) -> str:
        if v is None:
            return ""
        if isinstance(v, _datetime):
            # 零点输出纯日期（月末快照类列归为 date），带时间才输出完整 datetime
            if not (v.hour or v.minute or v.second or v.microsecond):
                return v.date().isoformat()
            return v.isoformat(sep=" ")
        if isinstance(v, _date):
            return v.isoformat()
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, (int, float)):
            return _norm_xlsx_number(v)
        return str(v)

    def gen() -> Iterator[list[str]]:
        for row in rows:
            out = []
            for j, v in enumerate(row):
                if j in ea_cols and isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = from_excel(v)  # 东亚日期列：序列号 → date/datetime
                out.append(_cell_str(v))
            yield out

    return header, gen()


def _detect_ea_date_columns(path: Path) -> set[int]:
    """探测使用东亚内置日期格式（27-36/50-58）的列（0-based 列号）。

    openpyxl read_only 惰性解析：仅读取前 25 行定位每列首个非空单元格的
    numFmtId（列内格式实践上统一），不做全文件扫描。
    """
    from openpyxl import load_workbook

    cols: set[int] = set()
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        for row in ws.iter_rows(max_row=25):
            for cell in row:
                if cell.value is None:
                    continue
                try:
                    if cell.has_style and cell.style_array.numFmtId in _EA_BUILTIN_DATE_FMT_IDS:
                        cols.add(cell.column - 1)
                except (AttributeError, IndexError, KeyError):
                    continue
    except (AttributeError, KeyError):
        pass
    finally:
        wb.close()
    return cols


def _iter_xlsx_rows_openpyxl(path: Path) -> tuple[list[str], Iterator[list[str]]]:
    from datetime import date as _date, datetime as _datetime

    from openpyxl import load_workbook
    from openpyxl.utils.datetime import from_excel

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows()
    try:
        raw_header = next(rows)
    except StopIteration as exc:
        wb.close()
        raise BusinessError("Excel 没有表头行") from exc
    header = normalize_header([(str(c.value) if c.value is not None else "") for c in raw_header])

    def _cell_str(cell) -> str:
        v = cell.value
        if v is None:
            return ""
        # 第三方 ETL 导出的 xlsx 常见：日期列底层存 Excel 序列号（data_type='n' +
        # 日期样式），Excel/WPS 按样式显示日期，但 openpyxl 只对 data_type='d'
        # 自动转换，'n' 返回裸数字（如 46295）。此处按工作簿纪元补一次换算。
        if isinstance(v, (int, float)) and not isinstance(v, bool) and _is_xlsx_date_cell(cell):
            v = from_excel(v, wb.epoch)
        if isinstance(v, _datetime):
            # 零点输出纯日期（月末快照类列归为 date），带时间才输出完整 datetime
            if not (v.hour or v.minute or v.second or v.microsecond):
                return v.date().isoformat()
            return v.isoformat(sep=" ")
        if isinstance(v, _date):
            return v.isoformat()
        if isinstance(v, bool):
            return str(v)
        return str(v)

    def gen() -> Iterator[list[str]]:
        try:
            for row in rows:
                yield [_cell_str(c) for c in row]
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

    # 保存原始上传件（保留原编码，供审计与重放；流式复制，不整文件读内存）
    dest = storage.uploads_dir / f"{final_name}{ext}"
    if src_path.resolve() != dest.resolve():
        shutil.copyfile(src_path, dest)

    dataset_dir = storage.parquet_dir / final_name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    stage_path = dataset_dir / f".stage-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.parquet"

    # ---- 第一遍：扫描推断 + 旁路缓存字符串行（文件只解析一遍）----
    if ext == ".xlsx":
        header, rows_iter = _iter_xlsx_rows(src_path)
        staged = _stage_rows(rows_iter, header, stage_path)
        try:
            stats, row_count, ragged = scan_rows(header, staged)
        except BaseException:
            stage_path.unlink(missing_ok=True)
            raise
    else:
        # 采样识别可能被"前段纯 ASCII + 后段 GBK"的文件骗过（头部判成 utf-8，
        # 流式解码到中后段才炸 UnicodeDecodeError）→ 按候选编码依次整文件重试
        candidates = [encoding] + [e for e in ("gbk", "gb18030") if e != encoding]
        last_error: UnicodeDecodeError | None = None
        for enc in candidates:
            header, rows_iter = _open_rows(src_path, enc)
            staged = _stage_rows(rows_iter, header, stage_path)
            try:
                stats, row_count, ragged = scan_rows(header, staged)
            except UnicodeDecodeError as exc:
                stage_path.unlink(missing_ok=True)
                last_error = exc
                continue
            except BaseException:
                stage_path.unlink(missing_ok=True)
                raise
            if enc != encoding:
                logger.warning(
                    "编码重试生效: 采样判定 %s，实际按 %s 解码成功 (%s)",
                    encoding, enc, src_path.name,
                )
                encoding = enc
            break
        else:
            stage_path.unlink(missing_ok=True)
            raise BusinessError(
                "文件无法用已支持的编码（UTF-8 / GBK / GB18030）完整解码，疑似混合编码；"
                "请将文件另存为 UTF-8 后重试",
                data={"byte_position": last_error.start if last_error else None},
            )
    if ragged:
        stage_path.unlink(missing_ok=True)
        raise BusinessError(
            f"列数不一致：{len(ragged)}+ 行结构异常，首见数据行号 {ragged}（1-based，最多列前 10 个）",
            data={"ragged_rows": ragged},
        )
    if row_count == 0:
        stage_path.unlink(missing_ok=True)
        raise BusinessError("文件没有数据行")

    col_types = [s.final_type() for s in stats]
    types_map = dict(zip(header, col_types))

    # ---- 第二阶段：向量化类型转换（不再二次解析源文件）----
    parquet_path = dataset_dir / "part-0001.parquet"
    try:
        _materialize_typed_parquet(stage_path, parquet_path, header, types_map)
    finally:
        stage_path.unlink(missing_ok=True)

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


def _load_semantics(dataset: Dataset) -> dict[str, str]:
    """已落库字段语义标注（P2 #4；空/损坏 → 空对象，零影响）。"""
    try:
        raw = json.loads(getattr(dataset, "column_semantics_json", None) or "{}")
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def dataset_to_dict(dataset: Dataset, coverages: list[DatasetCoverage] | None = None) -> dict:
    detail = {
        "id": dataset.id,
        "name": dataset.name,
        "source_filename": dataset.source_filename,
        "encoding": dataset.file_encoding,
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
        "project_id": dataset.project_id,
        "columns": json.loads(dataset.schema_json),
        "column_semantics": _load_semantics(dataset),
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

    # 存储路径（临时分片/旁路缓存均放数据集目录）
    old_path = Path(dataset.parquet_path)  # 可能是单文件（存量）或分片目录（B8 起）
    is_dir_layout = old_path.is_dir()
    dataset_dir = old_path if is_dir_layout else old_path.parent / dataset.name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    stage_path = dataset_dir / f".stage-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.parquet"

    # ---- 读取与校验（单遍解析 + 旁路缓存；编码重试与首次上传同款）----
    if ext == ".xlsx":
        header, rows_iter = _iter_xlsx_rows(src_path)
        staged = _stage_rows(rows_iter, header, stage_path)
        try:
            stats, row_count, ragged = scan_rows(header, staged)
        except BaseException:
            stage_path.unlink(missing_ok=True)
            raise
        encoding = "xlsx"
    else:
        encoding = detect_encoding(src_path)
        candidates = [encoding] + [e for e in ("gbk", "gb18030") if e != encoding]
        last_error: UnicodeDecodeError | None = None
        for enc in candidates:
            header, rows_iter = _open_rows(src_path, enc)
            staged = _stage_rows(rows_iter, header, stage_path)
            try:
                stats, row_count, ragged = scan_rows(header, staged)
            except UnicodeDecodeError as exc:
                stage_path.unlink(missing_ok=True)
                last_error = exc
                continue
            except BaseException:
                stage_path.unlink(missing_ok=True)
                raise
            if enc != encoding:
                logger.warning("编码重试生效: 采样判定 %s，实际按 %s 解码成功 (%s)", encoding, enc, src_path.name)
                encoding = enc
            break
        else:
            stage_path.unlink(missing_ok=True)
            raise BusinessError(
                "文件无法用已支持的编码（UTF-8 / GBK / GB18030）完整解码；请将文件另存为 UTF-8 后重试",
                data={"byte_position": last_error.start if last_error else None},
            )
    if ragged:
        stage_path.unlink(missing_ok=True)
        raise BusinessError(
            f"列数不一致：{len(ragged)}+ 行结构异常，首见数据行号 {ragged}（1-based，最多列前 10 个）",
            data={"ragged_rows": ragged},
        )
    if row_count == 0:
        stage_path.unlink(missing_ok=True)
        raise BusinessError("文件没有数据行")

    missing = [n for n in existing_names if n not in set(header)]
    extra = [n for n in header if n not in set(existing_names)]
    if missing or extra:
        stage_path.unlink(missing_ok=True)
        raise BusinessError(
            "表头与现有数据集不一致，增量导入要求列名集合完全一致（顺序可不同）",
            data={"missing_columns": missing, "extra_columns": extra},
        )

    # 输出列按现有 schema 顺序重排（cast SQL 里直接按名选取，无需 Python 重排）
    if mode == "replace":
        # 全量覆盖：数据全新，类型按本次文件重新推断并更新 schema（append 必须
        # 沿用现有类型保证分片可拼接；replace 若沿用旧 schema，会因存量坏类型
        # 卡死修正性重传——如 xlsx 日期序列号修复后 MONTH_END int → date）
        file_stats = {s.name: s for s in stats}
        types_map = {n: file_stats[n].final_type() for n in existing_names}
        new_schema = json.dumps(
            [file_stats[n].to_dict() for n in existing_names], ensure_ascii=False
        )
    else:
        # append：类型沿用现有 schema（不重新推断），保证分片 schema 一致
        types_map = {n: existing_types[n] for n in existing_names}
        new_schema = None

    # ---- 物化：向量化类型转换写临时分片（源文件不再二次解析）----
    # 安全顺序：先写临时分片，全部成功后才动既有数据；
    # 任何失败只留下待清理的临时文件，既有分片/单文件原样不动
    tmp_path = dataset_dir / f".tmp-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.parquet"
    try:
        _materialize_typed_parquet(stage_path, tmp_path, existing_names, types_map)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        stage_path.unlink(missing_ok=True)

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

    # 原始件留档（审计与重放；流式复制，不整文件读内存）
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    audit_path = storage.uploads_dir / f"{dataset.name}__{mode}__{ts}{ext}"
    try:
        shutil.copyfile(src_path, audit_path)
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
    if new_schema is not None:
        dataset.schema_json = new_schema

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
