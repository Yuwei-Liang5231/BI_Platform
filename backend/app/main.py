"""应用工厂与全局异常处理。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import app
from app.api.routes import auth, datasets, health, llm, metrics, modeling, notifications, projects, query, reports, templates
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.response import (
    DEFAULT_CODE_BY_HTTP_STATUS,
    BodyCode,
    BusinessError,
    fail_response,
)
from app.infra.database import create_all
from app.infra.storage.paths import StoragePaths

logger = get_logger("app.main")


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BusinessError)
    def _business_handler(request: Request, exc: BusinessError):
        logger.warning("业务异常: %s %s -> code=%s %s", request.method, request.url.path, exc.code, exc.message)
        return fail_response(exc.code, exc.message, exc.data)

    @app.exception_handler(Exception)
    def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        # 内部平台：把异常类型与消息透出到响应，便于在 Swagger 直接定位原因
        return fail_response(
            BodyCode.INTERNAL_ERROR,
            "服务器内部错误",
            data={"exception": type(exc).__name__, "detail": str(exc)[:500]},
        )

    @app.exception_handler(RequestValidationError)
    def _validation_handler(request: Request, exc: RequestValidationError):
        summary = "; ".join(
            f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors()[:5]
        )
        return fail_response(BodyCode.BAD_REQUEST, f"参数校验失败：{summary}")

    @app.exception_handler(StarletteHTTPException)
    def _http_handler(request: Request, exc: StarletteHTTPException):
        body_code = DEFAULT_CODE_BY_HTTP_STATUS.get(
            exc.status_code,
            BodyCode.BAD_REQUEST if exc.status_code < 500 else BodyCode.INTERNAL_ERROR,
        )
        return fail_response(body_code, str(exc.detail))


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        storage = StoragePaths(settings)
        storage.ensure_dirs()
        create_all()
        from app.domain.auth.service import ensure_bootstrap_admin
        from app.domain.project.service import ensure_default_project
        from app.infra.database import get_db

        ensure_bootstrap_admin()  # users 为空时创建引导 admin（幂等）
        # B9.3：默认项目幂等创建 + 存量 project_id NULL 回填（列由迁移补齐）
        db = next(get_db())
        try:
            ensure_default_project(db)
            db.commit()  # 手动取的会话不走 get_db 的自动 commit
        finally:
            db.close()
        logger.info(
            "启动完成 env=%s data_dir=%s db=%s",
            settings.app_env,
            storage.data_dir,
            settings.metadata_db_path,
        )
        yield

    application = FastAPI(
        title=settings.app_name,
        version=app.__version__,
        lifespan=lifespan,
    )
    application.include_router(health.router, prefix=settings.api_prefix, tags=["health"])
    application.include_router(auth.router, prefix=settings.api_prefix)
    application.include_router(datasets.router, prefix=settings.api_prefix)
    application.include_router(metrics.router, prefix=settings.api_prefix)
    application.include_router(projects.router, prefix=settings.api_prefix)
    application.include_router(query.router, prefix=settings.api_prefix)
    application.include_router(notifications.router, prefix=settings.api_prefix)
    application.include_router(templates.router, prefix=settings.api_prefix)
    application.include_router(reports.router, prefix=settings.api_prefix)
    application.include_router(modeling.router, prefix=settings.api_prefix)
    application.include_router(llm.router, prefix=settings.api_prefix)
    _register_exception_handlers(application)

    @application.get("/", include_in_schema=False)
    def root():
        """根路径跳转 Swagger，避免访问首页出现 404。"""
        return RedirectResponse(url="/docs")

    return application


app = create_app()
