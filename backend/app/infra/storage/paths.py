"""数据目录布局与创建。

布局（开发方案第四节）：
  {data_dir}/uploads/      原始上传文件（CSV/Excel，保留原编码）
  {data_dir}/parquet/      pyarrow 列式存储（B1 起）
  {data_dir}/metadata.db   SQLite 元数据库
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


class StoragePaths:
    """数据目录的统一访问点。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def data_dir(self) -> Path:
        return self.settings.resolved_data_dir

    @property
    def uploads_dir(self) -> Path:
        return self.settings.uploads_dir

    @property
    def parquet_dir(self) -> Path:
        return self.settings.parquet_dir

    @property
    def metadata_db_path(self) -> Path:
        return self.settings.metadata_db_path

    def ensure_dirs(self) -> None:
        """启动时确保目录存在；幂等。"""
        for d in (self.data_dir, self.uploads_dir, self.parquet_dir):
            d.mkdir(parents=True, exist_ok=True)
