"""配置模块测试。"""

from __future__ import annotations

from app.core.config import Settings, get_settings


def test_settings_env_is_test():
    assert get_settings().app_env == "test"


def test_derived_paths_under_data_dir():
    s = get_settings()
    assert s.uploads_dir.parent == s.resolved_data_dir
    assert s.parquet_dir.parent == s.resolved_data_dir
    assert s.metadata_db_path.name == "metadata.db"
    assert s.metadata_db_url.startswith("sqlite:///")
