"""统一响应契约（架构 D11：HTTP 码与 body code 对齐）。

约定（与前端规范 request.js 解包逻辑一致）：
- HTTP 200 且 code=0：业务成功，前端解包 data
- HTTP 401 / code=40100：未认证，前端跳登录
- HTTP 403 / code=40300：无权限，前端仅提示
- HTTP 400 / code=40000：参数错误
- HTTP 404 / code=40400、409 / code=40900、500 / code=50000
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class BodyCode(IntEnum):
    """body 业务码，前三位与 HTTP 状态码对齐（D11）。"""

    OK = 0
    BAD_REQUEST = 40000
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    INTERNAL_ERROR = 50000


HTTP_STATUS_BY_CODE: dict[int, int] = {
    BodyCode.OK: 200,
    BodyCode.BAD_REQUEST: 400,
    BodyCode.UNAUTHORIZED: 401,
    BodyCode.FORBIDDEN: 403,
    BodyCode.NOT_FOUND: 404,
    BodyCode.CONFLICT: 409,
    BodyCode.INTERNAL_ERROR: 500,
}

# 反向映射：HTTP 状态码 -> 默认业务码（用于异常处理器）
DEFAULT_CODE_BY_HTTP_STATUS: dict[int, BodyCode] = {
    400: BodyCode.BAD_REQUEST,
    401: BodyCode.UNAUTHORIZED,
    403: BodyCode.FORBIDDEN,
    404: BodyCode.NOT_FOUND,
    409: BodyCode.CONFLICT,
}


class ApiResponse(BaseModel):
    """统一响应体。所有接口必须经由 ok/fail 工厂产出。"""

    code: int
    message: str
    data: Any = None

    @classmethod
    def ok(cls, data: Any = None, message: str = "ok") -> "ApiResponse":
        return cls(code=BodyCode.OK, message=message, data=data)

    @classmethod
    def fail(cls, code: int | BodyCode, message: str, data: Any = None) -> "ApiResponse":
        return cls(code=int(code), message=message, data=data)


class BusinessError(Exception):
    """业务异常：路由内主动抛出，由全局异常处理器转成统一响应。

    data 可携带结构化载荷（如异常行号列表），随响应体 data 字段返回。
    """

    def __init__(
        self,
        message: str,
        code: BodyCode | int = BodyCode.BAD_REQUEST,
        data: Any = None,
    ):
        self.message = message
        self.code = int(code)
        self.data = data
        super().__init__(message)


def ok_response(data: Any = None, message: str = "ok") -> JSONResponse:
    """业务成功响应（HTTP 200 / code 0）。data 中的 date/datetime 等统一转 JSON 安全类型。"""
    return JSONResponse(
        status_code=200,
        content=ApiResponse.ok(data=jsonable_encoder(data), message=message).model_dump(),
    )


def fail_response(code: int | BodyCode, message: str, data: Any = None) -> JSONResponse:
    """业务失败响应，HTTP 状态码按映射表对齐。"""
    body_code = int(code)
    http_status = HTTP_STATUS_BY_CODE.get(body_code)
    if http_status is None:
        # 未登记的码一律按 500 处理，避免契约漂移
        http_status = 500
        body_code = int(BodyCode.INTERNAL_ERROR)
    return JSONResponse(
        status_code=http_status,
        content=ApiResponse.fail(body_code, message, jsonable_encoder(data)).model_dump(),
    )
