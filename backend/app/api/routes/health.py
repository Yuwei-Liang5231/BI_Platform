"""健康检查（B0 验收接口）。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

import app
from app.api.deps import DbDep, SettingsDep
from app.core.response import fail_response, ok_response
from app.infra.database import check_database

router = APIRouter()


@router.get("/health")
def health(settings: SettingsDep, db: DbDep):
    """存活 + 数据库连通性检查。db 异常时返回 500 / code 50000。"""
    _ = db  # 依赖注入即验证会话可创建
    db_ok = check_database()
    if not db_ok:
        return fail_response(50000, "元数据库不可用")
    return ok_response(
        {
            "status": "ok",
            "app": settings.app_name,
            "version": app.__version__,
            "env": settings.app_env,
            "database": "up",
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )
