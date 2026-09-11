"""四环境配置（架构 D2 / D22）。

环境解析规则：
- 进程环境变量 APP_ENV 决定加载 backend/env/.env.{APP_ENV}（dev/test/uat/pro）
- 进程环境变量优先级高于 env 文件（pydantic-settings 标准行为），便于测试覆盖
- 源码保持 Python 3.11+ 兼容（D22），运行于 3.13.12 managed venv
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（config.py 位于 backend/app/core/ 下，向上 3 级）
BASE_DIR = Path(__file__).resolve().parents[2]

ENV_DIR = BASE_DIR / "env"
VALID_ENVS = ("dev", "test", "uat", "pro")


class Settings(BaseSettings):
    """全局配置。所有路径与凭据均配置化，不写死。"""

    app_name: str = "AI Native BI Platform"
    app_env: str = "dev"
    api_prefix: str = "/api"
    log_level: str = "INFO"

    # 数据根目录：uploads / parquet / metadata.db 均置于其下（开发方案第四节）
    data_dir: Path = BASE_DIR.parent / "data"

    # 行业指标模板库目录（B5）：YAML 数据文件，行业内容只存在于数据层（不变式 7），
    # backend/app 代码保持行业无关。可用 TEMPLATES_DIR 环境变量覆盖
    templates_dir: Path = BASE_DIR.parent / "templates" / "seed"

    # 安全占位：B4 启用 JWT。默认值仅限 dev/test——长度满足 HMAC-SHA256
    # 最低 32 字节要求；pro 环境务必在 .env.pro 中覆盖为真实随机密钥
    secret_key: str = "dev-only-change-me-0123456789abcdef-bi-platform"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    # 引导管理员：users 表为空时启动自动创建（dev 默认 admin/admin123，
    # pro 环境务必在 .env.pro 中覆盖）
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin123"

    # LLM（阶段 2 起启用；来自工作区 env 文件的 DeepSeek 兼容接口）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    model_config = SettingsConfigDict(extra="ignore")

    # ---- 派生路径 ----

    @property
    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        return p

    @property
    def resolved_templates_dir(self) -> Path:
        p = Path(self.templates_dir)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        return p

    @property
    def uploads_dir(self) -> Path:
        return self.resolved_data_dir / "uploads"

    @property
    def parquet_dir(self) -> Path:
        return self.resolved_data_dir / "parquet"

    @property
    def metadata_db_path(self) -> Path:
        return self.resolved_data_dir / "metadata.db"

    @property
    def metadata_db_url(self) -> str:
        return f"sqlite:///{self.metadata_db_path.as_posix()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取配置。进程环境变量 > env 文件。"""
    app_env = os.getenv("APP_ENV", "dev").strip().lower()
    if app_env not in VALID_ENVS:
        raise ValueError(f"非法 APP_ENV={app_env!r}，允许值：{VALID_ENVS}")

    env_file = ENV_DIR / f".env.{app_env}"
    settings = Settings(_env_file=str(env_file), _env_file_encoding="utf-8")
    # APP_ENV 以进程环境为准（env 文件仅作缺省），保证一致性
    settings.app_env = app_env
    return settings
